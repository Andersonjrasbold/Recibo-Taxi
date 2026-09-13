-- ─────────────────────────────────────────────────────────────────────────────
-- Recibo Táxi — PR 2: autenticação passa para o Supabase Auth
--
-- A senha sai da tabela `drivers` e passa a viver em `auth.users`. A tabela
-- `drivers` vira o PERFIL: mesma chave primária do usuário de autenticação.
--
-- Localmente, rode antes o supabase/local/auth_shim.sql, que cria um
-- `auth.users` mínimo. No Supabase o schema já existe.
-- ─────────────────────────────────────────────────────────────────────────────

-- A migração recria as contas: não há como converter um hash do werkzeug no
-- formato do GoTrue. Seguro aqui porque a base está vazia.
truncate table public.drivers cascade;

alter table public.drivers
  drop column if exists password_hash;

-- A PK passa a ser a mesma de auth.users. `on delete cascade` faz a exclusão
-- de conta virar uma chamada só: apaga o usuário no Auth e o perfil, os
-- recibos e as cotas vão junto.
alter table public.drivers
  drop constraint if exists drivers_pkey cascade;

alter table public.drivers
  alter column id drop default;

alter table public.drivers
  add constraint drivers_pkey primary key (id);

alter table public.drivers
  add constraint drivers_user_fk
  foreign key (id) references auth.users (id) on delete cascade;

-- As FKs que apontavam para drivers foram derrubadas pelo cascade acima.
alter table public.receipts
  add constraint receipts_driver_fk
  foreign key (driver_id) references public.drivers (id) on delete cascade;

alter table public.receipt_quotas
  add constraint receipt_quotas_driver_fk
  foreign key (driver_id) references public.drivers (id) on delete cascade;

-- ── Perfil criado junto com o usuário ───────────────────────────────────────
-- Qualquer exceção aqui dentro aborta o INSERT em auth.users e o cadastro
-- inteiro falha — o usuário nem chega a existir. Por isso: nada de NOT NULL
-- que possa vir vazio, e left() em tudo para não estourar tamanho.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.drivers (
    id, email, full_name, cpf, whatsapp, city, plate,
    vehicle_model, taxi_prefix, license_number
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
    left(coalesce(new.raw_user_meta_data ->> 'license_number', ''), 40)
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- `plan` e os campos da Stripe NÃO saem do raw_user_meta_data: ele é editável
-- pelo próprio usuário. Quem escreve neles é o webhook, com a service_role.
