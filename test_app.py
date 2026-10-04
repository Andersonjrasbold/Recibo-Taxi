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
            # Recibos sem conta montados a mao pela suite usam 'Teste' como
            # passageiro (o gerador publico, que gravava 'Ze', foi desligado).
            conn.execute("""
                delete from public.receipts
                 where is_guest and passenger = 'Teste'
            """)
            conn.execute("delete from public.rate_limits where key like 'teste:%%'")
            # Painel: admins de teste e tentativas de login da suite.
            conn.execute("delete from public.admin_users where email like '%%@teste.invalid'")
            conn.execute("delete from public.rate_limits where key like 'admin_login:%%'")
            # Automacao de e-mails: modelos da suite que sobraram de uma execucao
            # interrompida. Os envios das contas de teste ja sairam no cascade.
            try:
                with conn.transaction():
                    conn.execute("""delete from public.email_sends where template_id in (
                                      select id from public.email_templates where name like 'teste-suite-%%')""")
                    conn.execute("delete from public.email_templates where name like 'teste-suite-%%'")
            except A.psycopg.errors.UndefinedTable:
                pass  # migracao 0007 ainda nao aplicada
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
for path in ["/privacidade", "/termos", "/recuperar-senha", "/excluir-conta"]:
    r = c0.get(path); check(f"GET {path}", r.status_code == 200, r.status_code)
r = c0.get("/login")
check("login mostra 'Esqueci minha senha'", "Esqueci minha senha" in r.get_data(as_text=True))
r = c0.get("/")
html = r.get_data(as_text=True)
check("rodapé linka Privacidade", "/privacidade" in html)
check("rodapé linka Termos", "/termos" in html)
check("home linka a App Store", A.APP_STORE_URL in html)
check("home linka o Google Play", A.PLAY_STORE_URL in html)
check("home não diz mais 'Em breve no Google Play'", "Em breve no Google Play" not in html)

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
# E-mails e IP sorteados: o teto de pedidos e por dia, e um valor fixo herdaria
# o contador da execucao anterior no mesmo dia.
_sufixo = os.urandom(3).hex()
r = c0.post("/recuperar-senha", data={"email":f"naoexiste-{_sufixo}@teste.invalid"})
check("e-mail inexistente não vaza cadastro", r.headers.get("Location","").endswith("/login"))

print("\n── Recuperar senha pelo app ──")
_cr = A.app.test_client()
_ip = f"203.0.113.{int(_sufixo[:2], 16) % 200 + 20}"
r = _cr.post("/api/recuperar-senha", json={"email": f"naoexiste-{_sufixo}@teste.invalid"},
             environ_base={"REMOTE_ADDR": _ip})
check("API: e-mail inexistente responde 200 sem vazar cadastro",
      r.status_code == 200 and r.get_json().get("enviado") is True, f"{r.status_code} {r.get_json()}")
r = _cr.post("/api/recuperar-senha", json={"email": "sem-arroba"}, environ_base={"REMOTE_ADDR": _ip})
check("API: e-mail inválido devolve 400", r.status_code == 400, r.status_code)
r = _cr.post("/api/recuperar-senha", json={}, environ_base={"REMOTE_ADDR": _ip})
check("API: corpo vazio devolve 400", r.status_code == 400, r.status_code)
_alvo = f"teto-{_sufixo}@teste.invalid"
_oks = sum(1 for _ in range(A.RESET_DAILY_LIMIT_EMAIL)
           if _cr.post("/api/recuperar-senha", json={"email": _alvo},
                       environ_base={"REMOTE_ADDR": _ip}).status_code == 200)
check(f"API: permite {A.RESET_DAILY_LIMIT_EMAIL} pedidos por e-mail/dia",
      _oks == A.RESET_DAILY_LIMIT_EMAIL, f"passaram {_oks}")
r = _cr.post("/api/recuperar-senha", json={"email": _alvo}, environ_base={"REMOTE_ADDR": _ip})
check("API: o seguinte responde 429", r.status_code == 429 and r.get_json().get("erro") == "muitos_pedidos",
      f"{r.status_code} {r.get_json()}")
r = _cr.post("/api/recuperar-senha", json={"email": f"outro-{_sufixo}@teste.invalid"},
             environ_base={"REMOTE_ADDR": _ip})
check("API: outro e-mail no mesmo IP tem cota própria", r.status_code == 200, r.status_code)
r = c0.post("/recuperar-senha", data={"email": _alvo})
check("site: mesmo e-mail estourado também responde 429", r.status_code == 429, r.status_code)

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


print("\n-- Gerador publico desligado: emitir exige conta --")
cg = A.app.test_client()
r = cg.get("/gerar")
check("GET /gerar sem sessao vai para o login", r.status_code == 302 and "/login" in r.headers.get("Location", ""),
      f"{r.status_code} {r.headers.get('Location')}")
r = cg.post("/gerar", data={"passageiro":"P","data":"2026-09-12","origem":"A","destino":"B",
                            "valor":"20","nome_motorista":"Ze"}, environ_base={"REMOTE_ADDR": "203.0.113.7"})
check("POST /gerar nao emite mais nada (405)", r.status_code == 405, r.status_code)
cgl = novo_cliente("gerar@teste.invalid")
r = cgl.get("/gerar")
check("GET /gerar logado vai para o painel", r.status_code == 302 and "/dashboard" in r.headers.get("Location", ""),
      f"{r.status_code} {r.headers.get('Location')}")

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
check("free ve o plano anual a R$ 119,90, o mesmo preco das lojas", "R$ 119,90/ano" in h and "economize R$ 118,90" in h)
# O anual era R$ 119,40 ate 2026-10-03. Preco velho esquecido numa pagina e
# promessa que o checkout nao cumpre.
for _pag in ("/", "/planos", "/termos"):
    check(f"{_pag} nao cita o preco antigo do anual",
          "119,40" not in A.app.test_client().get(_pag).get_data(as_text=True))
# -- Card proprio do anual ------------------------------------------------
# O valor por mes e o destaque, mas o total cobrado tem de estar no mesmo
# card: R$ 9,99 sozinho faria o motorista achar que paga isso todo mes. E o
# botao so vende pelo site quando o Price anual existe; sem ele, leva ao app.
def _card(html, plano):
    partes = html.split(f'data-plano="{plano}"', 1)
    return partes[1].split("data-plano=", 1)[0].split("Perguntas frequentes", 1)[0] if len(partes) == 2 else ""

_anual = _card(h, "anual")
check("o anual tem card proprio, com o valor por mes em destaque",
      'pricing-amount">9<' in _anual and ",99<" in _anual)
check("o card do anual mostra o total cobrado no ano",
      "R$ 119,90/ano" in _anual and "pagos de uma vez" in _anual)
check("o card do mensal nao repete o anual", "119,90" not in _card(h, "mensal"))
_home = A.app.test_client().get("/").get_data(as_text=True)
check("a home tambem tem o card do anual", 'pricing-amount">9<' in _card(_home, "anual"))
_c_anual = novo_cliente("cardanual@teste.invalid")
_preco_anual = os.environ.pop("STRIPE_PRO_ANUAL_PRICE_ID", None)
try:
    check("sem o preco anual no site, o card da home leva ao app",
          'href="/baixar"' in _card(A.app.test_client().get("/").get_data(as_text=True), "anual"))
    check("sem o preco anual no site, o card de /planos leva ao app",
          'href="/baixar"' in _card(_c_anual.get("/planos").get_data(as_text=True), "anual"))
finally:
    if _preco_anual is not None:
        os.environ["STRIPE_PRO_ANUAL_PRICE_ID"] = _preco_anual
if _preco_anual and os.environ.get("STRIPE_SECRET_KEY"):
    check("com o preco anual no site, o card vende pelo site",
          "/assinar/pro_anual" in _card(_c_anual.get("/planos").get_data(as_text=True), "anual"))
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
# pro_anual e uma chave valida de checkout. Sem Stripe no .env a rota volta
# para /planos (302); com as chaves de teste ela manda para o checkout da
# Stripe (303). Os dois provam a rota; so 400 seria chave desconhecida.
r = cb.post("/assinar/pro_anual")
check("checkout do Pro anual e uma rota valida (nao 400)", r.status_code in (302, 303), r.status_code)
check("plano_base reduz o anual ao Pro", A.plano_base("pro_anual") == "pro" and A.plano_base("pro") == "pro")

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

# -- Webhook do RevenueCat: o header ------------------------------------
# O painel manda o "Authorization header value" como foi colado. No primeiro
# evento real de producao veio sem "Bearer " e o servidor respondeu 401 — a
# compra aconteceu e o plano nao virou. As duas formas tem de valer.
print("\n-- Webhook do RevenueCat: header --")
_seg_antigo = os.environ.get("REVENUECAT_WEBHOOK_SECRET")
os.environ["REVENUECAT_WEBHOOK_SECRET"] = "segredo-de-teste-123"
_wc = A.app.test_client()
def _rc(auth):
    kw = {"data": '{"event":{"type":"TEST"}}', "content_type": "application/json"}
    if auth is not None: kw["headers"] = {"Authorization": auth}
    return _wc.post("/webhook/revenuecat", **kw).status_code
check("aceita 'Bearer <segredo>'", _rc("Bearer segredo-de-teste-123") == 200)
check("aceita o segredo sem Bearer", _rc("segredo-de-teste-123") == 200)
check("aceita espaco sobrando nas pontas", _rc("  Bearer segredo-de-teste-123  ") == 200)
check("recusa segredo errado", _rc("Bearer outro") == 401)
check("recusa sem header", _rc(None) == 401)
check("recusa segredo parcial", _rc("segredo-de-teste") == 401)
if _seg_antigo is None: del os.environ["REVENUECAT_WEBHOOK_SECRET"]
else: os.environ["REVENUECAT_WEBHOOK_SECRET"] = _seg_antigo

# -- Chave do RevenueCat --------------------------------------------------
# A Test Store (prefixo test_) serve para ensaiar a compra sem a Apple, sem
# contrato e sem cartao. Util no desenvolvimento, desastre se escapar: o app
# iria para a App Store vendendo numa loja de mentira, e ninguem receberia o
# que pagou. Esta checagem existe para que esquecer de trocar de volta doa.
print("\n-- Chave do RevenueCat --")
_cfg = io.open("mobile/www/config.js", encoding="utf-8").read()
_chaves = dict(_re.findall(r"(ios|android)\s*:\s*'([^']*)'", _cfg))
check("config.js declara uma chave por loja",
      set(_chaves) == {"ios", "android"},
      f"achou {sorted(_chaves)} — esperado ios e android")
for _loja, _chave in sorted(_chaves.items()):
    check(f"chave {_loja} nao e da Test Store",
          not _chave.startswith("test_"),
          f"achou {_chave[:9]}... — troque pela chave de producao antes de commitar")
check("a chave do iOS e uma chave da App Store",
      _chaves.get("ios", "").startswith("appl_"),
      f"achou {_chaves.get('ios', '')[:9]}...")
# A do Android nasce vazia e so e preenchida quando a conta do Play existir.
# Vazia passa; errada, nao: goog_ e o unico prefixo que o RevenueCat aceita la.
_and = _chaves.get("android", "")
check("a chave do Android esta vazia ou e uma chave do Google Play",
      _and == "" or _and.startswith("goog_"),
      f"achou {_and[:9]}...")

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
# porque o servidor nao confia no JavaScript do app para esconder nada.
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

# -- Botao voltar do Android ---------------------------------------------
# Sem ninguem escutando o evento, o Capacitor so tenta voltar no historico do
# WebView — que neste app nao existe. O voltar nao faria nada, e o motorista
# leria isso como aparelho travado.
print("\n-- Botao voltar do Android --")
_appjs = io.open("mobile/www/app.js", encoding="utf-8").read()
check("o app escuta o botao voltar", "'backButton'" in _appjs)
check("o voltar fecha o menu do perfil antes de sair",
      "voltarUmNivel" in _appjs and "fecharMenu()" in _appjs)
check("so sai do app na tela inicial", "exitApp()" in _appjs)
check("o app linka os Termos de Uso", "/termos" in _html_app)

# -- Tela de assinatura: mensal e anual ----------------------------------
# O preco vem da loja (a Apple e o Google cobram valores diferentes no anual);
# o pacote comprado e o que esta marcado, pelo identificador; e quem ja assina
# nao ve outra compra — no Google Play mensal e anual sao assinaturas
# separadas, e comprar a outra cobraria as duas.
print("\n-- Tela de assinatura --")
_tela_pro = _html_app.split('id="tela-assinatura"', 1)[1].split("</section>", 1)[0]
check("a tela oferece o anual e o mensal",
      'id="plano-anual"' in _tela_pro and 'id="plano-mensal"' in _tela_pro)
check("o app compra pelo identificador do pacote",
      "'$rc_annual'" in _appjs and "'$rc_monthly'" in _appjs and "pacotesPro[planoEscolhido]" in _appjs)
check("nenhum preco do anual escrito no app",
      not _re.search(r"119[,.]\d", _html_app + _appjs))
check("o preco do anual vem da loja", "anual.priceString" in _appjs)
check("quem ja assina nao ve outra compra",
      "mostrarAssinaturaAtiva" in _appjs and "entitlements?.active?.pro" in _appjs)
check("o aviso de renovacao cita o anual", "mensal ou anual" in _tela_pro)
check("Termos e Privacidade na propria tela de assinatura (3.1.2)",
      "/termos" in _tela_pro and "/privacidade" in _tela_pro)
_pbx = io.open("mobile/ios/App/App.xcodeproj/project.pbxproj", encoding="utf-8").read()
_gradle = io.open("mobile/android/app/build.gradle", encoding="utf-8").read()
_ver_ios = set(_re.findall(r"MARKETING_VERSION = ([0-9.]+);", _pbx))
_ver_and = _re.search(r'versionName "([0-9.]+)"', _gradle)
check("iPhone e Android com o mesmo numero de versao",
      len(_ver_ios) == 1 and _ver_and and _ver_ios == {_ver_and.group(1)},
      f"ios={_ver_ios} android={_ver_and.group(1) if _ver_and else None}")

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

# O iCloud tambem duplica DENTRO do .git: um "refs/remotes/origin/main 2"
# quebrou o fetch com "bad object" em 2026-09-15. Git nao avisa; so falha.
_dupes_git = [f for f in _g2.glob(".git/refs/**/*", recursive=True)
              if os.path.isfile(f) and _re.search(r" \d+$", f)]
check("nenhuma copia duplicada dentro do .git", not _dupes_git,
      ", ".join(_dupes_git[:3]) + " — apague; quebra o fetch")

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

# -- Painel administrativo (/admin) ---------------------------------------
# Conta separada da de motorista; a conta de teste e criada direto no store,
# porque nao existe cadastro de admin pelo site, e apagada no fim.
print("\n-- Painel administrativo --")
import re as _re_adm
from werkzeug.security import generate_password_hash as _gph
_sufixo_adm = os.urandom(3).hex()
_email_adm = f"admin-{_sufixo_adm}@teste.invalid"
_ip_adm = f"203.0.113.{int(_sufixo_adm[:2], 16) % 200 + 20}"
_adm = A.get_store().upsert_admin(_email_adm, _gph("1234"))
_ca = A.app.test_client()
def _adm_post(cli, path, **data):
    return cli.post(path, data=data, environ_base={"REMOTE_ADDR": _ip_adm})

r = _ca.get("/admin")
check("sem sessao, /admin manda para o login", r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""), r.status_code)
check("/admin sai com noindex", r.headers.get("X-Robots-Tag", "").startswith("noindex"), r.headers.get("X-Robots-Tag"))
# Sessao expirada + clique em "Sair": o login nao pode devolver para uma rota
# so-POST, senao o admin entra e cai num 405.
r = _ca.post("/admin/sair")
check("POST sem sessao vai ao login sem 'proximo'",
      r.status_code == 302 and r.headers.get("Location", "").endswith("/admin/login"), r.headers.get("Location"))
r = _ca.get("/admin/motoristas")
check("GET sem sessao guarda o caminho de volta", "proximo=/admin/motoristas" in r.headers.get("Location", ""), r.headers.get("Location"))
r = _adm_post(_ca, "/admin/login", email=_email_adm, senha="errada")
check("senha errada devolve 401", r.status_code == 401, r.status_code)
r = _adm_post(_ca, "/admin/login", email=f"ninguem-{_sufixo_adm}@teste.invalid", senha="1234")
check("e-mail inexistente devolve o mesmo 401", r.status_code == 401, r.status_code)
r = _adm_post(_ca, "/admin/login", email=_email_adm, senha="1234", proximo="https://evil.example/x")
check("login entra e ignora 'proximo' externo", r.status_code == 302 and r.headers.get("Location", "").endswith("/admin"), r.headers.get("Location"))
r = _ca.get("/admin"); _html_adm = r.get_data(as_text=True)
check("dashboard abre logado", r.status_code == 200, r.status_code)
check("senha curta gera aviso no painel", "senha do painel é curta" in _html_adm)
check("painel nao e cacheado", r.headers.get("Cache-Control") == "no-store", r.headers.get("Cache-Control"))
# Dashboard, lista e ficha — com a sessao ainda aberta
for _secao in ("Receita", "Crescimento", "Funil", "Uso", "Engajamento", "E-mail", "Abuso", "Saúde"):
    check(f"dashboard tem a seção {_secao}", _secao in _html_adm)
check("nenhuma seção indisponível", "Seção indisponível" not in _html_adm)
check("seção de e-mail anuncia a retenção dos contadores",
      f"E-mail (últimos {A.COUNTER_RETENTION_DAYS} dias" in _html_adm)
_segredos = [k for k in ("STRIPE_SECRET_KEY", "RESEND_API_KEY", "STRIPE_WEBHOOK_SECRET", "SECRET_KEY", "SUPABASE_DB_URL")
             if os.environ.get(k) and os.environ[k] in _html_adm]
check("dashboard nao vaza segredo de ambiente", not _segredos, str(_segredos))
r = _ca.get("/admin")
check("segunda abertura vem do cache", "em cache" in r.get_data(as_text=True))
r = _ca.get("/admin?atualizar=1")
check("atualizar=1 refaz as consultas", "em cache" not in r.get_data(as_text=True))
_alvo_m = A.get_store().get_user_by_email("reset@teste.invalid")
r = _ca.get("/admin/motoristas?q=teste.invalid"); _lista = r.get_data(as_text=True)
check("lista acha as contas de teste", r.status_code == 200 and _alvo_m["_id"] in _lista, r.status_code)
check("lista nao mostra CPF", "12345678900" not in _lista and "123.456.789-00" not in _lista)
# CPF e WhatsApp ficam gravados como foram digitados. A busca tem de achar o
# motorista com pontuacao no banco, na busca, nos dois ou em nenhum.
with A.get_store().pool.connection() as _conn:
    _conn.execute("update public.drivers set cpf = '123.456.789-00', whatsapp = '(11) 97777-6655' where id = %s",
                  (_alvo_m["_id"],))
for _busca in ("12345678900", "123.456.789-00", "(11) 97777-6655", "97777-6655", "977776655"):
    r = _ca.get("/admin/motoristas", query_string={"q": _busca})
    check(f"busca por '{_busca}' acha quem gravou com pontuação", _alvo_m["_id"] in r.get_data(as_text=True))
r = _ca.get("/admin/motoristas", query_string={"q": "000999000999"})
check("busca por dígitos que ninguém tem não traz o motorista", _alvo_m["_id"] not in r.get_data(as_text=True))
r = _ca.get("/admin/motoristas?q=%25_x&plano=pro&origem=loja&so_teto=1&sem_recibo=1")
check("filtros com % e _ nao quebram", r.status_code == 200, r.status_code)
# Botoes dos cards de Engajamento: abrem a lista com o mesmo criterio do card.
check("cada card de Engajamento tem botao para a lista filtrada",
      all(f"/admin/motoristas?filtro={_f}" in _html_adm for _f in A._FILTROS_ENGAJAMENTO))
# O cadastro pelo site nao passa pelo login do Auth: quem so se cadastrou
# "nunca entrou". Quem faz login depois, entrou.
_email_nunca, _email_entrou = f"nunca-{_sufixo_adm}@teste.invalid", f"entrou-{_sufixo_adm}@teste.invalid"
novo_cliente(_email_nunca); novo_cliente(_email_entrou)
A.app.test_client().post("/login", data={"email": _email_entrou, "senha": "senha12345"})
_id_nunca = A.get_store().get_user_by_email(_email_nunca)["_id"]
_id_entrou = A.get_store().get_user_by_email(_email_entrou)["_id"]
_tam = A.ADMIN_PAGE_SIZE; A.ADMIN_PAGE_SIZE = 100
_listas = {}
for _f in A._FILTROS_ENGAJAMENTO:
    r = _ca.get(f"/admin/motoristas?filtro={_f}"); _listas[_f] = r.get_data(as_text=True)
    check(f"filtro {_f} abre", r.status_code == 200, r.status_code)
_painel = A.montar_dashboard_admin()
A.ADMIN_PAGE_SIZE = _tam
check("'nunca entraram' traz quem so se cadastrou e deixa de fora quem fez login",
      _id_nunca in _listas["nunca_logou"] and _id_entrou not in _listas["nunca_logou"])
check("'entraram em 24 h' traz quem fez login e deixa de fora quem nunca entrou",
      _id_entrou in _listas["login_24h"] and _id_nunca not in _listas["login_24h"])
check("'inativos ha 30 dias' nao traz conta criada agora",
      _id_nunca not in _listas["inativos_30d"] and _id_entrou not in _listas["inativos_30d"])
# O numero do card e o tamanho da lista saem de consultas diferentes: tem de bater.
for _f, _n_card in (("login_24h", _painel["logins"]["login_24h"]), ("login_7d", _painel["logins"]["login_7d"]),
                    ("login_30d", _painel["logins"]["login_30d"]), ("nunca_logou", _painel["logins"]["nunca_logou"]),
                    ("sessao_viva", _painel["sessoes"]["usuarios"]), ("inativos_30d", _painel["inativos_30d"]["n"])):
    _n_lista = _listas[_f].count("/admin/motoristas/")
    check(f"card e lista de {_f} mostram o mesmo numero", _n_card > 100 or _n_lista == _n_card, f"card {_n_card}, lista {_n_lista}")
r = _ca.get("/admin/motoristas?filtro=nao_existe")
check("filtro desconhecido e ignorado", r.status_code == 200 and "Filtro:" not in r.get_data(as_text=True), r.status_code)
A.ADMIN_PAGE_SIZE = 1
r = _ca.get("/admin/motoristas?filtro=nunca_logou&q=teste.invalid")
check("proxima pagina mantem o filtro do card",
      _re_adm.search(r'cursor=[^"]*filtro=nunca_logou|filtro=nunca_logou[^"]*cursor=', r.get_data(as_text=True)) is not None)
A.ADMIN_PAGE_SIZE = _tam
_tam = A.ADMIN_PAGE_SIZE; A.ADMIN_PAGE_SIZE = 2
r = _ca.get("/admin/motoristas?q=teste.invalid"); _p1 = r.get_data(as_text=True)
_cursor = _re_adm.search(r'cursor=([^&"]+)', _p1)
check("pagina de 2 traz cursor para a proxima", _p1.count("/admin/motoristas/") == 2 and _cursor is not None)
r = _ca.get(f"/admin/motoristas?q=teste.invalid&cursor={_cursor.group(1) if _cursor else ''}")
check("proxima pagina abre e nao repete a primeira",
      r.status_code == 200 and _p1.split("/admin/motoristas/")[1][:36] not in r.get_data(as_text=True))
A.ADMIN_PAGE_SIZE = _tam
r = _ca.get("/admin/motoristas?cursor=adulterado")
check("cursor invalido volta a primeira pagina (200)", r.status_code == 200, r.status_code)
r = _ca.get(f"/admin/motoristas/{_alvo_m['_id']}"); _ficha = r.get_data(as_text=True)
check("ficha do motorista abre", r.status_code == 200, r.status_code)
check("ficha mascara o CPF", "***.***.789-00" in _ficha and "12345678900" not in _ficha and "123.456.789-00" not in _ficha)
check("ficha mostra e-mail e WhatsApp inteiros", "reset@teste.invalid" in _ficha)
check("uuid inexistente da 404", _ca.get("/admin/motoristas/00000000-0000-0000-0000-000000000000").status_code == 404)
check("id que nao e uuid da 404", _ca.get("/admin/motoristas/abc").status_code == 404)
check("ficha sai com no-referrer", r.headers.get("Referrer-Policy") == "no-referrer", r.headers.get("Referrer-Policy"))
check("/admin nao vira /administrador", _ca.get("/administrador").headers.get("X-Robots-Tag") is None)

# Excluir cadastro pelo painel
_email_vit = f"excluir-{_sufixo_adm}@teste.invalid"
novo_cliente(_email_vit)
_vit = A.get_store().get_user_by_email(_email_vit)
check("motorista para excluir foi criado", _vit is not None)
_vid = _vit["_id"] if _vit else "00000000-0000-0000-0000-000000000000"
_url_exc = f"/admin/motoristas/{_vid}/excluir"
_csrf_exc = _re_adm.search(r'name="csrf" value="([^"]+)"', _ca.get("/admin").get_data(as_text=True)).group(1)
r = _ca.get(f"/admin/motoristas/{_vid}")
check("ficha mostra o formulario de exclusao", 'id="formExcluirMotorista"' in r.get_data(as_text=True))
r = _adm_post(_ca, _url_exc, confirmacao="EXCLUIR")
check("excluir sem CSRF e recusado (400)",
      r.status_code == 400 and A.get_store().get_user_by_id(_vid) is not None, r.status_code)
r = _adm_post(_ca, _url_exc, csrf=_csrf_exc, confirmacao="nao")
check("sem digitar EXCLUIR nada e apagado",
      r.status_code == 302 and A.get_store().get_user_by_id(_vid) is not None, r.status_code)
A.get_store().update_user(_vid, {"plan": "pro", "subscription_status": "active"})
r = _ca.get(f"/admin/motoristas/{_vid}")
check("assinante da loja nao ve o formulario", 'id="formExcluirMotorista"' not in r.get_data(as_text=True))
r = _adm_post(_ca, _url_exc, csrf=_csrf_exc, confirmacao="EXCLUIR")
check("assinante da loja nao e apagado", A.get_store().get_user_by_id(_vid) is not None, r.status_code)
A.get_store().update_user(_vid, {"plan": "free", "subscription_status": None})
r = _adm_post(_ca, _url_exc, csrf=_csrf_exc, confirmacao="EXCLUIR")
check("EXCLUIR apaga e volta para a lista",
      r.status_code == 302 and r.headers.get("Location", "").endswith("/admin/motoristas"), r.headers.get("Location"))
check("cadastro sumiu do banco", A.get_store().get_user_by_id(_vid) is None)
with A.get_store().pool.connection() as _conn:
    _n_auth = _conn.execute("select count(*) as n from auth.users where id = %s", (_vid,)).fetchone()["n"]
check("login sumiu do Supabase Auth", _n_auth == 0, _n_auth)
check("excluir de novo da 404",
      _adm_post(_ca, _url_exc, csrf=_csrf_exc, confirmacao="EXCLUIR").status_code == 404)

r = _adm_post(_ca, "/admin/senha", senha_atual="1234", senha="novaSenha123", confirmar_senha="novaSenha123")
check("POST sem CSRF e recusado (400)", r.status_code == 400, r.status_code)
_csrf_adm = _re_adm.search(r'name="csrf" value="([^"]+)"', _html_adm).group(1)
r = _adm_post(_ca, "/admin/senha", csrf=_csrf_adm, senha_atual="errada", senha="novaSenha123", confirmar_senha="novaSenha123")
check("troca exige a senha atual (403)", r.status_code == 403, r.status_code)
r = _adm_post(_ca, "/admin/senha", csrf=_csrf_adm, senha_atual="1234", senha="curta", confirmar_senha="curta")
check("nova senha curta e recusada (400)", r.status_code == 400, r.status_code)
_cb = A.app.test_client()
_adm_post(_cb, "/admin/login", email=_email_adm, senha="1234")
check("segunda sessao entra antes da troca", _cb.get("/admin").status_code == 200)
r = _adm_post(_ca, "/admin/senha", csrf=_csrf_adm, senha_atual="1234", senha="novaSenha123", confirmar_senha="novaSenha123")
check("troca de senha redireciona ao painel", r.status_code == 302, r.status_code)
r = _ca.get("/admin")
check("quem trocou continua logado, sem aviso", r.status_code == 200 and "senha do painel é curta" not in r.get_data(as_text=True))
check("a outra sessao e derrubada pela troca", _cb.get("/admin").status_code == 302)
r = _adm_post(A.app.test_client(), "/admin/login", email=_email_adm, senha="1234")
check("senha antiga nao entra mais", r.status_code == 401, r.status_code)
_html_adm = _ca.get("/admin").get_data(as_text=True)
_csrf_adm = _re_adm.search(r'name="csrf" value="([^"]+)"', _html_adm).group(1)
r = _adm_post(_ca, "/admin/sair", csrf=_csrf_adm)
check("sair encerra a sessao", r.status_code == 302 and _ca.get("/admin").status_code == 302)
# teto de tentativas: e do par e-mail + IP, para quem erra trancar so a si mesmo
def _adm_login_de(ip, email, senha):
    return A.app.test_client().post("/admin/login", data={"email": email, "senha": senha},
                                    environ_base={"REMOTE_ADDR": ip})
_ip_intruso, _ip_dono = "198.51.100.7", "198.51.100.8"
_codigos = [_adm_login_de(_ip_intruso, _email_adm, "x").status_code
            for _ in range(A.ADMIN_LOGIN_DAILY_LIMIT_EMAIL + 1)]
check(f"apos {A.ADMIN_LOGIN_DAILY_LIMIT_EMAIL} tentativas do mesmo IP o login responde 429",
      _codigos[-1] == 429 and _codigos[-2] == 401, str(_codigos[-3:]))
r = _adm_login_de(_ip_intruso, _email_adm, "novaSenha123")
check("IP no teto nao entra nem com a senha certa", r.status_code == 429, r.status_code)
r = _adm_login_de(_ip_dono, _email_adm, "novaSenha123")
check("tentativas de um IP nao trancam o dono em outro IP", r.status_code == 302, r.status_code)
# teto total por e-mail: segura quem troca de IP a cada palpite
_total = A.ADMIN_LOGIN_DAILY_LIMIT_EMAIL_TOTAL; A.ADMIN_LOGIN_DAILY_LIMIT_EMAIL_TOTAL = 2
_alvo_adm = f"teto-{_sufixo_adm}@teste.invalid"
_codigos = [_adm_login_de(f"198.51.100.{20 + i}", _alvo_adm, "x").status_code for i in range(3)]
A.ADMIN_LOGIN_DAILY_LIMIT_EMAIL_TOTAL = _total
check("teto total por e-mail vale mesmo trocando de IP", _codigos == [401, 401, 429], str(_codigos))
A.get_store().delete_admin(_adm["_id"])
check("admin de teste apagado", A.get_store().get_admin_by_email(_email_adm) is None)

# -- Automacao de e-mails (/admin/emails, migracao 0007) ---------------------
# Roda contra producao, entao: o envio e um stub (nada sai pelo Resend), so as
# contas da suite entram (restringir_a), os modelos da suite nascem DESLIGADOS
# (o cron de producao nunca os ve) e o relogio e de janeiro de 2020 — o teto
# diario e o intervalo minimo nao enxergam os envios de verdade de hoje.
# Nada aqui chama /admin/emails/rodar, /tarefas/emails com o segredo, o envio
# de teste, nem muda o interruptor ou o teto: tudo isso agiria em producao.
print("\n-- Automacao de e-mails --")
from datetime import datetime as _dt_em, timedelta as _td_em
_suf_em = os.urandom(3).hex()
_agora_em = _dt_em(2020, 1, 15, 14, 0, tzinfo=A.BR_TZ)
_st_em = A.get_store()


def _motorista_em(nome, criado, plano="free", usados_mes=0, optout=False):
    email = f"em-{nome}-{_suf_em}@teste.invalid"
    novo_cliente(email)
    did = _st_em.get_user_by_email(email)["_id"]
    with _st_em.pool.connection() as conn:
        conn.execute("update public.drivers set created_at = %s, plan = %s, marketing_optout_at = %s where id = %s",
                     (criado, plano, _agora_em if optout else None, did))
        if usados_mes:
            conn.execute("""insert into public.receipt_quotas (driver_id, period, used)
                            values (%s, public.br_period(%s), %s)""", (did, _agora_em, usados_mes))
    return did


def _modelo_em(nome, gatilho, n, hora, posicao):
    with _st_em.pool.connection() as conn:
        return str(conn.execute(
            """insert into public.email_templates
                 (name, subject, body, button_text, button_url, trigger, trigger_value, send_hour, position, active)
               values (%s, %s, %s, 'Abrir', '{{link_app}}', %s, %s, %s, %s, false) returning id""",
            (f"teste-suite-{_suf_em}-{nome}", f"{nome} {{{{nome}}}}",
             "Olá, {{nome}}.\n\nVocê usou {{recibos_mes}} de {{limite}}.\n\n- item **um**",
             gatilho, n, hora, posicao)).fetchone()["id"])


_m_em = {
    "boas": _modelo_em("boas", "signup", 0, 8, 1),
    "lembrete": _modelo_em("lembrete", "no_receipt", 4, 10, 2),
    "quase": _modelo_em("quase", "monthly_usage", 4, 9, 3),
    "limite": _modelo_em("limite", "monthly_usage", 6, 8, 4),
}
_d_em = {
    "ana": _motorista_em("ana", _agora_em - _td_em(hours=5)),                 # cadastro hoje
    "bia": _motorista_em("bia", _agora_em - _td_em(days=4)),                  # 4 dias, sem recibo
    "eva": _motorista_em("eva", _agora_em - _td_em(days=20), usados_mes=6),   # bateu o limite
    "fabi": _motorista_em("fabi", _agora_em - _td_em(days=20), usados_mes=4), # quase
    "gil": _motorista_em("gil", _agora_em - _td_em(hours=5), plano="pro"),    # assinante
    "hugo": _motorista_em("hugo", _agora_em - _td_em(hours=5), optout=True),  # descadastrado
}
_nome_em = {v: k for k, v in _d_em.items()}
_saiu_em = []


def _stub_em(destino, msg, chave=""):
    _saiu_em.append((destino, msg, chave))
    return True, "re_suite", "", False


def _rodar_em(**kw):
    with A.app.test_request_context("/"):
        return A.rodar_automacao_email(**dict(dict(agora=_agora_em, enviar=_stub_em, pausa=0, ignorar_desligado=True,
                                                   restringir_a=list(_d_em.values()), modelos=list(_m_em.values())), **kw))


def _envios_em():
    with _st_em.pool.connection() as conn:
        linhas = conn.execute("""select s.driver_id, t.name, s.status from public.email_sends s
                                   join public.email_templates t on t.id = s.template_id
                                  where t.id = any(%s::uuid[])""", (list(_m_em.values()),)).fetchall()
    return {(_nome_em[str(l["driver_id"])], l["name"].rsplit("-", 1)[1], l["status"]) for l in linhas}


r = _rodar_em(agora=_dt_em(2020, 1, 15, 22, 0, tzinfo=A.BR_TZ))
check("motor nao manda fora do horario", r["motivo"] == "fora_do_horario" and not _saiu_em, str(r))
r = _rodar_em()
_esperado_em = {("ana", "boas", "sent"), ("bia", "lembrete", "sent"), ("eva", "limite", "sent"), ("fabi", "quase", "sent")}
check("cada motorista recebe o e-mail da sua vez", _envios_em() == _esperado_em, str(sorted(_envios_em() ^ _esperado_em)))
check("assinante e descadastrado ficam de fora",
      not {d for d, _, _ in _saiu_em} & {f"em-gil-{_suf_em}@teste.invalid", f"em-hugo-{_suf_em}@teste.invalid"})
check("quem bateu 6 nao recebe tambem o 'faltam 0'", ("eva", "quase", "sent") not in _envios_em())
_msg_em = next(m for d, m, _ in _saiu_em if d.startswith("em-fabi-"))
check("variaveis preenchidas no e-mail", _msg_em["assunto"] == "quase Ana" and "Você usou 4 de 6." in _msg_em["texto"],
      _msg_em["assunto"])
check("e-mail tem botao para /baixar e link de descadastro",
      "/baixar" in _msg_em["html"] and "/emails/sair/" in _msg_em["html"] and _msg_em["link_sair"] in _msg_em["texto"])
_antes_em = len(_saiu_em)
r = _rodar_em()
check("segunda passada nao repete ninguem", len(_saiu_em) == _antes_em and r["enviados"] == 0, str(r))

# Falha temporaria solta a reserva; falha definitiva fica marcada e nao repete.
with _st_em.pool.connection() as _conn:
    _conn.execute("delete from public.email_sends where driver_id = %s", (_d_em["ana"],))
r = _rodar_em(restringir_a=[_d_em["ana"]], enviar=lambda d, m, c="": (False, "", "HTTP 500", True))
check("erro 500 encerra a passada e solta a reserva",
      r["motivo"] == "provedor_indisponivel" and ("ana", "boas", "sent") not in _envios_em()
      and not any(d == "ana" for d, _, _ in _envios_em()), str(r))
r = _rodar_em(restringir_a=[_d_em["ana"]], enviar=lambda d, m, c="": (False, "", "HTTP 422", False))
check("erro 422 fica marcado como falha", ("ana", "boas", "failed") in _envios_em(), str(_envios_em()))
r = _rodar_em(restringir_a=[_d_em["ana"]])
check("falha definitiva nao e reenviada", r["enviados"] == 0, str(r))

# Descadastro: GET so mostra o botao (antivirus abre todo link do e-mail).
_cli_em = A.app.test_client()
_tok_em = A.token_descadastro(_d_em["fabi"])
def _optout_em():
    with _st_em.pool.connection() as conn:
        return conn.execute("select marketing_optout_at from public.drivers where id = %s",
                            (_d_em["fabi"],)).fetchone()["marketing_optout_at"]
r = _cli_em.get(f"/emails/sair/{_tok_em}")
check("GET do descadastro nao descadastra", r.status_code == 200 and _optout_em() is None, r.status_code)
r = _cli_em.post(f"/emails/sair/{_tok_em}", data={"acao": "sair"})
check("POST descadastra", r.status_code == 200 and _optout_em() is not None, r.status_code)
r = _cli_em.post(f"/emails/sair/{_tok_em}", data={"acao": "voltar"})
check("'foi engano' volta para a lista", r.status_code == 200 and _optout_em() is None, r.status_code)
r = _cli_em.post(f"/emails/sair/{_tok_em}", data="List-Unsubscribe=One-Click",
                 content_type="application/x-www-form-urlencoded")
check("descadastro de um clique do Gmail (RFC 8058)", r.status_code == 200 and _optout_em() is not None, r.status_code)
check("token adulterado da 404", _cli_em.get(f"/emails/sair/{_tok_em}x").status_code == 404)
check("cron de e-mails fica fechado sem o segredo", _cli_em.get("/tarefas/emails").status_code in (401, 503))
check("/baixar no iPhone vai a App Store", _cli_em.get("/baixar", headers={
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"}).headers.get("Location") == A.APP_STORE_URL)
check("/baixar no Android vai ao Google Play", _cli_em.get("/baixar", headers={
    "User-Agent": "Mozilla/5.0 (Linux; Android 14)"}).headers.get("Location") == A.PLAY_STORE_URL)

# Painel: so leitura e um modelo criado e apagado (desligado).
_adm_em = _st_em.upsert_admin(f"admin-em-{_suf_em}@teste.invalid", _gph("senha-da-suite-123"))
_ce = A.app.test_client()
_adm_post(_ce, "/admin/login", email=f"admin-em-{_suf_em}@teste.invalid", senha="senha-da-suite-123")
with _ce.session_transaction() as _s:
    _csrf_em = _s.get("admin_csrf", "")
r = _ce.get("/admin/emails"); _html_em = r.get_data(as_text=True)
check("painel de e-mails abre", r.status_code == 200 and "Automação de e-mails" in _html_em, r.status_code)
check("painel lista os modelos da suite", f"teste-suite-{_suf_em}-boas" in _html_em)
r = _ce.get(f"/admin/emails/{_m_em['quase']}")
check("pagina do modelo abre", r.status_code == 200, r.status_code)
check("paginas do painel continuam fora de iframe", "frame-ancestors 'none'" in r.headers.get("Content-Security-Policy", ""))
r = _ce.get(f"/admin/emails/{_m_em['quase']}/previa")
check("previa so pode ir em iframe do proprio site",
      r.status_code == 200 and "frame-ancestors 'self'" in r.headers.get("Content-Security-Policy", "")
      and "script-src" not in r.headers.get("Content-Security-Policy", ""), r.headers.get("Content-Security-Policy"))
_form_em = {"csrf": _csrf_em, "name": f"teste-suite-{_suf_em}-form", "subject": "Oi {{nome}}", "body": "Olá",
            "trigger": "signup", "trigger_value": "1", "send_hour": "10", "position": "99",
            "button_text": "", "button_url": ""}
r = _ce.post("/admin/emails/novo", data=dict(_form_em, subject="Oi {{nomee}}"))
check("variavel inexistente e recusada", r.status_code == 400 and "{{nomee}}" in r.get_data(as_text=True), r.status_code)
r = _ce.post("/admin/emails/novo", data=dict(_form_em, button_text="Ir", button_url="javascript:alert(1)"))
check("link do botao javascript: e recusado", r.status_code == 400, r.status_code)
r = _ce.post("/admin/emails/novo", data={k: v for k, v in _form_em.items() if k != "csrf"})
check("criar modelo sem csrf da 400", r.status_code == 400, r.status_code)
r = _ce.post("/admin/emails/novo", data=_form_em)
_novo_em = r.headers.get("Location", "").rstrip("/").rsplit("/", 1)[-1]
check("modelo novo nasce desligado", r.status_code == 302 and _novo_em, r.status_code)
if _novo_em:
    r = _ce.post(f"/admin/emails/{_novo_em}/excluir", data={"csrf": _csrf_em})
    with _st_em.pool.connection() as _conn:
        _resta = _conn.execute("select count(*) as n from public.email_templates where id = %s", (_novo_em,)).fetchone()["n"]
    check("modelo sem envios pode ser excluido", _resta == 0)
r = _ce.post(f"/admin/emails/{_m_em['boas']}/excluir", data={"csrf": _csrf_em})
with _st_em.pool.connection() as _conn:
    _resta = _conn.execute("select count(*) as n from public.email_templates where id = %s", (_m_em["boas"],)).fetchone()["n"]
check("modelo com historico nao e excluido", _resta == 1)
r = _ce.get(f"/admin/motoristas/{_d_em['fabi']}")
check("ficha mostra a situacao na automacao",
      r.status_code == 200 and "descadastrado em" in r.get_data(as_text=True), r.status_code)

_st_em.delete_admin(_adm_em["_id"])
with _st_em.pool.connection() as _conn:
    _conn.execute("delete from public.email_sends where template_id = any(%s::uuid[])", (list(_m_em.values()),))
    _conn.execute("delete from public.email_templates where id = any(%s::uuid[])", (list(_m_em.values()),))

# -- Avisos para a equipe: cadastro novo e assinatura nova -------------------
# Nada sai de verdade: send_email vira um gravador. Os contadores vao para
# chaves 'teste:', que a limpeza da suite apaga.
print("\n-- Avisos para a equipe --")
_avisos = []
_send_av, _chave_cad, _chave_ass = A.send_email, A.AVISO_CADASTRO_CHAVE, A.AVISO_ASSINATURA_CHAVE
_suf_av = os.urandom(3).hex()
A.AVISO_CADASTRO_CHAVE = f"teste:aviso_cadastro:{_suf_av}"
A.AVISO_ASSINATURA_CHAVE = f"teste:aviso_assinatura:{_suf_av}"


def _gravar_aviso(para, assunto, corpo, timeout=15):
    _avisos.append((para, assunto, corpo))
    return True


A.send_email = _gravar_aviso
_m_av = {"_id": "00000000-0000-0000-0000-0000000000a1", "email": "Rita.Lima@Gmail.com", "full_name": "Rita Lima",
         "whatsapp": "(45) 98888-7777", "city": "Cascavel", "plate": "ABC1D23", "vehicle_model": "Spin",
         "cpf": "123.456.789-00"}
try:
    with A.app.test_request_context("/"):
        _ok = A.avisar_novo_cadastro(_m_av["_id"], _m_av, "app")
    _para, _assunto, _corpo = _avisos[-1] if _avisos else ("", "", "")
    check("cadastro novo avisa o suporte", _ok and _para == "suporte@recibotaxi.com.br", _para)
    check("aviso de cadastro traz nome e cidade no assunto", _assunto == "Novo cadastro: Rita Lima (Cascavel)", _assunto)
    check("aviso de cadastro traz contato, origem e ficha",
          "rita.lima@gmail.com" in _corpo and "(45) 98888-7777" in _corpo and "pelo app" in _corpo
          and f"/admin/motoristas/{_m_av['_id']}" in _corpo, _corpo[:200])
    check("aviso de cadastro nao leva o CPF", "123.456.789-00" not in _corpo and "12345678900" not in _corpo)
    _n = len(_avisos)
    with A.app.test_request_context("/"):
        A.avisar_novo_cadastro("x", dict(_m_av, email="z@teste.invalid"), "site")
    check("conta de teste nao gera aviso", len(_avisos) == _n)
    _teto_av, A.EMAIL_AVISO_CADASTRO_DIA = A.EMAIL_AVISO_CADASTRO_DIA, 1
    with A.app.test_request_context("/"):
        _ok = A.avisar_novo_cadastro(_m_av["_id"], _m_av, "site")
    A.EMAIL_AVISO_CADASTRO_DIA = _teto_av
    check("passou do teto diario, cadastro nao avisa", _ok is False and len(_avisos) == _n)
    _env_av = os.environ.get("EMAIL_AVISOS")
    os.environ["EMAIL_AVISOS"] = ""
    with A.app.test_request_context("/"):
        check("EMAIL_AVISOS vazio desliga os dois avisos",
              A.avisar_novo_cadastro(_m_av["_id"], _m_av, "site") is False
              and A.avisar_nova_assinatura(_m_av, "anual", "site (Stripe)") is False)
    if _env_av is None:
        os.environ.pop("EMAIL_AVISOS", None)
    else:
        os.environ["EMAIL_AVISOS"] = _env_av

    with A.app.test_request_context("/"):
        _ok = A.avisar_nova_assinatura(_m_av, "anual", "Google Play (Android)", "R$ 119,90", teste=True,
                                       evento_id=f"evt-{_suf_av}")
    _para, _assunto, _corpo = _avisos[-1]
    check("assinatura nova avisa o suporte", _ok and _para == "suporte@recibotaxi.com.br", _para)
    check("assunto diz o plano e marca teste", _assunto == "[TESTE] Nova assinatura: Pro anual — Rita Lima", _assunto)
    check("aviso de assinatura traz plano, loja e valor",
          "Plano: Pro anual" in _corpo and "Onde: Google Play (Android)" in _corpo and "Valor: R$ 119,90" in _corpo
          and "ninguém foi cobrado" in _corpo, _corpo[:200])
    _n = len(_avisos)
    with A.app.test_request_context("/"):
        A.avisar_nova_assinatura(_m_av, "anual", "Google Play (Android)", "R$ 119,90", teste=True,
                                 evento_id=f"evt-{_suf_av}")
    check("webhook reenviado nao repete o aviso", len(_avisos) == _n)
    check("anual do Google (plano base) e da Apple viram 'anual'",
          A.ciclo_do_produto("br.com.recibotaxi.app.pro.mensal:anual") == "anual"
          and A.ciclo_do_produto("br.com.recibotaxi.app.pro.anual") == "anual"
          and A.ciclo_do_produto("br.com.recibotaxi.app.pro.mensal:mensal") == "mensal"
          and A.ciclo_do_produto("br.com.recibotaxi.app.pro.mensal") == "mensal")
    check("valor em real formatado", A.valor_da_compra(119.9, "BRL") == "R$ 119,90" and A.valor_da_compra(None, "BRL") == "")
    A.send_email = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provedor caiu"))
    with A.app.test_request_context("/"):
        check("falha no envio nao derruba cadastro nem webhook",
              A.avisar_novo_cadastro(_m_av["_id"], _m_av, "site") is False
              and A.avisar_nova_assinatura(_m_av, "mensal", "site (Stripe)", evento_id=f"evt2-{_suf_av}") is False)
    A.send_email = _gravar_aviso

    # Os pontos de chamada: cadastro pelo site e pelo app, e os dois webhooks.
    _chamadas = []
    _cad_orig, _ass_orig = A.avisar_novo_cadastro, A.avisar_nova_assinatura
    A.avisar_novo_cadastro = lambda uid, dados, origem: _chamadas.append(("cadastro", dados.get("email"), origem)) or True
    A.avisar_nova_assinatura = (lambda m, ciclo, onde, valor="", teste=False, evento_id="":
                                _chamadas.append(("assinatura", m.get("email"), ciclo, onde, valor, teste)) or True)
    try:
        _email_site = f"aviso-site-{_suf_av}@teste.invalid"
        novo_cliente(_email_site)
        _email_app = f"aviso-app-{_suf_av}@teste.invalid"
        A.app.test_client().post("/api/cadastro", json={
            "nome_completo": "Aviso App", "email": _email_app, "senha": "senha12345",
            "whatsapp": "(45) 99888-7777", "cpf": "12345678901", "cidade": "Cascavel", "placa": "AVS1234"})
        check("cadastro pelo site chama o aviso", ("cadastro", _email_site, "site") in _chamadas, str(_chamadas))
        check("cadastro pelo app chama o aviso", ("cadastro", _email_app, "app") in _chamadas, str(_chamadas))
        _id_site = A.get_store().get_user_by_email(_email_site)["_id"]
        _id_app = A.get_store().get_user_by_email(_email_app)["_id"]
        webhook_stripe(A.app.test_client(), "checkout.session.completed", {
            "metadata": {"user_id": _id_site, "plan": "pro", "ciclo": "anual"},
            "customer": f"cus_aviso_{_suf_av}", "subscription": f"sub_aviso_{_suf_av}",
            "amount_total": 11990, "currency": "brl"})
        check("checkout da Stripe avisa a assinatura com plano e valor",
              ("assinatura", _email_site, "anual", "site (Stripe)", "R$ 119,90", True) in _chamadas, str(_chamadas[-1:]))
        _seg_rc = os.environ.get("REVENUECAT_WEBHOOK_SECRET")
        os.environ["REVENUECAT_WEBHOOK_SECRET"] = "segredo-de-teste-123"
        try:
            _crc = A.app.test_client()

            def _evento_rc(tipo):
                return _crc.post("/webhook/revenuecat", headers={"Authorization": "Bearer segredo-de-teste-123"},
                                 json={"event": {"type": tipo, "id": f"rc-{tipo}-{_suf_av}", "app_user_id": _id_app,
                                                 "product_id": "br.com.recibotaxi.app.pro.mensal:anual",
                                                 "store": "PLAY_STORE", "environment": "SANDBOX",
                                                 "price_in_purchased_currency": 119.9, "currency": "BRL"}}).status_code

            _antes_rc = len(_chamadas)
            check("primeira compra no app responde 200", _evento_rc("INITIAL_PURCHASE") == 200)
            check("primeira compra no app avisa o plano e a loja",
                  ("assinatura", _email_app, "anual", "Google Play (Android)", "R$ 119,90", True) in _chamadas[_antes_rc:],
                  str(_chamadas[_antes_rc:]))
            _antes_rc = len(_chamadas)
            _evento_rc("RENEWAL")
            check("renovacao no app nao avisa", len(_chamadas) == _antes_rc, str(_chamadas[_antes_rc:]))
        finally:
            if _seg_rc is None:
                os.environ.pop("REVENUECAT_WEBHOOK_SECRET", None)
            else:
                os.environ["REVENUECAT_WEBHOOK_SECRET"] = _seg_rc
    finally:
        A.avisar_novo_cadastro, A.avisar_nova_assinatura = _cad_orig, _ass_orig
finally:
    A.send_email = _send_av
    A.AVISO_CADASTRO_CHAVE, A.AVISO_ASSINATURA_CHAVE = _chave_cad, _chave_ass

# -- Assinar antes de ter conta: cadastro e login vao direto ao pagamento -----
# Nada fala com a Stripe: get_stripe vira um falso que so anota os pedidos.
print("\n-- Assinar antes de ter conta --")
_suf_pg = os.urandom(3).hex()


class _StripeFalso:
    def __init__(self):
        self.sessoes = []
        falso = self

        class _Customer:
            @staticmethod
            def create(**kw):
                return type("C", (), {"id": f"cus_falso_{_suf_pg}_{len(falso.sessoes)}"})()

        class _Session:
            @staticmethod
            def create(**kw):
                falso.sessoes.append(kw)
                return type("S", (), {"url": "https://checkout.stripe.com/c/pay/cs_falso"})()

        self.Customer = _Customer
        self.checkout = type("K", (), {"Session": _Session})


_sf = _StripeFalso()
_get_stripe_orig = A.get_stripe
A.get_stripe = lambda: _sf
_env_precos = {k: os.environ.get(k) for k in ("STRIPE_PRO_PRICE_ID", "STRIPE_PRO_ANUAL_PRICE_ID")}
os.environ["STRIPE_PRO_PRICE_ID"] = "price_falso_mensal"
os.environ["STRIPE_PRO_ANUAL_PRICE_ID"] = "price_falso_anual"
_dados_pg = {"senha": "senha12345", "whatsapp": "11999998888", "cpf": "12345678900", "cidade": "SP", "placa": "abc1d23"}
try:
    _c = A.app.test_client()
    h = _c.get("/cadastro?plano=pro_anual").get_data(as_text=True)
    check("cadastro com plano avisa que vai direto ao pagamento", "Pro anual" in h and "direto para o pagamento" in h)
    check("cadastro guarda o plano no formulario", 'name="plano" value="pro_anual"' in h)
    check("link de login leva o plano junto", "/login?plano=pro_anual" in h)
    h = _c.get("/cadastro?plano=business").get_data(as_text=True)
    check("plano que nao esta a venda e ignorado", 'name="plano"' not in h and "direto para o pagamento" not in h)
    check("botoes de venda da home levam o plano ao cadastro",
          all(p in _c.get("/").get_data(as_text=True) for p in ("/cadastro?plano=pro_anual", "/cadastro?plano=pro\"")))

    _email_pg = f"pagamento-{_suf_pg}@teste.invalid"
    r = _c.post("/cadastro", base_url="https://www.recibotaxi.com.br",
                data=dict(_dados_pg, nome_completo="Paga Direto", email=_email_pg, plano="pro_anual"))
    check("cadastro com plano vai direto para a Stripe",
          r.status_code == 303 and r.headers.get("Location", "").startswith("https://checkout.stripe.com/"),
          f"{r.status_code} {r.headers.get('Location')}")
    _s = _sf.sessoes[-1] if _sf.sessoes else {}
    check("checkout do anual usa o preco anual", _s.get("line_items") == [{"price": "price_falso_anual", "quantity": 1}],
          str(_s.get("line_items")))
    check("volta para o mesmo dominio do cadastro",
          str(_s.get("success_url", "")).startswith("https://www.recibotaxi.com.br/dashboard")
          and str(_s.get("cancel_url", "")).startswith("https://www.recibotaxi.com.br/planos"), str(_s.get("success_url")))
    check("checkout nao manda payment_method_types", _s and "payment_method_types" not in _s)
    check("chave de idempotencia leva o dominio", str(_s.get("idempotency_key", "")).endswith(":www.recibotaxi.com.br"),
          str(_s.get("idempotency_key")))

    _n = len(_sf.sessoes)
    _email_sp = f"semplano-{_suf_pg}@teste.invalid"
    r = A.app.test_client().post("/cadastro", data=dict(_dados_pg, nome_completo="Sem Plano", email=_email_sp))
    check("cadastro sem plano vai ao painel, sem checkout",
          r.status_code == 302 and r.headers.get("Location", "").endswith("/dashboard") and len(_sf.sessoes) == _n,
          f"{r.status_code} {r.headers.get('Location')}")

    _c3 = A.app.test_client()
    h = _c3.get("/login?plano=pro").get_data(as_text=True)
    check("login com plano avisa e guarda o plano", "Pro mensal" in h and 'name="plano" value="pro"' in h)
    r = _c3.post("/login", data={"email": _email_sp, "senha": "senha12345", "plano": "pro"})
    check("login com plano vai direto para a Stripe",
          r.status_code == 303 and len(_sf.sessoes) == _n + 1
          and _sf.sessoes[-1]["line_items"][0]["price"] == "price_falso_mensal", f"{r.status_code}")

    # Pro pela loja (sem nada na Stripe) nao abre um segundo checkout no site.
    _id_loja = A.get_store().get_user_by_email(_email_sp)["_id"]
    A.get_store().update_user(_id_loja, {"plan": "pro", "subscription_status": "active",
                                         "stripe_customer_id": None, "stripe_subscription_id": None})
    _n = len(_sf.sessoes)
    r = _c3.post("/assinar/pro")
    check("Pro pela loja nao consegue pagar de novo no site",
          r.status_code == 302 and r.headers.get("Location", "").endswith("/dashboard") and len(_sf.sessoes) == _n,
          f"{r.status_code} {r.headers.get('Location')}")
finally:
    A.get_stripe = _get_stripe_orig
    for _k, _v in _env_precos.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

# -- Estimativa de corrida ----------------------------------------------------
# O servidor so devolve a rota (km e minutos); a faixa de preco e calculada no
# aparelho, com a tarifa que o motorista digitou. O Google nunca e chamado de
# verdade aqui: consultar_rota e trocada por uma falsa, e o pedido montado para
# a Routes API e conferido com um urlopen falso.
print("\n-- Estimativa de corrida --")
_cest = A.app.test_client()
_r = _cest.post("/api/cadastro", json={
    "nome_completo": "Rota Teste", "email": "rota@teste.invalid", "senha": "senha12345",
    "whatsapp": "(45) 99888-7777", "cpf": "12345678901", "cidade": "Cascavel", "placa": "ROT1234"})
_tke = _r.get_json().get("access_token", "") if _r.status_code == 201 else ""
_cabe = {"Authorization": f"Bearer {_tke}"}
_rota_falsa = {"km": 12.4, "minutos": 31, "minutos_sem_transito": 22}
_pedidos_rota = []
_consulta_real = A.consultar_rota
_chave_google = os.environ.get("GOOGLE_MAPS_API_KEY")


def _estimar(**corpo):
    return _cest.post("/api/estimativa", headers=_cabe, json=corpo)


try:
    _r = _cest.post("/api/estimativa", json={"lat": -24.95, "lng": -53.45, "destino": "Rodoviária"})
    check("estimativa sem sessao devolve 401", _r.status_code == 401, str(_r.status_code))

    # Rota com POST ganha o OPTIONS automatico do Flask (200), que vence o
    # api_preflight (204). Para o navegador tanto faz: o que libera e o cabecalho.
    _r = _cest.options("/api/estimativa", headers={
        "Origin": "capacitor://localhost", "Access-Control-Request-Method": "POST"})
    check("o app passa pelo preflight da estimativa",
          _r.status_code in (200, 204)
          and _r.headers.get("Access-Control-Allow-Origin") == "capacitor://localhost",
          f"{_r.status_code} {_r.headers.get('Access-Control-Allow-Origin')}")

    # Sem a chave do Google o app precisa de uma resposta que ele entenda, e
    # nao de um 500: a corrida e o recibo seguem funcionando sem estimativa.
    os.environ["GOOGLE_MAPS_API_KEY"] = ""
    A.consultar_rota = lambda origem, destino, timeout=8: _pedidos_rota.append((origem, destino)) or _rota_falsa
    _r = _estimar(lat=-24.95, lng=-53.45, destino="Rodoviária")
    check("sem chave do Google, responde 503 indisponivel",
          _r.status_code == 503 and (_r.get_json() or {}).get("erro") == "indisponivel",
          f"{_r.status_code} {_r.get_json()}")
    check("sem chave, o Google nem e chamado", not _pedidos_rota)

    os.environ["GOOGLE_MAPS_API_KEY"] = "chave-de-teste"
    _r = _estimar(lat=-24.95, lng=-53.45, destino="Rodoviária")
    check("devolve km e minutos da rota",
          _r.status_code == 200 and _r.get_json() == _rota_falsa, f"{_r.status_code} {_r.get_json()}")
    check("a origem vai como coordenada",
          _pedidos_rota and _pedidos_rota[-1][0] == {"lat": -24.95, "lng": -53.45}, str(_pedidos_rota[-1:]))
    check("a cidade do cadastro completa o destino",
          _pedidos_rota and _pedidos_rota[-1][1] == "Rodoviária, Cascavel", str(_pedidos_rota[-1:]))

    _estimar(lat=-24.95, lng=-53.45, destino="Rodoviária de Cascavel")
    check("cidade que ja esta no destino nao se repete",
          _pedidos_rota[-1][1] == "Rodoviária de Cascavel", _pedidos_rota[-1][1])

    _estimar(lat=-24.72, lng=-53.74, destino="Rodoviária", cidade="Toledo")
    check("a cidade de onde o aparelho esta vence a do cadastro",
          _pedidos_rota[-1][1] == "Rodoviária, Toledo", _pedidos_rota[-1][1])

    _estimar(origem="Av.  Brasil,   100", destino="Rodoviária")
    check("sem GPS, a origem digitada vai como texto",
          _pedidos_rota[-1][0] == {"endereco": "Av. Brasil, 100"}, str(_pedidos_rota[-1][0]))

    _r = _estimar(lat=200, lng=0, destino="Rodoviária")
    check("coordenada fora do mapa e sem origem digitada devolve 400",
          _r.status_code == 400 and (_r.get_json() or {}).get("erro") == "origem_invalida",
          f"{_r.status_code} {_r.get_json()}")
    _r = _estimar(lat="abc", lng=None, destino="Rodoviária")
    check("coordenada que nao e numero devolve 400", _r.status_code == 400, str(_r.status_code))
    _r = _estimar(lat=-24.95, lng=-53.45, destino=" ")
    check("destino vazio devolve 400",
          _r.status_code == 400 and (_r.get_json() or {}).get("erro") == "destino_invalido",
          f"{_r.status_code} {_r.get_json()}")
    _r = _estimar(lat=-24.95, lng=-53.45, destino="x" * 201)
    check("destino maior que o do recibo devolve 400", _r.status_code == 400, str(_r.status_code))

    A.consultar_rota = lambda *a, **k: None
    _r = _estimar(lat=-24.95, lng=-53.45, destino="Lugar que nao existe")
    check("destino que o Google nao acha devolve 404",
          _r.status_code == 404 and (_r.get_json() or {}).get("erro") == "destino_nao_encontrado",
          f"{_r.status_code} {_r.get_json()}")

    def _google_caiu(*a, **k):
        raise A.RotaIndisponivel("HTTP 503")
    A.consultar_rota = _google_caiu
    _r = _estimar(lat=-24.95, lng=-53.45, destino="Rodoviária")
    check("Google fora do ar devolve 502, sem estourar",
          _r.status_code == 502 and (_r.get_json() or {}).get("erro") == "falha_na_consulta",
          f"{_r.status_code} {_r.get_json()}")

    # Teto diario: um aparelho em laco nao pode gastar a cota de todos.
    A.consultar_rota = lambda *a, **k: _rota_falsa
    _teto_est = A.ESTIMATIVA_DIARIA_LIMITE
    A.ESTIMATIVA_DIARIA_LIMITE = 1
    try:
        _uid_est = A.get_store().get_user_by_email("rota@teste.invalid")["_id"]
        A.get_store().bump_counter(f"estimativa:{_uid_est}:{A.today_br()}")   # ja no teto
        _r = _estimar(lat=-24.95, lng=-53.45, destino="Rodoviária")
        check("no teto diario, a estimativa devolve 429",
              _r.status_code == 429 and (_r.get_json() or {}).get("limite") == 1,
              f"{_r.status_code} {_r.get_json()}")
    finally:
        A.ESTIMATIVA_DIARIA_LIMITE = _teto_est

    # A leitura da resposta do Google.
    check("ler_rota converte metros e segundos",
          A.ler_rota({"routes": [{"distanceMeters": 12400, "duration": "1860s",
                                  "staticDuration": "1320s"}]}) == _rota_falsa)
    check("ler_rota sem rota devolve None",
          A.ler_rota({}) is None and A.ler_rota({"routes": []}) is None
          and A.ler_rota({"routes": [{}]}) is None)
    check("ler_rota sem a duracao sem transito nao inventa hora parada",
          A.ler_rota({"routes": [{"distanceMeters": 1000, "duration": "120s"}]})
          == {"km": 1.0, "minutos": 2, "minutos_sem_transito": 2})

    # O pedido montado para a Routes API, sem sair para a rede.
    A.consultar_rota = _consulta_real
    import urllib.error as _uerr
    _urlopen_real = A.urllib.request.urlopen
    _enviado = {}

    class _RespostaFalsa:
        status = 200

        def __init__(self, corpo):
            self._corpo = corpo

        def read(self):
            return self._corpo

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen_falso(req, timeout=None):
        _enviado["url"], _enviado["corpo"] = req.full_url, json.loads(req.data)
        _enviado["cab"] = {k.lower(): v for k, v in req.header_items()}
        return _RespostaFalsa(json.dumps({"routes": [{"distanceMeters": 5000, "duration": "600s",
                                                      "staticDuration": "480s"}]}).encode())

    A.urllib.request.urlopen = _urlopen_falso
    try:
        _rota = A.consultar_rota({"lat": -24.95, "lng": -53.45}, "Rodoviária, Cascavel")
        check("consultar_rota le a resposta", _rota == {"km": 5.0, "minutos": 10, "minutos_sem_transito": 8},
              str(_rota))
        check("o pedido vai para a Routes API", _enviado.get("url") == A.ROUTES_API_URL)
        check("a chave vai no cabecalho, nunca na URL",
              _enviado["cab"].get("x-goog-api-key") == "chave-de-teste" and "key=" not in _enviado["url"])
        check("a mascara pede so distancia e duracao",
              _enviado["cab"].get("x-goog-fieldmask")
              == "routes.distanceMeters,routes.duration,routes.staticDuration")
        check("a origem vai como coordenada e o destino como texto",
              _enviado["corpo"]["origin"] == {"location": {"latLng": {"latitude": -24.95, "longitude": -53.45}}}
              and _enviado["corpo"]["destination"] == {"address": "Rodoviária, Cascavel"},
              str(_enviado["corpo"])[:160])
        check("rota de carro, com transito, em portugues",
              _enviado["corpo"]["travelMode"] == "DRIVE"
              and _enviado["corpo"]["routingPreference"] == "TRAFFIC_AWARE"
              and _enviado["corpo"]["languageCode"] == "pt-BR")

        def _urlopen_com_erro(codigo):
            def _abre(req, timeout=None):
                raise _uerr.HTTPError(req.full_url, codigo, "erro", {},
                                      io.BytesIO(b'{"error": {"status": "INVALID_ARGUMENT"}}'))
            return _abre

        A.urllib.request.urlopen = _urlopen_com_erro(400)
        check("endereco que o Google recusa vira 'sem rota'",
              A.consultar_rota({"endereco": "Av. Brasil, 100"}, "???") is None)
        A.urllib.request.urlopen = _urlopen_com_erro(403)
        try:
            A.consultar_rota({"endereco": "Av. Brasil, 100"}, "Rodoviária")
            check("chave recusada vira RotaIndisponivel", False, "nao levantou")
        except A.RotaIndisponivel:
            check("chave recusada vira RotaIndisponivel", True)
    finally:
        A.urllib.request.urlopen = _urlopen_real
finally:
    A.consultar_rota = _consulta_real
    if _chave_google is None:
        os.environ.pop("GOOGLE_MAPS_API_KEY", None)
    else:
        os.environ["GOOGLE_MAPS_API_KEY"] = _chave_google

# -- Data API do Supabase fechada (migracao 0006) ---------------------------
# O app nao usa o PostgREST, mas o Supabase expoe o schema public por ele.
# Tabela sem RLS ali e tabela legivel por quem tiver a chave publicavel.
print("\n-- Data API fechada --")
with A.get_store().pool.connection() as _conn:
    _sem_rls = [l["relname"] for l in _conn.execute(
        """select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity""").fetchall()]
    _abertas = [l["table_name"] for l in _conn.execute(
        """select distinct table_name from information_schema.role_table_grants
            where table_schema = 'public' and grantee in ('anon', 'authenticated')""").fetchall()]
check("toda tabela de public tem RLS ligado", not _sem_rls, str(_sem_rls))
check("anon e authenticated nao tem privilegio em tabela nenhuma", not _abertas, str(_abertas))
_sb_url, _sb_chave = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
if _sb_url and _sb_chave:
    import urllib.error, urllib.request
    for _tabela in ("admin_users", "drivers", "receipts"):
        _req = urllib.request.Request(
            f"{_sb_url}/rest/v1/{_tabela}?select=*", method="HEAD",
            headers={"apikey": _sb_chave, "Authorization": f"Bearer {_sb_chave}", "Prefer": "count=exact"})
        try:
            with urllib.request.urlopen(_req, timeout=20) as _resp:
                _status, _faixa = _resp.status, _resp.headers.get("Content-Range", "")
        except urllib.error.HTTPError as _erro:
            _status, _faixa = _erro.code, ""
        check(f"chave publicavel nao le {_tabela} pela Data API", _status >= 400, f"{_status} {_faixa}")

_depois = limpar_contas_de_teste()
print(f"\n  (limpeza final: {_depois} conta(s) de teste removida(s))")

print("\n" + ("="*50))
print(f"FALHAS: {len(fails)}" + ("" if not fails else " -> " + ", ".join(fails)))
sys.exit(1 if fails else 0)
