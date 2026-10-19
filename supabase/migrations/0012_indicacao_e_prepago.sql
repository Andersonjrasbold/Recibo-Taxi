-- Indicação de colegas e Pro pré-pago.
--
-- Duas formas de Pro que não são assinatura: o anual pago uma vez (Pix ou
-- cartão, fora das lojas) e o mês ganho por indicar um colega que começou a
-- usar. As duas vivem em `drivers.pro_until`: o cron diário rebaixa para o
-- Grátis quando a data passa. Quem tem assinatura (Stripe ou loja) não
-- depende de pro_until — se também tiver dias pré-pagos, eles valem quando a
-- assinatura acabar.
--
-- Pode rodar antes do código novo: tudo nasce nulo e o código de hoje não
-- lê nem escreve nestas colunas.

alter table public.drivers
  add column if not exists referral_code               text,
  add column if not exists referred_by                 uuid references public.drivers (id) on delete set null,
  add column if not exists pro_until                   timestamptz,
  add column if not exists pro_reminder_sent_at        timestamptz,
  add column if not exists referral_coupon_granted_at  timestamptz,
  add column if not exists referral_coupon_used_at     timestamptz;

-- O código é gerado pelo servidor na primeira vez que o motorista abre a
-- página de indicação (ou pede a sessão no app): quem nunca indica não ganha
-- código à toa.
create unique index if not exists drivers_referral_code_key
  on public.drivers (referral_code)
  where referral_code is not null;
create index if not exists drivers_referred_by_idx
  on public.drivers (referred_by)
  where referred_by is not null;
-- O cron procura quem venceu e quem vence em 15 dias.
create index if not exists drivers_pro_until_idx
  on public.drivers (pro_until)
  where pro_until is not null;

-- Uma linha por indicado. activated_at marca o primeiro recibo dele, que é o
-- que libera a recompensa de quem indicou; reward diz o que foi dado.
create table if not exists public.referrals (
  id           uuid        primary key default gen_random_uuid(),
  referrer_id  uuid        not null references public.drivers (id) on delete cascade,
  referred_id  uuid        not null unique references public.drivers (id) on delete cascade,
  created_at   timestamptz not null default now(),
  activated_at timestamptz,
  reward       text        not null default '',

  constraint referrals_nao_se_indica_ck check (referrer_id <> referred_id)
);

create index if not exists referrals_referrer_idx
  on public.referrals (referrer_id, created_at desc);

-- Toda tabela de public fecha a Data API (migração 0006).
alter table public.referrals enable row level security;

comment on column public.drivers.pro_until is
  'Fim do Pro pré-pago (anual por Pix ou mês de indicação). Nulo para assinantes.';
comment on table public.referrals is
  'Quem indicou quem. activated_at = primeiro recibo do indicado; reward = o que o indicador ganhou.';
