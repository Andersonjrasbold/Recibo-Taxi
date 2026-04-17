# Recibo Taxi

Aplicacao Flask para taxistas com:

- login e senha
- cadastro completo do taxista
- emissao de recibos digitais
- historico de recibos
- envio do recibo por WhatsApp ou e-mail
- persistencia preparada para Astra DB
- deploy preparado para Vercel

## Rodar localmente

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
flask --app app run --debug
```

Defina no `.env`:

- `SECRET_KEY`
- `APP_BASE_URL`
- `ASTRA_DB_API_ENDPOINT`
- `ASTRA_DB_APPLICATION_TOKEN`
- `ASTRA_DB_KEYSPACE`
- `ASTRA_DB_COLLECTION_USERS`
- `ASTRA_DB_COLLECTION_RECEIPTS`

Se o Astra DB nao estiver configurado, o app sobe em modo local temporario para desenvolvimento.

## Astra DB

O app usa a Data API do Astra DB com duas collections:

- `taxistas`
- `recibos`

Quando `ASTRA_DB_API_ENDPOINT` e `ASTRA_DB_APPLICATION_TOKEN` estiverem definidos, a aplicacao:

1. conecta na database
2. garante que as collections existam
3. passa a salvar login/cadastro e recibos no Astra DB

## Deploy na Vercel

1. Suba este repositorio para o GitHub, GitLab ou Bitbucket.
2. Importe o projeto na Vercel.
3. Configure as variaveis de ambiente do arquivo `.env.example`.
4. Publique o projeto.

Arquivos importantes para o deploy:

- `app.py`: entrypoint Flask
- `vercel.json`: configuracao da function
- `public/static/*`: arquivos estaticos servidos pela Vercel

## Fluxo do produto

1. O taxista cria a conta.
2. Faz login no painel.
3. Preenche os dados da corrida.
4. Gera o recibo.
5. Compartilha o link final por WhatsApp ou e-mail.
