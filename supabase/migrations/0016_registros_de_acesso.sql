-- Registros de acesso (Marco Civil da Internet, art. 15).
--
-- Provedor de aplicação com fins econômicos guarda, por 6 meses, data, hora e
-- IP de cada acesso. O código grava no login (site e app), na renovação da
-- sessão do app e no cadastro pelo app; a limpeza diária apaga o que passou de
-- 180 dias. Sem chave estrangeira de propósito: o registro tem de sobreviver à
-- exclusão da conta até completar o prazo. A Política de Privacidade e a
-- página de exclusão dizem exatamente isto (auditoria de 2026-11-10).
--
-- Pode rodar antes ou depois do código novo: sem a tabela, o login funciona e
-- só avisa no log que não gravou.

create table if not exists public.access_logs (
  id        bigserial primary key,
  driver_id uuid        not null,
  ip        text        not null default '',
  channel   text        not null default '',   -- 'site', 'app', 'app_refresh', 'app_cadastro'
  at        timestamptz not null default now()
);

create index if not exists access_logs_at_idx on public.access_logs (at);
create index if not exists access_logs_driver_idx on public.access_logs (driver_id, at desc);

-- Toda tabela de public fecha a Data API (migração 0006).
alter table public.access_logs enable row level security;

comment on table public.access_logs is
  'Data, hora e IP de login/renovação/cadastro, por 6 meses (Marco Civil, art. 15). Sem FK: sobrevive à exclusão da conta.';
