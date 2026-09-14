-- CPF ou CNPJ do passageiro, opcional.
--
-- Quem viaja a trabalho precisa do documento no recibo para prestar contas.
-- Fica vazio quando nao informado, nunca nulo, para nao espalhar checagem de
-- None por todo lugar que le recibo.
--
-- varchar(18) cobre a forma mais longa com pontuacao: 00.000.000/0000-00.
alter table public.receipts
  add column if not exists passenger_document varchar(18) not null default '';

comment on column public.receipts.passenger_document is
  'CPF ou CNPJ do passageiro, como digitado. Opcional.';
