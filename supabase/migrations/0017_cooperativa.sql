-- Cooperativa ou associação de táxi do motorista (pedido de um taxista,
-- 2026-11-12): nome e CNPJ, opcionais, preenchidos em Meu perfil no site.
-- Entram no retrato do motorista de cada recibo emitido depois (página
-- pública, e-mail, app) — é o que a empresa do passageiro procura para
-- aceitar o reembolso, como no bloquinho de papel das cooperativas.
-- Texto já pontuado (00.000.000/0001-00); vazio = não informado.

alter table public.drivers
  add column if not exists coop_name text not null default '',
  add column if not exists coop_cnpj text not null default '';

comment on column public.drivers.coop_name is 'Cooperativa ou associação de táxi (opcional); vai no retrato do motorista nos recibos';
comment on column public.drivers.coop_cnpj is 'CNPJ da cooperativa, pontuado (opcional)';
