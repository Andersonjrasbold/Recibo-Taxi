-- ─────────────────────────────────────────────────────────────────────────────
-- Fecha a Data API do Supabase para as tabelas do app.
--
-- O app fala com o banco pela conexão direta, como `postgres` (dono das
-- tabelas), e nunca pelo PostgREST. Mas o Supabase expõe o schema `public`
-- pela Data API e, por padrão, dá todos os privilégios a `anon` e
-- `authenticated` em tudo que o `postgres` cria ali. Sem RLS, quem tivesse a
-- chave publicável lia CPF, telefone e o hash da senha do admin — e gravava,
-- inclusive um administrador novo. A chave publicável é pública por desenho:
-- não serve de tranca.
--
-- RLS ligado sem nenhuma policy = negado para todo mundo, menos para o dono e
-- para quem tem BYPASSRLS (`postgres` e `service_role`). O revoke é a segunda
-- tranca, para o caso de alguém desligar o RLS de uma tabela sem perceber.
--
-- Localmente, rode antes o supabase/local/auth_shim.sql, que cria os papéis
-- `anon` e `authenticated`. No Supabase eles já existem.
-- ─────────────────────────────────────────────────────────────────────────────

alter table public.drivers        enable row level security;
alter table public.receipts       enable row level security;
alter table public.receipt_quotas enable row level security;
alter table public.rate_limits    enable row level security;
alter table public.admin_users    enable row level security;

revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;

-- `anon` herda o EXECUTE de PUBLIC, então tirar só dele não fecha nada.
-- handle_new_user fica como está: função de trigger não é chamável por RPC.
revoke execute on function public.bump_counter(text)                    from public, anon, authenticated;
revoke execute on function public.consume_receipt_quota(uuid, integer)  from public, anon, authenticated;
revoke execute on function public.br_period(timestamptz)                from public, anon, authenticated;

-- Tabela nova não nasce aberta de novo. Mesmo assim, toda migração que criar
-- tabela em `public` deve ligar o RLS nela: o default só cobre o privilégio.
alter default privileges for role postgres in schema public
  revoke all on tables from anon, authenticated;
alter default privileges for role postgres in schema public
  revoke all on sequences from anon, authenticated;
alter default privileges for role postgres in schema public
  revoke all on functions from anon, authenticated;
