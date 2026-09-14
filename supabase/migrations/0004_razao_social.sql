-- Razao social, quando o documento do passageiro e um CNPJ.
--
-- CNPJ sozinho nao serve para a empresa lancar a despesa: a contabilidade
-- precisa do nome da pessoa juridica no recibo. Para CPF nao existe, e o campo
-- fica vazio.
--
-- Vazio, nunca nulo, pelo mesmo motivo do passenger_document: nao espalhar
-- checagem de None por todo lugar que le recibo.
--
-- varchar(140) cobre com folga a razao social registrada na Receita Federal,
-- que tem limite de 130 caracteres.
alter table public.receipts
  add column if not exists passenger_company varchar(140) not null default '';

comment on column public.receipts.passenger_company is
  'Razao social do passageiro, quando o documento e CNPJ. Opcional.';
