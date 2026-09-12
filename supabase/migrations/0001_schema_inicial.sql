-- ─────────────────────────────────────────────────────────────────────────────
-- Recibo Táxi — schema inicial (PR 1: camada de dados)
--
-- Nesta etapa a autenticação continua sendo do próprio app (password_hash aqui).
-- A PR 2 liga drivers.id a auth.users(id) do Supabase Auth e remove a coluna.
-- Por isso ainda não há RLS: o app conecta como dono das tabelas e a regra de
-- quem vê o quê continua em Python, como hoje.
-- ─────────────────────────────────────────────────────────────────────────────

create extension if not exists pgcrypto;

-- ── Motoristas ──────────────────────────────────────────────────────────────
create table public.drivers (
  id                     uuid primary key default gen_random_uuid(),
  email                  varchar(254) not null,
  password_hash          text         not null,
  password_changed_at    timestamptz,
  full_name              varchar(120) not null,
  cpf                    varchar(14)  not null,
  whatsapp               varchar(20)  not null,
  city                   varchar(80)  not null,
  plate                  varchar(10)  not null,
  vehicle_model          varchar(80)  not null default '',
  taxi_prefix            varchar(20)  not null default '',
  license_number         varchar(40)  not null default '',
  plan                   text         not null default 'free'
                           check (plan in ('free', 'pro', 'business')),
  stripe_customer_id     text,
  stripe_subscription_id text,
  subscription_status    text,
  created_at             timestamptz  not null default now(),
  updated_at             timestamptz  not null default now()
);

create unique index drivers_email_key on public.drivers (lower(email));

-- Parcial: só linhas com customer_id entram, então vários NULL convivem.
create unique index drivers_stripe_customer_key
  on public.drivers (stripe_customer_id)
  where stripe_customer_id is not null;

-- ── Recibos ─────────────────────────────────────────────────────────────────
-- rid com 14 hex (2^56). Com 10 hex eram 2^40 e a chance de colisão passava
-- de 36% em 1 milhão de recibos.
create table public.receipts (
  rid                text primary key check (rid ~ '^[0-9A-F]{14}$'),
  driver_id          uuid references public.drivers (id) on delete cascade,
  is_guest           boolean      not null default false,
  passenger          varchar(120) not null,
  passenger_email    varchar(254) not null default '',
  passenger_whatsapp varchar(20)  not null default '',
  trip_date          date         not null,
  trip_time          varchar(5)   not null default '',
  origin             varchar(200) not null,
  destination        varchar(200) not null,
  amount             numeric(7,2) not null check (amount > 0 and amount <= 99999.99),
  payment_method     varchar(40)  not null default 'Pix',
  notes              varchar(500) not null default '',
  driver_snapshot    jsonb        not null,
  created_at         timestamptz  not null default now(),

  -- Ou é de convidado e não tem dono, ou tem dono e não é de convidado.
  constraint receipts_guest_owner_ck check (
    (is_guest and driver_id is null) or (not is_guest and driver_id is not null)
  ),
  constraint receipts_snapshot_is_object_ck check (
    jsonb_typeof(driver_snapshot) = 'object'
  )
);

-- Atende o histórico do painel E a contagem mensal por motorista.
create index receipts_driver_created_idx
  on public.receipts (driver_id, created_at desc)
  where driver_id is not null;

-- Atende a rotina de retenção dos recibos sem cadastro.
create index receipts_guest_created_idx
  on public.receipts (created_at)
  where is_guest;

-- ── Contadores de rate limit ────────────────────────────────────────────────
create table public.rate_limits (
  key        text primary key,
  count      integer     not null default 0 check (count >= 0),
  created_at timestamptz not null default now()
);

create index rate_limits_created_idx on public.rate_limits (created_at);

-- ── Cota mensal do plano Grátis ─────────────────────────────────────────────
-- Existe para acabar com a corrida do check-then-insert: contar e depois
-- inserir deixa uma janela em que duas emissões simultâneas passam as duas.
create table public.receipt_quotas (
  driver_id uuid    not null references public.drivers (id) on delete cascade,
  period    date    not null,           -- 1º dia do mês no fuso de Brasília
  used      integer not null default 0 check (used >= 0),
  primary key (driver_id, period)
);

create or replace function public.br_period(p_at timestamptz default now())
returns date
language sql
immutable
as $$
  select date_trunc('month', p_at at time zone 'America/Sao_Paulo')::date;
$$;

-- Incremento atômico com teto. O `where used < p_limit` é o que fecha a
-- corrida: se o teto já foi batido, o UPDATE não acontece e o RETURNING vem
-- vazio, então a função devolve false sem precisar de transação externa.
create or replace function public.consume_receipt_quota(
  p_driver uuid,
  p_limit  integer
)
returns boolean
language plpgsql
as $$
declare
  v_used integer;
begin
  insert into public.receipt_quotas as q (driver_id, period, used)
  values (p_driver, public.br_period(), 1)
  on conflict (driver_id, period) do update
    set used = q.used + 1
    where q.used < p_limit
  returning q.used into v_used;

  return v_used is not null;
end;
$$;

-- Mesma ideia para o contador de rate limit: uma ida ao banco, sem corrida.
create or replace function public.bump_counter(p_key text)
returns integer
language sql
as $$
  insert into public.rate_limits as r (key, count)
  values (p_key, 1)
  on conflict (key) do update
    set count = r.count + 1
  returning r.count;
$$;
