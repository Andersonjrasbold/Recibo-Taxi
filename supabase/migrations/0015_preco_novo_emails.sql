-- Preço novo do Pro (2026-11-10): mensal R$ 9,90 e anual R$ 59,90.
--
-- Os e-mails automáticos usam {{preco}} e já saem com o valor novo. O modelo 9
-- ("Plano anual", semeado pela migração 0007 e desligado) tem o preço antigo
-- escrito à mão; a semente só roda com a tabela vazia, então a linha que já
-- está em produção precisa deste update. Idempotente: a segunda rodada não
-- encontra o texto antigo e não muda nada.
update public.email_templates
   set body = replace(body,
       'custa metade do mensal: R$ 119,90 por ano no iPhone e R$ 119,99 no Android — menos de R$ 10 por mês.',
       'custa metade do mensal: R$ 59,90 por ano, o que dá R$ 4,99 por mês.')
 where body like '%R$ 119,90 por ano no iPhone%';
