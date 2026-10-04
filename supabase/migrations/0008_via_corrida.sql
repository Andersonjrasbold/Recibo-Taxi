-- Marca os recibos que sairam do caminho "Fazer corrida" do app (versao 1.2).
--
-- Serve so para medir se a funcionalidade e usada: nao aparece no recibo, na
-- pagina publica nem no PDF. Nenhum motorista pediu a corrida; o numero de
-- recibos que vem dela e o que diz se ela fica, cresce ou sai.
--
-- Falso, nunca nulo: recibo antigo, do site ou do formulario avulso do app nao
-- veio de corrida.
alter table public.receipts
  add column if not exists via_corrida boolean not null default false;

comment on column public.receipts.via_corrida is
  'Recibo emitido pelo caminho Fazer corrida do app. So para medir uso.';
