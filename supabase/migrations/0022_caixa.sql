-- Caixa do motorista (app 1.8, 2026-11-26): receitas e despesas lançadas no app.
--
-- O Caixa mora no aparelho, offline como os recibos: o motorista vê ao longo
-- do dia quanto ganhou, quanto gastou e o líquido, e o acumulado do mês. Esta
-- tabela guarda uma CÓPIA de cada lançamento, enviada pelo POST
-- /api/lancamentos, para ele não perder o que lançou se trocar de aparelho
-- (restaurar num aparelho novo é a fase 2). O trajeto (origem e destino) e o
-- passageiro nunca vêm para cá: ficam só no aparelho.
--
-- A chave é (driver_id, id). O id nasce no aparelho ("r-<rid>" para o
-- lançamento de um recibo, "l-<16 hex>" para o resto), então dois motoristas
-- com o mesmo id são linhas diferentes. `version` decide entre reenvios: só
-- uma versão igual ou maior troca a guardada. Apagado no app vira túmulo
-- (`deleted_at`), que a limpeza diária tira de vez em 30 dias.
--
-- Rodar ANTES do deploy do código que grava. Sem a tabela, o envio do app
-- responde 503 (o app guarda e tenta de novo) e o bloco do Caixa no painel
-- aparece como indisponível. Pode rodar duas vezes.

create table if not exists public.ledger_entries (
  driver_id      uuid          not null references public.drivers (id) on delete cascade,
  id             text          not null check (id ~ '^[A-Za-z0-9_-]{1,40}$'),
  kind           text          not null check (kind in ('income', 'expense')),
  category       text          not null check (category ~ '^[a-z][a-z0-9_]{0,29}$'),
  amount         numeric(12,2) not null check (amount > 0 and amount <= 99999999.99),
  currency       char(3)       not null,
  entry_date     date          not null,
  entry_time     varchar(5)    not null default '',
  payment_method varchar(40)   not null default '',
  source         text          not null check (source in ('receipt', 'ride', 'manual')),
  -- SEM chave estrangeira para receipts, de propósito: o lançamento de um
  -- recibo recusado por cota (que nunca existiu no servidor) também vale, e a
  -- fila de lançamentos do app pode chegar antes da de recibos. O rid vem do
  -- aparelho: quem um dia juntar rid com receipts TEM de conferir o driver_id
  -- dos dois, senão um motorista lê o recibo de outro.
  rid            text          check (rid is null or rid ~ '^[0-9A-F]{14}$'),
  note           varchar(120)  not null default '',
  distance_km    numeric(6,1),
  liters         numeric(6,2),
  version        integer       not null check (version >= 1),
  deleted_at     timestamptz,
  created_at     timestamptz   not null default now(),
  updated_at     timestamptz   not null default now(),
  primary key (driver_id, id)
);

-- O caixa do dia e do mês de um motorista (o GET da fase 2 lê por aqui).
create index if not exists ledger_entries_driver_date_idx
  on public.ledger_entries (driver_id, entry_date);
-- Só os túmulos: a limpeza procura os apagados há mais de 30 dias.
create index if not exists ledger_entries_deleted_idx
  on public.ledger_entries (deleted_at)
  where deleted_at is not null;
-- O bloco do Caixa no painel conta os que chegaram nos últimos 7 e 30 dias.
create index if not exists ledger_entries_created_idx
  on public.ledger_entries (created_at);

-- Toda tabela de public fecha a Data API (migração 0006). O revoke é a segunda
-- tranca: o default da 0006 só cobre tabela criada pelo papel postgres.
alter table public.ledger_entries enable row level security;
revoke all on table public.ledger_entries from anon, authenticated;

comment on table public.ledger_entries is
  'Caixa do app 1.8: cópia das receitas e despesas lançadas no aparelho. Sem trajeto nem passageiro.';
comment on column public.ledger_entries.id is
  'Id criado no aparelho: r-<rid> (lançamento de um recibo) ou l-<16 hex>';
comment on column public.ledger_entries.kind is 'income (receita) ou expense (despesa)';
comment on column public.ledger_entries.category is
  'Categoria do app (corrida, combustivel, aluguel_veiculo...); lista aberta, a fase 2 cria outras';
comment on column public.ledger_entries.amount is
  'Valor na moeda do lançamento; o app manda inteiro na menor unidade e a API converte';
comment on column public.ledger_entries.source is
  'receipt (recibo emitido), ride (corrida encerrada com "Só lançar no caixa") ou manual';
comment on column public.ledger_entries.rid is
  'Recibo de origem, SEM FK de propósito (recibo recusado por cota também vale); juntar com receipts exige conferir o driver_id dos dois';
comment on column public.ledger_entries.version is
  'Versão do aparelho: só uma igual ou maior substitui a guardada';
comment on column public.ledger_entries.deleted_at is
  'Apagado no app (túmulo); a limpeza diária tira de vez depois de 30 dias';
