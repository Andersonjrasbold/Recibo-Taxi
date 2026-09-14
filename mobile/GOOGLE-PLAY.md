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

A URL a declarar é `https://recibotaxi.com.br/excluir-conta`.

## Ficha da loja

| Campo | Limite | Valor |
|---|---|---|
| Nome | 30 | `Recibo Táxi` |
| Descrição breve | 80 | `Emita e envie recibos de corrida em segundos, mesmo sem internet.` |
| Descrição completa | 4000 | ver abaixo |
| Ícone | 512×512 PNG | de `assets/marca/` |
| Gráfico de destaque | 1024×500, sem transparência | **falta gerar** |
| Capturas | mín. 2, proporção entre 16:9 e 9:16 | **falta gerar** — 1080×1920 serve |

As capturas do iOS **não servem**: 1320×2868 é mais alto que 9:16.

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
