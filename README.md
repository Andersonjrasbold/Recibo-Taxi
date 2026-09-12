# Recibo Táxi

Aplicação Flask para taxistas emitirem e compartilharem recibos digitais.

- Cadastro e login do motorista, com edição dos próprios dados em `/perfil`
- Emissão de recibos, com histórico por conta
- Gerador público, sem cadastro, para experimentar
- Compartilhamento por WhatsApp ou e-mail, com link público e QR Code
- Assinatura Pro via Stripe
- Persistência em Astra DB (DataStax)
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

Com `ASTRA_DB_API_ENDPOINT` e `ASTRA_DB_APPLICATION_TOKEN` vazios, o app sobe em
**modo local**: tudo fica em memória e some ao reiniciar. Serve para desenvolver
sem rede. Em produção (variável `VERCEL` presente) essa situação levanta erro em
vez de passar batido, porque os dados não persistiriam.

## Testes

```bash
python test_app.py
```

Roda em memória, sem rede e sem dependências além das do `requirements.txt`.
Sai com código 1 se algo falhar. Cobre limite de plano, ciclo da assinatura,
redefinição de senha, exclusão de conta, cabeçalhos de segurança, rate limit e
a rotina de retenção.

> As implementações do `AstraStore` (contadores, limpeza, contagem mensal) não
> são exercitadas por esses testes — eles rodam no `InMemoryStore`. Valide contra
> um Astra de desenvolvimento antes de confiar nelas em produção.

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
| `SMTP_HOST` e `SMTP_*` | para o reset de senha | Vazio: o link só aparece no log |
| `CRON_SECRET` | para a limpeza | Vazio: `/tarefas/limpeza` responde 503 |

## Astra DB

Data API com três coleções, criadas sozinhas na primeira execução:

- `taxistas` — contas dos motoristas
- `recibos` — recibos emitidos
- `contadores` — contadores de rate limit do gerador público

## Planos

| Plano | Preço | Limite |
|---|---|---|
| Grátis | R$ 0,00 | 30 recibos por mês-calendário (fuso de Brasília) |
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
