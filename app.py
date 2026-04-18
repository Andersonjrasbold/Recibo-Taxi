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
    jsonify,
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
except ImportError:
    DataAPIClient = None

try:
    import stripe as stripe_lib
except ImportError:
    stripe_lib = None


APP_NAME = "Recibo Táxi"
DEFAULT_USERS_COLLECTION = "taxistas"
DEFAULT_RECEIPTS_COLLECTION = "recibos"

STRIPE_PRICE_IDS = {
    "pro": "STRIPE_PRO_PRICE_ID",
    "business": "STRIPE_BUSINESS_PRICE_ID",
}


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
    raw = (value or "").strip().replace("R$", "").replace("\xa0", "").replace(" ", "")
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
        raise ValueError("Informe um valor válido (ex: 45,00).") from exc

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


def build_whatsapp_link(receipt: dict, public_url: str) -> str:
    phone = sanitize_phone(receipt.get("passenger_whatsapp", ""))
    message = quote(compose_receipt_message(receipt, public_url))
    if phone:
        return f"https://wa.me/{phone}?text={message}"
    return f"https://wa.me/?text={message}"


def build_email_link(receipt: dict, public_url: str) -> str:
    target = quote((receipt.get("passenger_email") or "").strip())
    subject = quote(f"Recibo #{receipt['rid']} — Recibo Táxi")
    body = quote(compose_receipt_message(receipt, public_url))
    return f"mailto:{target}?subject={subject}&body={body}"


def public_receipt_url(rid: str) -> str:
    base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    if base_url:
        return f"{base_url}{url_for('recibo_view', rid=rid)}"
    return url_for("recibo_view", rid=rid, _external=True)


def get_stripe():
    if stripe_lib is None:
        return None
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return None
    stripe_lib.api_key = key
    return stripe_lib


# ── Stores ──────────────────────────────────────────────────────────────────

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
            raise ValueError("Já existe uma conta com este e-mail.")
        self.users_by_id[payload["_id"]] = dict(payload)
        self.user_ids_by_email[email] = payload["_id"]
        return dict(payload)

    def update_user(self, user_id: str, updates: dict) -> None:
        if user_id in self.users_by_id:
            self.users_by_id[user_id].update(updates)

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
            dict(r)
            for r in self.receipts_by_id.values()
            if r.get("driver_id") == driver_id
        ]
        receipts.sort(key=lambda r: r.get("created_at", ""), reverse=True)
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
                "A dependência 'astrapy' não está instalada. Rode 'pip install -r requirements.txt'."
            )
        self.client = DataAPIClient()
        self.database = self.client.get_database(
            api_endpoint, token=token, keyspace=keyspace
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
            raise ValueError("Já existe uma conta com este e-mail.")
        self.users.insert_one(payload)
        return dict(payload)

    def update_user(self, user_id: str, updates: dict) -> None:
        self.users.find_one_and_update({"_id": user_id}, {"$set": updates})

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
        receipts.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return [dict(r) for r in receipts]


# ── Store singleton ──────────────────────────────────────────────────────────

_STORE = None


def get_store():
    global _STORE
    if _STORE is not None:
        return _STORE

    api_endpoint = os.environ.get("ASTRA_DB_API_ENDPOINT", "").strip()
    token = os.environ.get("ASTRA_DB_APPLICATION_TOKEN", "").strip()
    keyspace = os.environ.get("ASTRA_DB_KEYSPACE", "default_keyspace").strip()
    users_col = os.environ.get("ASTRA_DB_COLLECTION_USERS", DEFAULT_USERS_COLLECTION).strip()
    receipts_col = os.environ.get("ASTRA_DB_COLLECTION_RECEIPTS", DEFAULT_RECEIPTS_COLLECTION).strip()

    if not api_endpoint and not token:
        _STORE = InMemoryStore()
        return _STORE

    if not api_endpoint or not token:
        raise RuntimeError(
            "Configuração Astra DB incompleta. Defina ASTRA_DB_API_ENDPOINT e ASTRA_DB_APPLICATION_TOKEN."
        )

    _STORE = AstraStore(
        api_endpoint=api_endpoint,
        token=token,
        keyspace=keyspace,
        users_collection=users_col,
        receipts_collection=receipts_col,
    )
    return _STORE


# ── App ──────────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

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
        "current_year": datetime.utcnow().year,
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
    return send_from_directory("public/static", filename)


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
        user = get_store().get_user_by_email(email)
        if not user or not check_password_hash(user["password_hash"], password):
            flash("E-mail ou senha inválidos.", "danger")
            return render_template("login.html", form=request.form), 401
        session["user_id"] = user["_id"]
        flash("Bem-vindo de volta!", "success")
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
            flash("Preencha todos os campos obrigatórios.", "danger")
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
            "plan": "free",
            "created_at": now_iso(),
        }

        try:
            created_user = get_store().create_user(payload)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("register.html", form=request.form), 409

        session["user_id"] = created_user["_id"]
        flash("Conta criada! Já pode emitir e salvar seus recibos.", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.get("/sair")
def sair():
    session.clear()
    flash("Sessão encerrada com sucesso.", "info")
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

    this_month = datetime.utcnow().strftime("%Y-%m")
    receipts_this_month = sum(
        1 for r in receipts if r.get("created_at", "").startswith(this_month)
    )

    if request.args.get("subscribed") == "1":
        plan_name = g.user.get("plan", "pro").capitalize()
        flash(f"✅ Plano {plan_name} ativado com sucesso! Seja bem-vindo.", "success")

    return render_template(
        "dashboard.html",
        receipts=receipts,
        receipts_this_month=receipts_this_month,
        today=datetime.now().strftime("%Y-%m-%d"),
        user_plan=g.user.get("plan", "free"),
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
        flash("Preencha os dados principais da corrida.", "danger")
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
    flash("Recibo gerado e salvo na sua conta!", "success")
    return redirect(url_for("recibo_view", rid=rid, created="1"))


# ── Public generator (no login) ───────────────────────────────────────────────

@app.route("/gerar", methods=["GET", "POST"])
def gerador():
    if g.user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
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
                "gerador.html", form=request.form, today=datetime.now().strftime("%Y-%m-%d")
            ), 400

        try:
            amount_value, amount_display = normalize_money(request.form.get("valor", ""))
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template(
                "gerador.html", form=request.form, today=datetime.now().strftime("%Y-%m-%d")
            ), 400

        rid = uuid4().hex[:10].upper()
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
        return redirect(url_for("recibo_view", rid=rid, created="1"))

    return render_template("gerador.html", today=datetime.now().strftime("%Y-%m-%d"))


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
        checkout = stripe.checkout.Session.create(
            customer_email=g.user["email"],
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"user_id": g.user["_id"], "plan": plan},
            locale="pt-BR",
        )
    except Exception as exc:
        flash(f"Erro ao iniciar pagamento: {exc}", "danger")
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

    base = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    return_url = (
        f"{base}{url_for('dashboard')}"
        if base
        else url_for("dashboard", _external=True)
    )
    portal = stripe.billing_portal.Session.create(
        customer=customer_id, return_url=return_url
    )
    return redirect(portal.url, code=303)


@app.post("/webhook/stripe")
def stripe_webhook():
    stripe = get_stripe()
    if not stripe:
        abort(503)

    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception:
        abort(400)

    obj = event["data"]["object"]

    if event["type"] == "checkout.session.completed":
        user_id = obj.get("metadata", {}).get("user_id")
        plan = obj.get("metadata", {}).get("plan", "pro")
        if user_id:
            get_store().update_user(user_id, {
                "plan": plan,
                "stripe_customer_id": obj.get("customer"),
                "stripe_subscription_id": obj.get("subscription"),
            })

    elif event["type"] == "customer.subscription.deleted":
        # Downgrade happens here — production: index customer_id → user_id
        pass

    elif event["type"] == "customer.subscription.updated":
        status = obj.get("status")
        if status not in ("active", "trialing"):
            # Subscription paused or past_due — handle accordingly
            pass

    return jsonify({"ok": True})


# ── Receipt view ──────────────────────────────────────────────────────────────

@app.get("/recibo/<rid>")
def recibo_view(rid: str):
    receipt = get_store().get_receipt(rid)
    if not receipt:
        abort(404, description="Recibo não encontrado.")

    share_url = public_receipt_url(rid)
    context = dict(receipt)
    context["public_url"] = share_url
    context["whatsapp_link"] = build_whatsapp_link(context, share_url)
    context["email_link"] = build_email_link(context, share_url)
    context["is_owner"] = bool(g.user and g.user["_id"] == receipt.get("driver_id"))
    context["is_guest"] = receipt.get("is_guest", False)
    context["just_created"] = request.args.get("created") == "1"

    return render_template("recibo_view.html", dados=context)


if __name__ == "__main__":
    app.run(debug=True)
