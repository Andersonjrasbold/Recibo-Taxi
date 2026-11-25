-- Canal de divulgação de cada cadastro (2026-11-24): de onde veio o motorista.
--
-- O anúncio, o cartão e o e-mail levam a recibotaxi.com.br/baixar?c=CANAL. O
-- clique já é contado (migração 0014); agora o canal fica gravado no cadastro,
-- para o painel dizer quantos cadastros cada canal trouxe e o custo por
-- cadastro sair do gasto real — sem mandar dado nenhum a Meta ou Google.
-- No site, o /baixar?c= leva o canal ao /cadastro?c=; no app para Android, o
-- Google Play entrega o canal (utm_source do Install Referrer) no primeiro
-- acesso (app 1.9+). Vazio = não informado (orgânico, iPhone, app antigo).
--
-- Pode rodar antes ou depois do código novo: sem a coluna, o cadastro só deixa
-- de gravar o canal (e avisa no log); o painel mostra a seção como indisponível.

alter table public.drivers
  add column if not exists signup_channel text not null default '';

comment on column public.drivers.signup_channel is
  'Canal de divulgação do cadastro (?c= do /baixar ou utm_source do Install Referrer); vazio = não informado';
