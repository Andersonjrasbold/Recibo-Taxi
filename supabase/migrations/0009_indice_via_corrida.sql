-- Indice parcial para o bloco "Fazer corrida" do painel (migracao 0008).
--
-- As duas contagens (30 dias e total) filtram por via_corrida, e sem indice
-- cada abertura do painel varria a tabela inteira de recibos, dentro do
-- statement_timeout de 4 s. Parcial: so os recibos marcados entram, e e deles
-- que o painel precisa; os outros numeros do painel ja tem receipts_created_idx.
create index if not exists receipts_via_corrida_idx
  on public.receipts (created_at)
  where via_corrida;
