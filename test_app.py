"""Testes de regressao do Recibo Taxi.

Roda com o store em memoria, sem rede e sem dependencias extras:

    python test_app.py

Sai com codigo 1 se algum teste falhar.
"""
import io
import os
import sys

# Desliga tudo que sai da maquina: Astra, SMTP e, principalmente, o Supabase
# de producao — get_store() prefere SUPABASE_DB_URL, e a suite cria dezenas de
# contas de teste. Isto nao pode nunca apontar para a nuvem.
# Vazio, nao removido: o app chama load_dotenv() no import, e o load_dotenv
# repoe o que foi REMOVIDO mas respeita o que ja existe — mesmo vazio.
for key in ("ASTRA_DB_API_ENDPOINT", "ASTRA_DB_APPLICATION_TOKEN",
            "SMTP_HOST", "SUPABASE_DB_URL"):
    os.environ[key] = ""

# Trava: se o DATABASE_URL nao for local, a suite se recusa a rodar.
_dsn = os.environ.get("DATABASE_URL", "")
if _dsn and not any(h in _dsn for h in ("127.0.0.1", "localhost", "::1")):
    raise SystemExit(
        f"DATABASE_URL aponta para fora desta maquina.\n"
        f"A suite cria dezenas de contas de teste — recusando rodar."
    )

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as A

fails = []
def check(label, cond, extra=""):
    print(("  OK  " if cond else " FALHA") + f"  {label}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond: fails.append(label)

def recibo_bruto(rid, created_at, driver_id=None, is_guest=True):
    """Recibo completo — o Postgres, ao contrario do Astra, cobra os NOT NULL."""
    return {
        "_id": rid, "rid": rid, "driver_id": driver_id, "is_guest": is_guest,
        "passenger": "Teste", "passenger_email": "", "passenger_whatsapp": "",
        "trip_date": "2026-09-12", "trip_time": "", "origin": "A", "destino": "B",
        "destination": "B", "amount_value": "10.00", "amount_display": "10,00",
        "payment_method": "Pix", "notes": "", "created_at": created_at,
        "driver_snapshot": {},
    }


def novo_cliente(email="a@t.com"):
    c = A.app.test_client()
    c.post("/cadastro", data={"nome_completo":"Ana Souza","email":email,"senha":"senha12345",
        "whatsapp":"11999998888","cpf":"12345678900","cidade":"SP","placa":"abc1d23"})
    return c

print("\n── Páginas públicas novas ──")
c0 = A.app.test_client()
for path in ["/privacidade", "/termos", "/recuperar-senha"]:
    r = c0.get(path); check(f"GET {path}", r.status_code == 200, r.status_code)
r = c0.get("/login")
check("login mostra 'Esqueci minha senha'", "Esqueci minha senha" in r.get_data(as_text=True))
r = c0.get("/")
html = r.get_data(as_text=True)
check("rodapé linka Privacidade", "/privacidade" in html)
check("rodapé linka Termos", "/termos" in html)

print("\n── Limite de 30 recibos do plano Grátis ──")
c = novo_cliente("limite@t.com")
criados = 0
for i in range(35):
    r = c.post("/recibo", data={"passageiro":f"P{i}","data":"2026-09-12","origem":"A","destino":"B","valor":"10"})
    if "/recibo/" in r.headers.get("Location", ""): criados += 1
check("para exatamente em 30 recibos", criados == 30, f"criou {criados}")
r = c.post("/recibo", data={"passageiro":"X","data":"2026-09-12","origem":"A","destino":"B","valor":"10"})
check("31º redireciona para /planos", r.headers.get("Location","").endswith("/planos"), r.headers.get("Location"))
r = c.get("/dashboard"); html = r.get_data(as_text=True)
check("painel mostra 30/30", "30<span class=\"stat-limit\">/30</span>" in html.replace("\n","").replace("  ",""), "contador")
check("painel mostra banner de bloqueio", "usou os 30 recibos deste mês" in html)

print("\n── Plano pago não tem limite ──")
cp = novo_cliente("pro@t.com")
store = A.get_store()
uid = store.get_user_by_email("pro@t.com")["_id"]
store.update_user(uid, {"plan": "pro"})
criados = sum(1 for i in range(40)
    if "/recibo/" in cp.post("/recibo", data={"passageiro":f"P{i}","data":"2026-09-12",
        "origem":"A","destino":"B","valor":"10"}).headers.get("Location",""))
check("plano Pro cria 40 sem bloqueio", criados == 40, f"criou {criados}")

print("\n── Contagem mensal respeita o fuso de Brasília ──")
start, end = A.month_range_utc(A.datetime(2026, 9, 15, 12, tzinfo=A.BR_TZ))
check("mês começa 2026-09-01T03:00Z", start == "2026-09-01T03:00:00Z", start)
check("mês termina 2026-10-01T03:00Z", end == "2026-10-01T03:00:00Z", end)
s12, _ = A.month_range_utc(A.datetime(2026, 12, 20, tzinfo=A.BR_TZ))
_, e12 = A.month_range_utc(A.datetime(2026, 12, 20, tzinfo=A.BR_TZ))
check("virada de ano em dezembro", e12 == "2027-01-01T03:00:00Z", e12)

print("\n── Redefinição de senha ──")
cr = novo_cliente("reset@t.com")
user = A.get_store().get_user_by_email("reset@t.com")
with A.app.test_request_context():
    token = A.build_reset_token(user)
c2 = A.app.test_client()
r = c2.get(f"/redefinir-senha/{token}")
check("link válido abre o formulário", r.status_code == 200, r.status_code)
r = c2.post(f"/redefinir-senha/{token}", data={"senha":"novasenha1","confirmar_senha":"novasenha1"})
check("redefine e manda para /login", r.headers.get("Location","").endswith("/login"), r.headers.get("Location"))
c3 = A.app.test_client()
r = c3.post("/login", data={"email":"reset@t.com","senha":"novasenha1"})
check("entra com a nova senha", r.status_code == 302)
c3b = A.app.test_client()
r = c3b.post("/login", data={"email":"reset@t.com","senha":"senha12345"})
check("senha antiga não funciona mais", r.status_code == 401, r.status_code)
c4 = A.app.test_client()
r = c4.get(f"/redefinir-senha/{token}", follow_redirects=False)
check("token é de uso único", r.status_code == 302 and "recuperar-senha" in r.headers.get("Location",""), r.status_code)
r = c4.get("/redefinir-senha/token-falso-123", follow_redirects=False)
check("token inválido é rejeitado", r.status_code == 302)
r = c0.post("/recuperar-senha", data={"email":"naoexiste@t.com"})
check("e-mail inexistente não vaza cadastro", r.headers.get("Location","").endswith("/login"))

print("\n── Exclusão de conta ──")
cd = novo_cliente("delete@t.com")
cd.post("/recibo", data={"passageiro":"M","data":"2026-09-12","origem":"A","destino":"B","valor":"30"})
uid = A.get_store().get_user_by_email("delete@t.com")["_id"]
check("recibo existe antes", len(A.get_store().list_receipts_by_driver(uid)) == 1)
r = cd.post("/excluir-conta", data={"senha":"senha12345","confirmacao":"talvez"})
check("recusa sem a palavra EXCLUIR", A.get_store().get_user_by_email("delete@t.com") is not None)
r = cd.post("/excluir-conta", data={"senha":"senha-errada","confirmacao":"EXCLUIR"})
check("recusa com senha errada", A.get_store().get_user_by_email("delete@t.com") is not None)
r = cd.post("/excluir-conta", data={"senha":"senha12345","confirmacao":"excluir"})
check("aceita 'excluir' minúsculo", A.get_store().get_user_by_email("delete@t.com") is None)
check("recibos foram apagados", len(A.get_store().list_receipts_by_driver(uid)) == 0)
r = cd.get("/dashboard")
check("sessão encerrada após exclusão", r.status_code == 302)

print("\n── Ciclo de vida da assinatura (webhook) ──")
cs = novo_cliente("sub@t.com")
uid = A.get_store().get_user_by_email("sub@t.com")["_id"]
store = A.get_store()
store.update_user(uid, {"plan":"pro","stripe_customer_id":"cus_123","stripe_subscription_id":"sub_1"})
check("acha usuário pelo customer_id", store.get_user_by_stripe_customer("cus_123")["_id"] == uid)

class FakeStripe:
    class Webhook:
        @staticmethod
        def construct_event(payload, sig, secret):
            import json; return json.loads(payload)
orig_get_stripe = A.get_stripe
A.get_stripe = lambda: FakeStripe
os.environ["STRIPE_SECRET_KEY"] = "sk_test_x"

import json
def webhook(evt_type, obj):
    return c0.post("/webhook/stripe", data=json.dumps({"type":evt_type,"data":{"object":obj}}),
                   content_type="application/json")

webhook("customer.subscription.updated", {"customer":"cus_123","status":"past_due"})
check("past_due mantém o plano Pro", store.get_user_by_id(uid)["plan"] == "pro", store.get_user_by_id(uid)["plan"])
webhook("customer.subscription.updated", {"customer":"cus_123","status":"unpaid"})
check("unpaid rebaixa para free", store.get_user_by_id(uid)["plan"] == "free", store.get_user_by_id(uid)["plan"])
store.update_user(uid, {"plan":"business"})
webhook("customer.subscription.deleted", {"customer":"cus_123","status":"canceled"})
u = store.get_user_by_id(uid)
check("cancelamento rebaixa para free", u["plan"] == "free", u["plan"])
check("limpa o id da assinatura", u.get("stripe_subscription_id") is None, u.get("stripe_subscription_id"))
check("registra o status", u.get("subscription_status") == "canceled", u.get("subscription_status"))
A.get_stripe = orig_get_stripe

print("\n-- Cabecalhos de seguranca e CSP --")
import re as _re
rs = c0.get("/")
for h, v in [("X-Content-Type-Options","nosniff"), ("X-Frame-Options","DENY"),
             ("Referrer-Policy","strict-origin-when-cross-origin")]:
    check(f"{h} presente", rs.headers.get(h) == v, rs.headers.get(h))
csp = rs.headers.get("Content-Security-Policy", "")
check("CSP presente", bool(csp))
check("script-src sem unsafe-inline",
      "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0], csp[:80])
check("frame-ancestors none", "frame-ancestors 'none'" in csp)
check("object-src none", "object-src 'none'" in csp)
check("form-action libera a Stripe", "checkout.stripe.com" in csp)

# o nonce do cabecalho tem de ser o mesmo dos <script> inline da pagina, e mudar por requisicao
cn = novo_cliente("csp@t.com")
rr = cn.post("/recibo", data={"passageiro":"M","data":"2026-09-12","origem":"A","destino":"B","valor":"30"})
rid = rr.headers["Location"].split("/recibo/")[1].split("?")[0]
r1 = cn.get(f"/recibo/{rid}")
h1 = _re.search(r"\'nonce-([^\']+)\'", r1.headers["Content-Security-Policy"]).group(1)
pg = set(_re.findall(r'<script nonce="([^"]+)"', r1.get_data(as_text=True)))
check("nonce da pagina bate com o do cabecalho", pg == {h1}, f"{pg} vs {h1[:10]}")
r2 = cn.get(f"/recibo/{rid}")
h2 = _re.search(r"\'nonce-([^\']+)\'", r2.headers["Content-Security-Policy"]).group(1)
check("nonce muda a cada requisicao", h1 != h2)
check("nenhum handler inline nos templates",
      not any("onclick=" in io.open(f, encoding="utf-8").read() or "onsubmit=" in io.open(f, encoding="utf-8").read()
              for f in __import__("glob").glob("templates/*.html")))

print("\n-- Falha explicita sem Astra em producao --")
A._STORE = None
_db = os.environ.pop("DATABASE_URL", "")
os.environ["VERCEL"] = "1"
try:
    A.get_store()
    check("levanta erro na Vercel sem banco nenhum", False, "nao levantou")
except RuntimeError as exc:
    check("levanta erro na Vercel sem banco nenhum", "ASTRA_DB_API_ENDPOINT" in str(exc))
finally:
    os.environ.pop("VERCEL", None)
    if _db: os.environ["DATABASE_URL"] = _db
    A._STORE = None


print("\n-- Rate limit do gerador publico --")
def gerar(cli, i, ip):
    return cli.post("/gerar", data={"passageiro":f"P{i}","data":"2026-09-12","origem":"A",
        "destino":"B","valor":"20","nome_motorista":"Ze"},
        environ_base={"REMOTE_ADDR": ip})

cg = A.app.test_client()
ok = sum(1 for i in range(A.GUEST_DAILY_LIMIT) if gerar(cg, i, "203.0.113.7").status_code == 302)
check(f"permite {A.GUEST_DAILY_LIMIT} recibos por IP/dia", ok == A.GUEST_DAILY_LIMIT, f"passaram {ok}")
r = gerar(cg, 99, "203.0.113.7")
check("o seguinte responde 429", r.status_code == 429, r.status_code)

# outro IP tem cota propria
r = gerar(A.app.test_client(), 0, "203.0.113.8")
check("IP diferente tem cota propria", r.status_code == 302, r.status_code)

# erro de preenchimento nao gasta cota
cq = A.app.test_client()
antes = A.get_store().bump_counter("gerar:203.0.113.9:" + A.today_br())
cq.post("/gerar", data={"passageiro":"", "data":"", "origem":"", "destino":"", "nome_motorista":""},
        environ_base={"REMOTE_ADDR": "203.0.113.9"})
depois = A.get_store().bump_counter("gerar:203.0.113.9:" + A.today_br())
check("submissao invalida nao gasta cota", depois == antes + 1, f"{antes} -> {depois}")

print("\n-- Rotina de limpeza (retencao) --")
os.environ.pop("CRON_SECRET", None)
r = c0.get("/tarefas/limpeza")
check("sem CRON_SECRET a rota fica fechada (503)", r.status_code == 503, r.status_code)

os.environ["CRON_SECRET"] = "segredo-de-teste"
r = c0.get("/tarefas/limpeza")
check("sem Authorization responde 401", r.status_code == 401, r.status_code)
r = c0.get("/tarefas/limpeza", headers={"Authorization": "Bearer errado"})
check("com segredo errado responde 401", r.status_code == 401, r.status_code)

# um recibo guest vencido e um recente
store = A.get_store()
velho = A.to_utc_iso(A.datetime.now(A.timezone.utc) - A.timedelta(days=A.GUEST_RETENTION_DAYS + 5))
novo_iso = A.now_iso()
store.create_receipt(recibo_bruto("AAAAAAAAAAAAAA", velho))
store.create_receipt(recibo_bruto("BBBBBBBBBBBBBB", novo_iso))
r = c0.get("/tarefas/limpeza", headers={"Authorization": "Bearer segredo-de-teste"})
check("com o segredo certo responde 200", r.status_code == 200, r.status_code)
check("apagou o recibo vencido", store.get_receipt("AAAAAAAAAAAAAA") is None)
check("preservou o recibo recente", store.get_receipt("BBBBBBBBBBBBBB") is not None)
os.environ.pop("CRON_SECRET", None)

print("\n-- Contador atomico --")
store = A.get_store()
vals = [store.bump_counter("teste:atomico") for _ in range(5)]
check("incrementa sequencialmente", vals == [1,2,3,4,5], str(vals))


print("\n-- Validacao de valor --")
for ruim in ["NaN", "nan", "  NaN  ", "-50,00", "-0,01", "0", "0,00", "Infinity", "999999999"]:
    try:
        A.normalize_money(ruim)
        check(f"rejeita {ruim!r}", False, "aceitou")
    except ValueError:
        check(f"rejeita {ruim!r}", True)
for bom, esperado in [("45,00","45,00"), ("45.50","45,50"), ("1.234,56","1234,56"), ("0,01","0,01")]:
    try:
        _, disp = A.normalize_money(bom)
        check(f"aceita {bom!r}", disp == esperado, f"virou {disp}")
    except ValueError as e:
        check(f"aceita {bom!r}", False, str(e))

cv = novo_cliente("valor@t.com")
r = cv.post("/recibo", data={"passageiro":"M","data":"2026-09-12","origem":"A","destino":"B","valor":"-10"})
check("POST /recibo com valor negativo nao cria recibo", "/recibo/" not in r.headers.get("Location",""))

print("\n-- Trocar a senha derruba as outras sessoes --")
cs1 = novo_cliente("sessao@t.com")
check("sessao 1 logada", cs1.get("/dashboard").status_code == 200)
cs2 = A.app.test_client()
cs2.post("/login", data={"email":"sessao@t.com","senha":"senha12345"})
check("sessao 2 logada", cs2.get("/dashboard").status_code == 200)
u = A.get_store().get_user_by_email("sessao@t.com")
with A.app.test_request_context():
    tok = A.build_reset_token(u)
A.app.test_client().post(f"/redefinir-senha/{tok}", data={"senha":"outrasenha9","confirmar_senha":"outrasenha9"})
check("sessao 1 foi derrubada", cs1.get("/dashboard").status_code == 302)
check("sessao 2 foi derrubada", cs2.get("/dashboard").status_code == 302)
cs3 = A.app.test_client()
check("login com a senha nova funciona",
      cs3.post("/login", data={"email":"sessao@t.com","senha":"outrasenha9"}).status_code == 302)
check("e a sessao nova sobrevive", cs3.get("/dashboard").status_code == 200)

print("\n-- Pagina de planos por plano do usuario --")
def planos_html(plano):
    c = novo_cliente(f"pl{plano}@t.com")
    uid = A.get_store().get_user_by_email(f"pl{plano}@t.com")["_id"]
    if plano != "free":
        A.get_store().update_user(uid, {"plan": plano})
    return c.get("/planos").get_data(as_text=True)

h = planos_html("free")
check("free ve o botao de assinar Pro", "Assinar Pro" in h)
check("Business nao aparece mais na pagina", "Business" not in h)
h = planos_html("pro")
check("pro nao ve botao de assinar", "Assinar Pro" not in h)
check("pro ve 'Seu plano atual' uma unica vez", h.count("Seu plano atual") == 1, h.count("Seu plano atual"))

print("\n-- Business fora de venda, assinante antigo preservado --")
h = planos_html("business")
check("Business nao e mais oferecido", "Assinar Business" not in h and "Começar com Business" not in h)
check("assinante antigo pode migrar para o Pro pelo portal", "Mudar para Pro no portal" in h)
check("nenhuma pagina promete recursos inexistentes",
      "API de integração" not in h and "Exportação de dados" not in h)

cb = novo_cliente("bizz@t.com")
uidb = A.get_store().get_user_by_email("bizz@t.com")["_id"]
A.get_store().update_user(uidb, {"plan": "business"})
os.environ["STRIPE_SECRET_KEY"] = "sk_test_x"
class _FakeStripe: pass
_orig = A.get_stripe
A.get_stripe = lambda: _FakeStripe
r = cb.post("/assinar/business")
check("checkout de Business e recusado (400)", r.status_code == 400, r.status_code)
A.get_stripe = _orig
os.environ.pop("STRIPE_SECRET_KEY", None)

# assinante antigo mantem recibos ilimitados
criados = sum(1 for i in range(35)
    if "/recibo/" in cb.post("/recibo", data={"passageiro":f"P{i}","data":"2026-09-12",
        "origem":"A","destino":"B","valor":"10"}).headers.get("Location",""))
check("assinante antigo do Business segue ilimitado", criados == 35, f"criou {criados}")

print("\n-- Header Authorization esquisito nao vira 500 --")
os.environ["CRON_SECRET"] = "segredo-de-teste"
r = c0.get("/tarefas/limpeza", headers={"Authorization": "Bearer caf\u00e9-n\u00e3o-ascii"})
check("responde 401, nao 500", r.status_code == 401, r.status_code)
os.environ.pop("CRON_SECRET", None)

print("\n-- Contato do passageiro nao vaza no link publico --")
cp2 = novo_cliente("vaza@t.com")
r = cp2.post("/recibo", data={"passageiro":"Maria","data":"2026-09-12","origem":"A","destino":"B",
    "valor":"45,00","whatsapp_passageiro":"11955554444","email_passageiro":"privado@exemplo.com"})
rid = r.headers["Location"].split("/recibo/")[1].split("?")[0]
dono_html = cp2.get(f"/recibo/{rid}").get_data(as_text=True)
anon_html = A.app.test_client().get(f"/recibo/{rid}").get_data(as_text=True)
check("dono mantem o link pre-preenchido", "11955554444" in dono_html and "privado" in dono_html)
check("anonimo nao ve o whatsapp do passageiro", "11955554444" not in anon_html)
check("anonimo nao ve o e-mail do passageiro", "privado@exemplo.com" not in anon_html
      and "privado%40exemplo.com" not in anon_html)


print("\n-- Limite de tamanho por campo --")
cl = novo_cliente("tam@t.com")
grande = "A" * 5000
r = cl.post("/recibo", data={"passageiro":grande,"data":"2026-09-12","origem":"A",
    "destino":"B","valor":"10"})
check("recibo com nome de 5000 chars e recusado", "/recibo/" not in r.headers.get("Location",""))
r = cl.post("/recibo", data={"passageiro":"Maria","data":"2026-09-12","origem":"A",
    "destino":"B","valor":"10","observacoes":grande})
check("observacoes de 5000 chars e recusada", "/recibo/" not in r.headers.get("Location",""))
r = cl.post("/recibo", data={"passageiro":"Maria","data":"2026-09-12","origem":"A",
    "destino":"B","valor":"10","observacoes":"tudo certo"})
check("recibo normal continua passando", "/recibo/" in r.headers.get("Location",""))

cg2 = A.app.test_client()
r = cg2.post("/gerar", data={"passageiro":grande,"data":"2026-09-12","origem":"A","destino":"B",
    "valor":"20","nome_motorista":"Ze"}, environ_base={"REMOTE_ADDR":"198.51.100.77"})
check("gerador publico tambem recusa (400)", r.status_code == 400, r.status_code)

cc = A.app.test_client()
r = cc.post("/cadastro", data={"nome_completo":grande,"email":"x@t.com","senha":"senha12345",
    "whatsapp":"11999998888","cpf":"12345678900","cidade":"SP","placa":"abc1d23"})
check("cadastro recusa nome gigante (400)", r.status_code == 400, r.status_code)
check("conta nao foi criada", A.get_store().get_user_by_email("x@t.com") is None)

print("\n-- Limpeza em lotes --")
store = A.get_store()
velho = A.to_utc_iso(A.datetime.now(A.timezone.utc) - A.timedelta(days=A.GUEST_RETENTION_DAYS + 5))
for i in range(A.CLEANUP_BATCH_LIMIT + 30):
    store.create_receipt(recibo_bruto(f"{i:014X}", velho))
os.environ["CRON_SECRET"] = "segredo-de-teste"
r = c0.get("/tarefas/limpeza", headers={"Authorization": "Bearer segredo-de-teste"})
d = r.get_json()
check(f"apaga no maximo {A.CLEANUP_BATCH_LIMIT} por execucao",
      d["recibos_removidos"] == A.CLEANUP_BATCH_LIMIT, d["recibos_removidos"])
check("avisa que sobrou backlog", d["backlog_restante"] is True, d)
r2 = c0.get("/tarefas/limpeza", headers={"Authorization": "Bearer segredo-de-teste"})
d2 = r2.get_json()
check("segunda execucao limpa o resto", d2["recibos_removidos"] == 30, d2["recibos_removidos"])
check("e avisa que acabou", d2["backlog_restante"] is False, d2)
os.environ.pop("CRON_SECRET", None)


print("\n-- Edicao de cadastro (/perfil) --")
cpf_ = novo_cliente("perfil@t.com")
r = cpf_.get("/perfil")
check("pagina abre para quem esta logado", r.status_code == 200, r.status_code)
check("vem preenchida com os dados atuais", "perfil@t.com" in r.get_data(as_text=True))
check("exige login", A.app.test_client().get("/perfil").status_code == 302)

base = {"nome_completo":"Ana Souza Lima","email":"perfil@t.com","whatsapp":"11988887777",
        "cpf":"12345678900","cidade":"Santos","placa":"xyz9k88","modelo_veiculo":"Corolla"}

r = cpf_.post("/perfil", data={**base, "senha_atual":"errada"})
check("senha errada nao salva nada", r.status_code == 401, r.status_code)
check("dados continuam os antigos",
      A.get_store().get_user_by_email("perfil@t.com")["city"] == "SP")

r = cpf_.post("/perfil", data={**base, "senha_atual":"senha12345"})
check("salva com a senha certa", r.status_code == 302, r.status_code)
u = A.get_store().get_user_by_email("perfil@t.com")
check("cidade atualizada", u["city"] == "Santos", u["city"])
check("placa normalizada para maiuscula", u["plate"] == "XYZ9K88", u["plate"])
check("nome atualizado", u["full_name"] == "Ana Souza Lima")

# e-mail duplicado
outro = novo_cliente("ocupado@t.com")
r = cpf_.post("/perfil", data={**base, "email":"ocupado@t.com", "senha_atual":"senha12345"})
check("recusa e-mail ja usado por outra conta", r.status_code == 409, r.status_code)

# troca de e-mail valida mantem o login funcionando
r = cpf_.post("/perfil", data={**base, "email":"novo-email@t.com", "senha_atual":"senha12345"})
check("troca de e-mail e aceita", r.status_code == 302, r.status_code)
check("e-mail antigo nao acha mais a conta", A.get_store().get_user_by_email("perfil@t.com") is None)
check("e-mail novo acha a conta", A.get_store().get_user_by_email("novo-email@t.com") is not None)
cnovo = A.app.test_client()
check("login com o e-mail novo funciona",
      cnovo.post("/login", data={"email":"novo-email@t.com","senha":"senha12345"}).status_code == 302)

r = cpf_.post("/perfil", data={**base, "email":"novo-email@t.com", "nome_completo":"A"*5000,
                               "senha_atual":"senha12345"})
check("campo gigante e recusado", r.status_code == 400, r.status_code)


print("\n-- Corrida da cota do plano Gratis --")
if A.get_store().kind == "postgres":
    import concurrent.futures as _cf
    cc = novo_cliente("corrida@t.com")
    uidc = A.get_store().get_user_by_email("corrida@t.com")["_id"]
    # 28 recibos ja usados no mes
    for i in range(28):
        A.get_store().create_receipt(
            recibo_bruto(f"C{i:013X}", A.now_iso(), driver_id=uidc, is_guest=False),
            quota_limit=A.FREE_MONTHLY_LIMIT)

    def emitir(i):
        return A.get_store().create_receipt(
            recibo_bruto(f"D{i:013X}", A.now_iso(), driver_id=uidc, is_guest=False),
            quota_limit=A.FREE_MONTHLY_LIMIT) is not None

    with _cf.ThreadPoolExecutor(max_workers=10) as ex:
        res = list(ex.map(emitir, range(10)))
    check("10 emissoes simultaneas partindo de 28 liberam exatamente 2",
          sum(res) == 2, f"liberou {sum(res)}")
    total = len(A.get_store().list_receipts_by_driver(uidc))
    check("total no mes para em 30", total == 30, total)
else:
    print("  (pulado: so faz sentido no Postgres)")

print("\n-- rid com 14 digitos --")
cr2 = novo_cliente("rid@t.com")
r = cr2.post("/recibo", data={"passageiro":"M","data":"2026-09-12","origem":"A",
                              "destino":"B","valor":"30"})
rid14 = r.headers["Location"].split("/recibo/")[1].split("?")[0]
check("rid tem 14 caracteres", len(rid14) == 14, f"{rid14} ({len(rid14)})")
check("rid e hexadecimal maiusculo", all(c in "0123456789ABCDEF" for c in rid14), rid14)


print("\n" + ("="*50))
print(f"FALHAS: {len(fails)}" + ("" if not fails else " -> " + ", ".join(fails)))
sys.exit(1 if fails else 0)
