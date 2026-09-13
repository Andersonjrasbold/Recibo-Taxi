import atexit
import hashlib
import json
import hmac
import os
import secrets
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from functools import wraps
from urllib.parse import quote
from uuid import uuid4

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from supabase import ClientOptions, create_client
except ImportError:
    create_client = None
    ClientOptions = None

try:
    from dotenv import load_dotenv
except ImportError:  # dependência opcional, usada só em desenvolvimento
    load_dotenv = None

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Json
    from psycopg_pool import ConnectionPool
except ImportError:  # só necessário quando DATABASE_URL/SUPABASE_DB_URL existe
    psycopg = None
    dict_row = None
    Json = None
    ConnectionPool = None

try:
    import stripe as stripe_lib
except ImportError:
    stripe_lib = None


# Lê o .env local. Em produção as variáveis já vêm do ambiente e o
# load_dotenv não sobrescreve o que existe, então isto vira um no-op.
if load_dotenv is not None:
    load_dotenv()


APP_NAME = "Recibo Táxi"

# Brasil não adota horário de verão desde 2019, então o offset é fixo.
BR_TZ = timezone(timedelta(hours=-3))

FREE_MONTHLY_LIMIT = 30
# "business" segue aqui de propósito: o plano saiu de venda, mas quem já assina
# mantém o acesso ilimitado até cancelar. Só STRIPE_PRICE_IDS perdeu a entrada,
# o que faz /assinar/business responder 400 para assinaturas novas.
PAID_PLANS = ("pro", "business")

# Gerador público: teto diário por IP, para o endpoint não virar porta aberta
# de escrita no banco. É best-effort — ver comentário em client_ip().
MAX_RECEIPT_AMOUNT = Decimal("99999.99")

# 14 dígitos hex = 2^56. Com 10 (2^40) a chance de colisão passava de 36% em
# 1 milhão de recibos, e uma colisão derruba a emissão com erro 500.
RID_LENGTH = 14

# Sem teto por campo, um POST de 200 KB por campo passava direto para o banco.
# (rótulo, tamanho máximo) — o rótulo entra na mensagem de erro.
RECEIPT_FIELD_LIMITS = {
    "passageiro": ("O nome do passageiro", 120),
    "origem": ("A origem", 200),
    "destino": ("O destino", 200),
    "observacoes": ("As observações", 500),
    "forma_pagamento": ("A forma de pagamento", 40),
    "email_passageiro": ("O e-mail do passageiro", 254),
    "whatsapp_passageiro": ("O WhatsApp do passageiro", 20),
    "data": ("A data", 10),
    "hora": ("A hora", 5),
    "nome_motorista": ("O nome do motorista", 120),
    "placa": ("A placa", 10),
    "whatsapp_motorista": ("O WhatsApp do motorista", 20),
    "cidade_motorista": ("A cidade", 80),
    "modelo_veiculo": ("O modelo do veículo", 80),
}

SIGNUP_FIELD_LIMITS = {
    "nome_completo": ("O nome completo", 120),
    "email": ("O e-mail", 254),
    "senha": ("A senha", 200),
    "whatsapp": ("O WhatsApp", 20),
    "cpf": ("O CPF", 14),
    "cidade": ("A cidade", 80),
    "placa": ("A placa", 10),
    "modelo_veiculo": ("O modelo do veículo", 80),
    "prefixo_taxi": ("O prefixo do táxi", 20),
    "numero_alvara": ("O número do alvará", 40),
}

# Teto por lote na limpeza, para a rotina não estourar o tempo da função
# quando houver backlog grande.
CLEANUP_BATCH_LIMIT = 500

GUEST_DAILY_LIMIT = 20
# A Política de Privacidade promete remover recibos sem cadastro em 12 meses.
GUEST_RETENTION_DAYS = 365
COUNTER_RETENTION_DAYS = 7
# past_due mantém o plano enquanto a Stripe tenta novas cobranças.
ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trialing", "past_due")

RESET_TOKEN_MAX_AGE = 3600  # 1 hora
RESET_TOKEN_SALT = "recibo-taxi-redefinir-senha"

# Os scripts inline carregam um nonce por requisição, então script-src dispensa
# 'unsafe-inline'. style-src ainda precisa: o Bootstrap injeta estilos inline em
# componentes como o collapse, e o risco de um estilo injetado é bem menor.
CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net 'nonce-{nonce}'; "
    "style-src 'self' https://cdn.jsdelivr.net https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com data:; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self' https://checkout.stripe.com https://billing.stripe.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'"
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=()",
}

STRIPE_PRICE_IDS = {
    "pro": "STRIPE_PRO_PRICE_ID",
}


def to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat() + "Z"


def now_iso() -> str:
    return to_utc_iso(datetime.now(timezone.utc))


def today_br() -> str:
    return datetime.now(BR_TZ).strftime("%Y-%m-%d")


def month_range_utc(reference: datetime | None = None) -> tuple[str, str]:
    """Início e fim do mês corrente no fuso de Brasília, como ISO em UTC.

    Os recibos são gravados em UTC, mas o mês que vale para o motorista é o
    local — sem isso um recibo emitido às 22h do dia 31 cairia no mês seguinte.
    """
    local = (reference or datetime.now(BR_TZ)).astimezone(BR_TZ)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return to_utc_iso(start), to_utc_iso(end)


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def sanitize_phone(value: str) -> str:
    return "".join(char for char in (value or "") if char.isdigit())


def format_date_br(date_value: str) -> str:
    if not date_value:
        return "-"
    try:
        return datetime.strptime(date_value, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return date_value


def normalize_money(value: str) -> tuple[str, str]:
    raw = (value or "").strip().replace("R$", "").replace("\xa0", "").replace(" ", "")
    if not raw:
        raise ValueError("Informe o valor da corrida.")

    normalized = raw
    if "," in raw and "." in raw:
        normalized = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        normalized = raw.replace(",", ".")

    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Informe um valor válido (ex: 45,00).") from exc

    # Decimal aceita "NaN" e "Infinity" como valores válidos.
    if not amount.is_finite():
        raise ValueError("Informe um valor válido (ex: 45,00).")

    amount = amount.quantize(Decimal("0.01"))

    if amount <= 0:
        raise ValueError("O valor da corrida precisa ser maior que zero.")
    if amount > MAX_RECEIPT_AMOUNT:
        raise ValueError("Valor acima do limite para um recibo de corrida.")

    display = f"{amount:.2f}".replace(".", ",")
    return str(amount), display


def compose_receipt_message(receipt: dict, public_url: str) -> str:
    lines = [
        f"✅ Recibo #{receipt['rid']}",
        f"👤 Passageiro: {receipt.get('passenger') or '-'}",
        f"📅 Data: {receipt.get('trip_date_display') or '-'}",
        f"📍 Origem: {receipt.get('origin') or '-'}",
        f"🏁 Destino: {receipt.get('destination') or '-'}",
        f"💰 Valor: R$ {receipt.get('amount_display') or '-'}",
        f"💳 Pagamento: {receipt.get('payment_method') or '-'}",
        "",
        f"🔗 Acesse o recibo: {public_url}",
    ]
    return "\n".join(lines)


def build_whatsapp_link(receipt: dict, public_url: str, with_recipient: bool = True) -> str:
    """Link de compartilhamento.

    with_recipient=False omite o telefone do passageiro: a página do recibo é
    pública e qualquer visitante com o link leria o destinatário no href.
    """
    phone = sanitize_phone(receipt.get("passenger_whatsapp", "")) if with_recipient else ""
    message = quote(compose_receipt_message(receipt, public_url))
    if phone:
        return f"https://wa.me/{phone}?text={message}"
    return f"https://wa.me/?text={message}"


def build_email_link(receipt: dict, public_url: str, with_recipient: bool = True) -> str:
    """Idem: sem with_recipient o mailto vai sem o e-mail do passageiro."""
    raw_target = (receipt.get("passenger_email") or "").strip() if with_recipient else ""
    target = quote(raw_target)
    subject = quote(f"Recibo #{receipt['rid']} — Recibo Táxi")
    body = quote(compose_receipt_message(receipt, public_url))
    return f"mailto:{target}?subject={subject}&body={body}"


def absolute_url(endpoint: str, **values) -> str:
    base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    if base_url:
        return f"{base_url}{url_for(endpoint, **values)}"
    return url_for(endpoint, _external=True, **values)


def public_receipt_url(rid: str) -> str:
    return absolute_url("recibo_view", rid=rid)


def field_limit_error(limits: dict[str, tuple[str, int]]) -> str | None:
    """Primeira mensagem de erro de tamanho, ou None se tudo couber."""
    for field, (label, maximo) in limits.items():
        if len(request.form.get(field, "")) > maximo:
            return f"{label} passa do limite de {maximo} caracteres."
    return None


def client_ip() -> str:
    """IP do cliente, já corrigido pelo ProxyFix.

    Atrás de CDN isto é best-effort: se o proxy não sobrescrever o
    X-Forwarded-For, o valor pode ser forjado. Serve para conter abuso
    casual do gerador público; contra um atacante determinado o caminho
    é o WAF da Vercel, que age antes da função rodar.
    """
    return (request.remote_addr or "desconhecido").strip()


def get_stripe():
    if stripe_lib is None:
        return None
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return None
    stripe_lib.api_key = key
    return stripe_lib


# ── Store ───────────────────────────────────────────────────────────────────

class PostgresStore:
    """Store em Postgres (Supabase).

    Devolve dicionários no mesmo formato que os outros stores — inclusive o
    `_id` e os campos derivados `amount_display`/`trip_date_display` — para o
    resto do app não precisar saber de onde os dados vieram.
    """

    kind = "postgres"
    label = "Postgres"

    # Colunas do motorista, já com o alias que o app espera.
    _DRIVER_COLS = """
        id as _id, email, password_changed_at, full_name, cpf,
        whatsapp, city, plate, vehicle_model, taxi_prefix, license_number,
        plan, stripe_customer_id, stripe_subscription_id, subscription_status,
        created_at, updated_at
    """

    _RECEIPT_COLS = """
        rid, rid as _id, driver_id, is_guest, passenger, passenger_email,
        passenger_whatsapp, trip_date, trip_time, origin, destination,
        amount, payment_method, notes, driver_snapshot, created_at
    """

    def __init__(self, dsn: str) -> None:
        if ConnectionPool is None:
            raise RuntimeError(
                "A dependência 'psycopg' não está instalada. "
                "Rode 'pip install -r requirements.txt'."
            )
        # prepare_threshold=None: o Supavisor em transaction mode não suporta
        # prepared statements, e o psycopg 3 os cria sozinho depois de 5
        # execuções da mesma query — o erro só apareceria depois, em produção.
        self.pool = ConnectionPool(
            dsn,
            min_size=0,
            max_size=int(os.environ.get("DB_POOL_MAX", "4")),
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
        )
        # Em serverless o pool sobrevive entre invocações (Fluid Compute);
        # no fim do processo, fechar evita threads penduradas.
        atexit.register(self.pool.close)

    # ── conversões ──────────────────────────────────────────────────────────
    @staticmethod
    def _driver_out(row: dict | None) -> dict | None:
        if not row:
            return None
        out = dict(row)
        # O resto do app trata _id como string: ele vai para a sessão, para o
        # token de reset (que é JSON) e para comparações com driver_id.
        out["_id"] = str(out["_id"])
        for campo in ("created_at", "updated_at", "password_changed_at"):
            valor = out.get(campo)
            out[campo] = to_utc_iso(valor) if valor else ""
        return out

    @staticmethod
    def _receipt_out(row: dict | None) -> dict | None:
        if not row:
            return None
        out = dict(row)
        amount = out.pop("amount", None)
        if amount is not None:
            out["amount_value"] = str(amount)
            out["amount_display"] = f"{amount:.2f}".replace(".", ",")
        trip_date = out.get("trip_date")
        out["trip_date"] = trip_date.isoformat() if trip_date else ""
        out["trip_date_display"] = format_date_br(out["trip_date"])
        out["driver_id"] = str(out["driver_id"]) if out.get("driver_id") else None
        out["created_at"] = to_utc_iso(out["created_at"]) if out.get("created_at") else ""
        return out

    # ── motoristas ──────────────────────────────────────────────────────────
    # Não há create_user: o perfil nasce do trigger on_auth_user_created
    # quando o usuário é criado no Supabase Auth.

    def update_user(self, user_id: str, updates: dict) -> None:
        if not updates:
            return
        # Só colunas conhecidas entram no UPDATE.
        permitidas = {
            "email", "password_changed_at", "full_name", "cpf",
            "whatsapp", "city", "plate", "vehicle_model", "taxi_prefix",
            "license_number", "plan", "stripe_customer_id",
            "stripe_subscription_id", "subscription_status",
        }
        campos = {k: v for k, v in updates.items() if k in permitidas}
        if not campos:
            return
        sets = ", ".join(f"{k} = %({k})s" for k in campos)
        campos["_uid"] = user_id
        with self.pool.connection() as conn:
            conn.execute(
                f"update public.drivers set {sets}, updated_at = now() where id = %(_uid)s",
                campos,
            )

    def get_user_by_email(self, email: str) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"select {self._DRIVER_COLS} from public.drivers where lower(email) = lower(%s)",
                (email,),
            ).fetchone()
        return self._driver_out(row)

    def get_user_by_id(self, user_id: str | None) -> dict | None:
        if not user_id:
            return None
        with self.pool.connection() as conn:
            try:
                row = conn.execute(
                    f"select {self._DRIVER_COLS} from public.drivers where id = %s",
                    (user_id,),
                ).fetchone()
            except psycopg.errors.InvalidTextRepresentation:
                return None  # id que não é uuid
        return self._driver_out(row)

    def get_user_by_stripe_customer(self, customer_id: str | None) -> dict | None:
        if not customer_id:
            return None
        with self.pool.connection() as conn:
            row = conn.execute(
                f"select {self._DRIVER_COLS} from public.drivers where stripe_customer_id = %s",
                (customer_id,),
            ).fetchone()
        return self._driver_out(row)

    def delete_user(self, user_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("delete from public.drivers where id = %s", (user_id,))

    def delete_receipts_by_driver(self, driver_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("delete from public.receipts where driver_id = %s", (driver_id,))

    # ── recibos ─────────────────────────────────────────────────────────────
    def create_receipt(self, payload: dict, quota_limit: int | None = None) -> dict | None:
        """Cria o recibo. Com quota_limit, consome a cota na MESMA transação.

        Devolve None se a cota do mês já acabou. Cota e insert juntos evitam
        as duas janelas: furar o teto, e queimar cota sem gravar o recibo.
        """
        dados = dict(payload)
        dados.setdefault("is_guest", False)
        dados["driver_id"] = dados.get("driver_id") or None
        dados["amount"] = dados.pop("amount_value", "0")
        dados.pop("amount_display", None)
        dados.pop("trip_date_display", None)
        dados.pop("_id", None)
        dados["driver_snapshot"] = Json(dados.get("driver_snapshot") or {})
        dados["trip_time"] = dados.get("trip_time") or ""

        # created_at explícito é usado por testes e importação; sem ele, o
        # default now() da coluna vale.
        created_at = dados.pop("created_at", None)
        col_created = ", created_at" if created_at else ""
        val_created = ", %(created_at)s" if created_at else ""
        if created_at:
            dados["created_at"] = created_at

        with self.pool.connection() as conn:
            if quota_limit is not None and dados["driver_id"]:
                liberado = conn.execute(
                    "select public.consume_receipt_quota(%s, %s)",
                    (dados["driver_id"], quota_limit),
                ).fetchone()["consume_receipt_quota"]
                if not liberado:
                    return None

            row = conn.execute(
                f"""insert into public.receipts
                    (rid, driver_id, is_guest, passenger, passenger_email,
                     passenger_whatsapp, trip_date, trip_time, origin, destination,
                     amount, payment_method, notes, driver_snapshot{col_created})
                    values (%(rid)s, %(driver_id)s, %(is_guest)s, %(passenger)s,
                            %(passenger_email)s, %(passenger_whatsapp)s, %(trip_date)s,
                            %(trip_time)s, %(origin)s, %(destination)s, %(amount)s,
                            %(payment_method)s, %(notes)s, %(driver_snapshot)s{val_created})
                    returning {self._RECEIPT_COLS}""",
                dados,
            ).fetchone()
        return self._receipt_out(row)

    def get_receipt(self, rid: str) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"select {self._RECEIPT_COLS} from public.receipts where rid = %s",
                (rid,),
            ).fetchone()
        return self._receipt_out(row)

    def list_receipts_by_driver(self, driver_id: str, limit: int | None = None) -> list[dict]:
        sql = f"""select {self._RECEIPT_COLS} from public.receipts
                  where driver_id = %s order by created_at desc"""
        params: tuple = (driver_id,)
        if limit is not None:
            sql += " limit %s"
            params = (driver_id, limit)
        with self.pool.connection() as conn:
            linhas = conn.execute(sql, params).fetchall()
        return [self._receipt_out(l) for l in linhas]

    def count_receipts_in_range(
        self, driver_id: str, start_iso: str, end_iso: str, cap: int
    ) -> int:
        with self.pool.connection() as conn:
            return conn.execute(
                """select count(*) as n from public.receipts
                   where driver_id = %s and created_at >= %s and created_at < %s""",
                (driver_id, start_iso, end_iso),
            ).fetchone()["n"]

    # ── contadores e limpeza ────────────────────────────────────────────────
    def bump_counter(self, key: str) -> int:
        with self.pool.connection() as conn:
            return conn.execute(
                "select public.bump_counter(%s) as n", (key,)
            ).fetchone()["n"]

    def purge_guest_receipts_before(self, cutoff_iso: str, limit: int) -> tuple[int, bool]:
        with self.pool.connection() as conn:
            apagados = conn.execute(
                """delete from public.receipts where rid in (
                     select rid from public.receipts
                      where is_guest and created_at < %s limit %s)
                   returning rid""",
                (cutoff_iso, limit),
            ).fetchall()
            sobrou = conn.execute(
                """select exists(select 1 from public.receipts
                                  where is_guest and created_at < %s) as e""",
                (cutoff_iso,),
            ).fetchone()["e"]
        return len(apagados), bool(sobrou)

    def purge_counters_before(self, cutoff_iso: str, limit: int) -> tuple[int, bool]:
        with self.pool.connection() as conn:
            apagados = conn.execute(
                """delete from public.rate_limits where key in (
                     select key from public.rate_limits
                      where created_at < %s limit %s)
                   returning key""",
                (cutoff_iso, limit),
            ).fetchall()
            sobrou = conn.execute(
                "select exists(select 1 from public.rate_limits where created_at < %s) as e",
                (cutoff_iso,),
            ).fetchone()["e"]
        return len(apagados), bool(sobrou)


# ── Store singleton ──────────────────────────────────────────────────────────

_STORE = None


def get_store():
    global _STORE
    if _STORE is not None:
        return _STORE

    # SUPABASE_DB_URL vence DATABASE_URL para produção não cair no banco local.
    dsn = (
        os.environ.get("SUPABASE_DB_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )
    if not dsn:
        raise RuntimeError(
            "Nenhum banco configurado. Defina SUPABASE_DB_URL (produção) ou "
            "DATABASE_URL (desenvolvimento) — sem um deles os dados não persistem."
        )

    _STORE = PostgresStore(dsn)
    return _STORE


# ── App ──────────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=None)
# x_for=1: sem isso request.remote_addr seria o IP do proxy da Vercel,
# e qualquer lógica por IP olharia sempre para o mesmo endereço.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

secret_key = os.environ.get("SECRET_KEY", "").strip()
if not secret_key:
    if os.environ.get("VERCEL"):
        raise RuntimeError("Defina a variável SECRET_KEY antes de publicar na Vercel.")
    secret_key = "dev-secret-key"

app.config.update(
    SECRET_KEY=secret_key,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
    # Nenhum formulário do app precisa de mais que isto.
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)


# ── Supabase Auth ────────────────────────────────────────────────────────────
#
# A senha vive no Supabase Auth; `drivers` é só o perfil, ligado por chave
# estrangeira. A sessão do Flask continua sendo a fonte da verdade de "quem
# está logado" — ela já é assinada pela SECRET_KEY. Assim uma página comum não
# paga ida à rede: só cadastro, login e troca de senha falam com o Auth.

_ADMIN_CLIENT = None


def supabase_admin():
    """Client com a chave de serviço. Ignora RLS — nunca exponha em template.

    Fica em cache de módulo porque nunca carrega sessão de usuário. Um client
    que faz sign_in muta os próprios headers e vazaria a sessão de um usuário
    para o request de outro; este não faz.
    """
    global _ADMIN_CLIENT
    if _ADMIN_CLIENT is not None:
        return _ADMIN_CLIENT

    if create_client is None:
        raise RuntimeError(
            "A dependência 'supabase' não está instalada. "
            "Rode 'pip install -r requirements.txt'."
        )

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (
        os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    )
    if not url or not key:
        raise RuntimeError(
            "Defina SUPABASE_URL e SUPABASE_SECRET_KEY para a autenticação funcionar."
        )

    # auto_refresh_token cria threading.Timer de renovação; numa função
    # serverless a thread fica pendurada sem servir para nada.
    _ADMIN_CLIENT = create_client(
        url, key, options=ClientOptions(auto_refresh_token=False, persist_session=False)
    )
    return _ADMIN_CLIENT


def supabase_public():
    """Client novo a cada uso, com a chave pública.

    Novo de propósito: sign_in_with_password muta os headers do objeto, então
    um client compartilhado entregaria a sessão de um usuário a outro.
    """
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Defina SUPABASE_URL e SUPABASE_PUBLISHABLE_KEY.")
    return create_client(
        url, key, options=ClientOptions(auto_refresh_token=False, persist_session=False)
    )


def auth_criar_usuario(email: str, password: str, metadata: dict) -> str:
    """Cria o usuário já confirmado e devolve o id.

    Usa a admin API em vez de sign_up por dois motivos: não dispara e-mail de
    confirmação (o fluxo do app loga direto após o cadastro) e não passa pelo
    limite de 30 cadastros a cada 5 minutos por IP.

    O perfil em `drivers` nasce pelo trigger on_auth_user_created.
    """
    try:
        resposta = supabase_admin().auth.admin.create_user(
            {
                "email": email,
                "password": password,
                "email_confirm": True,
                "user_metadata": metadata,
            }
        )
    except Exception as exc:
        texto = str(exc).lower()
        if "already" in texto or "registered" in texto or "exists" in texto:
            raise ValueError("Já existe uma conta com este e-mail.") from exc
        app.logger.error("Falha ao criar usuário no Auth: %s", exc)
        raise RuntimeError("Não foi possível criar a conta agora.") from exc

    if not resposta or not resposta.user:
        raise RuntimeError("Não foi possível criar a conta agora.")
    return str(resposta.user.id)


def auth_conferir_senha(email: str, password: str) -> str | None:
    """Devolve o id do usuário se a senha confere, ou None."""
    if not email or not password:
        return None
    try:
        resposta = supabase_public().auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except Exception:
        return None  # credencial inválida é o caso comum; não faz log
    return str(resposta.user.id) if resposta and resposta.user else None


def auth_definir_senha(user_id: str, password: str) -> None:
    supabase_admin().auth.admin.update_user_by_id(user_id, {"password": password})


def auth_definir_email(user_id: str, email: str) -> None:
    supabase_admin().auth.admin.update_user_by_id(
        user_id, {"email": email, "email_confirm": True}
    )


def auth_excluir_usuario(user_id: str) -> None:
    """Apaga no Auth. O cascade leva perfil, recibos e cotas junto."""
    supabase_admin().auth.admin.delete_user(user_id)


# ── E-mail transacional ──────────────────────────────────────────────────────

def remetente_padrao() -> str:
    """Endereço do remetente. onboarding@resend.dev é o domínio compartilhado
    do Resend — serve para desenvolvimento, mas só entrega para o dono da conta.
    """
    return os.environ.get("EMAIL_FROM", "").strip() or "onboarding@resend.dev"


def enviar_por_resend(to_address: str, subject: str, body: str) -> bool:
    """Envia pela API HTTP do Resend.

    HTTP em vez do relay SMTP de propósito: numa função serverless, o handshake
    do SMTP (EHLO, STARTTLS, AUTH, MAIL FROM, RCPT TO, DATA) são várias idas e
    voltas; aqui é um POST só.
    """
    chave = os.environ.get("RESEND_API_KEY", "").strip()
    if not chave:
        return False

    payload = json.dumps(
        {
            "from": f"{APP_NAME} <{remetente_padrao()}>",
            "to": [to_address],
            "subject": subject,
            "text": body,
        }
    ).encode("utf-8")

    requisicao = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {chave}",
            "Content-Type": "application/json",
            # Sem User-Agent próprio o Cloudflare do Resend devolve 403 com
            # "error code: 1010" — bloqueia o padrão do urllib.
            "User-Agent": f"{APP_NAME.replace(' ', '-')}/1.0",
        },
    )

    try:
        with urllib.request.urlopen(requisicao, timeout=15) as resposta:
            return 200 <= resposta.status < 300
    except urllib.error.HTTPError as exc:
        detalhe = (exc.read() or b"")[:200].decode("utf-8", "replace")
        app.logger.error(
            "Resend recusou o e-mail para %s (HTTP %s): %s", to_address, exc.code, detalhe
        )
        return False
    except Exception as exc:
        app.logger.error("Falha ao chamar o Resend para %s: %s", to_address, exc)
        return False


def send_email(to_address: str, subject: str, body: str) -> bool:
    """Envia por Resend; se não houver chave, cai para SMTP; sem nenhum, só loga."""
    if os.environ.get("RESEND_API_KEY", "").strip():
        return enviar_por_resend(to_address, subject, body)

    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        app.logger.warning(
            "Nenhum provedor de e-mail configurado — '%s' para %s não foi enviado.",
            subject,
            to_address,
        )
        return False

    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip() or username

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{APP_NAME} <{sender}>"
    message["To"] = to_address
    message.set_content(body)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.starttls()
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        return True
    except Exception as exc:
        app.logger.error("Falha ao enviar e-mail para %s: %s", to_address, exc)
        return False


# ── Token de redefinição de senha ────────────────────────────────────────────

def _password_fingerprint(user: dict) -> str:
    """Digital que muda quando a senha muda.

    Antes vinha do hash da senha, que morava aqui. Agora a senha vive no
    Supabase Auth, então a marca é o password_changed_at — atualizado toda vez
    que a senha é trocada. Continua garantindo token de uso único.
    """
    marca = (user.get("password_changed_at") or "") + "|" + str(user.get("_id", ""))
    return hashlib.sha256(marca.encode("utf-8")).hexdigest()[:16]


def build_reset_token(user: dict) -> str:
    serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=RESET_TOKEN_SALT)
    return serializer.dumps({"uid": user["_id"], "fp": _password_fingerprint(user)})


def load_reset_token(token: str) -> dict | None:
    """Devolve o usuário do token, ou None se inválido, expirado ou já usado."""
    serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=RESET_TOKEN_SALT)
    try:
        data = serializer.loads(token, max_age=RESET_TOKEN_MAX_AGE)
    except BadSignature:
        return None

    user = get_store().get_user_by_id(data.get("uid"))
    if not user:
        return None

    # A digital vem do password_changed_at: trocar a senha invalida o token.
    expected = _password_fingerprint(user)
    if not hmac.compare_digest(expected, str(data.get("fp", ""))):
        return None
    return user


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not g.user:
            flash("Entre com sua conta para continuar.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


@app.before_request
def assign_csp_nonce() -> None:
    g.csp_nonce = secrets.token_urlsafe(16)


@app.after_request
def apply_security_headers(response):
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    response.headers.setdefault(
        "Content-Security-Policy",
        CSP_TEMPLATE.format(nonce=g.get("csp_nonce", "")),
    )
    return response


@app.before_request
def load_current_user() -> None:
    user_id = session.get("user_id")
    g.user = None
    if not user_id:
        return
    user = get_store().get_user_by_id(user_id)
    if not user:
        session.pop("user_id", None)
        return

    # Se a senha mudou depois que esta sessão nasceu, a sessão morre — é o que
    # torna a redefinição capaz de expulsar um cookie roubado.
    if (user.get("password_changed_at") or "") != (session.get("pw_stamp") or ""):
        session.clear()
        return

    g.user = user


@app.context_processor
def inject_globals() -> dict:
    store = get_store()
    return {
        "app_name": APP_NAME,
        "current_user": g.get("user"),
        "storage_mode": store.kind,
        "storage_label": store.label,
        "current_year": datetime.now(BR_TZ).year,
        "csp_nonce": g.get("csp_nonce", ""),
        "stripe_configured": bool(os.environ.get("STRIPE_SECRET_KEY")),
        "stripe_pub_key": os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
    }


# ── Static & health ──────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return "OK", 200


@app.get("/favicon.ico")
def favicon():
    return redirect(url_for("public_static", filename="img/RECIBO.png"), code=307)


@app.get("/static/<path:filename>")
def public_static(filename: str):
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "static")
    return send_from_directory(static_dir, filename)


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    if g.user:
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))
        password = request.form.get("senha", "")
        user_id = auth_conferir_senha(email, password)
        user = get_store().get_user_by_id(user_id) if user_id else None
        if not user:
            flash("E-mail ou senha inválidos.", "danger")
            return render_template("login.html", form=request.form), 401
        session["user_id"] = user["_id"]
        session["pw_stamp"] = user.get("password_changed_at") or ""
        flash("Bem-vindo de volta!", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    if g.user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        erro_tamanho = field_limit_error(SIGNUP_FIELD_LIMITS)
        if erro_tamanho:
            flash(erro_tamanho, "danger")
            return render_template("register.html", form=request.form), 400

        full_name = request.form.get("nome_completo", "").strip()
        email = normalize_email(request.form.get("email", ""))
        password = request.form.get("senha", "")
        whatsapp = request.form.get("whatsapp", "").strip()
        cpf = request.form.get("cpf", "").strip()
        city = request.form.get("cidade", "").strip()
        plate = request.form.get("placa", "").strip().upper()
        vehicle_model = request.form.get("modelo_veiculo", "").strip()
        taxi_prefix = request.form.get("prefixo_taxi", "").strip()
        license_number = request.form.get("numero_alvara", "").strip()

        if not all([full_name, email, password, whatsapp, cpf, city, plate]):
            flash("Preencha todos os campos obrigatórios.", "danger")
            return render_template("register.html", form=request.form), 400

        if len(password) < 8:
            flash("A senha precisa ter pelo menos 8 caracteres.", "danger")
            return render_template("register.html", form=request.form), 400

        # O usuário nasce no Supabase Auth; o trigger on_auth_user_created
        # cria o perfil em `drivers` a partir deste metadata.
        try:
            user_id = auth_criar_usuario(
                email,
                password,
                {
                    "full_name": full_name,
                    "cpf": cpf,
                    "whatsapp": whatsapp,
                    "city": city,
                    "plate": plate,
                    "vehicle_model": vehicle_model,
                    "taxi_prefix": taxi_prefix,
                    "license_number": license_number,
                },
            )
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("register.html", form=request.form), 409
        except RuntimeError as exc:
            flash(str(exc), "danger")
            return render_template("register.html", form=request.form), 503

        session["user_id"] = user_id
        session["pw_stamp"] = ""
        flash("Conta criada! Já pode emitir e salvar seus recibos.", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.get("/sair")
def sair():
    session.clear()
    flash("Sessão encerrada com sucesso.", "info")
    return redirect(url_for("index"))


@app.route("/recuperar-senha", methods=["GET", "POST"])
def recuperar_senha():
    if g.user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))
        user = get_store().get_user_by_email(email) if email else None

        if user:
            link = absolute_url("redefinir_senha", token=build_reset_token(user))
            send_email(
                user["email"],
                f"Redefinição de senha — {APP_NAME}",
                (
                    f"Olá, {user['full_name'].split()[0]}.\n\n"
                    f"Recebemos um pedido para redefinir a senha da sua conta no {APP_NAME}.\n"
                    f"Abra o link abaixo para criar uma nova senha:\n\n"
                    f"{link}\n\n"
                    "O link vale por 1 hora e só pode ser usado uma vez.\n"
                    "Se não foi você quem pediu, ignore este e-mail — sua senha continua a mesma."
                ),
            )

        # Resposta idêntica exista ou não a conta, para não revelar cadastros.
        flash(
            "Se existir uma conta com este e-mail, enviamos um link de redefinição. "
            "O link vale por 1 hora.",
            "info",
        )
        return redirect(url_for("login"))

    return render_template("recuperar_senha.html")


@app.route("/redefinir-senha/<token>", methods=["GET", "POST"])
def redefinir_senha(token: str):
    user = load_reset_token(token)
    if not user:
        flash("Link inválido ou expirado. Peça um novo link de redefinição.", "danger")
        return redirect(url_for("recuperar_senha"))

    if request.method == "POST":
        password = request.form.get("senha", "")
        confirmation = request.form.get("confirmar_senha", "")

        if len(password) < 8:
            flash("A senha precisa ter pelo menos 8 caracteres.", "danger")
            return render_template("redefinir_senha.html", token=token), 400

        if password != confirmation:
            flash("As senhas não conferem.", "danger")
            return render_template("redefinir_senha.html", token=token), 400

        # A senha vai para o Auth; o carimbo fica no perfil e é o que derruba
        # as outras sessões e invalida este mesmo token.
        auth_definir_senha(user["_id"], password)
        get_store().update_user(user["_id"], {"password_changed_at": now_iso()})
        session.clear()
        flash("Senha redefinida! Entre com a nova senha.", "success")
        return redirect(url_for("login"))

    return render_template("redefinir_senha.html", token=token)


@app.route("/perfil", methods=["GET", "POST"])
@login_required
def perfil():
    """Correção de dados cadastrais — o que a LGPD chama de direito de retificação."""
    if request.method == "POST":
        erro_tamanho = field_limit_error(SIGNUP_FIELD_LIMITS)
        if erro_tamanho:
            flash(erro_tamanho, "danger")
            return render_template("perfil.html", form=request.form), 400

        # Alterar dados da conta pede a senha atual: sem isso, uma sessão
        # sequestrada trocaria o e-mail e tomaria a conta em silêncio.
        if not auth_conferir_senha(g.user["email"], request.form.get("senha_atual", "")):
            flash("Senha incorreta. Nenhuma alteração foi salva.", "danger")
            return render_template("perfil.html", form=request.form), 401

        full_name = request.form.get("nome_completo", "").strip()
        email = normalize_email(request.form.get("email", ""))
        whatsapp = request.form.get("whatsapp", "").strip()
        cpf = request.form.get("cpf", "").strip()
        city = request.form.get("cidade", "").strip()
        plate = request.form.get("placa", "").strip().upper()

        if not all([full_name, email, whatsapp, cpf, city, plate]):
            flash("Preencha todos os campos obrigatórios.", "danger")
            return render_template("perfil.html", form=request.form), 400

        if email != g.user["email"]:
            existente = get_store().get_user_by_email(email)
            if existente and existente["_id"] != g.user["_id"]:
                flash("Já existe uma conta com este e-mail.", "danger")
                return render_template("perfil.html", form=request.form), 409
            # O e-mail é o login: tem de mudar no Auth também, senão o
            # usuário salvaria o perfil e não conseguiria mais entrar.
            try:
                auth_definir_email(g.user["_id"], email)
            except Exception as exc:
                app.logger.error("Falha ao trocar o e-mail no Auth: %s", exc)
                flash("Não conseguimos alterar o e-mail agora. Tente mais tarde.", "danger")
                return render_template("perfil.html", form=request.form), 503

        get_store().update_user(
            g.user["_id"],
            {
                "full_name": full_name,
                "email": email,
                "whatsapp": whatsapp,
                "cpf": cpf,
                "city": city,
                "plate": plate,
                "vehicle_model": request.form.get("modelo_veiculo", "").strip(),
                "taxi_prefix": request.form.get("prefixo_taxi", "").strip(),
                "license_number": request.form.get("numero_alvara", "").strip(),
                "updated_at": now_iso(),
            },
        )
        flash("Cadastro atualizado.", "success")
        return redirect(url_for("dashboard"))

    return render_template("perfil.html", form=g.user)


@app.post("/excluir-conta")
@login_required
def excluir_conta():
    if request.form.get("confirmacao", "").strip().upper() != "EXCLUIR":
        flash('Digite EXCLUIR para confirmar a remoção da conta.', "danger")
        return redirect(url_for("dashboard"))

    if not auth_conferir_senha(g.user["email"], request.form.get("senha", "")):
        flash("Senha incorreta. A conta não foi excluída.", "danger")
        return redirect(url_for("dashboard"))

    # Apagar a conta sem conseguir parar a cobrança deixaria uma assinatura
    # órfã, cobrando alguém que já não tem como cancelá-la pelo app.
    subscription_id = g.user.get("stripe_subscription_id")
    if subscription_id:
        stripe = get_stripe()
        cancelada = False
        if stripe:
            try:
                stripe.Subscription.cancel(subscription_id)
                cancelada = True
            except Exception as exc:
                app.logger.error(
                    "Não foi possível cancelar a assinatura %s: %s", subscription_id, exc
                )

        if not cancelada:
            flash(
                "Não conseguimos cancelar sua assinatura agora, então a conta "
                "não foi excluída — apagá-la deixaria a cobrança ativa sem você "
                "poder pará-la. Tente de novo em alguns minutos ou fale com o "
                "suporte.",
                "danger",
            )
            return redirect(url_for("dashboard"))

    # Apagar no Auth basta: o cascade leva perfil, recibos e cotas.
    auth_excluir_usuario(g.user["_id"])
    session.clear()

    flash(
        "Sua conta e todos os seus recibos foram excluídos definitivamente.",
        "info",
    )
    return redirect(url_for("index"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard")
@login_required
def dashboard():
    receipts = []
    for receipt in get_store().list_receipts_by_driver(g.user["_id"]):
        share_url = public_receipt_url(receipt["rid"])
        item = dict(receipt)
        item["public_url"] = share_url
        item["whatsapp_link"] = build_whatsapp_link(item, share_url)
        item["email_link"] = build_email_link(item, share_url)
        receipts.append(item)

    month_start, month_end = month_range_utc()
    receipts_this_month = sum(
        1 for r in receipts if month_start <= r.get("created_at", "") < month_end
    )

    if request.args.get("subscribed") == "1":
        # O plano só muda quando o webhook chega, alguns segundos depois — não
        # dá para anunciar qual é sem arriscar dizer "Plano Free ativado".
        if g.user.get("plan") in PAID_PLANS:
            flash(
                f"✅ Plano {g.user['plan'].capitalize()} ativado com sucesso! "
                "Seja bem-vindo.",
                "success",
            )
        else:
            flash(
                "✅ Pagamento confirmado! Seu plano é ativado em alguns "
                "segundos — atualize a página se ainda aparecer como Grátis.",
                "success",
            )

    user_plan = g.user.get("plan", "free")
    return render_template(
        "dashboard.html",
        receipts=receipts,
        receipts_this_month=receipts_this_month,
        today=today_br(),
        user_plan=user_plan,
        monthly_limit=None if user_plan in PAID_PLANS else FREE_MONTHLY_LIMIT,
    )


@app.post("/recibo")
@login_required
def recibo_criar():
    erro_tamanho = field_limit_error(RECEIPT_FIELD_LIMITS)
    if erro_tamanho:
        flash(erro_tamanho, "danger")
        return redirect(url_for("dashboard"))

    passenger = request.form.get("passageiro", "").strip()
    trip_date = request.form.get("data", "").strip()
    origin = request.form.get("origem", "").strip()
    destination = request.form.get("destino", "").strip()
    payment_method = request.form.get("forma_pagamento", "").strip() or "Pix"

    if not all([passenger, trip_date, origin, destination]):
        flash("Preencha os dados principais da corrida.", "danger")
        return redirect(url_for("dashboard"))

    try:
        amount_value, amount_display = normalize_money(request.form.get("valor", ""))
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("dashboard"))

    rid = uuid4().hex[:RID_LENGTH].upper()
    receipt = {
        "_id": rid,
        "rid": rid,
        "driver_id": g.user["_id"],
        "passenger": passenger,
        "passenger_email": normalize_email(request.form.get("email_passageiro", "")),
        "passenger_whatsapp": request.form.get("whatsapp_passageiro", "").strip(),
        "trip_date": trip_date,
        "trip_date_display": format_date_br(trip_date),
        "trip_time": request.form.get("hora", "").strip(),
        "origin": origin,
        "destination": destination,
        "amount_value": amount_value,
        "amount_display": amount_display,
        "payment_method": payment_method,
        "notes": request.form.get("observacoes", "").strip(),
        "created_at": now_iso(),
        "driver_snapshot": {
            "full_name": g.user["full_name"],
            "email": g.user["email"],
            "whatsapp": g.user.get("whatsapp", ""),
            "city": g.user.get("city", ""),
            "plate": g.user.get("plate", ""),
            "vehicle_model": g.user.get("vehicle_model", ""),
            "taxi_prefix": g.user.get("taxi_prefix", ""),
            "license_number": g.user.get("license_number", ""),
        },
    }

    # Cota e gravação na mesma operação: sem a janela entre contar e inserir,
    # duas emissões simultâneas não furam mais o teto do plano Grátis.
    quota = None if g.user.get("plan", "free") in PAID_PLANS else FREE_MONTHLY_LIMIT
    if get_store().create_receipt(receipt, quota_limit=quota) is None:
        flash(
            f"Você atingiu o limite de {FREE_MONTHLY_LIMIT} recibos deste mês do "
            "plano Grátis. Faça upgrade para emitir recibos ilimitados.",
            "warning",
        )
        return redirect(url_for("planos"))

    flash("Recibo gerado e salvo na sua conta!", "success")
    return redirect(url_for("recibo_view", rid=rid, created="1"))


# ── Public generator (no login) ───────────────────────────────────────────────

@app.route("/gerar", methods=["GET", "POST"])
def gerador():
    if g.user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        erro_tamanho = field_limit_error(RECEIPT_FIELD_LIMITS)
        if erro_tamanho:
            flash(erro_tamanho, "danger")
            return render_template(
                "gerador.html", form=request.form, today=today_br()
            ), 400

        passenger = request.form.get("passageiro", "").strip()
        trip_date = request.form.get("data", "").strip()
        origin = request.form.get("origem", "").strip()
        destination = request.form.get("destino", "").strip()
        payment_method = request.form.get("forma_pagamento", "").strip() or "Pix"
        driver_name = request.form.get("nome_motorista", "").strip()
        driver_plate = request.form.get("placa", "").strip().upper()
        driver_phone = request.form.get("whatsapp_motorista", "").strip()
        driver_city = request.form.get("cidade_motorista", "").strip()
        driver_vehicle = request.form.get("modelo_veiculo", "").strip()

        if not all([passenger, trip_date, origin, destination, driver_name]):
            flash("Preencha os campos obrigatórios para gerar o recibo.", "danger")
            return render_template(
                "gerador.html", form=request.form, today=today_br()
            ), 400

        try:
            amount_value, amount_display = normalize_money(request.form.get("valor", ""))
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template(
                "gerador.html", form=request.form, today=today_br()
            ), 400

        # Só conta depois de validar: erro de preenchimento não gasta cota,
        # mas um bot mandando payload válido esbarra no teto.
        usados = get_store().bump_counter(f"gerar:{client_ip()}:{today_br()}")
        if usados > GUEST_DAILY_LIMIT:
            flash(
                f"Limite de {GUEST_DAILY_LIMIT} recibos por dia no gerador sem "
                "cadastro. Crie uma conta grátis para continuar emitindo.",
                "warning",
            )
            return render_template(
                "gerador.html", form=request.form, today=today_br()
            ), 429

        rid = uuid4().hex[:RID_LENGTH].upper()
        receipt = {
            "_id": rid,
            "rid": rid,
            "driver_id": None,
            "is_guest": True,
            "passenger": passenger,
            "passenger_email": normalize_email(request.form.get("email_passageiro", "")),
            "passenger_whatsapp": request.form.get("whatsapp_passageiro", "").strip(),
            "trip_date": trip_date,
            "trip_date_display": format_date_br(trip_date),
            "trip_time": request.form.get("hora", "").strip(),
            "origin": origin,
            "destination": destination,
            "amount_value": amount_value,
            "amount_display": amount_display,
            "payment_method": payment_method,
            "notes": request.form.get("observacoes", "").strip(),
            "created_at": now_iso(),
            "driver_snapshot": {
                "full_name": driver_name,
                "email": "",
                "whatsapp": driver_phone,
                "city": driver_city,
                "plate": driver_plate,
                "vehicle_model": driver_vehicle,
                "taxi_prefix": "",
                "license_number": "",
            },
        }

        get_store().create_receipt(receipt)

        # Quem acabou de emitir não tem conta, mas é o dono legítimo deste
        # recibo. A sessão é o que distingue ele de um visitante qualquer.
        emitidos = session.get("guest_receipts", [])
        session["guest_receipts"] = (emitidos + [rid])[-50:]

        return redirect(url_for("recibo_view", rid=rid, created="1"))

    return render_template("gerador.html", today=today_br())


# ── Pricing & Stripe ──────────────────────────────────────────────────────────

@app.get("/planos")
def planos():
    return render_template("planos.html")


@app.post("/assinar/<plan>")
@login_required
def assinar(plan: str):
    stripe = get_stripe()
    if not stripe:
        flash("Pagamentos ainda não configurados. Entre em contato conosco.", "warning")
        return redirect(url_for("planos"))

    price_env = STRIPE_PRICE_IDS.get(plan)
    if not price_env:
        abort(400)

    price_id = os.environ.get(price_env, "").strip()
    if not price_id:
        flash("Este plano não está disponível no momento.", "warning")
        return redirect(url_for("planos"))

    # Quem já assina troca de plano pelo portal. Abrir um segundo checkout
    # criaria uma assinatura paralela, cobrando os dois planos ao mesmo tempo.
    if g.user.get("plan") in PAID_PLANS and g.user.get("stripe_customer_id"):
        flash(
            "Você já tem uma assinatura ativa. Use 'Gerenciar plano' para "
            "trocar de plano ou cancelar.",
            "info",
        )
        return redirect(url_for("dashboard"))

    base = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    success_url = (
        f"{base}{url_for('dashboard')}?subscribed=1"
        if base
        else url_for("dashboard", subscribed=1, _external=True)
    )
    cancel_url = (
        f"{base}{url_for('planos')}"
        if base
        else url_for("planos", _external=True)
    )

    try:
        # Sem o parâmetro `customer`, o Checkout cria um Customer NOVO a cada
        # sessão — customer_email só preenche o campo. Dois checkouts virariam
        # dois clientes, com duas assinaturas cobrando em paralelo.
        customer_id = g.user.get("stripe_customer_id")
        if not customer_id:
            customer = stripe.Customer.create(
                email=g.user["email"],
                name=g.user.get("full_name") or None,
                metadata={"user_id": g.user["_id"]},
            )
            customer_id = customer.id
            get_store().update_user(g.user["_id"], {"stripe_customer_id": customer_id})

        checkout = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"user_id": g.user["_id"], "plan": plan},
            locale="pt-BR",
            idempotency_key=f"assinar:{g.user['_id']}:{plan}",
        )
    except Exception as exc:
        # A mensagem crua da Stripe pode conter identificadores internos.
        app.logger.error("Falha ao criar checkout para %s: %s", g.user["_id"], exc)
        flash(
            "Não conseguimos iniciar o pagamento agora. Tente de novo em "
            "alguns instantes ou fale com o suporte.",
            "danger",
        )
        return redirect(url_for("planos"))

    return redirect(checkout.url, code=303)


@app.post("/portal-cliente")
@login_required
def portal_cliente():
    stripe = get_stripe()
    if not stripe:
        abort(503)

    customer_id = g.user.get("stripe_customer_id")
    if not customer_id:
        flash("Você não possui uma assinatura ativa para gerenciar.", "warning")
        return redirect(url_for("dashboard"))

    return_url = absolute_url("dashboard")
    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id, return_url=return_url
        )
    except Exception as exc:
        # Sem isto, um erro da Stripe deixa quem paga sem caminho para cancelar.
        app.logger.error("Falha ao abrir o portal de %s: %s", g.user["_id"], exc)
        flash(
            "Não conseguimos abrir o portal de assinatura agora. Tente de novo "
            "em alguns instantes ou fale com o suporte.",
            "danger",
        )
        return redirect(url_for("dashboard"))

    return redirect(portal.url, code=303)


@app.post("/webhook/stripe")
def stripe_webhook():
    stripe = get_stripe()
    if not stripe:
        abort(503)

    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

    # construct_event só para validar a assinatura: o retorno é um StripeObject,
    # que não tem .get() e mudou de formato entre versões do SDK. O corpo já
    # chegou como JSON — tratá-lo como dict puro é estável e suficiente.
    try:
        stripe.Webhook.construct_event(payload, sig, secret)
        evento = json.loads(payload)
    except Exception:
        abort(400)

    obj = evento.get("data", {}).get("object") or {}
    tipo = evento.get("type", "")
    store = get_store()

    if tipo == "checkout.session.completed":
        user_id = obj.get("metadata", {}).get("user_id")
        plan = obj.get("metadata", {}).get("plan", "pro")
        if user_id:
            store.update_user(user_id, {
                "plan": plan,
                "stripe_customer_id": obj.get("customer"),
                "stripe_subscription_id": obj.get("subscription"),
                "subscription_status": "active",
            })

    elif tipo == "customer.subscription.deleted":
        user = store.get_user_by_stripe_customer(obj.get("customer"))
        if user:
            store.update_user(user["_id"], {
                "plan": "free",
                "stripe_subscription_id": None,
                "subscription_status": "canceled",
            })

    elif tipo == "customer.subscription.updated":
        user = store.get_user_by_stripe_customer(obj.get("customer"))
        if user:
            status = obj.get("status") or "inactive"
            if status in ACTIVE_SUBSCRIPTION_STATUSES:
                store.update_user(user["_id"], {"subscription_status": status})
            else:
                store.update_user(user["_id"], {
                    "plan": "free",
                    "subscription_status": status,
                })

    return jsonify({"ok": True})


# ── Manutenção ────────────────────────────────────────────────────────────────

@app.get("/tarefas/limpeza")
def tarefa_limpeza():
    """Apaga recibos do gerador público vencidos e contadores antigos.

    Chamada pelo Cron da Vercel, que envia 'Authorization: Bearer $CRON_SECRET'.
    Sem CRON_SECRET definido a rota fica fechada — falha fechada, não aberta.
    """
    secret = os.environ.get("CRON_SECRET", "").strip()
    if not secret:
        app.logger.warning("CRON_SECRET não definido — /tarefas/limpeza desativada.")
        abort(503)

    # compare_digest levanta TypeError com str não-ASCII; comparar bytes evita
    # que um header esquisito vire 500 em vez de 401.
    enviado = request.headers.get("Authorization", "").encode("utf-8", "replace")
    if not hmac.compare_digest(enviado, f"Bearer {secret}".encode("utf-8")):
        abort(401)

    store = get_store()
    now = datetime.now(timezone.utc)
    receipts_removed, receipts_left = store.purge_guest_receipts_before(
        to_utc_iso(now - timedelta(days=GUEST_RETENTION_DAYS)), CLEANUP_BATCH_LIMIT
    )
    counters_removed, counters_left = store.purge_counters_before(
        to_utc_iso(now - timedelta(days=COUNTER_RETENTION_DAYS)), CLEANUP_BATCH_LIMIT
    )

    incompleto = receipts_left or counters_left
    app.logger.info(
        "Limpeza: %s recibos sem cadastro e %s contadores removidos.%s",
        receipts_removed,
        counters_removed,
        " Ainda há backlog — a próxima execução continua." if incompleto else "",
    )
    return jsonify(
        {
            "ok": True,
            "recibos_removidos": receipts_removed,
            "contadores_removidos": counters_removed,
            "backlog_restante": incompleto,
        }
    )


# ── Legal ─────────────────────────────────────────────────────────────────────

@app.get("/privacidade")
def privacidade():
    return render_template("privacidade.html", updated_at="12 de setembro de 2026")


@app.get("/termos")
def termos():
    return render_template("termos.html", updated_at="12 de setembro de 2026")


# ── Receipt view ──────────────────────────────────────────────────────────────

@app.get("/recibo/<rid>")
def recibo_view(rid: str):
    receipt = get_store().get_receipt(rid)
    if not receipt:
        abort(404, description="Recibo não encontrado.")

    share_url = public_receipt_url(rid)
    is_owner = bool(g.user and g.user["_id"] == receipt.get("driver_id"))
    # O convidado que emitiu continua sendo o emissor enquanto durar a sessão.
    is_issuer = is_owner or rid in session.get("guest_receipts", [])

    context = dict(receipt)
    context["public_url"] = share_url
    context["whatsapp_link"] = build_whatsapp_link(context, share_url, with_recipient=is_issuer)
    context["email_link"] = build_email_link(context, share_url, with_recipient=is_issuer)
    context["is_owner"] = is_owner
    context["is_issuer"] = is_issuer
    context["is_guest"] = receipt.get("is_guest", False)
    context["just_created"] = request.args.get("created") == "1"

    return render_template("recibo_view.html", dados=context)


if __name__ == "__main__":
    app.run(debug=True)
