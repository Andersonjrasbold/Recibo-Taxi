# Recibo Táxi

Aplicação Flask para taxistas emitirem e compartilharem recibos digitais.

- Cadastro e login pelo **Supabase Auth**, com edição dos próprios dados em `/perfil`
- Emissão de recibos, com histórico por conta
- Gerador público, sem cadastro, para experimentar
- Compartilhamento por WhatsApp ou e-mail, com link público e QR Code
- Assinatura Pro via Stripe
- Persistência em Postgres (Supabase)
- Deploy na Vercel

## Rodar localmente

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # depois edite o .env
flask --app app run --debug
```

O `.env` é lido automaticamente (`python-dotenv`). Ele **não** vai para o git nem
para a Vercel — está no `.gitignore` e no `.vercelignore`.

> **Use chaves de teste no `.env`.** As chaves de produção ficam só nas
> Environment Variables do projeto na Vercel. Chave Stripe live move dinheiro real.

### Banco local para desenvolvimento

Os testes rodam contra Postgres de verdade, sem Docker:

```bash
brew install postgresql@17
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
initdb -D /tmp/rt-pgdata -U postgres --auth=trust
pg_ctl -D /tmp/rt-pgdata -o "-p 55432 -k /tmp/rtpg" -l /tmp/rt-pgdata/server.log start
createdb -h 127.0.0.1 -p 55432 -U postgres recibo_taxi
psql -h 127.0.0.1 -p 55432 -U postgres -d recibo_taxi -f supabase/migrations/0001_schema_inicial.sql
```

Depois é só `DATABASE_URL=postgresql://postgres@127.0.0.1:55432/recibo_taxi` no `.env`.

O app escolhe o store nesta ordem: `SUPABASE_DB_URL` > `DATABASE_URL` > Astra DB >
memória. Sem banco nenhum ele sobe em **modo local** (tudo em memória, some ao
reiniciar); em produção (variável `VERCEL` presente) isso levanta erro em vez de
passar batido, porque os dados não persistiriam.

## Testes

```bash
PERMITIR_TESTE_REMOTO=1 python test_app.py
```

A suíte roda **contra o Supabase**, não contra um banco local. Não é escolha:
desde que a autenticação passou para o Supabase Auth, `drivers.id` é chave
estrangeira de `auth.users`. Um usuário criado no Auth da nuvem não pode ter
perfil num Postgres local — a FK não fecha.

Por isso ela exige opt-in explícito. Toda conta que cria usa o domínio
`@teste.invalid` (TLD reservada pela RFC 2606) e é apagada no início e no fim,
junto com os recibos de convidado e os contadores de rate limit das faixas
reservadas para documentação.

> **Sem dublês onde o real importa.** A versão anterior stubava o
> `construct_event` da Stripe com um fake que devolvia `dict`. Isso escondeu um
> bug em que todo webhook dava 500 em produção, porque o SDK devolve um
> `StripeObject`, que não tem `.get()`. A suíte agora assina os webhooks de
> verdade.


## Variáveis de ambiente

| Variável | Obrigatória | Para quê |
|---|---|---|
| `SECRET_KEY` | sim em produção | Assina sessão e tokens de redefinição de senha |
| `APP_BASE_URL` | recomendada | Monta os links públicos de recibo e de reset |
| `ASTRA_DB_API_ENDPOINT` | sim em produção | Endpoint da Data API |
| `ASTRA_DB_APPLICATION_TOKEN` | sim em produção | Token do Astra |
| `ASTRA_DB_KEYSPACE` | não | Padrão `default_keyspace` |
| `ASTRA_DB_COLLECTION_USERS` | não | Padrão `taxistas` |
| `ASTRA_DB_COLLECTION_RECEIPTS` | não | Padrão `recibos` |
| `ASTRA_DB_COLLECTION_COUNTERS` | não | Padrão `contadores` |
| `STRIPE_SECRET_KEY` | para cobrar | Vazio desativa o checkout |
| `STRIPE_PUBLISHABLE_KEY` | para cobrar | — |
| `STRIPE_WEBHOOK_SECRET` | para cobrar | Valida a assinatura do webhook |
| `STRIPE_PRO_PRICE_ID` | para cobrar | Preço do plano Pro |
| `RESEND_API_KEY` | para o reset de senha | Vazio: cai para SMTP |
| `EMAIL_FROM` | recomendada | Remetente. Exige domínio verificado no Resend |
| `SMTP_HOST` e `SMTP_*` | alternativa ao Resend | Ignorado se houver `RESEND_API_KEY` |
| `CRON_SECRET` | para a limpeza | Vazio: `/tarefas/limpeza` responde 503 |

## Banco

Schema em `supabase/migrations/`. Quatro tabelas:

- `drivers` — contas dos motoristas
- `receipts` — recibos emitidos (`rid` de 14 hex; com 10 a chance de colisão
  passava de 36% em 1 milhão de recibos)
- `rate_limits` — contador do gerador público
- `receipt_quotas` — cota mensal do plano Grátis

Duas funções carregam a lógica que precisa ser atômica:

- `bump_counter(key)` — incremento do rate limit numa ida só ao banco
- `consume_receipt_quota(driver, limite)` — consome a cota com `where used < limite`.
  É o que fecha a corrida: sem ela, duas emissões simultâneas furavam o teto.

### Conexão na Vercel

Use o **Transaction pooler** (porta 6543). A conexão direta
(`db.<ref>.supabase.co:5432`) é **IPv6-only** e a Vercel só sai por IPv4 — ela
falha com erro de rede, que parece problema de DNS e não de credencial.

O `psycopg` 3 cria prepared statements sozinho depois de 5 execuções da mesma
query, e o Supavisor em transaction mode não os suporta. Por isso o pool é criado
com `prepare_threshold=None` — sem isso o erro só apareceria em produção, depois
de algum tempo no ar.

## Autenticação

A senha vive no **Supabase Auth**; `drivers` é só o perfil, com a mesma chave
primária de `auth.users`. O trigger `on_auth_user_created` cria o perfil a
partir do `raw_user_meta_data`.

A **sessão continua sendo a do Flask**, já assinada pela `SECRET_KEY`. Assim uma
página comum não paga ida à rede — só cadastro, login e troca de senha falam com
o Auth.

O cadastro usa `admin.create_user` em vez de `sign_up`: não dispara e-mail de
confirmação (o fluxo loga direto) e não passa pelo limite de 30 cadastros a cada
5 minutos.

### Redefinição de senha

Continua sendo o fluxo do app — token `itsdangerous` de uso único — e só a
gravação vai para `admin.update_user_by_id`.

O motivo é concreto: verifiquei no fonte do `supabase-py` 2.31.0 que
`reset_password_for_email` **não honra PKCE**. O link do e-mail entrega os
tokens no *fragmento* da URL, que o navegador não envia ao servidor. Um app
server-rendered não consegue lê-los.

A digital de uso único, que antes vinha do hash da senha, agora vem do
`password_changed_at`.

## Planos

| Plano | Preço | Limite |
|---|---|---|
| Grátis | R$ 0,00 | 5 recibos por mês-calendário (fuso de Brasília) |
| Pro | R$ 19,90/mês | Ilimitado |

O limite é aplicado em `recibo_criar`. O webhook da Stripe rebaixa a conta para
Grátis quando a assinatura é cancelada ou a cobrança falha em definitivo —
`past_due` mantém o plano enquanto a Stripe retenta.

## Rotina de limpeza

`GET /tarefas/limpeza` apaga recibos do gerador público com mais de 12 meses
(o que a Política de Privacidade promete) e contadores de rate limit antigos.

Protegida por `Authorization: Bearer $CRON_SECRET`. O `vercel.json` agenda a
chamada diária às 04:00 UTC. Sem `CRON_SECRET` a rota fica fechada.

Apaga no máximo 500 registros por execução, para não estourar o tempo da
função com backlog grande. A resposta traz `backlog_restante: true` quando
sobrou trabalho — a execução seguinte continua de onde parou.

## Segurança

- CSP com nonce por requisição — sem `'unsafe-inline'` em `script-src`
- `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`
- Sessão com cookie `HttpOnly`, `SameSite=Lax` e `Secure` em produção
- Senhas com hash `werkzeug` (PBKDF2 com sal)
- Token de reset ligado ao hash da senha atual, o que o torna de uso único
- Rate limit por IP no gerador público (best-effort; para abuso sério, WAF da Vercel)

## E-mail

Envio pelo **Resend**, via API HTTP. Numa função serverless o handshake do SMTP
são cinco ou seis idas e voltas (EHLO, STARTTLS, AUTH, MAIL FROM, RCPT TO,
DATA); pela API é um POST só. O caminho SMTP continua no código como alternativa.

> A requisição manda um `User-Agent` próprio de propósito: o Cloudflare do
> Resend devolve `403 error code: 1010` para o padrão do `urllib`.

O remetente precisa de domínio verificado no Resend. Sem isso, só
`onboarding@resend.dev` funciona — e ele entrega apenas para o e-mail dono
da conta.
- Teto de tamanho por campo em todo formulário que escreve no banco, mais
  `MAX_CONTENT_LENGTH` de 1 MB no corpo da requisição
- Alterar dados da conta em `/perfil` exige a senha atual

## Deploy na Vercel

1. Suba o repositório para o GitHub, GitLab ou Bitbucket.
2. Importe o projeto na Vercel.
3. Configure as variáveis da tabela acima nas Environment Variables.
4. Publique.

Arquivos que importam no deploy:

- `app.py` — entrypoint Flask
- `vercel.json` — build, rotas e cron
- `.vercelignore` — mantém `.env` e testes fora do bundle
- `public/static/*` — estáticos servidos pelo CDN

## Fluxo do produto

1. O taxista cria a conta (ou usa `/gerar` sem cadastro).
2. Preenche os dados da corrida.
3. Gera o recibo.
4. Compartilha o link por WhatsApp ou e-mail.
5. O passageiro abre o recibo pelo link público, sem precisar de conta.
