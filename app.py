import atexit
import hashlib
import json
import hmac
import os
import re
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
from werkzeug.security import check_password_hash, generate_password_hash

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

# Valor definido pelo Anderson em 2026-09-15, fechando a fase de testes (que
# rodou com 100). Os textos do site e o app leem daqui (inject_globals e
# /api/sessao), entao mudar este numero basta. A ficha da App Store (nota de
# revisao da assinatura) repete o numero e precisa ser ajustada a mao.
FREE_MONTHLY_LIMIT = 6
# "business" segue aqui de propósito: o plano saiu de venda, mas quem já assina
# mantém o acesso ilimitado até cancelar. Só STRIPE_PRICE_IDS perdeu a entrada,
# o que faz /assinar/business responder 400 para assinaturas novas.
PAID_PLANS = ("pro", "business")

# Página do app na App Store. Mora aqui, e não nos templates, para a landing,
# o rodapé e qualquer e-mail apontarem para o mesmo lugar.
APP_STORE_URL = "https://apps.apple.com/br/app/id6811583712"

MAX_RECEIPT_AMOUNT = Decimal("99999.99")

# 14 dígitos hex = 2^56. Com 10 (2^40) a chance de colisão passava de 36% em
# 1 milhão de recibos, e uma colisão derruba a emissão com erro 500.
RID_LENGTH = 14

# Origens do WebView do Capacitor. Só a /api/* responde a elas, e só com
# Bearer token — nunca com cookie, para não abrir caminho de CSRF.
ORIGENS_APP = ("capacitor://localhost", "https://localhost", "http://localhost")

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
    "documento_passageiro": ("O CPF/CNPJ do passageiro", 18),
    "razao_social": ("A razão social", 140),
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

# Teto diario de recibos enviados por e-mail, por motorista. Existe para
# proteger a reputacao do dominio: um endereco que dispara em volume vira spam
# para os provedores, e ai nem o e-mail de senha chega mais. Bater no teto nao
# impede emitir recibo — so deixa de mandar o e-mail daquele.
EMAIL_DAILY_LIMIT = 60
# A Política de Privacidade promete remover recibos sem cadastro em 12 meses.
GUEST_RETENTION_DAYS = 365
COUNTER_RETENTION_DAYS = 7
# past_due mantém o plano enquanto a Stripe tenta novas cobranças.
ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trialing", "past_due")

RESET_TOKEN_MAX_AGE = 3600  # 1 hora
# Pedidos de redefinicao por dia, por e-mail e por IP. Nao e economia: cada
# pedido e um e-mail, e um script disparando queima a reputacao do dominio —
# derrubando junto o e-mail de senha de quem precisa de verdade. O teto por IP e
# largo porque operadora de celular poe milhares de aparelhos atras do mesmo IP.
RESET_DAILY_LIMIT_EMAIL = 5
RESET_DAILY_LIMIT_IP = 30

# Painel /admin. Tentativas de login por dia: o painel mostra CPF e telefone de
# todos os motoristas, e a senha inicial e curta por decisao do dono — o teto
# e o que impede um script de adivinhar. A sessao do admin morre sozinha em
# 12 horas; a do motorista dura 90 dias porque um recibo nao vale um CPF alheio.
ADMIN_LOGIN_DAILY_LIMIT_IP = 20
ADMIN_LOGIN_DAILY_LIMIT_EMAIL = 10
ADMIN_SESSION_HOURS = 12
ADMIN_IDLE_MINUTES = 60
ADMIN_PASSWORD_MIN = 8
ADMIN_PAGE_SIZE = 50
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
    # Tudo desligado: o app não usa nenhuma dessas APIs. Menos permissão
    # declarada é menos coisa a justificar na revisão das lojas.
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=()",
}

STRIPE_PRICE_IDS = {
    "pro": "STRIPE_PRO_PRICE_ID",
    # Anual = o MESMO Pro, cobrado uma vez por ano com 50% de desconto
    # (R$ 119,40 = 19,90 × 12 × 0,5; decisão do Anderson em 2026-09-16, mesmo
    # preço no site e nas lojas). É um Price a mais no mesmo produto do Stripe:
    # o metadata.plan do checkout continua "pro", então webhook, gates e a
    # tela de planos não distinguem ciclo — só o Stripe sabe.
    "pro_anual": "STRIPE_PRO_ANUAL_PRICE_ID",
}
# Ciclo de cada chave de checkout. Tudo que não é "anual" é mensal.
PLAN_CYCLES = {"pro_anual": "anual"}
# True = o anual renova sozinho a cada 12 meses (cancelável no portal, como o
# mensal). False = "pagamento único" literal: o webhook marca
# cancel_at_period_end e o Stripe encerra no fim do ano, derrubando pra free
# pelo customer.subscription.deleted de sempre. Trocar aqui basta.
ANUAL_RENOVA_AUTOMATICAMENTE = True


def plano_base(plan: str) -> str:
    """'pro_anual' → 'pro'. É o valor gravado em drivers.plan."""
    return (plan or "").split("_", 1)[0]


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


def email_valido(value: str) -> bool:
    """Confere so o que evita erro bobo: uma arroba, um ponto no dominio, sem
    espaco e dentro do tamanho da RFC.

    Nao tenta validar se o endereco existe. Isso quem responde e o provedor do
    destinatario, e o motorista digitou o que o passageiro ditou — recusar um
    endereco plausivel na tela custaria mais que um e-mail devolvido.
    """
    endereco = normalize_email(value)
    if not endereco or len(endereco) > 254 or any(c.isspace() for c in endereco):
        return False
    if endereco.count("@") != 1:
        return False
    usuario, _, dominio = endereco.partition("@")
    return bool(usuario) and "." in dominio and not dominio.startswith(".") \
        and not dominio.endswith(".")


def sanitize_phone(value: str) -> str:
    return "".join(char for char in (value or "") if char.isdigit())


def phone_e164(value: str, ddi: str = "55") -> str:
    """Telefone no formato que o wa.me exige: so digitos, com codigo do pais.

    Sem o 55 na frente, o link abre o WhatsApp sem destinatario e o motorista
    precisa ter o passageiro salvo na agenda — exatamente o que este app existe
    para evitar.

    A classificacao e por tamanho, nao por prefixo, e isso importa: existe o DDD
    55 (Santa Maria/RS). "5598765432" tem 10 digitos, entao e DDD 55 + fixo, e
    vira "555598765432". Quem olhasse so o prefixo trataria como codigo de pais
    e mandaria a mensagem para um numero que nao existe.
    """
    digitos = sanitize_phone(value)
    if digitos.startswith("00"):      # prefixo de discagem internacional
        digitos = digitos[2:]
    digitos = digitos.lstrip("0")     # zero de DDD interurbano
    if len(digitos) in (10, 11):      # DDD + numero, sem pais
        return ddi + digitos
    if len(digitos) in (12, 13):      # ja veio com o codigo do pais
        return digitos
    return ""                         # curto ou longo demais: nao da link


def razao_social_de(documento: str, valor: str) -> str:
    """Razao social so existe quando o documento e CNPJ.

    A regra mora aqui, e nao so na tela do app: o servidor nao confia no
    JavaScript que esconde o campo, e um CPF com nome de empresa junto seria
    um recibo que a contabilidade recusa. Quatorze digitos e CNPJ; qualquer
    outra coisa devolve vazio.
    """
    if len(re.sub(r"\D", "", documento or "")) != 14:
        return ""
    return " ".join((valor or "").split())


def format_document_br(value: str) -> str:
    """Pontua CPF e CNPJ; devolve como veio se nao reconhecer o tamanho.

    Nao valida digito verificador de proposito: o campo e opcional e serve para
    o passageiro prestar contas. Recusar um documento estrangeiro, ou um CPF
    que o passageiro ditou errado, so trocaria um recibo util por um erro na
    tela do taxista, com o passageiro esperando dentro do carro.
    """
    d = sanitize_phone(value)          # mesma limpeza: so digitos
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return (value or "").strip()


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
        # Documento e razao social so entram quando existem: sao o que faz o
        # recibo valer para a empresa lancar a despesa. O app ja mandava no
        # WhatsApp; sem isto, o mesmo recibo por e-mail saia sem eles.
        *([f"🧾 CPF/CNPJ: {receipt['passenger_document']}"]
          if receipt.get("passenger_document") else []),
        *([f"🏢 Razão social: {receipt['passenger_company']}"]
          if receipt.get("passenger_company") else []),
        f"📅 Data: {receipt.get('trip_date_display') or '-'}",
        f"📍 Origem: {receipt.get('origin') or '-'}",
        f"🏁 Destino: {receipt.get('destination') or '-'}",
        f"💰 Valor: R$ {receipt.get('amount_display') or '-'}",
        f"💳 Pagamento: {receipt.get('payment_method') or '-'}",
        "",
        f"🔗 Acesse o recibo: {public_url}",
    ]
    # O recibo e repassado entre passageiros; o telefone impresso traz corrida
    # nova para o motorista sem custo de divulgacao.
    fone_motorista = (receipt.get("driver_snapshot") or {}).get("whatsapp") or ""
    if fone_motorista:
        lines += ["", f"🚕 Precisou de corrida? Me chame no WhatsApp: {fone_motorista}"]
    return "\n".join(lines)


def build_whatsapp_link(receipt: dict, public_url: str, with_recipient: bool = True) -> str:
    """Link de compartilhamento.

    with_recipient=False omite o telefone do passageiro: a página do recibo é
    pública e qualquer visitante com o link leria o destinatário no href.
    """
    phone = phone_e164(receipt.get("passenger_whatsapp", "")) if with_recipient else ""
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


def enviar_recibo_por_email(receipt: dict) -> bool:
    """Manda o recibo para o e-mail do passageiro. Devolve se o provedor aceitou.

    O corpo e o mesmo texto do WhatsApp, com o link da pagina publica — e la que
    o passageiro ve o recibo formatado, imprime ou salva em PDF. Texto puro, e
    nao HTML, porque o que importa e chegar: e-mail transacional simples passa
    por qualquer filtro e qualquer leitor.
    """
    destino = normalize_email(receipt.get("passenger_email", ""))
    if not email_valido(destino):
        return False

    motorista = (receipt.get("driver_snapshot") or {}).get("full_name") or ""
    abertura = f"Segue o recibo da sua corrida com {motorista}." if motorista \
        else "Segue o recibo da sua corrida."
    corpo = "\n".join([
        "Olá!",
        "",
        abertura,
        "",
        compose_receipt_message(receipt, public_receipt_url(receipt["rid"])),
        "",
        "Este recibo não é documento fiscal.",
        "Você recebeu este e-mail porque este endereço foi informado no momento "
        "da corrida. Não é preciso responder.",
    ])
    # Tempo curto: este envio acontece dentro da requisicao que grava o recibo,
    # e o aparelho espera essa resposta para liberar o botao de enviar.
    return send_email(destino, f"Recibo da sua corrida — #{receipt['rid']}",
                      corpo, timeout=8)


def pode_enviar_email(driver_id: str) -> bool:
    """Consome uma unidade do teto diario do motorista."""
    usados = get_store().bump_counter(f"email:{driver_id}:{today_br()}")
    return usados <= EMAIL_DAILY_LIMIT


def pode_pedir_redefinicao(email: str) -> bool:
    """Consome uma unidade dos tetos diarios de redefinicao (e-mail e IP)."""
    store = get_store()
    dia = today_br()
    por_email = store.bump_counter(f"reset:email:{email}:{dia}")
    por_ip = store.bump_counter(f"reset:ip:{client_ip()}:{dia}")
    return por_email <= RESET_DAILY_LIMIT_EMAIL and por_ip <= RESET_DAILY_LIMIT_IP


def enviar_link_de_redefinicao(user: dict) -> bool:
    """Manda o link de nova senha. Usado pelo site e pelo app.

    O link abre a pagina do site mesmo quando o pedido vem do app: a senha
    vive no Supabase Auth e a troca acontece no servidor. O motorista cria a
    senha nova no navegador e volta ao app para entrar.
    """
    link = absolute_url("redefinir_senha", token=build_reset_token(user))
    return send_email(
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
    casual (cadastro, pedido de nova senha, login do painel); contra um
    atacante determinado o caminho é o WAF da Vercel, que age antes da
    função rodar.
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
        passenger_whatsapp, passenger_document, passenger_company,
        trip_date, trip_time,
        origin, destination, amount, payment_method, notes, driver_snapshot,
        created_at
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
        # Campo novo: as versoes do app ja instaladas no aparelho do motorista
        # nao enviam documento. Sem o default, cada recibo dessas versoes
        # quebraria no insert em vez de gravar sem o campo opcional.
        dados["passenger_document"] = dados.get("passenger_document") or ""
        dados["passenger_company"] = dados.get("passenger_company") or ""

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
                     passenger_whatsapp, passenger_document, passenger_company,
                     trip_date, trip_time,
                     origin, destination, amount, payment_method, notes,
                     driver_snapshot{col_created})
                    values (%(rid)s, %(driver_id)s, %(is_guest)s, %(passenger)s,
                            %(passenger_email)s, %(passenger_whatsapp)s,
                            %(passenger_document)s, %(passenger_company)s,
                            %(trip_date)s,
                            %(trip_time)s, %(origin)s, %(destination)s, %(amount)s,
                            %(payment_method)s, %(notes)s, %(driver_snapshot)s{val_created})
                    returning {self._RECEIPT_COLS}""",
                dados,
            ).fetchone()
        return self._receipt_out(row)

    def create_receipt_idempotente(
        self, payload: dict, quota_limit: int | None
    ) -> tuple[dict | None, bool]:
        """Cria o recibo com um rid escolhido pelo CLIENTE.

        Devolve (recibo, criado_agora). Se o rid já existe e é do mesmo
        motorista, devolve o existente com criado_agora=False e **não gasta
        cota** — é retentativa da fila offline, não recibo novo.

        Devolve (None, False) quando a cota do mês acabou.
        """
        rid = payload["rid"]
        existente = self.get_receipt(rid)
        if existente:
            mesmo_dono = str(existente.get("driver_id") or "") == str(
                payload.get("driver_id") or ""
            )
            return (existente, False) if mesmo_dono else (None, False)

        criado = self.create_receipt(payload, quota_limit=quota_limit)
        return (criado, criado is not None)

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

    # ── administradores (/admin) ────────────────────────────────────────────
    _ADMIN_COLS = "id as _id, email, password_hash, created_at, password_changed_at, last_login_at"

    def get_admin_by_email(self, email: str) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"select {self._ADMIN_COLS} from public.admin_users where lower(email) = lower(%s)",
                (email,),
            ).fetchone()
        return dict(row) if row else None

    def get_admin_by_id(self, admin_id: str | None) -> dict | None:
        if not admin_id:
            return None
        with self.pool.connection() as conn:
            row = conn.execute(
                f"select {self._ADMIN_COLS} from public.admin_users where id = %s",
                (admin_id,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_admin(self, email: str, password_hash: str) -> dict:
        """Cria o administrador, ou redefine a senha se o e-mail ja existir."""
        with self.pool.connection() as conn:
            row = conn.execute(
                f"""insert into public.admin_users (email, password_hash)
                    values (lower(%s), %s)
                    on conflict (lower(email)) do update
                      set password_hash = excluded.password_hash,
                          password_changed_at = now()
                    returning {self._ADMIN_COLS}""",
                (email, password_hash),
            ).fetchone()
        return dict(row)

    def set_admin_password(self, admin_id: str, password_hash: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """update public.admin_users
                      set password_hash = %s, password_changed_at = now()
                    where id = %s""",
                (password_hash, admin_id),
            )

    def touch_admin_login(self, admin_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "update public.admin_users set last_login_at = now() where id = %s",
                (admin_id,),
            )

    def delete_admin(self, admin_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("delete from public.admin_users where id = %s", (admin_id,))

    # ── contadores e limpeza ────────────────────────────────────────────────
    def bump_counter(self, key: str) -> int:
        with self.pool.connection() as conn:
            return conn.execute(
                "select public.bump_counter(%s) as n", (key,)
            ).fetchone()["n"]

    def reset_counter(self, key: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("delete from public.rate_limits where key = %s", (key,))

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
    # Sem PERMANENT_SESSION_LIFETIME o cookie sai sem Expires e vira cookie de
    # sessão do navegador: some quando o processo fecha. Num WebView isso
    # significa deslogar o motorista toda vez que ele abre o app.
    PERMANENT_SESSION_LIFETIME=timedelta(days=90),
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


def auth_entrar_com_token(email: str, password: str) -> dict | None:
    """Login para o app nativo: devolve os tokens, não cria sessão de cookie.

    O WebView do Capacitor roda em capacitor://localhost, uma origem diferente
    da API — o cookie de sessão não viajaria. Por isso o app guarda o token.
    """
    try:
        resposta = supabase_public().auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except Exception:
        return None
    if not resposta or not resposta.session or not resposta.user:
        return None
    return {
        "user_id": str(resposta.user.id),
        "access_token": resposta.session.access_token,
        "refresh_token": resposta.session.refresh_token,
        "expires_at": resposta.session.expires_at,
    }


def auth_renovar_sessao(refresh_token: str) -> dict | None:
    """Troca o refresh token por um access token novo.

    O access token do Supabase vale uma hora. Sem esta rota o app parava de
    sincronizar em silencio depois desse tempo: o 401 fazia a fila desistir, o
    motorista seguia emitindo recibo que nunca subia, e o passageiro recebia
    link que nunca ia funcionar. So o botao "Sair", manual, destravava.
    """
    if not refresh_token:
        return None
    try:
        resposta = supabase_public().auth.refresh_session(refresh_token)
    except Exception:
        return None
    if not resposta or not resposta.session or not resposta.user:
        return None
    return {
        "user_id": str(resposta.user.id),
        "access_token": resposta.session.access_token,
        "refresh_token": resposta.session.refresh_token,
        "expires_at": resposta.session.expires_at,
    }


def auth_usuario_do_token(token: str) -> str | None:
    """Valida o JWT no Supabase e devolve o id do usuário."""
    if not token:
        return None
    try:
        resposta = supabase_admin().auth.get_user(token)
    except Exception:
        return None
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


def enviar_por_resend(to_address: str, subject: str, body: str, timeout: int = 15) -> bool:
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
        with urllib.request.urlopen(requisicao, timeout=timeout) as resposta:
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


def send_email(to_address: str, subject: str, body: str, timeout: int = 15) -> bool:
    """Envia por Resend; se não houver chave, cai para SMTP; sem nenhum, só loga."""
    if os.environ.get("RESEND_API_KEY", "").strip():
        return enviar_por_resend(to_address, subject, body, timeout=timeout)

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


def api_login_required(view):
    """Aceita sessão de cookie (navegador) ou Bearer token (app nativo)."""

    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.get("user"):
            return view(*args, **kwargs)

        cabecalho = request.headers.get("Authorization", "")
        if cabecalho.startswith("Bearer "):
            user_id = auth_usuario_do_token(cabecalho[7:].strip())
            if user_id:
                usuario = get_store().get_user_by_id(user_id)
                if usuario:
                    g.user = usuario
                    return view(*args, **kwargs)

        return jsonify({"erro": "nao_autenticado"}), 401

    return wrapped_view


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
    # Vale para toda resposta: o cookie ganha Expires e sobrevive ao reinício
    # do app. Sem isto, PERMANENT_SESSION_LIFETIME não é aplicado.
    session.permanent = True


@app.after_request
def apply_cors_do_app(response):
    """CORS restrito à /api/*, só para as origens do app nativo."""
    origem = request.headers.get("Origin", "")
    if origem in ORIGENS_APP and request.path.startswith("/api/"):
        response.headers["Access-Control-Allow-Origin"] = origem
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Vary"] = "Origin"
        # Sem Allow-Credentials de propósito: o app usa Bearer, não cookie.
    return response


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
        "free_monthly_limit": FREE_MONTHLY_LIMIT,
        "app_store_url": APP_STORE_URL,
    }


# ── Static & health ──────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return "OK", 200


@app.get("/favicon.ico")
def favicon():
    return redirect(url_for("public_static", filename="img/RECIBO.png"), code=307)


@app.get("/app/")
@app.get("/app/<path:filename>")
def app_offline(filename: str = "index.html"):
    """Serve o bundle do app nativo, para testar no navegador.

    No Capacitor estes mesmos arquivos vão empacotados no aparelho — este
    caminho existe só para desenvolvimento.
    """
    pasta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mobile", "www")
    return send_from_directory(pasta, filename)


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
        if email and not pode_pedir_redefinicao(email):
            flash("Muitos pedidos para este e-mail hoje. Tente novamente mais tarde.", "danger")
            return render_template("recuperar_senha.html"), 429

        user = get_store().get_user_by_email(email) if email else None
        if user:
            enviar_link_de_redefinicao(user)

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
        "passenger_document": format_document_br(
            request.form.get("documento_passageiro", "")),
        "passenger_company": razao_social_de(
            request.form.get("documento_passageiro", ""),
            request.form.get("razao_social", "")),
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
    salvo = get_store().create_receipt(receipt, quota_limit=quota)
    if salvo is None:
        flash(
            f"Você atingiu o limite de {FREE_MONTHLY_LIMIT} recibos deste mês do "
            "plano Grátis. Faça upgrade para emitir recibos ilimitados.",
            "warning",
        )
        return redirect(url_for("planos"))

    flash("Recibo gerado e salvo na sua conta!", "success")
    return redirect(url_for("recibo_view", rid=rid, created="1"))


# ── Gerador público (desligado) ───────────────────────────────────────────────

@app.get("/gerar")
@login_required
def gerador():
    """Emitir recibo no site exige conta desde 2026-09-22.

    O gerador sem cadastro foi desligado; a rota fica só para os links antigos
    (favoritos, WhatsApp, resultado do Google) não caírem em 404. Sem sessão o
    login_required manda para o login; com sessão, o painel já tem o formulário.
    """
    return redirect(url_for("dashboard"))


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
        # Exceção: mensal → anual é troca de Price na assinatura que já existe.
        # O Stripe zera o ciclo, credita os dias não usados do mês e cobra o
        # ano na hora (always_invoice) — sem segunda assinatura.
        if plan == "pro_anual" and g.user.get("stripe_subscription_id"):
            return _migrar_para_anual(stripe, price_id)
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
            metadata={"user_id": g.user["_id"], "plan": plano_base(plan),
                      "ciclo": PLAN_CYCLES.get(plan, "mensal")},
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


def _migrar_para_anual(stripe, price_id: str):
    """Troca o Price da assinatura ativa pelo anual. Só é chamado pelo /assinar."""
    try:
        sub = stripe.Subscription.retrieve(g.user["stripe_subscription_id"])
        item = sub["items"]["data"][0]
        if item["price"]["id"] == price_id:
            flash("Você já está no plano anual.", "info")
            return redirect(url_for("dashboard"))
        stripe.Subscription.modify(
            sub["id"],
            items=[{"id": item["id"], "price": price_id}],
            proration_behavior="always_invoice",
            cancel_at_period_end=not ANUAL_RENOVA_AUTOMATICAMENTE,
            metadata={"plan": "pro", "ciclo": "anual"},
            idempotency_key=f"anual:{g.user['_id']}:{sub['id']}",
        )
    except Exception as exc:
        app.logger.error("Falha ao migrar %s para o anual: %s", g.user["_id"], exc)
        flash(
            "Não conseguimos trocar para o plano anual agora. Tente de novo "
            "em alguns instantes ou fale com o suporte.",
            "danger",
        )
        return redirect(url_for("planos"))
    flash(
        "Pronto! Sua assinatura passou para o plano anual: R$ 119,40 por ano, "
        "com o que sobrou do mês já descontado.",
        "success",
    )
    return redirect(url_for("dashboard"))


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
        # plano_base por segurança: um checkout antigo/manual com "pro_anual"
        # no metadata não pode gravar um plano que os gates não conhecem.
        plan = plano_base(obj.get("metadata", {}).get("plan", "pro")) or "pro"
        if user_id:
            store.update_user(user_id, {
                "plan": plan,
                "stripe_customer_id": obj.get("customer"),
                "stripe_subscription_id": obj.get("subscription"),
                "subscription_status": "active",
            })
        # "Pagamento único" literal: o anual não renova. O Stripe encerra no
        # fim dos 12 meses e o subscription.deleted abaixo derruba pra free.
        if (not ANUAL_RENOVA_AUTOMATICAMENTE
                and obj.get("metadata", {}).get("ciclo") == "anual"
                and obj.get("subscription")):
            try:
                stripe.Subscription.modify(obj["subscription"], cancel_at_period_end=True)
            except Exception as exc:
                app.logger.error("cancel_at_period_end falhou para %s: %s", user_id, exc)

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


# ── API do app (fila offline) ─────────────────────────────────────────────────

@app.route("/api/<path:_qualquer>", methods=["OPTIONS"])
def api_preflight(_qualquer: str):
    """Responde ao preflight do navegador. O CORS vem do after_request."""
    return ("", 204)


@app.post("/api/login")
def api_login():
    """Login do app nativo. Devolve tokens em vez de criar cookie."""
    dados = request.get_json(silent=True) or {}
    tokens = auth_entrar_com_token(
        normalize_email(str(dados.get("email", ""))), str(dados.get("senha", ""))
    )
    if not tokens:
        return jsonify({"erro": "credenciais_invalidas"}), 401

    usuario = get_store().get_user_by_id(tokens["user_id"])
    if not usuario:
        return jsonify({"erro": "perfil_ausente"}), 401

    return jsonify(tokens)


@app.post("/api/recuperar-senha")
def api_recuperar_senha():
    """Pedido de nova senha pelo app.

    Sem isto o motorista que esquecia a senha nao tinha saida dentro do app: a
    tela de login so oferecia entrar ou criar conta. A resposta e a mesma exista
    ou nao a conta — dizer "nao encontrado" contaria a um estranho quem esta
    cadastrado.
    """
    dados = request.get_json(silent=True) or {}
    email = normalize_email(str(dados.get("email", "")))
    if not email_valido(email):
        return jsonify({"erro": "email_invalido"}), 400
    if not pode_pedir_redefinicao(email):
        return jsonify({"erro": "muitos_pedidos"}), 429

    user = get_store().get_user_by_email(email)
    if user:
        try:
            enviar_link_de_redefinicao(user)
        except Exception as exc:
            app.logger.error("Falha ao enviar link de redefinicao: %s", exc)

    return jsonify({"enviado": True})


@app.post("/api/excluir-conta")
@api_login_required
def api_excluir_conta():
    """Exclusao de conta pelo proprio app.

    As duas lojas exigem que quem cria conta dentro do app consiga apagar
    dentro do app — Apple na diretriz 5.1.1(v), Google na politica de exclusao
    de dados. Mandar o motorista para o site nao cumpre.

    Pede a senha, e nao so o token: token vazado num aparelho emprestado nao
    pode apagar a conta e todos os recibos de alguem.
    """
    dados = request.get_json(silent=True) or {}
    if not auth_conferir_senha(g.user["email"], str(dados.get("senha", ""))):
        return jsonify({"erro": "senha_incorreta"}), 403

    # Apagar sem conseguir parar a cobranca deixaria assinatura orfa, cobrando
    # alguem que ja nao tem como cancelar. Mesma regra do site.
    assinatura = g.user.get("stripe_subscription_id")
    if assinatura:
        stripe = get_stripe()
        try:
            if not stripe:
                raise RuntimeError("Stripe nao configurada")
            stripe.Subscription.cancel(assinatura)
        except Exception as exc:
            app.logger.error("Nao cancelou a assinatura %s: %s", assinatura, exc)
            return jsonify({"erro": "assinatura_ativa"}), 409

    # Quem assinou pela loja cancela na loja: a Apple e a Google nao deixam o
    # servidor cancelar por fora, e apagar aqui nao para a cobranca de la.
    if g.user.get("plan") in PAID_PLANS and not assinatura:
        return jsonify({"erro": "assinatura_na_loja"}), 409

    auth_excluir_usuario(g.user["_id"])
    return jsonify({"excluida": True})


@app.post("/api/recibos/<rid>/email")
@api_login_required
def api_enviar_recibo_por_email(rid: str):
    """Manda o recibo para o e-mail do passageiro.

    Este e o UNICO lugar que envia recibo por e-mail. Emitir nao envia nada:
    preencher o campo e uma coisa, mandar e outra, e cada envio custa — no
    plano gratuito do Resend sao 100 por dia somados todos os motoristas.
    """
    recibo = get_store().get_receipt(str(rid).strip().upper())
    # Mesma resposta para recibo inexistente e recibo de outro motorista: dizer
    # qual dos dois e contaria a um estranho que aquele codigo existe.
    if not recibo or str(recibo.get("driver_id") or "") != str(g.user["_id"]):
        return jsonify({"erro": "nao_encontrado"}), 404

    if not email_valido(recibo.get("passenger_email", "")):
        return jsonify({"erro": "sem_email"}), 400

    if not pode_enviar_email(g.user["_id"]):
        return jsonify({"erro": "limite_diario", "limite": EMAIL_DAILY_LIMIT}), 429

    try:
        enviou = enviar_recibo_por_email(recibo)
    except Exception as exc:
        app.logger.error("Falha ao enviar recibo %s: %s", recibo.get("rid"), exc)
        enviou = False
    if not enviou:
        return jsonify({"erro": "falha_no_envio"}), 502

    return jsonify({"enviado": True, "para": recibo["passenger_email"]})


@app.post("/api/refresh")
def api_refresh():
    """Renova a sessao do app. Nao exige o access token — ele ja expirou."""
    dados = request.get_json(silent=True) or {}
    tokens = auth_renovar_sessao(str(dados.get("refresh_token", "")).strip())
    if not tokens:
        return jsonify({"erro": "refresh_invalido"}), 401

    # Se o perfil sumiu (conta apagada), renovar nao adianta: manda para login.
    if not get_store().get_user_by_id(tokens["user_id"]):
        return jsonify({"erro": "perfil_ausente"}), 401

    return jsonify(tokens)


@app.post("/api/cadastro")
def api_cadastro():
    """Cadastro pelo app nativo. Devolve os tokens já logado.

    Existe porque mandar o motorista para o site abriria o Safari — além da
    experiência ruim, é um dos padrões que a Apple rejeita (Guideline 4.2).
    """
    dados = request.get_json(silent=True) or {}

    campos = {k: str(dados.get(k, "") or "").strip() for k in (
        "nome_completo", "email", "senha", "whatsapp", "cpf", "cidade",
        "placa", "modelo_veiculo", "prefixo_taxi", "numero_alvara")}
    campos["email"] = normalize_email(campos["email"])
    campos["placa"] = campos["placa"].upper()

    for nome, (rotulo, maximo) in SIGNUP_FIELD_LIMITS.items():
        if len(campos.get(nome, "")) > maximo:
            return jsonify({"erro": "campo_longo", "campo": nome, "maximo": maximo}), 400

    obrigatorios = ["nome_completo", "email", "senha", "whatsapp", "cpf", "cidade", "placa"]
    faltando = [c for c in obrigatorios if not campos[c]]
    if faltando:
        return jsonify({"erro": "campos_obrigatorios", "campos": faltando}), 400

    if len(campos["senha"]) < 8:
        return jsonify({"erro": "senha_curta", "minimo": 8}), 400

    try:
        auth_criar_usuario(
            campos["email"],
            campos["senha"],
            {k: campos[k] for k in (
                "nome_completo", "cpf", "whatsapp", "cidade", "placa",
                "modelo_veiculo", "prefixo_taxi", "numero_alvara")}
            | {"full_name": campos["nome_completo"], "city": campos["cidade"],
               "plate": campos["placa"], "vehicle_model": campos["modelo_veiculo"],
               "taxi_prefix": campos["prefixo_taxi"],
               "license_number": campos["numero_alvara"]},
        )
    except ValueError as exc:
        return jsonify({"erro": "email_em_uso", "mensagem": str(exc)}), 409
    except RuntimeError:
        return jsonify({"erro": "indisponivel"}), 503

    tokens = auth_entrar_com_token(campos["email"], campos["senha"])
    if not tokens:
        return jsonify({"erro": "criado_sem_login"}), 500
    return jsonify(tokens), 201


@app.get("/api/sessao")
@api_login_required
def api_sessao():
    """Quem sou eu — o app usa para saber se a sessão ainda vale."""
    mes_inicio, mes_fim = month_range_utc()
    plano = g.user.get("plan", "free")
    return jsonify(
        {
            "id": g.user["_id"],
            "nome": g.user.get("full_name", ""),
            "email": g.user.get("email", ""),
            "plano": plano,
            "limite_mensal": None if plano in PAID_PLANS else FREE_MONTHLY_LIMIT,
            # O app precisa saber a origem: quem assinou pela Stripe no site
            # gerencia lá; quem assinou pelo app gerencia na loja.
            "origem_assinatura": (
                "stripe" if g.user.get("stripe_customer_id")
                else "loja" if plano in PAID_PLANS
                else None
            ),
            "usados_no_mes": get_store().count_receipts_in_range(
                g.user["_id"], mes_inicio, mes_fim, 10_000
            ),
            "motorista": {
                "full_name": g.user.get("full_name", ""),
                "email": g.user.get("email", ""),
                "whatsapp": g.user.get("whatsapp", ""),
                "city": g.user.get("city", ""),
                "plate": g.user.get("plate", ""),
                "vehicle_model": g.user.get("vehicle_model", ""),
                "taxi_prefix": g.user.get("taxi_prefix", ""),
                "license_number": g.user.get("license_number", ""),
            },
        }
    )


@app.post("/api/recibos")
@api_login_required
def api_criar_recibo():
    """Recebe um recibo emitido OFFLINE, com rid escolhido pelo aparelho.

    É idempotente de propósito: a fila do app reenvia o que não teve resposta
    confirmada, e sem isso uma resposta perdida viraria recibo duplicado.
    """
    dados = request.get_json(silent=True) or {}

    rid = str(dados.get("rid", "")).strip().upper()
    if not re.fullmatch(r"[0-9A-F]{%d}" % RID_LENGTH, rid):
        return jsonify({"erro": "rid_invalido"}), 400

    campos = {
        "passageiro": dados.get("passageiro", ""),
        "data": dados.get("data", ""),
        "origem": dados.get("origem", ""),
        "destino": dados.get("destino", ""),
        "valor": dados.get("valor", ""),
        "forma_pagamento": dados.get("forma_pagamento", ""),
        "observacoes": dados.get("observacoes", ""),
        "email_passageiro": dados.get("email_passageiro", ""),
        "whatsapp_passageiro": dados.get("whatsapp_passageiro", ""),
        "documento_passageiro": dados.get("documento_passageiro", ""),
        "razao_social": dados.get("razao_social", ""),
        "hora": dados.get("hora", ""),
    }
    for nome, (rotulo, maximo) in RECEIPT_FIELD_LIMITS.items():
        if len(str(campos.get(nome, "") or "")) > maximo:
            return jsonify({"erro": "campo_longo", "campo": nome, "maximo": maximo}), 400

    if not all([campos["passageiro"], campos["data"], campos["origem"], campos["destino"]]):
        return jsonify({"erro": "campos_obrigatorios"}), 400

    try:
        amount_value, amount_display = normalize_money(str(campos["valor"]))
    except ValueError as exc:
        return jsonify({"erro": "valor_invalido", "mensagem": str(exc)}), 400

    receipt = {
        "_id": rid,
        "rid": rid,
        "driver_id": g.user["_id"],
        "is_guest": False,
        "passenger": str(campos["passageiro"]).strip(),
        "passenger_email": normalize_email(str(campos["email_passageiro"])),
        "passenger_whatsapp": str(campos["whatsapp_passageiro"]).strip(),
        "passenger_document": format_document_br(str(campos["documento_passageiro"])),
        "passenger_company": razao_social_de(
            str(campos["documento_passageiro"]), str(campos["razao_social"])),
        "trip_date": str(campos["data"]).strip(),
        "trip_time": str(campos["hora"]).strip(),
        "origin": str(campos["origem"]).strip(),
        "destination": str(campos["destino"]).strip(),
        "amount_value": amount_value,
        "amount_display": amount_display,
        "payment_method": str(campos["forma_pagamento"]).strip() or "Pix",
        "notes": str(campos["observacoes"]).strip(),
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
    if dados.get("created_at"):
        receipt["created_at"] = str(dados["created_at"])

    quota = None if g.user.get("plan", "free") in PAID_PLANS else FREE_MONTHLY_LIMIT
    salvo, criado_agora = get_store().create_receipt_idempotente(receipt, quota)

    if salvo is None:
        # Ou a cota acabou, ou o rid pertence a outro motorista.
        existente = get_store().get_receipt(rid)
        if existente:
            return jsonify({"erro": "rid_de_outro_motorista"}), 409
        return jsonify({"erro": "limite_mensal", "limite": FREE_MONTHLY_LIMIT}), 402

    # O e-mail NAO sai daqui. Quem decide e o motorista, tocando em "Enviar por
    # e-mail" na tela do recibo (POST /api/recibos/<rid>/email). Preencher o
    # campo e uma coisa; mandar e outra — o passageiro pode ter dito o endereco
    # so para o motorista guardar.
    return (
        jsonify(
            {
                "rid": salvo["rid"],
                "url": public_receipt_url(salvo["rid"]),
                "criado_agora": criado_agora,
            }
        ),
        201 if criado_agora else 200,
    )


# ── Webhook do RevenueCat (assinatura pelo app) ───────────────────────────────
#
# Três fontes podem conceder o Pro: Stripe (site), App Store e Google Play
# (app). Todas convergem para a mesma coluna `plan` — o app e o site só olham
# para ela, e nunca precisam saber de onde a assinatura veio.

# Eventos que concedem acesso, e os que tiram.
RC_EVENTOS_ATIVA = {
    "INITIAL_PURCHASE", "RENEWAL", "UNCANCELLATION",
    "PRODUCT_CHANGE", "SUBSCRIPTION_EXTENDED", "TRANSFER",
}
RC_EVENTOS_ENCERRA = {"EXPIRATION", "SUBSCRIPTION_PAUSED"}


@app.post("/webhook/revenuecat")
def webhook_revenuecat():
    """Recebe mudanças de assinatura feitas dentro do app."""
    segredo = os.environ.get("REVENUECAT_WEBHOOK_SECRET", "").strip()
    if not segredo:
        app.logger.warning("REVENUECAT_WEBHOOK_SECRET ausente — webhook desativado.")
        abort(503)

    # O painel do RevenueCat manda o campo "Authorization header value" como
    # veio: com "Bearer " na frente ou sem, com espaco sobrando ou nao. Exigir
    # a forma exata custou um 401 em producao no primeiro evento de verdade.
    # Aceitar as duas formas nao afrouxa nada: o segredo continua sendo
    # comparado inteiro, em tempo constante.
    enviado = request.headers.get("Authorization", "").strip()
    if enviado[:7].lower() == "bearer ":
        enviado = enviado[7:].strip()
    if not hmac.compare_digest(enviado.encode("utf-8", "replace"), segredo.encode("utf-8")):
        abort(401)

    evento = (request.get_json(silent=True) or {}).get("event") or {}
    tipo = str(evento.get("type", ""))

    # app_user_id é o id do motorista: o app faz logIn no RevenueCat com ele.
    user_id = str(evento.get("app_user_id") or "").strip()
    if not user_id:
        return jsonify({"ok": True, "ignorado": "sem_app_user_id"})

    store = get_store()
    usuario = store.get_user_by_id(user_id)
    if not usuario:
        app.logger.warning("RevenueCat: motorista %s não encontrado.", user_id)
        return jsonify({"ok": True, "ignorado": "motorista_desconhecido"})

    if tipo == "CANCELLATION":
        # Cancelou, mas segue com acesso até o fim do período pago. Quem tira o
        # acesso é o EXPIRATION, depois.
        store.update_user(user_id, {"subscription_status": "canceled_pendente"})

    elif tipo in RC_EVENTOS_ATIVA:
        store.update_user(user_id, {
            "plan": "pro",
            "subscription_status": "active",
            "stripe_subscription_id": None,
        })

    elif tipo in RC_EVENTOS_ENCERRA:
        # Só rebaixa se o Pro veio do app. Quem assinou pela Stripe no site
        # continua — são assinaturas independentes (Guideline 3.1.3b).
        if not usuario.get("stripe_customer_id"):
            store.update_user(user_id, {"plan": "free", "subscription_status": tipo.lower()})

    return jsonify({"ok": True, "evento": tipo})


# ── Manutenção ────────────────────────────────────────────────────────────────

@app.get("/tarefas/limpeza")
def tarefa_limpeza():
    """Apaga recibos sem conta (do antigo gerador público) vencidos e contadores antigos.

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
    return render_template("privacidade.html", updated_at="22 de setembro de 2026")


@app.get("/termos")
def termos():
    return render_template("termos.html", updated_at="12 de setembro de 2026")


@app.get("/excluir-conta")
def pagina_excluir_conta():
    """Pagina publica de exclusao de conta.

    O Google Play exige as DUAS coisas de quem deixa criar conta no app: um
    caminho dentro do app e um link na web onde qualquer um possa pedir a
    exclusao — inclusive quem ja desinstalou e nao consegue mais entrar. O
    formulario do painel nao serve sozinho: ele fica atras do login. Esta
    pagina abre sem sessao e a URL vai declarada no formulario de Seguranca
    dos Dados do Play Console.

    Mesma URL do POST logo abaixo, que e quem realmente apaga: aqui e GET.
    """
    return render_template("excluir_conta.html", updated_at="14 de setembro de 2026")


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


# ── Painel administrativo (/admin) ───────────────────────────────────────────
#
# Conta separada da de motorista, com sessao propria (`admin_id`). O painel
# mostra CPF, telefone e placa de todo mundo, entao ele e mais fechado que o
# resto do site: tentativas de login com teto diario, sessao que morre em 12 h,
# CSRF em todo POST e noindex em toda resposta.

# Hash de mentira para e-mail inexistente: check_password_hash roda do mesmo
# jeito, entao o tempo de resposta nao conta quem e admin. Calculado uma vez —
# scrypt a cada tentativa errada seria custo sem motivo.
_ADMIN_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


def _admin_pw_stamp(admin: dict) -> str:
    return to_utc_iso(admin["password_changed_at"]) if admin.get("password_changed_at") else ""


def admin_atual() -> dict | None:
    """Administrador da sessao, ou None. Derruba sessao velha ou de senha trocada."""
    admin_id = session.get("admin_id")
    if not admin_id:
        return None
    agora = datetime.now(timezone.utc)
    entrou_em = session.get("admin_login_at")
    visto_em = session.get("admin_seen_at") or entrou_em
    if (not entrou_em
            or datetime.fromisoformat(entrou_em) + timedelta(hours=ADMIN_SESSION_HOURS) < agora
            or datetime.fromisoformat(visto_em) + timedelta(minutes=ADMIN_IDLE_MINUTES) < agora):
        encerrar_sessao_admin()
        return None
    # Renova a marca de atividade no maximo a cada 5 min, senao o cookie e
    # reescrito em toda resposta.
    if datetime.fromisoformat(visto_em) + timedelta(minutes=5) < agora:
        session["admin_seen_at"] = to_utc_iso(agora)
    admin = get_store().get_admin_by_id(admin_id)
    if not admin or _admin_pw_stamp(admin) != session.get("admin_pw_stamp", ""):
        encerrar_sessao_admin()
        return None
    return admin


def iniciar_sessao_admin(admin: dict, senha_digitada: str) -> None:
    # Sessao nova do zero: nao herda nada de um login de motorista no mesmo
    # navegador, e ganha um token CSRF proprio.
    session.clear()
    session["admin_id"] = admin["_id"]
    session["admin_login_at"] = to_utc_iso(datetime.now(timezone.utc))
    session["admin_seen_at"] = session["admin_login_at"]
    session["admin_pw_stamp"] = _admin_pw_stamp(admin)
    session["admin_csrf"] = secrets.token_urlsafe(32)
    # A senha inicial e "1234" por decisao do dono. O painel avisa em toda tela
    # ate ela ser trocada — o aviso vive na sessao para nao rodar scrypt a cada
    # pagina.
    session["admin_senha_fraca"] = len(senha_digitada) < ADMIN_PASSWORD_MIN
    get_store().touch_admin_login(admin["_id"])


def encerrar_sessao_admin() -> None:
    for chave in ("admin_id", "admin_login_at", "admin_seen_at", "admin_pw_stamp", "admin_csrf", "admin_senha_fraca"):
        session.pop(chave, None)


def admin_csrf_ok() -> bool:
    esperado = session.get("admin_csrf", "")
    enviado = request.form.get("csrf", "")
    return bool(esperado) and hmac.compare_digest(esperado, enviado)


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        admin = admin_atual()
        if not admin:
            return redirect(url_for("admin_login", proximo=request.path))
        if request.method == "POST" and not admin_csrf_ok():
            abort(400)
        g.admin = admin
        return view(*args, **kwargs)

    return wrapped_view


@app.after_request
def admin_noindex(response):
    # O painel nao existe para o Google, e a URL nao deve nem aparecer numa
    # busca. Vale para login, erro e tudo mais debaixo de /admin.
    if request.path == "/admin" or request.path.startswith("/admin/"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Cache-Control"] = "no-store"
        # As URLs do painel carregam id de motorista e ha links para o painel
        # da Stripe: nada disso deve viajar no Referer.
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.context_processor
def inject_admin() -> dict:
    return {
        "admin_user": g.get("admin"),
        "admin_csrf": session.get("admin_csrf", ""),
        "admin_senha_fraca": bool(session.get("admin_senha_fraca")),
    }


def _chave_login_admin(email: str) -> str:
    # O e-mail tentado nao vai em claro para a tabela de contadores.
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]
    return f"admin_login:email:{digest}:{today_br()}"


def pode_tentar_login_admin(email: str) -> bool:
    store = get_store()
    por_ip = store.bump_counter(f"admin_login:ip:{client_ip()}:{today_br()}")
    por_email = store.bump_counter(_chave_login_admin(email))
    return por_ip <= ADMIN_LOGIN_DAILY_LIMIT_IP and por_email <= ADMIN_LOGIN_DAILY_LIMIT_EMAIL


def _proximo_seguro(valor: str) -> str:
    """So aceita caminho interno debaixo de /admin: nada de mandar para fora."""
    return valor if valor.startswith("/admin") and "//" not in valor else url_for("admin_dashboard")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if admin_atual():
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))
        senha = request.form.get("senha", "")
        if not email or not senha:
            flash("Informe e-mail e senha.", "danger")
            return render_template("admin/login.html", form={"email": email}), 400

        if not pode_tentar_login_admin(email):
            flash("Muitas tentativas hoje. Tente novamente mais tarde.", "danger")
            return render_template("admin/login.html", form={"email": email}), 429

        admin = get_store().get_admin_by_email(email)
        # check_password_hash roda mesmo sem conta, com um hash de mentira:
        # responder mais rapido para e-mail inexistente contaria quem e admin.
        hash_ = admin["password_hash"] if admin else _ADMIN_DUMMY_HASH
        if not admin or not check_password_hash(hash_, senha):
            app.logger.warning("admin: login recusado para %s de %s", _chave_login_admin(email), client_ip())
            flash("E-mail ou senha inválidos.", "danger")
            return render_template("admin/login.html", form={"email": email}), 401

        # Login certo zera as tentativas do e-mail: o dono errar a senha nove
        # vezes num dia nao pode trancar a conta dele.
        get_store().reset_counter(_chave_login_admin(email))
        iniciar_sessao_admin(admin, senha)
        app.logger.info("admin %s entrou de %s", admin["email"], client_ip())
        return redirect(_proximo_seguro(request.form.get("proximo", "")))

    return render_template("admin/login.html", form={}, proximo=request.args.get("proximo", ""))


@app.post("/admin/sair")
@admin_required
def admin_sair():
    encerrar_sessao_admin()
    flash("Você saiu do painel.", "info")
    return redirect(url_for("admin_login"))


@app.route("/admin/senha", methods=["GET", "POST"])
@admin_required
def admin_senha():
    if request.method == "POST":
        atual = request.form.get("senha_atual", "")
        nova = request.form.get("senha", "")
        confirmacao = request.form.get("confirmar_senha", "")

        if not check_password_hash(g.admin["password_hash"], atual):
            flash("A senha atual não confere.", "danger")
            return render_template("admin/senha.html"), 403
        if len(nova) < ADMIN_PASSWORD_MIN:
            flash(f"A nova senha precisa ter pelo menos {ADMIN_PASSWORD_MIN} caracteres.", "danger")
            return render_template("admin/senha.html"), 400
        if nova != confirmacao:
            flash("As senhas não conferem.", "danger")
            return render_template("admin/senha.html"), 400
        if nova == atual:
            flash("A nova senha é igual à atual.", "danger")
            return render_template("admin/senha.html"), 400

        get_store().set_admin_password(g.admin["_id"], generate_password_hash(nova))
        # Trocar a senha derruba as outras sessoes (o carimbo muda); esta
        # continua, com o carimbo novo.
        admin = get_store().get_admin_by_id(g.admin["_id"])
        iniciar_sessao_admin(admin, nova)
        flash("Senha alterada.", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("admin/senha.html")


# Cache em processo: na Vercel a instancia sobrevive entre invocacoes, e o
# painel sao ~35 idas ao banco. Um minuto de atraso nao muda decisao nenhuma.
_ADMIN_CACHE: dict = {}
ADMIN_CACHE_SECONDS = 60


@app.get("/admin")
@admin_required
def admin_dashboard():
    import time as _time

    agora = _time.monotonic()
    guardado = _ADMIN_CACHE.get("dashboard")
    if guardado and guardado[0] > agora and request.args.get("atualizar") != "1":
        dados = dict(guardado[1], cache=True)
    else:
        dados = montar_dashboard_admin()
        _ADMIN_CACHE["dashboard"] = (agora + ADMIN_CACHE_SECONDS, dados)
    return render_template("admin/dashboard.html", d=dados)


# ── Dashboard: consultas ─────────────────────────────────────────────────────
#
# Tudo numa conexao so, com um savepoint por secao: se uma consulta falhar (por
# exemplo, permissao em auth.*), a secao sai como None e a pagina continua de pe.
# Janelas sao calculadas em Python e passadas como constante, para o planner
# usar os indices em created_at. Dia e mes "de negocio" sao no fuso de Brasilia.

def _inicio_dia_br(dias_atras: int = 0) -> str:
    d = datetime.now(BR_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return to_utc_iso(d - timedelta(days=dias_atras))


def _inicio_mes_br(meses_atras: int = 0) -> str:
    hoje = datetime.now(BR_TZ)
    ano, mes = hoje.year, hoje.month - meses_atras
    while mes <= 0:
        mes += 12
        ano -= 1
    return to_utc_iso(datetime(ano, mes, 1, tzinfo=BR_TZ))


def _serie_dias(linhas: list[dict], dias: int, campos: tuple[str, ...]) -> list[dict]:
    """Preenche os dias sem linha com zero, do mais antigo para hoje."""
    por_dia = {str(l["dia"]): l for l in linhas}
    hoje = datetime.now(BR_TZ).date()
    saida = []
    for i in range(dias - 1, -1, -1):
        dia = hoje - timedelta(days=i)
        l = por_dia.get(dia.isoformat(), {})
        saida.append({"dia": dia, **{c: l.get(c) or 0 for c in campos}})
    return saida


def _serie_meses(linhas: list[dict], meses: int, campos: tuple[str, ...]) -> list[dict]:
    por_mes = {str(l["mes"]): l for l in linhas}
    hoje = datetime.now(BR_TZ)
    saida = []
    for i in range(meses - 1, -1, -1):
        ano, mes = hoje.year, hoje.month - i
        while mes <= 0:
            mes += 12
            ano -= 1
        chave = f"{ano:04d}-{mes:02d}-01"
        l = por_mes.get(chave, {})
        saida.append({"mes": f"{mes:02d}/{ano % 100:02d}", **{c: l.get(c) or 0 for c in campos}})
    return saida


def _com_maximo(serie: list[dict], campo: str) -> list[dict]:
    """Acrescenta pct (0-100) para desenhar barra em CSS sem JavaScript."""
    maior = max((float(l[campo] or 0) for l in serie), default=0) or 1
    for l in serie:
        l["pct"] = round(float(l[campo] or 0) / maior * 100)
    return serie


_SQL_ADMIN = {
    "geral": """
        select count(*) as total,
               count(*) filter (where created_at >= %(ini_hoje)s) as novos_hoje,
               count(*) filter (where created_at >= %(ini_7d)s) as novos_7d,
               count(*) filter (where created_at >= %(ini_mes)s) as novos_mes,
               count(*) filter (where plan in ('pro','business')) as pagantes,
               count(*) filter (where plan='pro' and stripe_subscription_id is not null) as pro_stripe,
               count(*) filter (where plan='pro' and stripe_subscription_id is null) as pro_loja,
               count(*) filter (where plan='business') as business,
               count(*) filter (where subscription_status='past_due') as past_due,
               count(*) filter (where subscription_status='canceled_pendente') as cancel_pendente,
               count(*) filter (where plan='free' and subscription_status is not null) as ex_assinantes,
               count(*) filter (where plan='free') as gratis
          from public.drivers""",
    "recibos_mes": """
        select count(*) as mes,
               count(*) filter (where created_at >= %(ini_hoje)s) as hoje,
               count(*) filter (where is_guest) as mes_guest,
               coalesce(sum(amount), 0) as mes_valor
          from public.receipts where created_at >= %(ini_mes)s""",
    "recibos_estim": """
        select greatest(reltuples, 0)::bigint as n from pg_class where oid = 'public.receipts'::regclass""",
    "pagantes": """
        select id, full_name, email, city, plan, subscription_status,
               case when stripe_subscription_id is not null then 'Stripe'
                    when stripe_customer_id is not null then 'Loja (ex-Stripe)'
                    else 'Loja' end as origem,
               created_at, updated_at
          from public.drivers where plan in ('pro','business')
         order by updated_at desc limit 50""",
    "ex_assinantes": """
        select subscription_status as status, count(*) as n
          from public.drivers where plan='free' and subscription_status is not null
         group by 1 order by n desc""",
    "novos_por_dia": """
        select (created_at at time zone 'America/Sao_Paulo')::date as dia, count(*) as n
          from public.drivers where created_at >= %(ini_30d)s group by 1 order by 1""",
    "novos_por_mes": """
        select public.br_period(created_at) as mes, count(*) as n
          from public.drivers where created_at >= %(ini_12m)s group by 1 order by 1""",
    "ativacao": """
        select count(*) as total,
               count(*) filter (where ativo) as ativados,
               count(*) filter (where created_at >= %(ini_30d)s) as coorte_30d,
               count(*) filter (where created_at >= %(ini_30d)s and ativo) as coorte_30d_ativados,
               count(*) filter (where created_at >= %(ini_30d)s and ativo_24h) as coorte_30d_ativados_24h
          from (select d.created_at,
                       exists (select 1 from public.receipts r where r.driver_id = d.id) as ativo,
                       exists (select 1 from public.receipts r where r.driver_id = d.id
                                  and r.created_at < d.created_at + interval '24 hours') as ativo_24h
                  from public.drivers d) s""",
    "funil": """
        select count(*) as free_ativos_mes,
               count(*) filter (where q.used >= %(limite)s and d.plan='free') as no_teto,
               count(*) filter (where q.used >= %(limite)s and d.plan<>'free') as no_teto_converteram,
               count(*) filter (where q.used between %(limite)s-2 and %(limite)s-1 and d.plan='free') as quase_teto,
               count(*) filter (where q.used between 1 and %(limite)s-3 and d.plan='free') as uso_baixo
          from public.receipt_quotas q join public.drivers d on d.id = q.driver_id
         where q.period = public.br_period()""",
    "candidatos": """
        select d.id, d.full_name, d.email, d.whatsapp, d.city, q.used, d.created_at,
               (select count(*) from public.receipts r where r.driver_id = d.id) as recibos_total
          from public.receipt_quotas q join public.drivers d on d.id = q.driver_id
         where q.period = public.br_period() and q.used >= %(limite)s and d.plan = 'free'
         order by recibos_total desc limit 50""",
    "uso_por_dia": """
        select (created_at at time zone 'America/Sao_Paulo')::date as dia,
               count(*) filter (where not is_guest) as logado,
               count(*) filter (where is_guest) as guest,
               count(*) as n,
               coalesce(sum(amount), 0) as valor
          from public.receipts where created_at >= %(ini_30d)s group by 1 order by 1""",
    "uso_por_mes": """
        select public.br_period(created_at) as mes,
               count(*) filter (where not is_guest) as logado,
               count(*) filter (where is_guest) as guest,
               count(*) as n,
               coalesce(sum(amount), 0) as valor,
               count(distinct driver_id) as motoristas_ativos
          from public.receipts where created_at >= %(ini_12m)s group by 1 order by 1""",
    "pagamentos": """
        select payment_method as forma, count(*) as n, coalesce(sum(amount), 0) as valor
          from public.receipts where created_at >= %(ini_30d)s group by 1 order by n desc limit 6""",
    "cidades_recibos": """
        select coalesce(nullif(initcap(trim(driver_snapshot->>'city')), ''), '(sem cidade)') as cidade,
               count(*) as n, count(distinct driver_id) as motoristas
          from public.receipts where created_at >= %(ini_30d)s group by 1 order by n desc limit 15""",
    "cidades_cadastro": """
        select initcap(trim(city)) as cidade, count(*) as n,
               count(*) filter (where plan <> 'free') as pagantes
          from public.drivers group by 1 order by n desc limit 15""",
    "ticket": """
        select round(avg(amount), 2) as media,
               percentile_cont(0.5) within group (order by amount) as mediana,
               min(amount) as minimo, max(amount) as maximo
          from public.receipts where created_at >= %(ini_30d)s""",
    "top_emissores": """
        select d.id, d.full_name, d.plan, count(*) as n, sum(r.amount) as valor
          from public.receipts r join public.drivers d on d.id = r.driver_id
         where r.created_at >= %(ini_30d)s
         group by d.id, d.full_name, d.plan order by n desc limit 10""",
    "por_hora": """
        select extract(hour from created_at at time zone 'America/Sao_Paulo')::int as hora, count(*) as n
          from public.receipts where created_at >= %(ini_30d)s group by 1 order by 1""",
    "ultimos_recibos": """
        select rid, is_guest, driver_id, driver_snapshot->>'full_name' as motorista,
               driver_snapshot->>'city' as cidade, passenger, amount, payment_method, created_at
          from public.receipts order by created_at desc limit 20""",
    "logins": """
        select count(*) filter (where u.last_sign_in_at >= now() - interval '1 day') as login_24h,
               count(*) filter (where u.last_sign_in_at >= now() - interval '7 days') as login_7d,
               count(*) filter (where u.last_sign_in_at >= now() - interval '30 days') as login_30d,
               count(*) filter (where u.last_sign_in_at is null) as nunca_logou
          from auth.users u join public.drivers d on d.id = u.id""",
    "ultimos_logins": """
        select d.id, d.full_name, d.email, d.plan, u.last_sign_in_at
          from auth.users u join public.drivers d on d.id = u.id
         order by u.last_sign_in_at desc nulls last limit 20""",
    "sessoes": """
        select count(*) as sessoes, count(distinct user_id) as usuarios
          from auth.sessions
         where coalesce(refreshed_at, updated_at, created_at) >= now() - interval '7 days'
           and (not_after is null or not_after > now())""",
    "inativos_30d": """
        select count(*) as n from public.drivers d join auth.users u on u.id = d.id
         where d.created_at < now() - interval '30 days'
           and coalesce(u.last_sign_in_at, 'epoch') < now() - interval '30 days'
           and not exists (select 1 from public.receipts r
                            where r.driver_id = d.id and r.created_at >= now() - interval '30 days')""",
    "pagantes_parados": """
        select d.id, d.full_name, d.email, d.whatsapp, d.plan,
               (select max(created_at) from public.receipts r where r.driver_id = d.id) as ultimo_recibo
          from public.drivers d
         where d.plan in ('pro','business')
           and not exists (select 1 from public.receipts r
                            where r.driver_id = d.id and r.created_at >= now() - interval '30 days')
         order by ultimo_recibo nulls first limit 30""",
    "emails_por_dia": r"""
        select substring(key from '(\d{4}-\d{2}-\d{2})$') as dia,
               sum(count) as tentativas, sum(least(count, %(teto_email)s)) as enviados,
               count(*) as motoristas, count(*) filter (where count > %(teto_email)s) as no_teto
          from public.rate_limits where key like 'email:%%' group by 1 order by 1 desc""",
    "resets_por_dia": r"""
        select substring(key from '(\d{4}-\d{2}-\d{2})$') as dia,
               coalesce(sum(count) filter (where key like 'reset:email:%%'), 0) as pedidos,
               count(*) filter (where key like 'reset:email:%%') as emails_distintos,
               count(*) filter (where key like 'reset:ip:%%' and count >= %(teto_reset_ip)s) as ips_no_teto
          from public.rate_limits where key like 'reset:%%' group by 1 order by 1 desc""",
    "tentativas_admin": """
        select key, count, created_at from public.rate_limits
         where key like 'admin_login:%%' order by count desc limit 20""",
    "retencao": """
        select (select min(created_at) from public.receipts where is_guest) as guest_mais_antigo,
               (select count(*) from public.receipts where is_guest and created_at < %(corte_guest)s) as guest_vencidos,
               (select min(created_at) from public.rate_limits) as contador_mais_antigo,
               (select count(*) from public.rate_limits where created_at < %(corte_contadores)s) as contadores_vencidos""",
    "tabelas": """
        select c.relname as tabela, greatest(c.reltuples, 0)::bigint as linhas,
               pg_total_relation_size(c.oid) as bytes
          from pg_class c join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relkind = 'r' order by bytes desc""",
    "indices": """
        select indexname from pg_indexes where schemaname = 'public'
           and indexname in ('receipts_created_idx', 'drivers_created_idx')""",
    "ultimo_webhook": """
        select max(updated_at) as em from public.drivers where plan <> 'free'""",
    "admins": """
        select email, last_login_at, password_changed_at from public.admin_users
         order by last_login_at desc nulls last""",
}

# Uma linha (fetchone) ou varias (fetchall)?
_ADMIN_UMA_LINHA = {
    "geral", "recibos_mes", "recibos_estim", "ativacao", "funil", "ticket", "logins",
    "sessoes", "inativos_30d", "retencao", "ultimo_webhook",
}


def _executar_secoes(conn, nomes: list[str], params: dict, dados: dict) -> None:
    import time as _time

    for nome in nomes:
        inicio = _time.perf_counter()
        try:
            with conn.transaction():  # savepoint: a falha de uma nao derruba as outras
                cur = conn.execute(_SQL_ADMIN[nome], params)
                dados[nome] = dict(cur.fetchone() or {}) if nome in _ADMIN_UMA_LINHA \
                    else [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            app.logger.warning("admin: secao %s indisponivel: %s", nome, exc)
            dados[nome] = None
        dados["_tempos"][nome] = round((_time.perf_counter() - inicio) * 1000)


def _saude_do_sistema() -> dict:
    """Configuracao presente ou ausente. Nunca o valor — so booleano e prefixo."""
    def tem(nome: str) -> bool:
        return bool(os.environ.get(nome, "").strip())

    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    return {
        "integracoes": [
            ("Stripe (chave secreta)", bool(stripe_key),
             "modo live" if stripe_key.startswith("sk_live_") else "modo teste" if stripe_key else ""),
            ("Stripe (webhook)", tem("STRIPE_WEBHOOK_SECRET"), ""),
            ("Stripe (preço Pro mensal)", tem("STRIPE_PRO_PRICE_ID"), ""),
            ("Stripe (preço Pro anual)", tem("STRIPE_PRO_ANUAL_PRICE_ID"), ""),
            ("Resend (e-mail)", tem("RESEND_API_KEY"), os.environ.get("EMAIL_FROM", "").strip()),
            ("RevenueCat (webhook)", tem("REVENUECAT_WEBHOOK_SECRET"), ""),
            ("Cron de limpeza", tem("CRON_SECRET"), "04:00 UTC, diário"),
            ("APP_BASE_URL", tem("APP_BASE_URL"), os.environ.get("APP_BASE_URL", "").strip()),
            ("Supabase (URL)", tem("SUPABASE_URL"), ""),
            ("Supabase (chave de serviço)", tem("SUPABASE_SECRET_KEY") or tem("SUPABASE_SERVICE_ROLE_KEY"), ""),
            ("SECRET_KEY com 32+ caracteres", len(os.environ.get("SECRET_KEY", "")) >= 32, ""),
        ],
        "deploy": {
            "ambiente": os.environ.get("VERCEL_ENV", "local"),
            "regiao": os.environ.get("VERCEL_REGION", ""),
            "commit": os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:7],
            "mensagem": os.environ.get("VERCEL_GIT_COMMIT_MESSAGE", "")[:80],
        },
        "constantes": [
            ("Recibos/mês no Grátis", FREE_MONTHLY_LIMIT),
            ("E-mails/dia por motorista", EMAIL_DAILY_LIMIT),
            ("Retenção de recibos sem conta (dias)", GUEST_RETENTION_DAYS),
            ("Retenção de contadores (dias)", COUNTER_RETENTION_DAYS),
            ("Pedidos de nova senha/dia por e-mail", RESET_DAILY_LIMIT_EMAIL),
            ("Tentativas de login no painel/dia por IP", ADMIN_LOGIN_DAILY_LIMIT_IP),
        ],
    }


def montar_dashboard_admin() -> dict:
    """Numeros do painel, por secao. Ver templates/admin/dashboard.html."""
    import time as _time

    store = get_store()
    agora = datetime.now(timezone.utc)
    params = {
        "ini_hoje": _inicio_dia_br(0),
        "ini_7d": _inicio_dia_br(6),
        "ini_30d": _inicio_dia_br(29),
        "ini_mes": _inicio_mes_br(0),
        "ini_12m": _inicio_mes_br(11),
        "limite": FREE_MONTHLY_LIMIT,
        "teto_email": EMAIL_DAILY_LIMIT,
        "teto_reset_ip": RESET_DAILY_LIMIT_IP,
        "corte_guest": to_utc_iso(agora - timedelta(days=GUEST_RETENTION_DAYS + 1)),
        "corte_contadores": to_utc_iso(agora - timedelta(days=COUNTER_RETENTION_DAYS + 1)),
    }
    d: dict = {"_tempos": {}, "gerado_em": datetime.now(BR_TZ)}

    inicio = _time.perf_counter()
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute("set local statement_timeout = 4000")
            d["latencia_ms"] = None
            t0 = _time.perf_counter()
            conn.execute("select 1").fetchone()
            d["latencia_ms"] = round((_time.perf_counter() - t0) * 1000, 1)
            _executar_secoes(conn, list(_SQL_ADMIN), params, d)
    d["_tempos"]["total"] = round((_time.perf_counter() - inicio) * 1000)

    # Series com os dias/meses vazios preenchidos e a barra ja calculada.
    d["novos_por_dia"] = _com_maximo(_serie_dias(d.get("novos_por_dia") or [], 30, ("n",)), "n")
    d["novos_por_mes"] = _com_maximo(_serie_meses(d.get("novos_por_mes") or [], 12, ("n",)), "n")
    d["uso_por_dia"] = _com_maximo(_serie_dias(d.get("uso_por_dia") or [], 30, ("logado", "guest", "n", "valor")), "n")
    d["uso_por_mes"] = _com_maximo(
        _serie_meses(d.get("uso_por_mes") or [], 12, ("logado", "guest", "n", "valor", "motoristas_ativos")), "n")
    por_hora = {int(l["hora"]): int(l["n"]) for l in (d.get("por_hora") or [])}
    d["por_hora"] = _com_maximo([{"hora": h, "n": por_hora.get(h, 0)} for h in range(24)], "n")
    for nome in ("pagamentos", "cidades_recibos", "cidades_cadastro"):
        d[nome] = _com_maximo(d.get(nome) or [], "n")

    # MRR como faixa: o banco nao diz quem e mensal e quem e anual, nem o preco
    # do Business legado. Piso = todo Pro da Stripe anual (119,40/12);
    # teto = todo Pro mensal. Loja: bruto; a Apple fica com 15%.
    g = d.get("geral") or {}
    pro_stripe = int(g.get("pro_stripe") or 0)
    pro_loja = int(g.get("pro_loja") or 0)
    d["mrr"] = {
        "piso": round(pro_stripe * 119.40 / 12 + pro_loja * 19.90 * 0.85, 2),
        "teto": round((pro_stripe + pro_loja) * 19.90, 2),
        "business_sem_preco": int(g.get("business") or 0),
    }
    a = d.get("ativacao") or {}
    d["ativacao_pct"] = round(100 * int(a.get("ativados") or 0) / max(int(a.get("total") or 0), 1))
    d["ativacao_30d_pct"] = round(100 * int(a.get("coorte_30d_ativados") or 0) / max(int(a.get("coorte_30d") or 0), 1))
    d["saude"] = _saude_do_sistema()
    d["indices_ok"] = {r["indexname"] for r in (d.get("indices") or [])} >= {"receipts_created_idx", "drivers_created_idx"}
    return d


# ── Lista e ficha de motoristas ──────────────────────────────────────────────

_ADMIN_CURSOR_SALT = "admin-cursor"


def _cursor_dumps(created_at, driver_id: str) -> str:
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=_ADMIN_CURSOR_SALT).dumps(
        [to_utc_iso(created_at), str(driver_id)])


def _cursor_loads(valor: str) -> tuple[str | None, str | None]:
    """Cursor invalido ou adulterado vira primeira pagina, nao 500."""
    if not valor:
        return None, None
    try:
        ts, did = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=_ADMIN_CURSOR_SALT).loads(valor, max_age=86400)
        return str(ts), str(did)
    except Exception:
        return None, None


def _escapar_like(valor: str) -> str:
    return valor.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@app.get("/admin/motoristas")
@admin_required
def admin_motoristas():
    q = request.args.get("q", "").strip()[:80]
    plano = request.args.get("plano", "") if request.args.get("plano") in ("free", "pro", "business") else ""
    status = request.args.get("status", "").strip()[:40]
    origem = request.args.get("origem", "") if request.args.get("origem") in ("stripe", "loja") else ""
    so_teto = request.args.get("so_teto") == "1"
    sem_recibo = request.args.get("sem_recibo") == "1"
    cur_ts, cur_id = _cursor_loads(request.args.get("cursor", ""))
    tamanho = max(1, min(int(ADMIN_PAGE_SIZE), 100))

    digitos = re.sub(r"\D", "", q)
    params = {
        "cur_ts": cur_ts, "cur_id": cur_id,
        "plan": plano or None, "status": status or None, "origem": origem or None,
        "q": f"%{_escapar_like(q)}%" if q else None,
        "qdig": f"{digitos}%" if len(digitos) >= 4 else None,
        "so_teto": so_teto, "sem_recibo": sem_recibo,
        "limite": FREE_MONTHLY_LIMIT, "n": tamanho + 1,
    }
    sql = """
        select d.id, d.full_name, d.email, d.city, d.plate, d.plan, d.subscription_status,
               case when d.plan = 'free' then '-'
                    when d.stripe_subscription_id is not null then 'Stripe' else 'Loja' end as origem,
               d.created_at, u.last_sign_in_at, s.n as recibos, s.ultimo as ultimo_recibo, q.used as uso_mes
          from public.drivers d
          left join auth.users u on u.id = d.id
          left join lateral (select count(*) as n, max(created_at) as ultimo
                               from public.receipts r where r.driver_id = d.id) s on true
          left join public.receipt_quotas q on q.driver_id = d.id and q.period = public.br_period()
         where (%(cur_ts)s::timestamptz is null
                or (d.created_at, d.id) < (%(cur_ts)s::timestamptz, %(cur_id)s::uuid))
           and (%(plan)s::text is null or d.plan = %(plan)s)
           and (%(status)s::text is null or d.subscription_status = %(status)s)
           and (%(origem)s::text is null
                or (%(origem)s = 'stripe' and d.stripe_subscription_id is not null)
                or (%(origem)s = 'loja' and d.plan <> 'free' and d.stripe_subscription_id is null))
           and (%(q)s::text is null or d.email ilike %(q)s or d.full_name ilike %(q)s
                or d.plate ilike %(q)s
                or (%(qdig)s::text is not null and (d.whatsapp like %(qdig)s or d.cpf like %(qdig)s)))
           and (not %(so_teto)s or coalesce(q.used, 0) >= %(limite)s)
           and (not %(sem_recibo)s or s.n = 0)
         order by d.created_at desc, d.id desc
         limit %(n)s"""
    with get_store().pool.connection() as conn:
        linhas = [dict(r) for r in conn.execute(sql, params).fetchall()]

    proximo = ""
    if len(linhas) > tamanho:
        linhas = linhas[:tamanho]
        proximo = _cursor_dumps(linhas[-1]["created_at"], linhas[-1]["id"])
    app.logger.info("admin %s listou motoristas q=%r", g.admin["email"], q)
    return render_template(
        "admin/motoristas.html", motoristas=linhas, proximo=proximo,
        filtros={"q": q, "plano": plano, "status": status, "origem": origem,
                 "so_teto": so_teto, "sem_recibo": sem_recibo},
    )


@app.get("/admin/motoristas/<uuid:driver_id>")
@admin_required
def admin_motorista(driver_id):
    store = get_store()
    did = str(driver_id)
    with store.pool.connection() as conn:
        cols = ", ".join("d." + c.strip() for c in PostgresStore._DRIVER_COLS.split(",") if c.strip())
        perfil = conn.execute(
            f"""select {cols}, u.last_sign_in_at, u.email_confirmed_at,
                       u.banned_until, u.created_at as auth_created_at
                  from public.drivers d left join auth.users u on u.id = d.id
                 where d.id = %(id)s""", {"id": did}).fetchone()
        if not perfil:
            abort(404)
        totais = conn.execute(
            """select count(*) as n, coalesce(sum(amount), 0) as valor,
                      min(created_at) as primeiro, max(created_at) as ultimo,
                      count(*) filter (where created_at >= %(ini_30d)s) as n_30d
                 from public.receipts where driver_id = %(id)s""",
            {"id": did, "ini_30d": _inicio_dia_br(29)}).fetchone()
        cotas = conn.execute(
            "select period, used from public.receipt_quotas where driver_id = %s order by period desc limit 6",
            (did,)).fetchall()
        try:
            with conn.transaction():
                sessoes = conn.execute(
                    "select count(*) as n from auth.sessions where user_id = %s and (not_after is null or not_after > now())",
                    (did,)).fetchone()["n"]
        except Exception:
            sessoes = None
        emails_hoje = conn.execute(
            "select count from public.rate_limits where key = %s", (f"email:{did}:{today_br()}",)).fetchone()
    recibos = store.list_receipts_by_driver(did, limit=20)
    app.logger.info("admin %s abriu a ficha de %s", g.admin["email"], did)
    return render_template(
        "admin/motorista.html", m=dict(perfil), totais=dict(totais), cotas=[dict(c) for c in cotas],
        sessoes=sessoes, emails_hoje=(emails_hoje or {}).get("count", 0), recibos=recibos,
        limite=FREE_MONTHLY_LIMIT,
    )


# ── Filtros Jinja do painel ──────────────────────────────────────────────────

@app.template_filter("brl")
def filtro_brl(valor) -> str:
    try:
        n = float(valor or 0)
    except (TypeError, ValueError):
        return "-"
    inteiro, _, dec = f"{n:,.2f}".partition(".")
    return "R$ " + inteiro.replace(",", ".") + "," + dec


@app.template_filter("mascarar_cpf")
def filtro_mascarar_cpf(valor) -> str:
    digitos = re.sub(r"\D", "", str(valor or ""))
    if len(digitos) == 11:
        return f"***.***.{digitos[6:9]}-{digitos[9:]}"
    if len(digitos) == 14:  # CNPJ e dado publico
        return format_document_br(digitos)
    return "***" if digitos else ""


@app.template_filter("mascarar_email")
def filtro_mascarar_email(valor) -> str:
    usuario, _, dominio = str(valor or "").partition("@")
    return f"{usuario[:1]}***@{dominio}" if dominio else ""


@app.template_filter("mascarar_fone")
def filtro_mascarar_fone(valor) -> str:
    digitos = re.sub(r"\D", "", str(valor or ""))
    return f"*****-{digitos[-4:]}" if len(digitos) >= 4 else ""


@app.template_filter("data_br")
def filtro_data_br(valor, com_hora: bool = True) -> str:
    if not valor:
        return "-"
    if isinstance(valor, str):
        try:
            valor = datetime.fromisoformat(valor.replace("Z", "+00:00"))
        except ValueError:
            return valor
    if isinstance(valor, datetime):
        if valor.tzinfo is None:
            valor = valor.replace(tzinfo=timezone.utc)
        valor = valor.astimezone(BR_TZ)
        return valor.strftime("%d/%m/%Y %H:%M") if com_hora else valor.strftime("%d/%m/%Y")
    return valor.strftime("%d/%m/%Y")


@app.cli.command("criar-admin")
def cli_criar_admin():
    """Cria um administrador do painel, ou redefine a senha se o e-mail ja existir.

    Uso: flask --app app criar-admin  (pede e-mail e senha; a senha nao ecoa)
    Nao ha cadastro de admin pelo site de proposito.
    """
    import getpass

    email = normalize_email(input("E-mail do administrador: "))
    if not email_valido(email):
        raise SystemExit("E-mail inválido.")
    senha = getpass.getpass("Senha: ")
    if not senha:
        raise SystemExit("Senha vazia.")
    admin = get_store().upsert_admin(email, generate_password_hash(senha))
    print(f"Administrador pronto: {admin['email']} (id {admin['_id']}).")


if __name__ == "__main__":
    app.run(debug=True)
