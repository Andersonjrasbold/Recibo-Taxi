"""Testes de regressao do Recibo Taxi.

    PERMITIR_TESTE_REMOTO=1 python test_app.py

A suite roda contra o Supabase, e nao contra um banco local. Nao e escolha:
desde que a autenticacao passou para o Supabase Auth, `drivers.id` e chave
estrangeira de `auth.users`. Um usuario criado no Auth da nuvem nao pode ter
perfil num Postgres local — a FK nao fecha.

Por isso a suite exige opt-in explicito: ela cria dezenas de contas e nao pode
rodar por acidente. Toda conta que ela cria usa o dominio @teste.invalid
(TLD reservada pela RFC 2606) e e apagada no inicio e no fim.

Sai com codigo 1 se algum teste falhar.
"""
import base64
import hashlib
import hmac as _hmac
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Vazio, nao removido: o app chama load_dotenv() no import, e o load_dotenv
# repoe o que foi REMOVIDO mas respeita o que ja existe — mesmo vazio.
for key in ("ASTRA_DB_API_ENDPOINT", "ASTRA_DB_APPLICATION_TOKEN", "SMTP_HOST"):
    os.environ[key] = ""
# O Auth vive na nuvem: o banco tem de ser o mesmo, senao a FK nao fecha.
os.environ["DATABASE_URL"] = ""

if os.environ.get("PERMITIR_TESTE_REMOTO") != "1":
    raise SystemExit(
        "Esta suite escreve no Supabase — inclusive criando e apagando contas.\n"
        "Rode com PERMITIR_TESTE_REMOTO=1 se for isso mesmo que voce quer."
    )

import app as A

DOMINIO_TESTE = "@teste.invalid"


def limpar_contas_de_teste():
    """Apaga o rastro da suite. Contra a nuvem, o estado sobrevive entre execucoes.

    Tres coisas precisam sair, e so a primeira tem cascade:
      1. contas @teste.invalid  -> cascade leva perfil, recibos e cotas
      2. recibos de convidado   -> driver_id NULL, ninguem os leva junto
      3. contadores de rate limit -> senao a segunda execucao ja nasce no teto
    """
    removidas = 0
    for u in A.supabase_admin().auth.admin.list_users():
        if (u.email or "").endswith(DOMINIO_TESTE):
            A.supabase_admin().auth.admin.delete_user(str(u.id))
            removidas += 1

    store = A.get_store()
    if getattr(store, "kind", "") == "postgres":
        with store.pool.connection() as conn:
            # Recibos de convidado dos testes: o gerador publico grava 'Ze' como
            # motorista, e os recibos montados a mao usam 'Teste' como passageiro.
            conn.execute("""
                delete from public.receipts
                 where is_guest
                   and (driver_snapshot->>'full_name' = 'Ze' or passenger = 'Teste')
            """)
            # Faixas reservadas para documentacao (RFC 5737) — so a suite usa.
            conn.execute("""
                delete from public.rate_limits
                 where key like 'gerar:203.0.113.%%'
                    or key like 'gerar:198.51.100.%%'
                    or key like 'gerar:192.0.2.%%'
                    or key like 'teste:%%'
            """)
    return removidas


_antes = limpar_contas_de_teste()
if _antes:
    print(f"  (limpeza inicial: {_antes} conta(s) de execucao anterior removida(s))")


def webhook_stripe(cliente, tipo, obj):
    """Chama /webhook/stripe com assinatura Stripe DE VERDADE.

    A versao anterior stubava construct_event com um fake que devolvia dict.
    Isso escondeu um bug real: o construct_event devolve StripeObject, que nao
    tem .get(), e todo webhook dava 500 em producao. Assinar de verdade custa
    tres linhas e testa o caminho que roda.
    """
    corpo = json.dumps({"type": tipo, "data": {"object": obj}})
    ts = int(time.time())
    segredo = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    assinatura = _hmac.new(
        segredo.encode(), f"{ts}.{corpo}".encode(), hashlib.sha256
    ).hexdigest()
    return cliente.post(
        "/webhook/stripe", data=corpo, content_type="application/json",
        headers={"Stripe-Signature": f"t={ts},v1={assinatura}"},
    )

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


def novo_cliente(email="a@teste.invalid"):
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

print(f"\n── Limite de {A.FREE_MONTHLY_LIMIT} recibos do plano Grátis ──")
c = novo_cliente("limite@teste.invalid")
criados = 0
for i in range(A.FREE_MONTHLY_LIMIT + 5):
    r = c.post("/recibo", data={"passageiro":f"P{i}","data":"2026-09-12","origem":"A","destino":"B","valor":"10"})
    if "/recibo/" in r.headers.get("Location", ""): criados += 1
check(f"para exatamente em {A.FREE_MONTHLY_LIMIT} recibos", criados == A.FREE_MONTHLY_LIMIT, f"criou {criados}")
r = c.post("/recibo", data={"passageiro":"X","data":"2026-09-12","origem":"A","destino":"B","valor":"10"})
check("o seguinte redireciona para /planos", r.headers.get("Location","").endswith("/planos"), r.headers.get("Location"))
r = c.get("/dashboard"); html = r.get_data(as_text=True)
L = A.FREE_MONTHLY_LIMIT
check(f"painel mostra {L}/{L}", f'{L}<span class="stat-limit">/{L}</span>' in html.replace("\n","").replace("  ",""), "contador")
check("painel mostra banner de bloqueio", f"usou os {A.FREE_MONTHLY_LIMIT} recibos deste mês" in html)

print("\n── Plano pago não tem limite ──")
cp = novo_cliente("pro@teste.invalid")
store = A.get_store()
uid = store.get_user_by_email("pro@teste.invalid")["_id"]
store.update_user(uid, {"plan": "pro"})
criados = sum(1 for i in range(A.FREE_MONTHLY_LIMIT + 10)
    if "/recibo/" in cp.post("/recibo", data={"passageiro":f"P{i}","data":"2026-09-12",
        "origem":"A","destino":"B","valor":"10"}).headers.get("Location",""))
check("plano pago não tem teto", criados == A.FREE_MONTHLY_LIMIT + 10, f"criou {criados}")

print("\n── Contagem mensal respeita o fuso de Brasília ──")
start, end = A.month_range_utc(A.datetime(2026, 9, 15, 12, tzinfo=A.BR_TZ))
check("mês começa 2026-09-01T03:00Z", start == "2026-09-01T03:00:00Z", start)
check("mês termina 2026-10-01T03:00Z", end == "2026-10-01T03:00:00Z", end)
s12, _ = A.month_range_utc(A.datetime(2026, 12, 20, tzinfo=A.BR_TZ))
_, e12 = A.month_range_utc(A.datetime(2026, 12, 20, tzinfo=A.BR_TZ))
check("virada de ano em dezembro", e12 == "2027-01-01T03:00:00Z", e12)

print("\n── Redefinição de senha ──")
cr = novo_cliente("reset@teste.invalid")
user = A.get_store().get_user_by_email("reset@teste.invalid")
with A.app.test_request_context():
    token = A.build_reset_token(user)
c2 = A.app.test_client()
r = c2.get(f"/redefinir-senha/{token}")
check("link válido abre o formulário", r.status_code == 200, r.status_code)
r = c2.post(f"/redefinir-senha/{token}", data={"senha":"novasenha1","confirmar_senha":"novasenha1"})
check("redefine e manda para /login", r.headers.get("Location","").endswith("/login"), r.headers.get("Location"))
c3 = A.app.test_client()
r = c3.post("/login", data={"email":"reset@teste.invalid","senha":"novasenha1"})
check("entra com a nova senha", r.status_code == 302)
c3b = A.app.test_client()
r = c3b.post("/login", data={"email":"reset@teste.invalid","senha":"senha12345"})
check("senha antiga não funciona mais", r.status_code == 401, r.status_code)
c4 = A.app.test_client()
r = c4.get(f"/redefinir-senha/{token}", follow_redirects=False)
check("token é de uso único", r.status_code == 302 and "recuperar-senha" in r.headers.get("Location",""), r.status_code)
r = c4.get("/redefinir-senha/token-falso-123", follow_redirects=False)
check("token inválido é rejeitado", r.status_code == 302)
r = c0.post("/recuperar-senha", data={"email":"naoexiste@teste.invalid"})
check("e-mail inexistente não vaza cadastro", r.headers.get("Location","").endswith("/login"))

print("\n── Exclusão de conta ──")
cd = novo_cliente("delete@teste.invalid")
cd.post("/recibo", data={"passageiro":"M","data":"2026-09-12","origem":"A","destino":"B","valor":"30"})
uid = A.get_store().get_user_by_email("delete@teste.invalid")["_id"]
check("recibo existe antes", len(A.get_store().list_receipts_by_driver(uid)) == 1)
r = cd.post("/excluir-conta", data={"senha":"senha12345","confirmacao":"talvez"})
check("recusa sem a palavra EXCLUIR", A.get_store().get_user_by_email("delete@teste.invalid") is not None)
r = cd.post("/excluir-conta", data={"senha":"senha-errada","confirmacao":"EXCLUIR"})
check("recusa com senha errada", A.get_store().get_user_by_email("delete@teste.invalid") is not None)
r = cd.post("/excluir-conta", data={"senha":"senha12345","confirmacao":"excluir"})
check("aceita 'excluir' minúsculo", A.get_store().get_user_by_email("delete@teste.invalid") is None)
check("recibos foram apagados", len(A.get_store().list_receipts_by_driver(uid)) == 0)
r = cd.get("/dashboard")
check("sessão encerrada após exclusão", r.status_code == 302)

print("\n── Ciclo de vida da assinatura (webhook) ──")
cs = novo_cliente("sub@teste.invalid")
uid = A.get_store().get_user_by_email("sub@teste.invalid")["_id"]
store = A.get_store()
store.update_user(uid, {"plan":"pro","stripe_customer_id":"cus_123","stripe_subscription_id":"sub_1"})
check("acha usuário pelo customer_id", store.get_user_by_stripe_customer("cus_123")["_id"] == uid)

# Sem dubles: assinatura Stripe de verdade, pelo caminho que roda em producao.
r = webhook_stripe(c0, "customer.subscription.updated", {"customer":"cus_123","status":"past_due"})
check("webhook com assinatura válida é aceito", r.status_code == 200, r.status_code)
check("past_due mantém o plano Pro", store.get_user_by_id(uid)["plan"] == "pro", store.get_user_by_id(uid)["plan"])
webhook_stripe(c0, "customer.subscription.updated", {"customer":"cus_123","status":"unpaid"})
check("unpaid rebaixa para free", store.get_user_by_id(uid)["plan"] == "free", store.get_user_by_id(uid)["plan"])
store.update_user(uid, {"plan":"business"})
webhook_stripe(c0, "customer.subscription.deleted", {"customer":"cus_123","status":"canceled"})
u = store.get_user_by_id(uid)
check("cancelamento rebaixa para free", u["plan"] == "free", u["plan"])
check("limpa o id da assinatura", u.get("stripe_subscription_id") is None, u.get("stripe_subscription_id"))
check("registra o status", u.get("subscription_status") == "canceled", u.get("subscription_status"))

r = c0.post("/webhook/stripe", data='{"type":"x","data":{"object":{}}}',
            content_type="application/json", headers={"Stripe-Signature": "t=1,v1=forjada"})
check("webhook com assinatura forjada é recusado", r.status_code == 400, r.status_code)
r = c0.post("/webhook/stripe", data="{}", content_type="application/json")
check("webhook sem assinatura é recusado", r.status_code == 400, r.status_code)

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
cn = novo_cliente("csp@teste.invalid")
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

print("\n-- Falha explicita sem banco configurado --")
A._STORE = None
_guardado = {k: os.environ.pop(k, "") for k in ("DATABASE_URL", "SUPABASE_DB_URL")}
try:
    A.get_store()
    check("sem banco nenhum, get_store levanta erro", False, "nao levantou")
except RuntimeError as exc:
    check("sem banco nenhum, get_store levanta erro", "SUPABASE_DB_URL" in str(exc), str(exc)[:60])
finally:
    for k, v in _guardado.items():
        if v: os.environ[k] = v
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

cv = novo_cliente("valor@teste.invalid")
r = cv.post("/recibo", data={"passageiro":"M","data":"2026-09-12","origem":"A","destino":"B","valor":"-10"})
check("POST /recibo com valor negativo nao cria recibo", "/recibo/" not in r.headers.get("Location",""))

print("\n-- Trocar a senha derruba as outras sessoes --")
cs1 = novo_cliente("sessao@teste.invalid")
check("sessao 1 logada", cs1.get("/dashboard").status_code == 200)
cs2 = A.app.test_client()
cs2.post("/login", data={"email":"sessao@teste.invalid","senha":"senha12345"})
check("sessao 2 logada", cs2.get("/dashboard").status_code == 200)
u = A.get_store().get_user_by_email("sessao@teste.invalid")
with A.app.test_request_context():
    tok = A.build_reset_token(u)
A.app.test_client().post(f"/redefinir-senha/{tok}", data={"senha":"outrasenha9","confirmar_senha":"outrasenha9"})
check("sessao 1 foi derrubada", cs1.get("/dashboard").status_code == 302)
check("sessao 2 foi derrubada", cs2.get("/dashboard").status_code == 302)
cs3 = A.app.test_client()
check("login com a senha nova funciona",
      cs3.post("/login", data={"email":"sessao@teste.invalid","senha":"outrasenha9"}).status_code == 302)
check("e a sessao nova sobrevive", cs3.get("/dashboard").status_code == 200)

print("\n-- Pagina de planos por plano do usuario --")
def planos_html(plano):
    c = novo_cliente(f"pl{plano}@teste.invalid")
    uid = A.get_store().get_user_by_email(f"pl{plano}@teste.invalid")["_id"]
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

cb = novo_cliente("bizz@teste.invalid")
uidb = A.get_store().get_user_by_email("bizz@teste.invalid")["_id"]
A.get_store().update_user(uidb, {"plan": "business"})
r = cb.post("/assinar/business")
check("checkout de Business e recusado (400)", r.status_code == 400, r.status_code)

# assinante antigo mantem recibos ilimitados
criados = sum(1 for i in range(A.FREE_MONTHLY_LIMIT + 5)
    if "/recibo/" in cb.post("/recibo", data={"passageiro":f"P{i}","data":"2026-09-12",
        "origem":"A","destino":"B","valor":"10"}).headers.get("Location",""))
check("assinante antigo do Business segue ilimitado", criados == A.FREE_MONTHLY_LIMIT + 5, f"criou {criados}")

print("\n-- Header Authorization esquisito nao vira 500 --")
os.environ["CRON_SECRET"] = "segredo-de-teste"
r = c0.get("/tarefas/limpeza", headers={"Authorization": "Bearer caf\u00e9-n\u00e3o-ascii"})
check("responde 401, nao 500", r.status_code == 401, r.status_code)
os.environ.pop("CRON_SECRET", None)

print("\n-- Contato do passageiro nao vaza no link publico --")
cp2 = novo_cliente("vaza@teste.invalid")
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
cl = novo_cliente("tam@teste.invalid")
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
r = cc.post("/cadastro", data={"nome_completo":grande,"email":"x@teste.invalid","senha":"senha12345",
    "whatsapp":"11999998888","cpf":"12345678900","cidade":"SP","placa":"abc1d23"})
check("cadastro recusa nome gigante (400)", r.status_code == 400, r.status_code)
check("conta nao foi criada", A.get_store().get_user_by_email("x@teste.invalid") is None)

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
cpf_ = novo_cliente("perfil@teste.invalid")
r = cpf_.get("/perfil")
check("pagina abre para quem esta logado", r.status_code == 200, r.status_code)
check("vem preenchida com os dados atuais", "perfil@teste.invalid" in r.get_data(as_text=True))
check("exige login", A.app.test_client().get("/perfil").status_code == 302)

base = {"nome_completo":"Ana Souza Lima","email":"perfil@teste.invalid","whatsapp":"11988887777",
        "cpf":"12345678900","cidade":"Santos","placa":"xyz9k88","modelo_veiculo":"Corolla"}

r = cpf_.post("/perfil", data={**base, "senha_atual":"errada"})
check("senha errada nao salva nada", r.status_code == 401, r.status_code)
check("dados continuam os antigos",
      A.get_store().get_user_by_email("perfil@teste.invalid")["city"] == "SP")

r = cpf_.post("/perfil", data={**base, "senha_atual":"senha12345"})
check("salva com a senha certa", r.status_code == 302, r.status_code)
u = A.get_store().get_user_by_email("perfil@teste.invalid")
check("cidade atualizada", u["city"] == "Santos", u["city"])
check("placa normalizada para maiuscula", u["plate"] == "XYZ9K88", u["plate"])
check("nome atualizado", u["full_name"] == "Ana Souza Lima")

# e-mail duplicado
outro = novo_cliente("ocupado@teste.invalid")
r = cpf_.post("/perfil", data={**base, "email":"ocupado@teste.invalid", "senha_atual":"senha12345"})
check("recusa e-mail ja usado por outra conta", r.status_code == 409, r.status_code)

# troca de e-mail valida mantem o login funcionando
r = cpf_.post("/perfil", data={**base, "email":"novo-email@teste.invalid", "senha_atual":"senha12345"})
check("troca de e-mail e aceita", r.status_code == 302, r.status_code)
check("e-mail antigo nao acha mais a conta", A.get_store().get_user_by_email("perfil@teste.invalid") is None)
check("e-mail novo acha a conta", A.get_store().get_user_by_email("novo-email@teste.invalid") is not None)
cnovo = A.app.test_client()
check("login com o e-mail novo funciona",
      cnovo.post("/login", data={"email":"novo-email@teste.invalid","senha":"senha12345"}).status_code == 302)

r = cpf_.post("/perfil", data={**base, "email":"novo-email@teste.invalid", "nome_completo":"A"*5000,
                               "senha_atual":"senha12345"})
check("campo gigante e recusado", r.status_code == 400, r.status_code)


print("\n-- Corrida da cota do plano Gratis --")
if A.get_store().kind == "postgres":
    import concurrent.futures as _cf
    cc = novo_cliente("corrida@teste.invalid")
    uidc = A.get_store().get_user_by_email("corrida@teste.invalid")["_id"]
    # 28 recibos ja usados no mes
    for i in range(A.FREE_MONTHLY_LIMIT - 2):
        A.get_store().create_receipt(
            recibo_bruto(f"C{i:013X}", A.now_iso(), driver_id=uidc, is_guest=False),
            quota_limit=A.FREE_MONTHLY_LIMIT)

    def emitir(i):
        return A.get_store().create_receipt(
            recibo_bruto(f"D{i:013X}", A.now_iso(), driver_id=uidc, is_guest=False),
            quota_limit=A.FREE_MONTHLY_LIMIT) is not None

    with _cf.ThreadPoolExecutor(max_workers=10) as ex:
        res = list(ex.map(emitir, range(10)))
    check(f"10 emissoes simultaneas no teto liberam exatamente 2",
          sum(res) == 2, f"liberou {sum(res)}")
    total = len(A.get_store().list_receipts_by_driver(uidc))
    check(f"total no mes para em {A.FREE_MONTHLY_LIMIT}", total == A.FREE_MONTHLY_LIMIT, total)
else:
    print("  (pulado: so faz sentido no Postgres)")

print("\n-- rid com 14 digitos --")
cr2 = novo_cliente("rid@teste.invalid")
r = cr2.post("/recibo", data={"passageiro":"M","data":"2026-09-12","origem":"A",
                              "destino":"B","valor":"30"})
rid14 = r.headers["Location"].split("/recibo/")[1].split("?")[0]
check("rid tem 14 caracteres", len(rid14) == 14, f"{rid14} ({len(rid14)})")
check("rid e hexadecimal maiusculo", all(c in "0123456789ABCDEF" for c in rid14), rid14)


print("\n-- Envio de e-mail (Resend) --")
_chave = os.environ.get("RESEND_API_KEY", "").strip()
if _chave:
    with A.app.app_context():
        # delivered@resend.dev e o endereco de teste oficial: aceita e descarta.
        check("send_email entrega pelo Resend",
              A.send_email("delivered@resend.dev", "Teste automatizado",
                           "Corpo com acentuacao: acao, coracao."))
        check("remetente definido", "@" in A.remetente_padrao(), A.remetente_padrao())

    # Sem provedor nenhum, nao pode estourar — so registrar e devolver False.
    _guard = {k: os.environ.pop(k, "") for k in ("RESEND_API_KEY", "SMTP_HOST")}
    try:
        with A.app.app_context():
            check("sem provedor, degrada em vez de estourar",
                  A.send_email("x@teste.invalid", "s", "b") is False)
    finally:
        for k, v in _guard.items():
            if v: os.environ[k] = v

    # O User-Agent proprio existe porque o Cloudflare do Resend devolve 403
    # "error code: 1010" para o padrao do urllib. Sem ele, quebra em producao.
    _fonte = io.open("app.py", encoding="utf-8").read()
    check("envio manda User-Agent proprio", '"User-Agent"' in _fonte)
else:
    print("  (pulado: RESEND_API_KEY ausente)")


# -- Renovacao de sessao do app ------------------------------------------
# O access token do Supabase vale uma hora. Sem renovar, passada a hora a fila
# do app desistia em silencio: o motorista seguia emitindo recibo que nunca
# subia e o passageiro recebia link que nunca ia funcionar.
print("\n-- Renovacao de sessao do app --")
_capp = A.app.test_client()
_email_app = "renova@teste.invalid"
_r = _capp.post("/api/cadastro", json={
    "nome_completo": "Renova Teste", "email": _email_app, "senha": "senha12345",
    "whatsapp": "(45) 99888-7777", "cpf": "12345678901", "cidade": "Cascavel",
    "placa": "RNV1234"})
check("cadastro pela API devolve refresh_token",
      _r.status_code == 201 and bool(_r.get_json().get("refresh_token")),
      f"{_r.status_code} {str(_r.get_json())[:120]}")

_tokens = _r.get_json() if _r.status_code == 201 else {}
_refresh = _tokens.get("refresh_token", "")

_r = _capp.post("/api/refresh", json={"refresh_token": _refresh})
check("refresh devolve access_token novo",
      _r.status_code == 200 and bool(_r.get_json().get("access_token")),
      f"{_r.status_code} {str(_r.get_json())[:120]}")

_novo = _r.get_json().get("access_token", "") if _r.status_code == 200 else ""
_r = _capp.get("/api/sessao", headers={"Authorization": f"Bearer {_novo}"})
check("token renovado abre a sessao", _r.status_code == 200, str(_r.status_code))

_r = _capp.post("/api/refresh", json={"refresh_token": "invalido"})
check("refresh invalido devolve 401", _r.status_code == 401, str(_r.status_code))

_r = _capp.post("/api/refresh", json={})
check("refresh sem token devolve 401", _r.status_code == 401, str(_r.status_code))

# -- CPF/CNPJ do passageiro ----------------------------------------------
print("\n-- CPF/CNPJ do passageiro --")
for _entrada, _esperado, _porque in [
    ("12345678901",       "123.456.789-01",     "CPF so digitos"),
    ("123.456.789-01",    "123.456.789-01",     "CPF ja pontuado"),
    ("12345678000190",    "12.345.678/0001-90", "CNPJ so digitos"),
    ("12.345.678/0001-90","12.345.678/0001-90", "CNPJ ja pontuado"),
    ("",                  "",                   "vazio, campo e opcional"),
    # Nao validamos digito verificador: recusar aqui trocaria um recibo util
    # por um erro na tela, com o passageiro esperando no carro.
    ("999",               "999",                "tamanho estranho passa como veio"),
    ("  A1B2  ",          "A1B2",               "documento estrangeiro nao quebra"),
]:
    check(f"documento: {_porque}", A.format_document_br(_entrada) == _esperado,
          f"{_entrada!r} -> {A.format_document_br(_entrada)!r}")

# Ida e volta pelo banco, com e sem o campo. O "sem" importa: as versoes do app
# ja instaladas nao enviam documento, e um recibo delas nao pode falhar.
_rid_doc = "DDDDDDDDDDDDDD"
_bruto = recibo_bruto(_rid_doc, A.now_iso())
_bruto["passenger_document"] = "123.456.789-01"
store.create_receipt(_bruto)
_lido = store.get_receipt(_rid_doc)
check("documento sobrevive ao banco",
      (_lido or {}).get("passenger_document") == "123.456.789-01",
      repr((_lido or {}).get("passenger_document")))

_rid_sem = "EEEEEEEEEEEEEE"
store.create_receipt(recibo_bruto(_rid_sem, A.now_iso()))   # sem a chave
_lido2 = store.get_receipt(_rid_sem)
check("recibo de versao antiga do app nao quebra",
      _lido2 is not None and _lido2.get("passenger_document") == "")

# -- Telefone para o wa.me ------------------------------------------------
# Sem codigo do pais o link abre o WhatsApp sem destinatario, e o motorista so
# consegue enviar se ja tiver o passageiro salvo na agenda.
print("\n-- Telefone para o wa.me --")
_casos = [
    ("11987654321",        "5511987654321", "celular com DDD"),
    ("(11) 98765-4321",    "5511987654321", "pontuado"),
    ("011987654321",       "5511987654321", "com zero de interurbano"),
    ("+55 11 98765-4321",  "5511987654321", "ja internacional"),
    ("5511987654321",      "5511987654321", "so digitos, com pais"),
    ("00551198765432",     "551198765432",  "prefixo 00 de discagem"),
    ("1132654321",         "551132654321",  "fixo com DDD"),
    # DDD 55 e de Santa Maria/RS. Classificar por prefixo trataria estes 10
    # digitos como codigo de pais e mandaria para um numero que nao existe.
    ("5598765432",         "555598765432",  "DDD 55 nao e codigo de pais"),
    ("98765432",           "",              "sem DDD, nao da link"),
    ("123",                "",              "curto demais"),
    ("",                   "",              "vazio"),
]
for _entrada, _esperado, _porque in _casos:
    check(f"telefone: {_porque}", A.phone_e164(_entrada) == _esperado,
          f"{_entrada!r} -> {A.phone_e164(_entrada)!r}, esperado {_esperado!r}")

# O link so leva destinatario quando o numero e utilizavel.
_rec = {"rid": "X" * 14, "passenger_whatsapp": "(11) 98765-4321",
        "passenger": "Teste", "driver_snapshot": {"whatsapp": "(44) 99999-1111"}}
_link = A.build_whatsapp_link(_rec, "https://recibotaxi.com.br/r/x")
check("wa.me leva o numero com 55", "wa.me/5511987654321?" in _link, _link[:60])
check("mensagem traz a chamada do motorista",
      "(44) 99999-1111" in A.compose_receipt_message(_rec, "https://x"))

# A mesma mensagem serve ao WhatsApp do site e ao e-mail do recibo. Ela tem de
# dizer o mesmo que a do app: recibo de empresa sem CNPJ e sem razao social nao
# serve para lancar despesa.
_rec_pj = dict(_rec, passenger_document="12.345.678/0001-90",
               passenger_company="Taxi Central Ltda")
_msg_pj = A.compose_receipt_message(_rec_pj, "https://x")
check("a mensagem leva o CNPJ", "12.345.678/0001-90" in _msg_pj)
check("a mensagem leva a razao social", "Taxi Central Ltda" in _msg_pj)
_msg_pf = A.compose_receipt_message(dict(_rec, passenger_document="123.456.789-01"), "https://x")
check("sem razao social, a linha nao aparece", "Razão social" not in _msg_pf)
check("sem documento, a linha nao aparece",
      "CPF/CNPJ" not in A.compose_receipt_message(_rec, "https://x"))
_sem = A.build_whatsapp_link(_rec, "https://x", with_recipient=False)
check("sem destinatario, nao vaza o numero", "5511987654321" not in _sem)

# -- Chave do RevenueCat --------------------------------------------------
# A Test Store (prefixo test_) serve para ensaiar a compra sem a Apple, sem
# contrato e sem cartao. Util no desenvolvimento, desastre se escapar: o app
# iria para a App Store vendendo numa loja de mentira, e ninguem receberia o
# que pagou. Esta checagem existe para que esquecer de trocar de volta doa.
print("\n-- Chave do RevenueCat --")
_cfg = io.open("mobile/www/config.js", encoding="utf-8").read()
_m = _re.search(r"RC_CHAVE_PUBLICA\s*=\s*'([^']*)'", _cfg)
_chave = _m.group(1) if _m else ""
check("config.js nao carrega chave da Test Store",
      not _chave.startswith("test_"),
      f"achou {_chave[:9]}... — troque pela appl_ antes de commitar")

# O bundle iOS e uma copia: se o sync nao rodou, o aparelho testa codigo velho.
_ios = "mobile/ios/App/App/public/config.js"
if os.path.exists(_ios):
    check("bundle iOS esta sincronizado com o www",
          io.open(_ios, encoding="utf-8").read() == _cfg,
          "rode: npx cap sync ios")


# -- Limite do plano Gratis nos textos do site ---------------------------
# O numero vive em FREE_MONTHLY_LIMIT e os templates leem de la. Um "5"
# escrito a mao num template voltaria a divergir na primeira mudanca de plano
# — e foi assim que o site prometia 5 enquanto a fase de testes libera 100.
print("\n-- Limite do plano Gratis nos textos do site --")
_pub = A.app.test_client()
for _rota in ("/", "/planos", "/termos"):
    _html = _pub.get(_rota).get_data(as_text=True)
    check(f"{_rota} mostra 'Ate {A.FREE_MONTHLY_LIMIT} recibos'",
          f"Até {A.FREE_MONTHLY_LIMIT} recibos" in _html)
import glob as _glob
_fixos = [f for f in _glob.glob("templates/*.html")
          if _re.search(r"\b5 recibos", io.open(f, encoding="utf-8").read())]
check("nenhum template com o limite escrito a mao", not _fixos, ", ".join(_fixos))

# -- PDF gerado no aparelho, com a marca ---------------------------------
# pdf.js escreve o arquivo a mao, deslocamento por deslocamento. Imagem
# embutida e o jeito mais facil de errar a tabela xref sem perceber: o leitor
# do iPhone ainda abre, outros reclamam. Gera um PDF de verdade com o node e
# confere cada deslocamento.
print("\n-- PDF do aparelho --")
import subprocess as _sp
_js = r"""
global.window = global;
require(process.cwd() + '/mobile/www/marca.js');
const { pdfDoRecibo } = require(process.cwd() + '/mobile/www/pdf.js');
process.stdout.write(pdfDoRecibo({
  rid: 'ABCDEF01234567',
  motorista: { full_name: 'M', plate: 'ABC1D23', whatsapp: '45999990000' },
  dados: { passageiro: 'P', data: '2026-09-14', data_exibida: '14/09/2026', hora: '10:00',
           origem: 'A', destino: 'B', valor_exibido: '10,00', forma_pagamento: 'Pix', observacoes: '' },
}));
"""
_saida = None
try:
    _saida = _sp.run(["node", "-e", _js], capture_output=True, text=True, timeout=60,
                     cwd=os.path.dirname(os.path.abspath(__file__)))
    _pdf = base64.b64decode(_saida.stdout) if _saida.returncode == 0 else b""
except Exception:
    _pdf = b""
check("node gera o PDF", bool(_pdf), (_saida.stderr[:200] if _saida else "node ausente"))
if _pdf:
    _ini = int(_re.search(rb"startxref\s+(\d+)", _pdf).group(1))
    _tab = _pdf[_ini:].split(b"\n")
    _n = int(_tab[1].split()[1])
    _ruins = [i for i in range(1, _n)
              if not _pdf[int(_tab[2 + i].split()[0]):].startswith(f"{i} 0 obj".encode())]
    check("todos os deslocamentos do xref batem", not _ruins, str(_ruins))
    check("a marca esta embutida como JPEG", b"/Filter/DCTDecode" in _pdf and b"/Im1 Do" in _pdf)
    _m = _re.search(rb"/Subtype/Image[^>]*?/Length (\d+)>>\nstream\n", _pdf)
    _fim = _m.end() + int(_m.group(1))
    check("o tamanho declarado da imagem confere", _pdf[_fim:_fim + 10] == b"\nendstream")

# -- Razao social do passageiro ------------------------------------------
# CNPJ sozinho nao serve: a contabilidade da empresa precisa do nome da pessoa
# juridica no recibo. Com CPF o campo nao existe, e a regra mora no servidor
# porque o gerador do site nao tem JavaScript para esconder nada.
print("\n-- Razao social do passageiro --")
for _doc, _valor, _esperado, _porque in [
    ("12.345.678/0001-90", "Taxi Central Ltda", "Taxi Central Ltda", "CNPJ pontuado"),
    ("12345678000190",     "Taxi Central Ltda", "Taxi Central Ltda", "CNPJ so digitos"),
    ("12345678000190",     "  Taxi   Central  ", "Taxi Central",     "espaco sobrando some"),
    ("123.456.789-01",     "Taxi Central Ltda", "",                  "CPF nao leva razao social"),
    ("12345678901",        "Taxi Central Ltda", "",                  "CPF so digitos idem"),
    ("",                   "Taxi Central Ltda", "",                  "sem documento, sem razao"),
    ("12345678000190",     "",                  "",                  "CNPJ sem razao fica vazio"),
]:
    check(f"razao_social_de: {_porque}", A.razao_social_de(_doc, _valor) == _esperado,
          repr(A.razao_social_de(_doc, _valor)))

if A.get_store().kind == "postgres":
    _crz = A.app.test_client()
    _r = _crz.post("/api/cadastro", json={
        "nome_completo": "Razao Teste", "email": "razao@teste.invalid", "senha": "senha12345",
        "whatsapp": "45998238620", "cpf": "12345678901", "cidade": "Cascavel", "placa": "RZS1234"})
    _tkz = _r.get_json().get("access_token", "") if _r.status_code == 201 else ""
    _cabz = {"Authorization": f"Bearer {_tkz}"}

    def _emitir_doc(rid, doc, razao):
        return _crz.post("/api/recibos", headers=_cabz, json={
            "rid": rid, "passageiro": "Empresa", "data": "2026-09-14", "origem": "A",
            "destino": "B", "valor": "10", "forma_pagamento": "Pix",
            "documento_passageiro": doc, "razao_social": razao})

    _r = _emitir_doc("DA" + "0" * 12, "12345678000190", "Taxi Central Ltda")
    check("recibo com CNPJ grava a razao social", _r.status_code == 201, str(_r.status_code))
    _guardado = A.get_store().get_receipt("DA" + "0" * 12)
    check("a razao social volta do banco",
          (_guardado or {}).get("passenger_company") == "Taxi Central Ltda",
          str((_guardado or {}).get("passenger_company")))
    check("o CNPJ tambem foi formatado",
          (_guardado or {}).get("passenger_document") == "12.345.678/0001-90",
          str((_guardado or {}).get("passenger_document")))

    _r = _emitir_doc("DB" + "1" * 12, "12345678901", "Taxi Central Ltda")
    _guardado = A.get_store().get_receipt("DB" + "1" * 12)
    check("com CPF o servidor descarta a razao social",
          (_guardado or {}).get("passenger_company") == "",
          str((_guardado or {}).get("passenger_company")))

    # App antigo, ja instalado no aparelho, nao manda o campo novo.
    _r = _crz.post("/api/recibos", headers=_cabz, json={
        "rid": "DC" + "2" * 12, "passageiro": "Sem campo novo", "data": "2026-09-14",
        "origem": "A", "destino": "B", "valor": "10", "forma_pagamento": "Pix"})
    check("versao antiga do app, sem o campo, continua gravando",
          _r.status_code == 201, f"{_r.status_code} {str(_r.get_json())[:90]}")

    # A pagina publica mostra os dois.
    _html = A.app.test_client().get("/recibo/DA000000000000").get_data(as_text=True)
    check("a pagina do recibo mostra a razao social", "Taxi Central Ltda" in _html)
    check("a pagina do recibo mostra o CNPJ", "12.345.678/0001-90" in _html)

    # Campo gigante e recusado, nao truncado.
    _r = _emitir_doc("DD" + "3" * 12, "12345678000190", "E" * 500)
    check("razao social gigante e recusada", _r.status_code == 400, str(_r.status_code))
else:
    print("  (pulado: precisa do Postgres)")

# -- Recibo por e-mail ----------------------------------------------------
# O campo de e-mail do passageiro existia no site e nao servia para nada: era
# guardado e esquecido. Agora o recibo sai por e-mail na hora em que chega ao
# servidor. Nenhum teste aqui manda e-mail de verdade — send_email e trocado
# por um contador.
print("\n-- Recibo por e-mail --")

for _v, _ok in [
    ("joao@exemplo.com.br", True),
    ("  Joao@Exemplo.com.br  ", True),
    ("joao@exemplo", False),          # dominio sem ponto
    ("joao@@exemplo.com", False),
    ("@exemplo.com", False),
    ("joao exemplo@x.com", False),    # espaco no meio
    ("joao@.com", False),
    ("", False),
    ("a" * 250 + "@x.com", False),    # passa de 254
]:
    check(f"email_valido({_v.strip()[:26]!r}) = {_ok}", A.email_valido(_v) is _ok)

_enviados = []
_send_real = A.send_email
A.send_email = lambda para, assunto, corpo, timeout=15: (
    _enviados.append({"para": para, "assunto": assunto, "corpo": corpo}) or True)
try:
    _cmail = A.app.test_client()
    _r = _cmail.post("/api/cadastro", json={
        "nome_completo": "Correio Teste", "email": "correio@teste.invalid",
        "senha": "senha12345", "whatsapp": "(45) 99888-7777", "cpf": "12345678901",
        "cidade": "Cascavel", "placa": "EML1234"})
    _tk = _r.get_json().get("access_token", "") if _r.status_code == 201 else ""
    _cab = {"Authorization": f"Bearer {_tk}"}

    def _emitir(rid, email=""):
        return _cmail.post("/api/recibos", headers=_cab, json={
            "rid": rid, "passageiro": "Passageiro", "data": "2026-09-14",
            "origem": "A", "destino": "B", "valor": "10", "forma_pagamento": "Pix",
            "email_passageiro": email})

    # Emitir NAO envia. Preencher o campo e uma coisa, mandar e outra — e cada
    # envio custa. Quem decide e o motorista, tocando no botao.
    _r = _emitir("E1" + "0" * 12, "passageiro@exemplo.com.br")
    check("emitir com e-mail NAO envia nada",
          _r.status_code == 201 and not _enviados, f"{_r.status_code} enviados={len(_enviados)}")
    check("a resposta da emissao nao fala de e-mail",
          "email_enviado" not in (_r.get_json() or {}), str(_r.get_json())[:90])

    _r = _emitir("E2" + "0" * 12)
    check("emitir sem e-mail tambem nao envia", _r.status_code == 201 and not _enviados)

    _r = _cmail.post("/api/recibos/E1000000000000/email", headers=_cab)
    check("o botao do motorista envia",
          _r.status_code == 200 and len(_enviados) == 1,
          f"{_r.status_code} {str(_r.get_json())[:80]}")
    if _enviados:
        check("o e-mail leva o link do recibo", "E1000000000000" in _enviados[0]["corpo"])
        check("o assunto nomeia o recibo", "E1000000000000" in _enviados[0]["assunto"])
        check("vai para o endereco informado",
              _enviados[0]["para"] == "passageiro@exemplo.com.br", _enviados[0]["para"])

    # Tocar duas vezes manda duas vezes: e o motorista pedindo, nao retentativa.
    _r = _cmail.post("/api/recibos/E1000000000000/email", headers=_cab)
    check("pedir de novo envia de novo", _r.status_code == 200 and len(_enviados) == 2)

    # A fila do aparelho reenvia o mesmo rid ate ter certeza de que chegou.
    # Nenhuma dessas retentativas pode virar e-mail para o passageiro.
    _antes = len(_enviados)
    _r = _emitir("E1" + "0" * 12, "passageiro@exemplo.com.br")
    check("retentativa da fila nao manda e-mail",
          _r.status_code == 200 and len(_enviados) == _antes, f"{_r.status_code}")

    _r = _cmail.post("/api/recibos/E2000000000000/email", headers=_cab)
    check("envio sem e-mail no recibo devolve 400", _r.status_code == 400, str(_r.status_code))

    _r = _cmail.post("/api/recibos/FFFFFFFFFFFFFF/email", headers=_cab)
    check("envio de recibo inexistente devolve 404", _r.status_code == 404, str(_r.status_code))

    # Recibo de outro motorista tem de responder 404, e nao 403: 403 confirmaria
    # a um estranho que aquele codigo existe.
    _outro = A.app.test_client()
    _r = _outro.post("/api/cadastro", json={
        "nome_completo": "Outro Motorista", "email": "outro-email@teste.invalid",
        "senha": "senha12345", "whatsapp": "(45) 99777-6666", "cpf": "98765432100",
        "cidade": "Cascavel", "placa": "OTR1234"})
    _tk2 = _r.get_json().get("access_token", "") if _r.status_code == 201 else ""
    _r = _outro.post("/api/recibos/E1000000000000/email",
                     headers={"Authorization": f"Bearer {_tk2}"})
    check("recibo de outro motorista responde 404", _r.status_code == 404, str(_r.status_code))

    _r = _cmail.post("/api/recibos/E1000000000000/email")
    check("reenvio sem sessao devolve 401", _r.status_code == 401, str(_r.status_code))

    # Teto diario: protege a reputacao do dominio. Emitir continua funcionando.
    _teto = A.EMAIL_DAILY_LIMIT
    A.EMAIL_DAILY_LIMIT = 1
    try:
        _uid = A.get_store().get_user_by_email("correio@teste.invalid")["_id"]
        A.get_store().bump_counter(f"email:{_uid}:{A.today_br()}")   # ja no teto
        _r = _cmail.post("/api/recibos/E1000000000000/email", headers=_cab)
        check("no teto diario, o envio devolve 429", _r.status_code == 429, str(_r.status_code))
        _antes = len(_enviados)
        _r = _emitir("E3" + "0" * 12, "outro@exemplo.com.br")
        check("no teto diario, emitir recibo continua funcionando",
              _r.status_code == 201 and len(_enviados) == _antes, f"{_r.status_code}")
    finally:
        A.EMAIL_DAILY_LIMIT = _teto

    # Provedor fora do ar: resposta clara, e nenhuma excecao vazando.
    A.send_email = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provedor caiu"))
    _r = _cmail.post("/api/recibos/E1000000000000/email", headers=_cab)
    check("provedor fora do ar devolve 502, sem estourar",
          _r.status_code == 502, f"{_r.status_code} {str(_r.get_json())[:80]}")
    _r = _emitir("E4" + "0" * 12, "passageiro@exemplo.com.br")
    check("provedor fora do ar nao atrapalha emitir recibo", _r.status_code == 201,
          str(_r.status_code))
finally:
    A.send_email = _send_real

# -- Exclusao de conta pelo app ------------------------------------------
# As duas lojas exigem que quem cria conta DENTRO do app consiga apagar dentro
# do app: Apple na diretriz 5.1.1(v), Google na politica de exclusao de dados.
# Mandar o motorista para o site nao cumpre. Faltava, e so nao deu rejeicao
# porque o app nunca passou por revisao completa — TestFlight nao revisa.
print("\n-- Exclusao de conta pelo app --")
_cdel = A.app.test_client()
_r = _cdel.post("/api/cadastro", json={
    "nome_completo": "Some Teste", "email": "some@teste.invalid", "senha": "senha12345",
    "whatsapp": "45998238620", "cpf": "12345678901", "cidade": "Cascavel", "placa": "DEL1234"})
_tkd = _r.get_json().get("access_token", "") if _r.status_code == 201 else ""
_cabd = {"Authorization": f"Bearer {_tkd}"}

_r = _cdel.post("/api/excluir-conta", json={"senha": "senha12345"})
check("excluir sem sessao devolve 401", _r.status_code == 401, str(_r.status_code))

_r = _cdel.post("/api/excluir-conta", headers=_cabd, json={"senha": "errada"})
check("senha errada nao exclui", _r.status_code == 403, str(_r.status_code))
check("a conta continua de pe depois da senha errada",
      A.get_store().get_user_by_email("some@teste.invalid") is not None)

# Assinante pela loja: so a loja para a cobranca. Apagar aqui deixaria o
# motorista pagando sem ter como cancelar.
_uid_del = A.get_store().get_user_by_email("some@teste.invalid")["_id"]
A.get_store().update_user(_uid_del, {"plan": "pro"})
_r = _cdel.post("/api/excluir-conta", headers=_cabd, json={"senha": "senha12345"})
check("assinante pela loja e barrado, com motivo",
      _r.status_code == 409 and (_r.get_json() or {}).get("erro") == "assinatura_na_loja",
      f"{_r.status_code} {str(_r.get_json())[:80]}")
check("e a conta continua existindo",
      A.get_store().get_user_by_email("some@teste.invalid") is not None)

A.get_store().update_user(_uid_del, {"plan": "free"})
_r = _cdel.post("/api/excluir-conta", headers=_cabd, json={"senha": "senha12345"})
check("com a senha certa, exclui", _r.status_code == 200, f"{_r.status_code} {str(_r.get_json())[:80]}")
check("a conta sumiu do banco",
      A.get_store().get_user_by_email("some@teste.invalid") is None)
_r = _cdel.get("/api/sessao", headers=_cabd)
check("o token antigo nao abre mais a sessao", _r.status_code == 401, str(_r.status_code))

# A tela existe no app, com senha e confirmacao.
_html_app = io.open("mobile/www/index.html", encoding="utf-8").read()
check("o app tem tela de excluir conta", 'id="tela-excluir"' in _html_app)
check("a tela pede senha e a palavra EXCLUIR",
      'id="senha-exclusao"' in _html_app and 'id="confirmacao-exclusao"' in _html_app)
check("o app linka a Politica de Privacidade", "/privacidade" in _html_app)
check("o app linka os Termos de Uso", "/termos" in _html_app)

# A pagina publica do recibo nao pode ser indexada: mostra nome e CPF.
_html_rec = A.app.test_client().get("/recibo/E9CB5C97A3C444").get_data(as_text=True)
check("a pagina do recibo pede noindex", 'content="noindex, nofollow"' in _html_rec)
check("a home continua indexavel",
      "noindex" not in A.app.test_client().get("/").get_data(as_text=True))

# -- Lixo dentro do pacote do app ----------------------------------------
# O iCloud duplica arquivo dentro da pasta sincronizada: "index.html" vira
# tambem "index 2.html". O `cap sync` so sobrescreve os arquivos que conhece,
# entao a copia sobrevive, e o Xcode empacota TUDO que esta na pasta public.
# Foi assim que um "index 2.html" com uma sonda de teste dentro subiu para o
# TestFlight nos builds 6 a 10. O app carrega index.html e nunca executou a
# sonda, mas codigo de teste nao viaja junto com o app, ponto.
print("\n-- Lixo dentro do pacote do app --")
import glob as _g2
_pastas = ["mobile/www", "mobile/ios/App/App", "mobile/android/app/src"]
_dupes = []
for _p in _pastas:
    _dupes += [f for f in _g2.glob(f"{_p}/**/*", recursive=True)
               if _re.search(r" \d+\.[A-Za-z0-9]+$", f)]
check("nenhuma copia duplicada no pacote", not _dupes,
      ", ".join(_dupes[:3]) + " — apague; o iCloud as recria")

_sondas = []
for _p in _pastas:
    for _f in _g2.glob(f"{_p}/**/*", recursive=True):
        if not os.path.isfile(_f) or os.path.getsize(_f) > 2_000_000:
            continue
        try:
            if "SONDA" in io.open(_f, encoding="utf-8", errors="ignore").read():
                _sondas.append(_f)
        except Exception:
            pass
check("nenhuma sonda de teste no pacote", not _sondas, ", ".join(_sondas[:3]))

# A chave que assina o Android nao pode entrar no repositorio.
_ign = io.open("mobile/android/.gitignore", encoding="utf-8").read()
check("o .gitignore do Android barra a chave de assinatura",
      "\n*.jks" in _ign and "\n*.keystore" in _ign,
      "descomente as linhas *.jks e *.keystore")

_depois = limpar_contas_de_teste()
print(f"\n  (limpeza final: {_depois} conta(s) de teste removida(s))")

print("\n" + ("="*50))
print(f"FALHAS: {len(fails)}" + ("" if not fails else " -> " + ", ".join(fails)))
sys.exit(1 if fails else 0)
