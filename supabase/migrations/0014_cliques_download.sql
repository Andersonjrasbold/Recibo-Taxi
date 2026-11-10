-- Cliques no link de download por canal (plano de divulgação de 2026-11-09).
--
-- O link `recibotaxi.com.br/baixar?c=CANAL` vai em cartão no ponto de táxi,
-- em cartaz de cooperativa, em anúncio e em e-mail. O servidor repassa o
-- canal às lojas (Play: `referrer` com UTM; App Store: `ct`) e soma o clique
-- aqui, por dia, canal e loja. As lojas agrupam campanha pequena em "Outros"
-- ou escondem a que tem menos de 5 downloads; este é o número que sempre
-- existe. Só contagem: nem IP, nem agente, nem quem clicou.
--
-- Pode rodar antes ou depois do código novo: sem a tabela, o /baixar só deixa
-- de contar (e avisa no log); com a tabela e sem o código, ela fica vazia.

create table if not exists public.download_clicks (
  day      date    not null,                 -- dia de Brasília
  channel  text    not null,                 -- 'direto' quando o link não tem ?c=
  store    text    not null,                 -- 'play', 'appstore' ou 'site' (computador)
  clicks   integer not null default 0,
  primary key (day, channel, store)
);

-- Toda tabela de public fecha a Data API (migração 0006).
alter table public.download_clicks enable row level security;

comment on table public.download_clicks is
  'Cliques em /baixar por dia, canal (?c=) e loja. Só contagem, sem dado pessoal.';
