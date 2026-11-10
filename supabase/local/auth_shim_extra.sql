-- APENAS DESENVOLVIMENTO LOCAL. Complemento ao auth_shim.sql: o que o GoTrue
-- mantem em auth.users e auth.sessions e o painel /admin le. Nunca aplicar no Supabase.
alter table auth.users add column if not exists last_sign_in_at timestamptz;
alter table auth.users add column if not exists email_confirmed_at timestamptz;
alter table auth.users add column if not exists banned_until timestamptz;
create unique index if not exists auth_users_email_lower on auth.users (lower(email));
create table if not exists auth.sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  not_after timestamptz
);
alter table auth.sessions add column if not exists refreshed_at timestamptz;
