import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import quote
from uuid import uuid4

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from astrapy import DataAPIClient
except ImportError:  # pragma: no cover - covered indirectly when dependency is missing
    DataAPIClient = None


APP_NAME = "Recibo Taxi"
DEFAULT_USERS_COLLECTION = "taxistas"
DEFAULT_RECEIPTS_COLLECTION = "recibos"


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


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
    raw = (value or "").strip().replace("R$", "").replace(" ", "")
    if not raw:
        raise ValueError("Informe o valor da corrida.")

    normalized = raw
    if "," in raw and "." in raw:
        normalized = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        normalized = raw.replace(",", ".")

    try:
        amount = Decimal(normalized).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("Informe um valor valido.") from exc

    display = f"{amount:.2f}".replace(".", ",")
    return str(amount), display


def compose_receipt_message(receipt: dict, public_url: str) -> str:
    lines = [
        f"Recibo {receipt['rid']}",
        f"Passageiro: {receipt.get('passenger') or '-'}",
        f"Data: {receipt.get('trip_date_display') or '-'}",
        f"Origem: {receipt.get('origin') or '-'}",
        f"Destino: {receipt.get('destination') or '-'}",
        f"Valor: R$ {receipt.get('amount_display') or '-'}",
        f"Pagamento: {receipt.get('payment_method') or '-'}",
        "",
        f"Acesse o recibo: {public_url}",
    ]
    return "\n".join(lines)


def build_whatsapp_link(receipt: dict, public_url: str) -> str:
    phone = sanitize_phone(receipt.get("passenger_whatsapp", ""))
    message = quote(compose_receipt_message(receipt, public_url))
    if phone:
        return f"https://wa.me/{phone}?text={message}"
    return f"https://wa.me/?text={message}"


def build_email_link(receipt: dict, public_url: str) -> str:
    target = quote((receipt.get("passenger_email") or "").strip())
    subject = quote(f"Recibo {receipt['rid']} - {APP_NAME}")
    body = quote(compose_receipt_message(receipt, public_url))
    return f"mailto:{target}?subject={subject}&body={body}"


def public_receipt_url(rid: str) -> str:
    base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    if base_url:
        return f"{base_url}{url_for('recibo_view', rid=rid)}"
    return url_for("recibo_view", rid=rid, _external=True)


class InMemoryStore:
    kind = "memory"
    label = "Modo local"

    def __init__(self) -> None:
        self.users_by_id: dict[str, dict] = {}
        self.user_ids_by_email: dict[str, str] = {}
        self.receipts_by_id: dict[str, dict] = {}

    def create_user(self, payload: dict) -> dict:
        email = payload["email"]
        if email in self.user_ids_by_email:
            raise ValueError("Ja existe uma conta com este e-mail.")
        self.users_by_id[payload["_id"]] = dict(payload)
        self.user_ids_by_email[email] = payload["_id"]
        return dict(payload)

    def get_user_by_email(self, email: str) -> dict | None:
        user_id = self.user_ids_by_email.get(email)
        if not user_id:
            return None
        return dict(self.users_by_id[user_id])

    def get_user_by_id(self, user_id: str | None) -> dict | None:
        if not user_id:
            return None
        user = self.users_by_id.get(user_id)
        return dict(user) if user else None

    def create_receipt(self, payload: dict) -> dict:
        self.receipts_by_id[payload["_id"]] = dict(payload)
        return dict(payload)

    def get_receipt(self, rid: str) -> dict | None:
        receipt = self.receipts_by_id.get(rid)
        return dict(receipt) if receipt else None

    def list_receipts_by_driver(self, driver_id: str) -> list[dict]:
        receipts = [
            dict(receipt)
            for receipt in self.receipts_by_id.values()
            if receipt.get("driver_id") == driver_id
        ]
        receipts.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return receipts


class AstraStore:
    kind = "astra"
    label = "Astra DB"

    def __init__(
        self,
        api_endpoint: str,
        token: str,
        keyspace: str,
        users_collection: str,
        receipts_collection: str,
    ) -> None:
        if DataAPIClient is None:
            raise RuntimeError(
                "A dependencia 'astrapy' nao esta instalada. Rode 'pip install -r requirements.txt'."
            )

        self.client = DataAPIClient()
        self.database = self.client.get_database(
            api_endpoint,
            token=token,
            keyspace=keyspace,
        )
        self.users_collection_name = users_collection
        self.receipts_collection_name = receipts_collection
        self._ensure_collections()
        self.users = self.database.get_collection(users_collection)
        self.receipts = self.database.get_collection(receipts_collection)

    def _ensure_collections(self) -> None:
        existing = set(self.database.list_collection_names())
        if self.users_collection_name not in existing:
            self.database.create_collection(self.users_collection_name)
        if self.receipts_collection_name not in existing:
            self.database.create_collection(self.receipts_collection_name)

    def create_user(self, payload: dict) -> dict:
        email = payload["email"]
        if self.get_user_by_email(email):
            raise ValueError("Ja existe uma conta com este e-mail.")
        self.users.insert_one(payload)
        return dict(payload)

    def get_user_by_email(self, email: str) -> dict | None:
        user = self.users.find_one({"email": email})
        return dict(user) if user else None

    def get_user_by_id(self, user_id: str | None) -> dict | None:
        if not user_id:
            return None
        user = self.users.find_one({"_id": user_id})
        return dict(user) if user else None

    def create_receipt(self, payload: dict) -> dict:
        self.receipts.insert_one(payload)
        return dict(payload)

    def get_receipt(self, rid: str) -> dict | None:
        receipt = self.receipts.find_one({"_id": rid})
        return dict(receipt) if receipt else None

    def list_receipts_by_driver(self, driver_id: str) -> list[dict]:
        receipts = self.receipts.find({"driver_id": driver_id}).to_list()
        receipts.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return [dict(receipt) for receipt in receipts]


_STORE = None


def get_store():
    global _STORE
    if _STORE is not None:
        return _STORE

    api_endpoint = os.environ.get("ASTRA_DB_API_ENDPOINT", "").strip()
    token = os.environ.get("ASTRA_DB_APPLICATION_TOKEN", "").strip()
    keyspace = os.environ.get("ASTRA_DB_KEYSPACE", "default_keyspace").strip()
    users_collection = os.environ.get(
        "ASTRA_DB_COLLECTION_USERS",
        DEFAULT_USERS_COLLECTION,
    ).strip()
    receipts_collection = os.environ.get(
        "ASTRA_DB_COLLECTION_RECEIPTS",
        DEFAULT_RECEIPTS_COLLECTION,
    ).strip()

    if not api_endpoint and not token:
        _STORE = InMemoryStore()
        return _STORE

    if not api_endpoint or not token:
        raise RuntimeError(
            "Configuracao do Astra DB incompleta. Defina ASTRA_DB_API_ENDPOINT e ASTRA_DB_APPLICATION_TOKEN."
        )

    _STORE = AstraStore(
        api_endpoint=api_endpoint,
        token=token,
        keyspace=keyspace,
        users_collection=users_collection,
        receipts_collection=receipts_collection,
    )
    return _STORE


app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

secret_key = os.environ.get("SECRET_KEY", "").strip()
if not secret_key:
    if os.environ.get("VERCEL"):
        raise RuntimeError("Defina a variavel SECRET_KEY antes de publicar na Vercel.")
    secret_key = "dev-secret-key"

app.config.update(
    SECRET_KEY=secret_key,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
)


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not g.user:
            flash("Entre com sua conta para continuar.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


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
    g.user = user


@app.context_processor
def inject_globals() -> dict:
    store = get_store()
    return {
        "app_name": APP_NAME,
        "current_user": g.get("user"),
        "storage_mode": store.kind,
        "storage_label": store.label,
    }


@app.get("/health")
def health():
    return "OK", 200


@app.get("/favicon.ico")
def favicon():
    return redirect(url_for("public_static", filename="img/RECIBO.png"), code=307)


@app.get("/static/<path:filename>")
def public_static(filename: str):
    return send_from_directory("public/static", filename)


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

        user = get_store().get_user_by_email(email)
        if not user or not check_password_hash(user["password_hash"], password):
            flash("E-mail ou senha invalidos.", "danger")
            return render_template("login.html", form=request.form), 401

        session["user_id"] = user["_id"]
        flash("Login realizado com sucesso.", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    if g.user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
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
            flash("Preencha os campos obrigatorios do cadastro.", "danger")
            return render_template("register.html", form=request.form), 400

        if len(password) < 8:
            flash("A senha precisa ter pelo menos 8 caracteres.", "danger")
            return render_template("register.html", form=request.form), 400

        payload = {
            "_id": uuid4().hex,
            "full_name": full_name,
            "email": email,
            "password_hash": generate_password_hash(password),
            "whatsapp": whatsapp,
            "cpf": cpf,
            "city": city,
            "plate": plate,
            "vehicle_model": vehicle_model,
            "taxi_prefix": taxi_prefix,
            "license_number": license_number,
            "created_at": now_iso(),
        }

        try:
            created_user = get_store().create_user(payload)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("register.html", form=request.form), 409

        session["user_id"] = created_user["_id"]
        flash("Cadastro concluido. Agora voce ja pode emitir recibos.", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.get("/sair")
def sair():
    session.clear()
    flash("Sua sessao foi encerrada.", "info")
    return redirect(url_for("index"))


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

    return render_template(
        "dashboard.html",
        receipts=receipts,
        today=datetime.now().strftime("%Y-%m-%d"),
    )


@app.post("/recibo")
@login_required
def recibo_criar():
    passenger = request.form.get("passageiro", "").strip()
    trip_date = request.form.get("data", "").strip()
    origin = request.form.get("origem", "").strip()
    destination = request.form.get("destino", "").strip()
    payment_method = request.form.get("forma_pagamento", "").strip() or "Pix"

    if not all([passenger, trip_date, origin, destination]):
        flash("Preencha os dados principais da corrida para gerar o recibo.", "danger")
        return redirect(url_for("dashboard"))

    try:
        amount_value, amount_display = normalize_money(request.form.get("valor", ""))
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("dashboard"))

    rid = uuid4().hex[:10].upper()
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

    get_store().create_receipt(receipt)
    flash("Recibo pronto para envio por WhatsApp ou e-mail.", "success")
    return redirect(url_for("recibo_view", rid=rid, created="1"))


@app.get("/recibo/<rid>")
def recibo_view(rid: str):
    receipt = get_store().get_receipt(rid)
    if not receipt:
        abort(404, description="Recibo nao encontrado.")

    share_url = public_receipt_url(rid)
    context = dict(receipt)
    context["public_url"] = share_url
    context["whatsapp_link"] = build_whatsapp_link(context, share_url)
    context["email_link"] = build_email_link(context, share_url)
    context["is_owner"] = bool(g.user and g.user["_id"] == receipt.get("driver_id"))

    return render_template("recibo_view.html", dados=context)


if __name__ == "__main__":
    app.run(debug=True)
