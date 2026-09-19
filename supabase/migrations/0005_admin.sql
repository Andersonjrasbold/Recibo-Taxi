-- Painel administrativo (/admin).
--
-- Administrador NAO e motorista: tabela propria, senha propria (hash do
-- werkzeug, scrypt), sessao propria. Assim o dono do negocio pode ter uma
-- conta de motorista com o mesmo e-mail sem misturar as duas coisas, e um
-- vazamento de token do app nunca abre o painel.
create table if not exists public.admin_users (
  id                  uuid primary key default gen_random_uuid(),
  email               varchar(254) not null,
  password_hash       text         not null,
  created_at          timestamptz  not null default now(),
  password_changed_at timestamptz  not null default now(),
  last_login_at       timestamptz
);

create unique index if not exists admin_users_email_key
  on public.admin_users (lower(email));

comment on table public.admin_users is
  'Quem entra em /admin. Criado pelo comando "flask criar-admin", nao por cadastro.';

-- O painel agrega recibos e motoristas por dia. Sem estes indices cada
-- abertura varreria as tabelas inteiras — barato hoje, caro no primeiro mes bom.
create index if not exists receipts_created_idx on public.receipts (created_at);
-- Composto para a lista de motoristas paginar por cursor (created_at, id).
create index if not exists drivers_created_idx  on public.drivers  (created_at desc, id desc);
