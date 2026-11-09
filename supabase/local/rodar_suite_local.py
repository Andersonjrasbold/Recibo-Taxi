"""APENAS DESENVOLVIMENTO LOCAL. Roda o test_app.py num Postgres local com um
GoTrue falso, sem tocar a producao (a suite normal cria e apaga contas reais).

Receita (Postgres 17 do Homebrew; ver a memoria do projeto "suite-local"):
  PG=/opt/homebrew/opt/postgresql@17/bin
  $PG/initdb -D <pasta>/data -U postgres --auth=trust -E UTF8
  $PG/pg_ctl -D <pasta>/data -o "-p 55432 -k '' -c listen_addresses=127.0.0.1 -c timezone=UTC" start
  $PG/psql -h 127.0.0.1 -p 55432 -U postgres -c "create database recibo"
  psql ... -f supabase/local/auth_shim.sql
  psql ... -f supabase/local/auth_shim_extra.sql
  for f in supabase/migrations/*.sql; do psql ... -f "$f"; done
  cd <pasta do codigo a testar>
  env -i PATH="/usr/local/bin:/usr/bin:/bin" HOME="$HOME" PYTHONDONTWRITEBYTECODE=1 \
      PERMITIR_TESTE_REMOTO=1 SUPABASE_DB_URL=postgresql://postgres@127.0.0.1:55432/recibo \
      SECRET_KEY=qualquer <venv>/bin/python supabase/local/rodar_suite_local.py

Falhas de ambiente esperadas (iguais no main): as da Stripe, sem chave, e
"a pagina do recibo pede noindex", que usa um recibo que so existe em producao.
"""
import hashlib, json, os, runpy, secrets, socket, sys, time

DSN = os.environ.get("SUPABASE_DB_URL", "")
assert DSN.startswith("postgresql://postgres@127.0.0.1:55432/"), "DSN tem de ser o Postgres local"
for chave, valor in os.environ.items():
    baixo = valor.lower()
    assert not any(p in baixo for p in ("supabase.co", "vercel.app", "stripe", "resend", "revenuecat")), \
        f"variavel {chave} aponta para servico externo"

# Guarda de rede: so loopback. Qualquer tentativa de sair da maquina estoura.
_connect = socket.socket.connect
def _so_loopback(self, endereco):
    host = endereco[0] if isinstance(endereco, tuple) else str(endereco)
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise OSError(f"rede bloqueada na suite local: {host}")
    return _connect(self, endereco)
socket.socket.connect = _so_loopback

# Nada de .env: as variaveis vem so do ambiente desta execucao.
import dotenv
dotenv.load_dotenv = lambda *a, **k: False

sys.path.insert(0, os.getcwd())
import app as A  # noqa: E402

import psycopg
from psycopg.rows import dict_row


def _hash(s):
    return hashlib.sha256(("gotrue-falso:" + s).encode()).hexdigest()


class _Obj(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


def _conn():
    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True)


_TOKENS = {}   # access_token -> user_id
_REFRESH = {}  # refresh_token -> user_id


def _sessao(uid, email):
    acesso, renovacao = "acc-" + secrets.token_urlsafe(24), "ref-" + secrets.token_urlsafe(24)
    _TOKENS[acesso], _REFRESH[renovacao] = uid, uid
    with _conn() as c:
        c.execute("insert into auth.sessions (user_id, not_after) values (%s, now() + interval '7 days')", (uid,))
        c.execute("update auth.users set last_sign_in_at = now() where id = %s", (uid,))
    return _Obj(user=_Obj(id=uid, email=email),
                session=_Obj(access_token=acesso, refresh_token=renovacao, expires_at=int(time.time()) + 3600))


class _Admin:
    def create_user(self, attrs):
        email = (attrs.get("email") or "").strip().lower()
        with _conn() as c:
            if c.execute("select 1 from auth.users where lower(email) = %s", (email,)).fetchone():
                raise Exception("User already registered")
            linha = c.execute(
                """insert into auth.users (email, encrypted_password, raw_user_meta_data, email_confirmed_at)
                   values (%s, %s, %s::jsonb, now()) returning id""",
                (email, _hash(attrs.get("password") or ""), json.dumps(attrs.get("user_metadata") or {}))).fetchone()
        return _Obj(user=_Obj(id=str(linha["id"]), email=email))

    def update_user_by_id(self, uid, attrs):
        with _conn() as c:
            if "password" in attrs:
                c.execute("update auth.users set encrypted_password = %s, updated_at = now() where id = %s",
                          (_hash(attrs["password"]), uid))
            if "email" in attrs:
                c.execute("update auth.users set email = %s, updated_at = now() where id = %s",
                          (attrs["email"].strip().lower(), uid))
        return _Obj(user=_Obj(id=str(uid)))

    def delete_user(self, uid):
        with _conn() as c:
            c.execute("delete from auth.users where id = %s", (uid,))


class _Auth:
    admin = _Admin()

    def sign_in_with_password(self, cred):
        email = (cred.get("email") or "").strip().lower()
        with _conn() as c:
            u = c.execute("select id, email, encrypted_password, banned_until from auth.users where lower(email) = %s",
                          (email,)).fetchone()
        if not u or u["encrypted_password"] != _hash(cred.get("password") or ""):
            raise Exception("Invalid login credentials")
        if u["banned_until"] and u["banned_until"].timestamp() > time.time():
            raise Exception("User is banned")
        return _sessao(str(u["id"]), u["email"])

    def refresh_session(self, token):
        uid = _REFRESH.pop(token, None)
        if not uid:
            raise Exception("Invalid Refresh Token")
        with _conn() as c:
            u = c.execute("select email from auth.users where id = %s", (uid,)).fetchone()
        if not u:
            raise Exception("User not found")
        return _sessao(uid, u["email"])

    def get_user(self, token):
        uid = _TOKENS.get(token)
        if not uid:
            raise Exception("invalid JWT")
        with _conn() as c:
            u = c.execute("select email from auth.users where id = %s", (uid,)).fetchone()
        if not u:
            raise Exception("User not found")
        return _Obj(user=_Obj(id=uid, email=u["email"]))


class _Client:
    auth = _Auth()


_CLIENTE = _Client()
A.supabase_admin = lambda: _CLIENTE
A.supabase_public = lambda: _CLIENTE

runpy.run_path(os.path.join(os.getcwd(), "test_app.py"), run_name="__main__")
