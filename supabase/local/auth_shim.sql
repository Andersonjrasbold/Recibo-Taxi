-- ─────────────────────────────────────────────────────────────────────────────
-- APENAS DESENVOLVIMENTO LOCAL. Nunca aplicar no Supabase.
--
-- O Supabase já traz o schema `auth` com a tabela `auth.users`. O Postgres do
-- Homebrew não. Este arquivo cria o mínimo para que a migration 0002 — chave
-- estrangeira e trigger — rode igual nos dois lugares.
--
-- Só as colunas que o app realmente toca.
-- ─────────────────────────────────────────────────────────────────────────────

create schema if not exists auth;

create table if not exists auth.users (
  id                 uuid primary key default gen_random_uuid(),
  email              varchar(255),
  encrypted_password varchar(255),
  raw_user_meta_data jsonb default '{}'::jsonb,
  created_at         timestamptz default now(),
  updated_at         timestamptz default now()
);

-- auth.uid() existe no Supabase e lê o JWT da requisição. Localmente o app
-- conecta como dono e não usa RLS, então basta existir para nada quebrar.
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
$$;
