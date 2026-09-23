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

**Pacote atual:** `android/app/build/outputs/bundle/release/app-release.aab`, versionCode **16**
(19/09/2026: recuperação de senha pelo app, links de Termos e Privacidade na tela de
assinatura, aviso de cancelamento "pelo Google Play" e a chave `goog_` do RevenueCat em
`www/config.js`; o 14 foi publicado em teste fechado em 16/09 e o 15 subiu em 19/09 sem a
chave). Gerar exige
`JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home`: o Java do
Homebrew não está no PATH e o `java_home` do sistema não o enxerga. Regerar com `./gradlew bundleRelease`
após `npx cap sync android` sempre que `www/` mudar.

### Descrição completa (a da App Store, com o cancelamento "pelo Google Play" em vez de "pelos Ajustes do aparelho")

```
Recibo para o passageiro, na hora, sem depender de internet.

O Recibo Táxi foi feito para quem trabalha dirigindo. Você preenche o nome do passageiro, de onde saiu, para onde foi e quanto deu. O recibo fica pronto na mesma tela, com seus dados de motorista, sua placa e seu telefone.

FUNCIONA SEM SINAL
Garagem, subsolo, estrada, túnel. O recibo é montado dentro do aparelho e guardado ali mesmo. Quando o sinal volta, ele sobe sozinho. Você nunca fica esperando a internet com o passageiro dentro do carro.

ENTREGA DIRETO NO WHATSAPP DO PASSAGEIRO
Digite o número e toque em enviar. O app abre a conversa certa, com a mensagem pronta e o link do recibo. Não precisa salvar o contato antes.

TAMBÉM POR E-MAIL E EM PDF
O recibo vira PDF no próprio aparelho, para compartilhar por onde você quiser. E dá para enviar por e-mail quando o passageiro pedir.

PARA QUEM VIAJA A TRABALHO
CPF ou CNPJ do passageiro no recibo, e razão social da empresa quando for CNPJ. É o que a contabilidade dele precisa para lançar a despesa.

TRAZ CORRIDA DE VOLTA
Todo recibo sai com seu WhatsApp e um convite para te chamar de novo. O recibo circula, e o telefone vai junto.

HISTÓRICO SEMPRE À MÃO
Toque em qualquer recibo do histórico para reenviar, mesmo dias depois.

PLANO GRÁTIS E PLANO PRO
O plano Grátis já resolve o dia a dia. O Pro libera recibos ilimitados e histórico permanente por R$ 19,90 por mês, renovado automaticamente, cancelável quando quiser pelo Google Play.

Este recibo não é documento fiscal.
```

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
   e assinaturas, gerenciar presença na loja. **Feito em 19/09/2026**
   (`revenuecat@recibo-taxi.iam.gserviceaccount.com`, JSON em
   `~/Credenciais/Recibo-Taxi/google-play/`). A página "Acesso à API" do console
   não existe mais: a conta de serviço é convidada como usuário comum. O RevenueCat
   também exige a API Pub/Sub ativada no projeto Cloud e o papel *Pub/Sub Admin*
   para a conta de serviço (notificações em tempo real).
5. **Até 36 horas** para as credenciais propagarem. Em 19/09 as leituras de catálogo
   passaram na hora e só "validar compras" ficou pendente — é essa a que demora.

A chave `goog_VckMEzIGFTMkTrkmKdxCaRIXPBs` está em `www/config.js` desde o build 16.
