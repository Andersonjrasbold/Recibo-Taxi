-- País e moeda do motorista, e a moeda de cada recibo.
--
-- O app está à venda em 175 países e já há motorista de fora (Costa Rica,
-- 2026-10) emitindo recibo com "R$" na frente de um valor em colones. Quem
-- decide o país e a moeda é o motorista, no cadastro ou no perfil; o recibo
-- guarda a moeda de quando foi emitido, como já guarda os dados do motorista
-- no driver_snapshot — trocar de moeda depois não muda recibo já entregue.
--
-- Pode rodar antes do código novo: as colunas nascem com o padrão do Brasil,
-- e o código de hoje não as lê nem as escreve.

alter table public.drivers
  add column if not exists country  text not null default 'BR',
  add column if not exists currency text not null default 'BRL';

alter table public.drivers
  drop constraint if exists drivers_country_ck,
  add constraint drivers_country_ck check (country ~ '^[A-Z]{2}$'),
  drop constraint if exists drivers_currency_ck,
  add constraint drivers_currency_ck check (currency ~ '^[A-Z]{3}$');

alter table public.receipts
  add column if not exists currency text not null default 'BRL';

alter table public.receipts
  drop constraint if exists receipts_currency_ck,
  add constraint receipts_currency_ck check (currency ~ '^[A-Z]{3}$');

-- O teto de 99.999,99 cabe no real, mas não numa corrida de 150.000 guaranis.
-- O teto de cada moeda agora vive no código (MOEDAS em app.py); o banco só
-- recusa zero, negativo e o que não cabe na coluna.
alter table public.receipts
  drop constraint if exists receipts_amount_check;

alter table public.receipts
  alter column amount type numeric(12,2);

alter table public.receipts
  add constraint receipts_amount_check check (amount > 0);

-- ── Perfil criado junto com o usuário ───────────────────────────────────────
-- Mesmo cuidado da 0002: nada aqui pode levantar exceção, senão o cadastro
-- inteiro falha. País e moeda fora do formato viram o padrão do Brasil, e o
-- código confere se são da lista antes de mandar.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_country  text := upper(coalesce(new.raw_user_meta_data ->> 'country', ''));
  v_currency text := upper(coalesce(new.raw_user_meta_data ->> 'currency', ''));
begin
  if v_country !~ '^[A-Z]{2}$' then
    v_country := 'BR';
  end if;
  if v_currency !~ '^[A-Z]{3}$' then
    v_currency := 'BRL';
  end if;

  insert into public.drivers (
    id, email, full_name, cpf, whatsapp, city, plate,
    vehicle_model, taxi_prefix, license_number, country, currency
  )
  values (
    new.id,
    lower(coalesce(new.email, '')),
    left(coalesce(new.raw_user_meta_data ->> 'full_name', ''), 120),
    left(coalesce(new.raw_user_meta_data ->> 'cpf', ''), 14),
    left(coalesce(new.raw_user_meta_data ->> 'whatsapp', ''), 20),
    left(coalesce(new.raw_user_meta_data ->> 'city', ''), 80),
    left(coalesce(new.raw_user_meta_data ->> 'plate', ''), 10),
    left(coalesce(new.raw_user_meta_data ->> 'vehicle_model', ''), 80),
    left(coalesce(new.raw_user_meta_data ->> 'taxi_prefix', ''), 20),
    left(coalesce(new.raw_user_meta_data ->> 'license_number', ''), 40),
    v_country,
    v_currency
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

comment on column public.drivers.country is
  'País do motorista (ISO 3166-1 alfa-2; ZZ = outro). Escolhido no cadastro.';
comment on column public.drivers.currency is
  'Moeda dos recibos novos (ISO 4217). Escolhida no cadastro ou no perfil.';
comment on column public.receipts.currency is
  'Moeda do recibo (ISO 4217), copiada do motorista na emissão.';
