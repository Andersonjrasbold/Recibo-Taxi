# Google Play — o caminho até a publicação

Anotações do que foi verificado na fonte oficial em 14/09/2026. O que muda de
ano para ano aqui é muito: confira as datas antes de confiar.

## A conta

Tipo **Organização**, não pessoal. A diferença não é cosmética:

> "(Personal accounts only) Step 6: Meet testing requirements"
> — [Criar conta](https://support.google.com/googleplay/android-developer/answer/6112435)

A regra dos **12 testadores por 14 dias corridos** vale só para conta pessoal
criada depois de 13/11/2023. Organização é isenta e vai direto para produção.

| | |
|---|---|
| Taxa | US$ 25, uma vez. Cartão no nome legal, pré-pago não serve |
| D-U-N-S | `626996147` — obrigatório para organização, o mesmo da Apple |
| Também exige | Documento de identidade, documento oficial da empresa, site e telefone da organização |
| Verificação | Costuma levar de 2 a 4 semanas |

Conta pessoal **pode** ser convertida em organização sem perder apps, avaliações
ou histórico: *Conta de desenvolvedor → Sobre você*, verificar o site primeiro,
depois **Alterar tipo de conta**. É de mão única — o Google não converte de
volta.

## Requisitos de plataforma (conferidos contra este app)

| Regra | Desde | Como estamos |
|---|---|---|
| `targetSdk` ≥ 36 | 31/08/2026 | **OK** — já é 36 |
| Páginas de 16 KB | 01/02/2027 | **OK por construção** — o pacote não tem nenhum `.so` |
| AAB, não APK | — | **OK** — `bundleRelease` |
| Play App Signing | — | Chave de upload em `~/Credenciais/Recibo-Taxi/google-play/` |

O `targetSdk 36` cobra dois preços, e os dois já foram pagos no código:

- **Edge-to-edge obrigatório.** O app desenha sob a barra de status e a de
  navegação, e o Android 16 ignora o pedido de desligar isso. O CSS já usa
  `env(safe-area-inset-*)`; **falta conferir no aparelho**.
- **Predictive back.** `onBackPressed` não é mais chamado. O Capacitor não se
  importa: ele registra pelo `OnBackPressedDispatcher` do AndroidX, que continua
  valendo. O que faltava era alguém escutar do lado do JS — ver `voltarUmNivel()`
  em `www/app.js`.

## Exclusão de conta: são duas, não uma

O Play exige caminho **dentro do app** e **um link na web** que abra sem login,
para quem já desinstalou:

> "provide users with an in-app path to delete their app accounts (...) **and**
> provide a web link resource where users can request app account deletion"

A URL a declarar é `https://www.recibotaxi.com.br/excluir-conta` — **com `www`**: o apex
`recibotaxi.com.br` não tem registro DNS no Registro.br e não responde (conferido em 15/09/2026).

## Verificação de desenvolvedor Android — prazo 30/09/2026

Coisa **separada** de publicar na loja, e com data curta. A partir de 30/09/2026,
aparelhos Android certificados no **Brasil**, Indonésia, Singapura e Tailândia
bloqueiam a instalação normal de app cujo desenvolvedor não registrou identidade —
venha ele da Play ou de qualquer outra loja participante. Apps já publicados na Play
que não estiverem registrados são **removidos globalmente**.

Ter conta no Play Console **não basta sozinho**: apps distribuídos fora da loja
precisam de registro manual do par *nome do pacote + chave de assinatura*.

| | |
|---|---|
| Nome do pacote | `br.com.recibotaxi.app` |
| Chave de upload, SHA-256 | `44:5B:A8:B2:68:5F:79:6A:2C:98:D5:37:C6:BA:0C:E7:6B:A3:12:C6:EB:F0:22:D5:1B:BF:95:EA:AC:CA:68:DB` |
| Chave de upload, SHA-1 | `F0:DC:BF:4F:1C:FB:DD:72:BB:67:A0:8B:7B:A0:F6:9E:A3:B5:D8:2A` |

**O campo da impressão digital só aceita um formato, e não é nenhum dos dois que o
Google imprime.** Testado em 14/09/2026:

| Forma | Origem | Resultado |
|---|---|---|
| `44:5B:A8:...` maiúscula com dois-pontos | saída do `keytool` | **recusado** |
| `445ba8b2...` minúscula contínua | saída do `apksigner` | não testado |
| `445BA8B2...` **maiúscula contínua** | nenhuma ferramenta imprime assim | **aceito** |

Ou seja: pegue a saída do `keytool` e tire os dois-pontos, sem mexer no caixa. Se
aparecer "impressão digital inválida", é isso — o valor está certo, a forma é que não.

Essa é a **nossa** chave, a que assina o APK que instalamos direto no aparelho. O que
sai pela Play é assinado pelo Google (Play App Signing) e entra pelo registro
automático quando o app existir no console.

Sideload de app não registrado continua possível pelo *advanced flow* e por `adb`,
mas com atrito — não serve para entregar a taxista.

## Ficha da loja

| Campo | Limite | Valor |
|---|---|---|
| Nome | 30 | `Recibo Táxi` |
| Descrição breve | 80 | `Emita e envie recibos de corrida em segundos, mesmo sem internet.` |
| Descrição completa | 4000 | ver abaixo |
| Ícone | 512×512 PNG | de `assets/marca/` |
| Gráfico de destaque | 1024×500, sem transparência | `assets/loja/play-grafico-destaque-1024x500.png` |
| Capturas | mín. 2, proporção entre 16:9 e 9:16 | `assets/loja/play-captura-{1..4}-*.png`, 1080×1920, geradas das capturas do iOS sem a barra de status (15/09/2026) |

As capturas do iOS cruas **não servem**: 1320×2868 é mais alto que 9:16. As de `assets/loja` já vêm
recortadas (sem a barra de status do iPhone) e centralizadas num fundo 1080×1920.

**Pacote atual:** `android/app/build/outputs/bundle/release/app-release.aab`, versionCode **14**
(15/09/2026, com o código do app após a correção do erro 23). Regerar com `./gradlew bundleRelease`
após `npx cap sync android` sempre que `www/` mudar.

## Segurança dos Dados — respostas

Coletado, nada vendido, nada compartilhado para publicidade. Transferências
para Supabase, Resend, RevenueCat e Stripe são de *operadores* processando em
nosso nome, o que o Google não classifica como compartilhamento.

| Categoria | Item | Por quê |
|---|---|---|
| Informações pessoais | Nome, e-mail, telefone | Conta do motorista |
| Informações pessoais | CPF, placa, alvará | Identificação no recibo |
| Informações pessoais | Nome, CPF/CNPJ, telefone e e-mail do passageiro | Vão impressos no recibo |
| Informações pessoais | Endereço | Origem e destino, digitados pelo motorista |
| Financeiras | Histórico de compras | Assinatura do plano Pro |

- Criptografia em trânsito: **sim** (HTTPS).
- Usuário pode pedir exclusão dos dados: **sim**, com a URL acima.
- Localização do aparelho: **não**. O app não pede permissão de GPS — o
  manifesto só declara `INTERNET`.

## Assinatura: a ordem importa

Nada de compra funciona antes disto, e a ordem não é negociável:

1. App criado no console e **AAB enviado**, com a revisão da faixa concluída.
2. App numa faixa **fechada ou aberta**, com pelo menos um testador.
3. Assinatura criada e **ativa** (produto, plano base, oferta).
4. Service account convidada em *Usuários e permissões* com as quatro
   permissões: ver informações do app, ver dados financeiros, gerenciar pedidos
   e assinaturas, gerenciar presença na loja.
5. **Até 36 horas** para as credenciais propagarem. Antes disso o RevenueCat
   responde "Invalid Play Store credentials" e a compra falha.

Só depois disso a chave `goog_` entra em `www/config.js`.
