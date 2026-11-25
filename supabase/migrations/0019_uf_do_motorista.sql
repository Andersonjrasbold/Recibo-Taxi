-- UF do motorista (2026-11-25). O cadastro do site passou a pedir a UF e a
-- cidade de uma lista oficial (IBGE): a cidade continua em `city`, com o nome
-- oficial ("São Paulo", e não "sao paulo" ou "SP"), e a UF fica aqui. Vazio =
-- cadastro antigo, fora do Brasil, ou feito pelo app (que ainda pede a cidade
-- em texto livre; o app 1.9 passa a usar a mesma lista).
--
-- Rodar ANTES do deploy do código que a lê: a coluna entra na lista de colunas
-- do motorista (_DRIVER_COLS), e sem ela nenhuma página logada abre.

alter table public.drivers
  add column if not exists state text not null default '';

comment on column public.drivers.state is 'UF do motorista (cadastro do site a partir de 2026-11-25); vazio = não informada';
