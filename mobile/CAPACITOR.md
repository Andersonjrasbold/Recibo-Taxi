# App nativo — decisões de arquitetura

## Não usamos `server.url`

A tentação é apontar o WebView para `https://recibo-taxi.vercel.app` e pronto.
Não fazemos isso, por dois motivos independentes:

**1. A Apple rejeita.** Guideline 4.2 (Minimum Functionality): "Your app should
include features, content, and UI that elevate it beyond a repackaged website."
Ter login e conteúdo do usuário **não** imuniza — há rejeição textual da Apple
derrubando exatamente esse argumento. Híbrido parcial também cai.

**2. Não resolve o problema do motorista.** Com `server.url`, sem rede o app é
uma tela branca. O taxista trabalha em garagem e túnel.

A própria doc do Capacitor diz, no código-fonte da configuração:
`server.url` → *"This is not intended for use in production."*

## O que fazemos

Bundle **totalmente local** em `www/`. O recibo é gerado no aparelho:
o `rid` nasce de `crypto.getRandomValues`, o recibo é montado e renderizado
localmente, e vai para uma fila em IndexedDB. A rede entra só na sincronização.

O servidor trata o mesmo `rid` como o mesmo recibo (`POST /api/recibos` é
idempotente), então reenviar a fila nunca duplica.

## Autenticação

Token, não cookie. O WebView roda em `capacitor://localhost` (iOS) ou
`https://localhost` (Android) — origem diferente da API, então o cookie de
sessão não viajaria. `POST /api/login` devolve o `access_token` do Supabase,
que o app guarda e manda como `Bearer`.

O CORS da API responde só a essas origens, e sem `Allow-Credentials`.

## Comandos

```bash
npm run sync      # copia www/ para os projetos nativos
npm run ios       # abre no Xcode
npm run android   # abre no Android Studio
```
