# Recibo Táxi

Aplicação Flask para taxistas emitirem e compartilharem recibos digitais.

- Cadastro e login pelo **Supabase Auth**, com edição dos próprios dados em `/perfil`
- Emissão de recibos, com histórico por conta
- Emitir recibo exige conta (o gerador público sem cadastro foi desligado em 2026-09-22; `/gerar` só redireciona)
- App para iPhone na App Store (link em `APP_STORE_URL`)
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
| `CRON_SECRET` | para os crons | Vazio: `/tarefas/limpeza` e `/tarefas/emails` respondem 503 |
| `EMAIL_REPLY_TO` | para a automação | Caixa que recebe as respostas dos e-mails automáticos. Sem ela, as respostas se perdem |
| `EMAIL_FROM_AUTOMACAO` | não | Remetente dos e-mails automáticos. Padrão: o `EMAIL_FROM` |

## Banco

Schema em `supabase/migrations/`. Tabelas principais:

- `drivers` — contas dos motoristas
- `receipts` — recibos emitidos (`rid` de 14 hex; com 10 a chance de colisão
  passava de 36% em 1 milhão de recibos)
- `rate_limits` — contadores antiabuso (cadastro, nova senha, login do painel)
- `receipt_quotas` — cota mensal do plano Grátis
- `email_templates`, `email_sends` e `email_settings` — automação de e-mails
  (migração 0007; ver a seção própria)

**Toda tabela de `public` tem RLS ligado e nenhuma policy** (migração 0006). O
app conecta como `postgres`, dono das tabelas, e não é afetado; o que o RLS
fecha é a Data API do Supabase, que expõe `public` para quem tiver a chave
publicável. Migração que criar tabela nova precisa ligar o RLS nela — a suíte
falha se alguma ficar sem.

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

## Painel administrativo (`/admin`)

Painel interno com os números da plataforma: motoristas, receita (MRR como
faixa, porque o banco não distingue mensal de anual), crescimento e ativação,
funil do Grátis (quem bateu o teto e segue sem pagar), uso, engajamento,
e-mail, abuso e saúde do sistema. Mais lista de motoristas com busca, filtros e
paginação por cursor, e ficha por motorista com CPF e dados do passageiro
mascarados.

Os cards de Engajamento têm botões que abrem a lista de motoristas já filtrada
(`?filtro=login_24h`, `sessao_viva`, `inativos_30d`, `nunca_logou`…). A condição
de cada filtro (`_FILTROS_ENGAJAMENTO`) repete a da consulta que gera o número
do card; a suíte confere que card e lista mostram o mesmo número.

**Administrador não é motorista.** Tabela própria (`admin_users`, migração
0005), senha com hash do werkzeug, sessão própria (`admin_id`). Não há cadastro
pelo site: o admin nasce pelo comando

```bash
flask --app app criar-admin   # pede e-mail e senha; repetir o e-mail redefine a senha
```

Proteções: teto de tentativas de login por dia (20 por IP; 10 por e-mail vindo
do mesmo IP, zerado no login certo; 100 por e-mail somando todos os IPs — o
teto apertado é do par, para que errar a senha tranque quem errou e não o dono
da conta), sessão que expira em 12 h ou 60 min sem uso, CSRF em todo POST,
troca de senha derruba as outras sessões, `noindex` + `no-store` +
`no-referrer` em tudo debaixo de `/admin`, e nada de `/admin` em página pública
ou `robots.txt`. As consultas do dashboard rodam numa conexão só, com savepoint
por seção (uma consulta falhando marca só aquela seção como indisponível) e
`statement_timeout` de 4 s; o resultado fica em cache no processo por 60 s
(`?atualizar=1` força).

**Excluir cadastro.** A ficha do motorista tem um bloco para apagar a conta de
vez: login, perfil, recibos e cotas, pelo mesmo cascade do Supabase Auth que a
exclusão feita pelo próprio motorista usa. Pede "EXCLUIR" digitado e uma
confirmação no navegador. Assinatura da Stripe é cancelada antes; se a Stripe
falhar, nada é apagado. Assinante pela App Store ou Google Play não pode ser
apagado pelo painel, porque o servidor não consegue parar a cobrança da loja.
Cada exclusão fica no log com o e-mail do admin.

O que o painel ainda **não** mede, por falta de instrumentação: upgrades e
cancelamentos por dia, mensal × anual, e-mails entregues × falhos, canal do
recibo (site × app) e se o cron rodou. O caminho é uma tabela de eventos
alimentada pelos webhooks e pelo cron.

## Automação de e-mails (`/admin/emails`)

Régua de e-mails para quem tem conta e ainda está no Grátis. Cada **modelo** é
um e-mail com a sua programação, editável no painel (assunto, texto, botão,
gatilho, hora, ordem, ativo). A migração 0007 traz 13 modelos prontos; o do
plano anual e a campanha de exemplo nascem desligados.

| Gatilho | Sai quando |
|---|---|
| `signup` | N dias depois do cadastro |
| `no_receipt` | N dias depois do cadastro, se ainda não emitiu nenhum recibo |
| `monthly_usage` | chegou a N recibos no mês (uma vez por mês; só o modelo de maior N alcançado) |
| `inactive` | N dias sem emitir, para quem já emitiu (volta a valer se ele emitir e parar de novo) |
| `scheduled` | campanha em data marcada, para quem tem N+ dias de cadastro |

O cron `GET /tarefas/emails` roda de hora em hora (minuto 5) e só manda das 8h
às 21h de Brasília. O que garante o comportamento:

- **Nunca duas vezes:** a linha de `email_sends` nasce antes do envio, com
  unique (modelo, motorista, `dedupe_key`), e o Resend recebe um
  `Idempotency-Key`.
- **Assinou ou descadastrou, para:** a consulta só pega `plan = 'free'` e
  `marketing_optout_at` vazio na hora do envio.
- **Não é retroativo:** quem passou mais de 3 dias do ponto de um e-mail (7
  para campanha) não recebe mais aquele. Ligar o motor não despeja a régua
  inteira em quem se cadastrou há meses; para a base antiga, use campanha.
- **Um por vez:** no máximo um e-mail por motorista a cada N horas (padrão 20)
  e um teto diário somando todos (padrão 80), editáveis no painel. O teto
  existe porque a cota do Resend é a mesma do e-mail de senha e de recibo.
- **Falhas:** 429, 5xx, rede e 401/403 (chave ou domínio mal configurados)
  soltam a reserva e encerram a passada — a próxima hora tenta de novo. Os
  outros 4xx marcam `failed` e não repetem.
- **Fora da régua:** contas `.invalid` (suíte) e `@recibotaxi.com.br` (como a
  de demonstração da Apple — o domínio não recebe e-mail).

**Descadastro.** Todo e-mail tem link no rodapé e os cabeçalhos
`List-Unsubscribe` + `List-Unsubscribe-Post` (RFC 8058), que dão o botão
"Cancelar inscrição" do Gmail. `GET /emails/sair/<token>` só mostra o botão:
antivírus de e-mail abrem todo link da mensagem, e um GET que descadastrasse
tiraria todo mundo da lista. O token é assinado e não expira.

**Links.** Os botões de venda usam `{{link_app}}` → `/baixar`, que manda para a
App Store ou o Google Play conforme o celular (no computador, para a seção do
app na home). A assinatura acontece no app; o checkout do site depende da
Stripe em produção.

**Medição.** O painel mostra, por modelo, quantos receberam e quantos dos que
receberam são Pro hoje — correlação, não prova de que o e-mail vendeu. Aberturas
e cliques não são medidos.

Os modelos "Quase no limite do mês" (gatilho 4) e "Limite do mês atingido"
(gatilho 6) têm o número fixo no gatilho; o texto usa `{{limite}}`. Mudou o
`FREE_MONTHLY_LIMIT`, ajuste os dois gatilhos no painel.

O motor nasce **desligado** (`email_settings.enabled = false`). Para ligar:
aplicar a migração 0007, definir `EMAIL_REPLY_TO`, revisar os modelos (o botão
"Enviar teste para mim" manda a versão salva para o e-mail do admin) e clicar
em "Ligar o motor".

## Planos

| Plano | Preço | Limite |
|---|---|---|
| Grátis | R$ 0,00 | 6 recibos por mês-calendário (fuso de Brasília) |
| Pro | R$ 19,90/mês | Ilimitado |

O limite é aplicado em `recibo_criar`. O webhook da Stripe rebaixa a conta para
Grátis quando a assinatura é cancelada ou a cobrança falha em definitivo —
`past_due` mantém o plano enquanto a Stripe retenta.

## Rotina de limpeza

`GET /tarefas/limpeza` apaga recibos sem conta (do antigo gerador público) com mais de 12 meses
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
- Rate limit por IP em cadastro e nova senha (best-effort; para abuso sério, WAF da Vercel)

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

1. O taxista cria a conta (no site ou no app para iPhone).
2. Preenche os dados da corrida.
3. Gera o recibo.
4. Compartilha o link por WhatsApp ou e-mail.
5. O passageiro abre o recibo pelo link público, sem precisar de conta.
