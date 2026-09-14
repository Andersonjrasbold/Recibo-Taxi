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

## Publicar no TestFlight

**Use o script, não os comandos soltos:**

```bash
./mobile/subir_build.sh
```

Ele faz `cap sync`, arquiva, **abre o pacote e procura sonda de teste e cópia
duplicada antes de enviar**, e aborta se achar. Também recusa começar se já
houver outro `xcodebuild` rodando. As duas travas existem porque as duas coisas
já aconteceram. Os comandos abaixo são o que o script roda, para referência.


Tudo por linha de comando, sem abrir o Xcode. Precisa da chave de API da App
Store Connect com papel **Admin** (`Z53W4BVFVV`, em
`~/Credenciais/Recibo-Taxi/apple/`). A chave de App Manager não consegue
criar o certificado de distribuição, e o `exportArchive` falha com
"Cloud signing permission error".

```bash
# 1. Bundle novo dentro do projeto iOS
npm run sync

# 2. Número do build: tem de subir a cada envio, a Apple recusa repetido
sed -i '' 's/CURRENT_PROJECT_VERSION = 5;/CURRENT_PROJECT_VERSION = 6;/g' \
  ios/App/App.xcodeproj/project.pbxproj

# 3. Arquivar, assinado na nuvem
CHAVE="$HOME/Credenciais/Recibo-Taxi/apple/AuthKey_Z53W4BVFVV.p8"
ISSUER=1a48f074-d9f7-4c4a-b9c9-7f172ac0d5a5
cd ios/App
xcodebuild -project App.xcodeproj -scheme App -configuration Release \
  -destination 'generic/platform=iOS' -archivePath /tmp/ReciboTaxi.xcarchive \
  -allowProvisioningUpdates -authenticationKeyPath "$CHAVE" \
  -authenticationKeyID Z53W4BVFVV -authenticationKeyIssuerID "$ISSUER" archive

# 4. Exportar E enviar: o ExportOptions.plist tem destination=upload
xcodebuild -exportArchive -archivePath /tmp/ReciboTaxi.xcarchive \
  -exportOptionsPlist ../ExportOptions.plist -exportPath /tmp/upload \
  -allowProvisioningUpdates -authenticationKeyPath "$CHAVE" \
  -authenticationKeyID Z53W4BVFVV -authenticationKeyIssuerID "$ISSUER"
```

A Apple leva alguns minutos processando. Depois, para o build chegar ao
testador, ainda é preciso anexá-lo ao grupo interno e escrever a nota
"O que testar", pelo site do App Store Connect ou pela API
(`POST /v1/betaGroups/{id}/relationships/builds` e
`PATCH /v1/betaBuildLocalizations/{id}`; a localização pt-BR já existe, então
é PATCH, não POST — o POST responde 409).

`DEVELOPMENT_TEAM = C78J3R5666` já está no `project.pbxproj`. Sem ele o
archive sai assinado com o certificado de desenvolvimento e não sobe.

## Ver uma tela no simulador sem fazer login

Injete uma sonda em `ios/App/App/public/index.html` — a **cópia**, nunca em
`www/`; o próximo `npm run sync` apaga. Num `<script>` colocado **antes** do
`app.js`, remova `access_token` e `refresh_token` do `localStorage`: sem isso
a fila tenta subir os dados de mentira para produção. Depois, no `load`, grave
recibos com `salvarRecibo()` e chame `mostrarHistorico()`, `abrirRecibo()` etc.

`open -a Simulator` é obrigatório: sem janela o WKWebView não compõe e a
captura sai preta. A captura é `xcrun simctl io <id> screenshot x.png`, que
não pede permissão de gravação de tela. Ao terminar,
`xcrun simctl uninstall <id> br.com.recibotaxi.app` apaga os dados da sonda.

Três armadilhas que já custaram tempo:

- **Rode `npm run sync` ANTES de instalar a sonda.** Senão a sonda anterior
  continua no arquivo e as duas rodam juntas — uma abre o menu, a outra mede
  outra coisa, e você depura um estado que não existe.
- **Limpe o `localStorage` no topo da sonda**, não só os tokens. O aparelho
  guarda o estado da execução passada e ele reaparece na seguinte.
- **`xcrun simctl uninstall` antes de instalar**, para o IndexedDB não vir
  junto da rodada anterior.
- **Apague as cópias que o iCloud cria.** A pasta do projeto está sincronizada,
  e o iCloud duplica arquivo com um número no nome: `index.html` vira também
  `index 2.html`. O `cap sync` só sobrescreve os arquivos que conhece, então a
  cópia sobrevive, e o Xcode empacota tudo o que estiver em `public/`. Foi
  assim que um `index 2.html` **com sonda dentro** subiu para o TestFlight nos
  builds 6 a 10. O app carrega `index.html` e nunca executou a sonda, mas
  código de teste não viaja junto com o app. A suíte agora barra isso; para
  limpar à mão:

  ```bash
  find . -name "* [0-9].*" -not -path "*/node_modules/*" -not -path "*/.git/*" \
    -not -path "*/DerivedData/*" -delete
  ```

E cuidado com `grep -c` dentro de uma cadeia `&&`: contagem zero devolve
status 1 e o resto da linha não roda. Já aconteceu de a sonda nunca ser
instalada e eu passar vinte minutos investigando o app limpo.


## Campo de data e hora do iOS

`input[type="date"]` e `input[type="time"]` não encolhem abaixo da largura do
próprio texto. Numa tela de 402 pt, os dois lado a lado mediam 207 pt cada numa
coluna de 177, e a página passava a rolar de lado. Diminuir a fonte não muda
nada — o mínimo é do controle nativo, não do texto. `min-width:0` no item do
grid também não basta.

O que resolve é `-webkit-appearance:none`. O campo passa a obedecer à coluna,
continua mostrando a data por extenso e continua abrindo o seletor do sistema.
Medido no simulador, com `getBoundingClientRect()`, antes e depois.
