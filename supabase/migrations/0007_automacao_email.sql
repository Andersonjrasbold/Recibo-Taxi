-- ─────────────────────────────────────────────────────────────────────────────
-- Automação de e-mails para quem ainda não assinou o Pro.
--
-- Cada modelo é um e-mail com a sua própria programação ("3 dias depois do
-- cadastro, a partir das 10h"). O cron /tarefas/emails roda de hora em hora,
-- acha quem está na vez e manda. Tudo se edita em /admin/emails.
--
-- O motor nasce DESLIGADO (email_settings.enabled = false): nenhum e-mail sai
-- até alguém revisar os textos e ligar pelo painel.
-- ─────────────────────────────────────────────────────────────────────────────

-- Descadastro. Fica no perfil e não numa tabela à parte para a consulta do
-- motor ser um filtro simples, e para a exclusão da conta levar junto.
alter table public.drivers
  add column if not exists marketing_optout_at timestamptz;

-- ── Modelos ─────────────────────────────────────────────────────────────────
create table if not exists public.email_templates (
  id            uuid primary key default gen_random_uuid(),
  name          varchar(80)  not null,
  subject       varchar(150) not null,
  body          text         not null check (char_length(body) <= 5000),
  button_text   varchar(60)  not null default '',
  button_url    varchar(300) not null default '',
  -- signup        N dias depois do cadastro
  -- no_receipt    N dias depois do cadastro, se ainda não emitiu recibo
  -- monthly_usage chegou a N recibos no mês (uma vez por mês)
  -- inactive      N dias sem emitir, para quem já emitiu
  -- scheduled     campanha em data marcada (N = cadastrado há pelo menos N dias)
  trigger       text         not null
                  check (trigger in ('signup', 'no_receipt', 'monthly_usage', 'inactive', 'scheduled')),
  trigger_value integer      not null default 0 check (trigger_value between 0 and 365),
  send_hour     smallint     not null default 10 check (send_hour between 0 and 23),
  scheduled_at  timestamptz,
  position      smallint     not null default 0,
  active        boolean      not null default false,
  created_at    timestamptz  not null default now(),
  updated_at    timestamptz  not null default now(),

  constraint email_templates_scheduled_ck check (trigger <> 'scheduled' or scheduled_at is not null or not active)
);

-- ── Envios ──────────────────────────────────────────────────────────────────
-- A linha nasce ANTES do envio (status 'sending'): o unique é o que garante
-- que duas execuções do cron não mandem o mesmo e-mail duas vezes.
-- dedupe_key: '' para os e-mails de uma vez só; o mês ('2026-10') para o de
-- uso no mês; a data do último recibo para o de inatividade, que volta a
-- valer se o motorista emitir de novo e parar outra vez.
create table if not exists public.email_sends (
  id          bigint generated always as identity primary key,
  template_id uuid        not null references public.email_templates (id),
  driver_id   uuid        not null references public.drivers (id) on delete cascade,
  dedupe_key  text        not null default '',
  status      text        not null default 'sending' check (status in ('sending', 'sent', 'failed')),
  provider_id text,
  error       text,
  created_at  timestamptz not null default now(),
  sent_at     timestamptz,

  constraint email_sends_once_key unique (template_id, driver_id, dedupe_key)
);

-- Intervalo mínimo entre e-mails do mesmo motorista e histórico na ficha.
create index if not exists email_sends_driver_idx on public.email_sends (driver_id, created_at desc);
-- Teto diário e lista dos últimos envios no painel.
create index if not exists email_sends_created_idx on public.email_sends (created_at desc);

-- ── Configuração (uma linha só) ─────────────────────────────────────────────
create table if not exists public.email_settings (
  id            boolean primary key default true check (id),
  enabled       boolean     not null default false,
  daily_cap     integer     not null default 80 check (daily_cap between 0 and 10000),
  min_gap_hours integer     not null default 20 check (min_gap_hours between 0 and 720),
  updated_at    timestamptz not null default now()
);

insert into public.email_settings (id) values (true) on conflict (id) do nothing;

-- ── Data API fechada, como nas outras tabelas (ver 0006) ────────────────────
alter table public.email_templates enable row level security;
alter table public.email_sends     enable row level security;
alter table public.email_settings  enable row level security;

revoke all on public.email_templates from anon, authenticated;
revoke all on public.email_sends     from anon, authenticated;
revoke all on public.email_settings  from anon, authenticated;

-- ── Modelos iniciais ────────────────────────────────────────────────────────
-- Só entram se a tabela estiver vazia: rodar a migração de novo não duplica
-- nada nem desfaz o que foi editado no painel.
insert into public.email_templates
  (position, name, trigger, trigger_value, send_hour, active, subject, body, button_text, button_url)
select * from (values

(1, 'Boas-vindas', 'signup', 0, 8, true,
 'Bem-vindo ao Recibo Táxi, {{nome}}',
$t$Olá, {{nome}}!

Que bom ter você no Recibo Táxi. A partir de agora, o passageiro que pedir recibo recebe um documento profissional, com os seus dados, QR Code e um link para conferir — direto no WhatsApp ou no e-mail dele.

Para emitir o primeiro:
- Abra o app e preencha passageiro, origem, destino e valor
- Toque em **Emitir recibo**
- Envie pelo WhatsApp ou por e-mail, na hora

O app funciona até sem internet: o recibo fica guardado no celular e sobe sozinho quando o sinal voltar.

No plano Grátis você tem {{limite}} recibos por mês. Boas corridas!$t$,
 'Emitir meu primeiro recibo', '{{link_app}}'),

(2, 'Lembrete do primeiro recibo', 'no_receipt', 2, 10, true,
 '{{nome}}, seu primeiro recibo leva 30 segundos',
$t$Olá, {{nome}}.

Vimos que você ainda não emitiu nenhum recibo. Na próxima vez que um passageiro pedir, experimente pelo app — é mais rápido que o bloquinho de papel:
- Passageiro, origem, destino e valor
- Um toque em **Emitir recibo**
- Pronto: o recibo sai com seu nome, placa e QR Code, e vai pelo WhatsApp ou por e-mail

Se travou em alguma parte, responda este e-mail que a gente ajuda.$t$,
 'Abrir o Recibo Táxi', '{{link_app}}'),

(3, 'Passageiro de empresa', 'signup', 4, 10, true,
 'O que o passageiro de empresa precisa no recibo',
$t$Olá, {{nome}}.

Quem viaja a trabalho quase sempre precisa do recibo para pedir reembolso. E o setor financeiro da empresa costuma devolver recibo incompleto ou ilegível.

No Recibo Táxi o recibo já sai com tudo no lugar:
- Seu nome e placa e, se você cadastrou, prefixo e alvará
- Nome do passageiro e, se ele pedir, CPF ou CNPJ com razão social
- Data, horário, origem, destino, valor e forma de pagamento
- QR Code e link para qualquer pessoa conferir que o recibo é verdadeiro

Passageiro de empresa costuma voltar a chamar quem facilita a vida dele.$t$,
 'Ver como fica o recibo', '{{link_painel}}'),

(4, 'Conheça o Pro', 'signup', 7, 10, true,
 'Recibos sem limite por menos que uma corrida',
$t$Olá, {{nome}}.

O plano Grátis tem {{limite}} recibos por mês. Para quem roda todo dia, isso acaba rápido — e acaba justamente quando um passageiro está pedindo recibo.

O **Recibo Táxi Pro** custa {{preco}} por mês, menos que uma corrida curta, e tira esse limite:
- Recibos ilimitados
- Histórico completo e guardado para sempre
- Envio por WhatsApp e e-mail, QR Code e link público

Para assinar, abra o app, toque no menu e depois em **Recibo Táxi Pro**. Sem fidelidade: você cancela quando quiser pela App Store ou pelo Google Play.$t$,
 'Conhecer o Pro', '{{link_app}}'),

(5, 'Quase no limite do mês', 'monthly_usage', 4, 9, true,
 'Faltam {{restantes}} recibos grátis este mês',
$t$Olá, {{nome}}.

Você já emitiu {{recibos_mes}} dos {{limite}} recibos grátis de {{mes}}. Ótimo sinal: seus passageiros estão pedindo recibo.

Quando o limite acabar, o app só volta a emitir no dia 1º. Para não correr o risco de deixar um passageiro sem recibo no meio do mês, o **Pro** libera recibos ilimitados por {{preco}} por mês.

No app: menu → **Recibo Táxi Pro**.$t$,
 'Liberar recibos ilimitados', '{{link_app}}'),

(6, 'Limite do mês atingido', 'monthly_usage', 6, 8, true,
 'Você usou os {{limite}} recibos grátis de {{mes}}',
$t$Olá, {{nome}}.

Você chegou aos {{limite}} recibos grátis deste mês. Até o dia 1º, o app não emite recibos novos no plano Grátis.

Se aparecer passageiro pedindo recibo antes disso, o **Pro** resolve na hora: assinou, liberou. São {{preco}} por mês, com recibos ilimitados e todo o seu histórico guardado.

No app: menu → **Recibo Táxi Pro**. Sem fidelidade, cancela quando quiser.$t$,
 'Assinar o Pro agora', '{{link_app}}'),

(7, 'Histórico organizado', 'signup', 10, 10, true,
 'Achou o recibo daquela corrida?',
$t$Olá, {{nome}}.

Passageiro pedindo segunda via, empresa conferindo um reembolso, você querendo saber quanto rodou no mês: tudo isso fica fácil quando os recibos estão guardados num lugar só.

No **Pro**, o histórico é completo e permanente. Cada recibo continua com o mesmo link, para você reenviar quando precisar.$t$,
 'Ver meu histórico', '{{link_painel}}'),

(8, 'Dúvidas sobre o Pro', 'signup', 14, 10, true,
 'Pro: as dúvidas mais comuns',
$t$Olá, {{nome}}. Respondemos aqui o que mais perguntam sobre o Pro.

**Tem fidelidade?**
Não. Você cancela quando quiser e o Pro vale até o fim do período já pago.

**Como eu cancelo?**
Pela App Store (iPhone) ou pelo Google Play (Android), em Assinaturas.

**Funciona sem internet?**
Sim. O recibo é emitido no celular e sobe quando o sinal voltar.

**O recibo é nota fiscal?**
Não. É um comprovante de pagamento da corrida, com os seus dados e os do passageiro.

**Como eu assino?**
No app: menu → **Recibo Táxi Pro**. São {{preco}} por mês.

Ficou outra dúvida? É só responder este e-mail.$t$,
 'Conhecer o Pro', '{{link_app}}'),

(9, 'Plano anual (ligar quando o anual sair no app)', 'signup', 21, 10, false,
 'Pro anual: metade do preço',
$t$Olá, {{nome}}.

Para quem já sabe que vai usar o ano todo, o **Pro anual** custa metade do mensal: R$ 119,90 por ano no iPhone e R$ 119,99 no Android — menos de R$ 10 por mês.

É o mesmo Pro, com recibos ilimitados e histórico completo, pago uma vez só. No app: menu → **Recibo Táxi Pro** → plano anual.$t$,
 'Ver o plano anual no app', '{{link_app}}'),

(10, 'Sentimos sua falta', 'inactive', 14, 10, true,
 'Tudo certo por aí, {{nome}}?',
$t$Olá, {{nome}}.

Faz duas semanas que você não emite um recibo pelo Recibo Táxi. Aconteceu alguma coisa? Se algo não funcionou como deveria, responda este e-mail e conte para a gente — lemos todas as mensagens.

Se foi só uma fase mais parada, o app continua pronto para o próximo passageiro que pedir recibo.$t$,
 'Emitir um recibo', '{{link_app}}'),

(11, 'Um mês de Recibo Táxi', 'signup', 30, 10, true,
 'Um mês de Recibo Táxi, {{nome}}',
$t$Olá, {{nome}}.

Faz um mês que você entrou no Recibo Táxi. Esperamos que o recibo digital já tenha poupado algum tempo — e algum bloquinho de papel.

Se ele já entrou na sua rotina, o **Pro** tira a preocupação com o limite de {{limite}} por mês: recibos ilimitados, histórico guardado para sempre, por {{preco}} por mês.

No app: menu → **Recibo Táxi Pro**.$t$,
 'Assinar o Pro', '{{link_app}}'),

(12, 'Pedido de opinião', 'signup', 45, 10, true,
 'Uma pergunta rápida, {{nome}}',
$t$Olá, {{nome}}.

Uma pergunta só, e a resposta ajuda muito: **o que falta no Recibo Táxi para valer {{preco}} por mês para você?**

Pode ser uma função, um preço diferente, uma dificuldade no app — qualquer coisa. É só responder este e-mail. Lemos todas as respostas.

Obrigado!$t$,
 '', ''),

(13, 'Campanha para a base atual (exemplo)', 'scheduled', 3, 10, false,
 '{{nome}}, conheça o Recibo Táxi Pro',
$t$Olá, {{nome}}.

Obrigado por usar o Recibo Táxi. Quem já se cadastrou antes desta régua de e-mails começar não recebeu as nossas dicas — então vai aqui um resumo.

O plano Grátis tem {{limite}} recibos por mês. O **Pro**, por {{preco}} por mês, libera:
- Recibos ilimitados
- Histórico completo e permanente
- Envio por WhatsApp e e-mail, QR Code e link público

No app: menu → **Recibo Táxi Pro**. Sem fidelidade.$t$,
 'Conhecer o Pro', '{{link_app}}')

) as seed (position, name, trigger, trigger_value, send_hour, active, subject, body, button_text, button_url)
where not exists (select 1 from public.email_templates);
