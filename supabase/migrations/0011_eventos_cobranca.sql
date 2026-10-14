-- Eventos de cobrança: tudo que o RevenueCat e a Stripe mandam ao servidor.
--
-- Até aqui o webhook mudava `drivers.plan` e esquecia o evento. Sem o
-- histórico, o painel não respondia quando alguém assinou, renovou, cancelou
-- ou foi rebaixado, nem por que uma compra não virou Pro (App User ID
-- anônimo, motorista excluído). O RevenueCat guarda isso no painel dele; aqui
-- fica a cópia que o /admin lê, junto com o que o servidor fez em cada caso.
--
-- O registro também é memória: um App User ID anônimo que chegou com o id do
-- motorista em `aliases` fica ligado a ele, e o evento seguinte que vier só
-- com o anônimo ainda acha o motorista.
create table if not exists public.billing_events (
  id           uuid        primary key default gen_random_uuid(),
  source       text        not null check (source in ('revenuecat', 'stripe')),
  -- id do evento na origem. Vazio só quando a origem não mandou (a suíte).
  event_id     text        not null default '',
  -- posição dentro do evento: um TRANSFER mexe em dois motoristas e vira duas
  -- linhas (seq 0 e 1). Com event_id, (source, event_id, seq) é único, então o
  -- reenvio do mesmo evento pela origem não duplica.
  seq          smallint    not null default 0,
  type         text        not null,
  driver_id    uuid        references public.drivers (id) on delete cascade,
  app_user_id  text        not null default '',   -- como veio; pode ser $RCAnonymousID
  store        text        not null default '',   -- APP_STORE, PLAY_STORE, STRIPE
  environment  text        not null default '',   -- PRODUCTION, SANDBOX, TEST
  product_id   text        not null default '',
  cycle        text        not null default '',   -- mensal, anual ou vazio
  price        numeric(12,2),
  currency     text        not null default '',
  outcome      text        not null default '',   -- o que o servidor fez
  payload      jsonb       not null default '{}'::jsonb,
  received_at  timestamptz not null default now(),

  constraint billing_events_payload_is_object_ck check (jsonb_typeof(payload) = 'object')
);

create unique index if not exists billing_events_origem_key
  on public.billing_events (source, event_id, seq)
  where event_id <> '';

-- A ficha do motorista lista os eventos dele; o painel, os últimos de todos.
create index if not exists billing_events_driver_idx
  on public.billing_events (driver_id, received_at desc)
  where driver_id is not null;
create index if not exists billing_events_received_idx
  on public.billing_events (received_at desc);
-- Procura do motorista por um App User ID anônimo já visto.
create index if not exists billing_events_app_user_idx
  on public.billing_events (app_user_id)
  where driver_id is not null;

-- Toda tabela de public fecha a Data API (migração 0006).
alter table public.billing_events enable row level security;

comment on table public.billing_events is
  'Webhooks de cobrança (RevenueCat e Stripe) como chegaram, e o que o servidor fez com cada um.';
