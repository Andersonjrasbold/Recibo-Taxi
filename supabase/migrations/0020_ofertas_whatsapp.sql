-- Ofertas do plano Pro pelo WhatsApp oficial (Cloud API da Meta), 2026-11-25.
--
-- A tarefa /tarefas/whatsapp manda um modelo aprovado pela Meta a quem bateu o
-- teto do Grátis no mês e segue no Grátis, uma vez a cada 30 dias por
-- motorista, e o webhook /webhook/whatsapp traz o status (entregue, lido,
-- falhou) e a resposta do motorista: "Parar ofertas" ou "SAIR" marca
-- drivers.marketing_optout_at, a mesma marca do descadastro por e-mail.
--
-- Esta tabela é o registro de cada mensagem, nas duas direções: é ela que
-- impede a segunda oferta no mês e que casa a resposta (pelo número) com o
-- motorista. Guarda o mínimo: nem o texto da resposta nem o do modelo, só o
-- tipo. O número fica em dígitos (E.164) porque a Meta identifica quem
-- respondeu pelo número.
--
-- Pode rodar antes ou depois do código: sem a tabela, a tarefa e o webhook só
-- avisam no log (migracao_0020_pendente) e o painel mostra "indisponível".

create table if not exists public.whatsapp_messages (
  id            bigserial    primary key,
  driver_id     uuid         not null references public.drivers (id) on delete cascade,
  direction     text         not null check (direction in ('saida', 'entrada')),
  kind          text         not null,                   -- saída: 'oferta_teto'; entrada: 'parar' ou 'texto'
  template      text         not null default '',        -- nome do modelo aprovado (saída)
  wa_message_id text         unique,                     -- id da Meta (wamid…); nulo se o envio nem saiu
  to_phone      text         not null default '',        -- dígitos E.164, para casar a resposta
  status        text         not null default 'enviado', -- enviado | entregue | lido | falhou | recebido
  error         text         not null default '',
  link_nonce    text         not null default '',        -- o `n` do link /pagar que foi na mensagem
  created_at    timestamptz  not null default now(),
  updated_at    timestamptz  not null default now()
);

create index if not exists whatsapp_messages_driver_idx on public.whatsapp_messages (driver_id, created_at desc);
create index if not exists whatsapp_messages_phone_idx  on public.whatsapp_messages (to_phone, created_at desc);

-- Toda tabela de public fecha a Data API (migração 0006).
alter table public.whatsapp_messages enable row level security;

comment on table public.whatsapp_messages is
  'Ofertas do Pro pelo WhatsApp oficial e as respostas: uma linha por mensagem, sem o texto.';
