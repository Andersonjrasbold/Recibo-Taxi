-- Fila de sincronização com o PilotPostAi (CRM e atendimento da mesma empresa), 2026-11-26.
--
-- Cada motorista do Recibo Táxi vira cliente da conta do suporte no PilotPostAi
-- (rota /internal/integracoes/recibo-taxi/clientes de lá). Em vez de espalhar
-- chamadas pelo código, um trigger em `drivers` anota aqui o que mudou: criação,
-- alteração dos dados que o CRM recebe, "parar ofertas" e exclusão. O envio
-- (/tarefas/crm a cada 15 min, e logo depois do cadastro) manda o estado ATUAL
-- do motorista; vários eventos pendentes do mesmo motorista viram um envio só.
--
-- Guarda só o id do motorista e o tipo do evento: o conteúdo é lido de `drivers`
-- na hora de mandar. Pode rodar antes ou depois do código: sem a tabela, o cron
-- e o painel só avisam; sem o código, a fila cresce e é consumida depois.

create table if not exists public.crm_sync (
  id          bigserial    primary key,
  driver_id   uuid         not null,          -- sem FK: a exclusão precisa sobreviver ao motorista
  event       text         not null check (event in ('created', 'updated', 'opted_out', 'deleted')),
  created_at  timestamptz  not null default now(),
  next_try_at timestamptz  not null default now(),
  attempts    integer      not null default 0,
  sent_at     timestamptz,
  last_error  text         not null default ''
);

create index if not exists crm_sync_pendentes_idx on public.crm_sync (next_try_at) where sent_at is null;
create index if not exists crm_sync_driver_idx on public.crm_sync (driver_id, id);

-- Toda tabela de public fecha a Data API (migração 0006).
alter table public.crm_sync enable row level security;

comment on table public.crm_sync is
  'Fila de envio do cadastro do motorista ao PilotPostAi (CRM). Só o id e o tipo do evento.';

-- security definer: o trigger também roda quando o Auth cria o perfil
-- (on_auth_user_created, papel supabase_auth_admin), que não tem acesso à fila.
create or replace function public.crm_enfileirar() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if tg_op = 'INSERT' then
    insert into public.crm_sync (driver_id, event) values (new.id, 'created');
    return new;
  elsif tg_op = 'DELETE' then
    insert into public.crm_sync (driver_id, event) values (old.id, 'deleted');
    return old;
  end if;
  if new.marketing_optout_at is not null and old.marketing_optout_at is null then
    insert into public.crm_sync (driver_id, event) values (new.id, 'opted_out');
  elsif (new.full_name, new.whatsapp, new.email, new.city, new.state, new.country, new.plan,
         new.subscription_status, new.signup_channel, new.marketing_optout_at)
        is distinct from
        (old.full_name, old.whatsapp, old.email, old.city, old.state, old.country, old.plan,
         old.subscription_status, old.signup_channel, old.marketing_optout_at) then
    insert into public.crm_sync (driver_id, event) values (new.id, 'updated');
  end if;
  return new;
end
$$;

drop trigger if exists drivers_crm_sync on public.drivers;
create trigger drivers_crm_sync
  after insert or update or delete on public.drivers
  for each row execute function public.crm_enfileirar();
