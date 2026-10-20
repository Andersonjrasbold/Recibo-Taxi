-- Quilometragem e hora de chegada no recibo.
--
-- Pedido de um motorista (2026-10-20): quem presta contas para a empresa
-- quer o percurso, o km rodado e o horário de início e fim. O recibo tinha
-- origem, destino e um horário só. Os dois campos são opcionais; a corrida
-- do app ("Fazer corrida") preenche os dois sozinha.
--
-- Pode rodar antes do código novo: nascem vazios e o código de hoje não os lê.
alter table public.receipts
  add column if not exists trip_end_time varchar(5)   not null default '',
  add column if not exists distance_km   numeric(6,1);

alter table public.receipts
  drop constraint if exists receipts_distance_km_ck,
  add constraint receipts_distance_km_ck check (distance_km is null or distance_km > 0);

comment on column public.receipts.trip_end_time is 'Hora de chegada (HH:MM), opcional. trip_time é a de saída.';
comment on column public.receipts.distance_km is 'Km rodado, opcional, uma casa decimal.';
