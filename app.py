import atexit
import calendar
import hashlib
import json
import unicodedata
import hmac
import math
import os
import re
import secrets
import smtplib
import time
import http.client
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from email.message import EmailMessage
from functools import lru_cache, wraps
from urllib.parse import quote
from uuid import uuid4

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    has_request_context,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer, URLSafeTimedSerializer
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from supabase import ClientOptions, create_client
except ImportError:
    create_client = None
    ClientOptions = None

try:  # supabase-py >= 2.10 chama o módulo de supabase_auth; antes era gotrue
    from supabase_auth.errors import AuthRetryableError
except ImportError:
    try:
        from gotrue.errors import AuthRetryableError
    except ImportError:
        AuthRetryableError = None

try:
    from dotenv import load_dotenv
except ImportError:  # dependência opcional, usada só em desenvolvimento
    load_dotenv = None

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Json
    from psycopg_pool import ConnectionPool
except ImportError:  # só necessário quando DATABASE_URL/SUPABASE_DB_URL existe
    psycopg = None
    dict_row = None
    Json = None
    ConnectionPool = None

try:
    import stripe as stripe_lib
except ImportError:
    stripe_lib = None


# Lê o .env local. Em produção as variáveis já vêm do ambiente e o
# load_dotenv não sobrescreve o que existe, então isto vira um no-op.
if load_dotenv is not None:
    load_dotenv()


APP_NAME = "Recibo Táxi"

# Brasil não adota horário de verão desde 2019, então o offset é fixo.
BR_TZ = timezone(timedelta(hours=-3))

# Valor definido pelo Anderson em 2026-09-15, fechando a fase de testes (que
# rodou com 100). Os textos do site e o app leem daqui (inject_globals e
# /api/sessao), entao mudar este numero basta. A ficha da App Store (nota de
# revisao da assinatura) repete o numero e precisa ser ajustada a mao.
FREE_MONTHLY_LIMIT = 6
# "business" segue aqui de propósito: o plano saiu de venda, mas quem já assina
# mantém o acesso ilimitado até cancelar. Só STRIPE_PRICE_IDS perdeu a entrada,
# o que faz /assinar/business responder 400 para assinaturas novas.
PAID_PLANS = ("pro", "business")

# Páginas do app nas lojas. Moram aqui, e não nos templates, para a landing,
# o rodapé e qualquer e-mail apontarem para o mesmo lugar.
APP_STORE_URL = "https://apps.apple.com/br/app/id6811583712"
# Publicado em produção no Google Play em 2026-09-29.
PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=br.com.recibotaxi.app"
APP_STORE_ID = "6811583712"

# Endereço público do site, para canonical, sitemap e prévia de link. É fixo
# (e não APP_BASE_URL) porque o mesmo site responde em recibo-taxi.vercel.app,
# endereço que o app usa para a API e que nunca pode ser redirecionado: para
# o Google, o dono do conteúdo tem de ser sempre o domínio próprio.
SITE_URL = "https://www.recibotaxi.com.br"

# ── UF e municípios do Brasil ────────────────────────────────────────────────
# O cadastro do site pede a UF e a cidade de uma lista oficial (IBGE, API de
# localidades, lida em 2026-11-25): "Sao Paulo", "são paulo" e "SÃO PAULO" viram
# "São Paulo", e o painel deixa de ter dez grafias da mesma cidade. Os nomes
# ficam em public/static/data/municipios/UF.json — o mesmo arquivo que o
# formulário carrega para o filtro — e o servidor confere por aqui.
UFS = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará",
    "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão", "MT": "Mato Grosso",
    "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
    "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "São Paulo",
    "SE": "Sergipe", "TO": "Tocantins",
}
MUNICIPIOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "static", "data", "municipios")


def sem_acento(texto: str) -> str:
    """Chave de comparação de nomes: sem acento, minúsculas, um espaço só."""
    base = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return " ".join(base.casefold().split())


@lru_cache(maxsize=None)
def municipios_da_uf(uf: str) -> dict[str, str]:
    """{chave sem acento: nome oficial} dos municípios da UF; UF desconhecida: vazio."""
    if uf not in UFS:
        return {}
    with open(os.path.join(MUNICIPIOS_DIR, f"{uf}.json"), encoding="utf-8") as arquivo:
        return {sem_acento(nome): nome for nome in json.load(arquivo)}


def cidade_do_brasil(uf, cidade) -> tuple[str, str]:
    """(UF, nome oficial) quando a cidade existe na UF; ("", "") quando não."""
    uf = str(uf or "").strip().upper()
    nome = municipios_da_uf(uf).get(sem_acento(str(cidade or "")))
    return (uf, nome) if nome else ("", "")


# Canal de divulgação no link /baixar?c=CANAL (plano de 2026-11-09). Vai ao
# Play dentro do `referrer` (utm_source) e à App Store em `ct`, e é somado na
# tabela download_clicks. Minúsculas, dígitos, hífen e sublinhado, até 30
# caracteres: é o teto do `ct` da Apple.
CANAL_DOWNLOAD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")
# Token do provedor da conta da AJR no App Store Connect (Analytics →
# Acquisition → Campaigns → "+", o número depois de `pt=`). Sem ele a Apple
# ignora o `ct`, então o iPhone vai à loja sem campanha.
APPLE_PROVIDER_TOKEN = os.environ.get("APPLE_PROVIDER_TOKEN", "").strip()


def link_play_com_canal(canal: str, conteudo: str = "") -> str:
    """Ficha do Play com o canal no `referrer` (o Console mostra como Origem do UTM).

    `conteudo` vai em utm_content: é como o código de quem indicou chega ao
    aparelho junto com a instalação (o app lê o referrer numa versão futura).
    """
    utm = f"utm_source={canal}&utm_medium=link&utm_campaign={canal}"
    if conteudo:
        utm += f"&utm_content={conteudo}"
    return f"{PLAY_STORE_URL}&referrer={quote(utm, safe='')}"


def qr_svg(texto: str) -> str:
    """QR como SVG inline: a CSP só deixa imagem de 'self' e data:, e um SVG no
    HTML dispensa as duas. Só o desenho; o tamanho e o fundo vêm do CSS."""
    import qrcode
    import qrcode.image.svg
    imagem = qrcode.make(texto, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return imagem.to_string(encoding="unicode")


def link_appstore_com_canal(canal: str) -> str:
    """Ficha da App Store com o canal em `ct`; sem o token da conta, a ficha pura."""
    if not APPLE_PROVIDER_TOKEN:
        return APP_STORE_URL
    return f"{APP_STORE_URL}?pt={APPLE_PROVIDER_TOKEN}&ct={canal}&mt=8"

MAX_RECEIPT_AMOUNT = Decimal("99999.99")
# Moedas em que a corrida custa milhares de unidades: 150.000 guaranis é uma
# corrida comum, e o teto do real recusaria.
MAX_RECEIPT_AMOUNT_GRANDE = Decimal("99999999.99")

# País e moeda do motorista (migração 0010). O app está à venda em 175 países,
# e em 2026-10 um motorista da Costa Rica emitia recibo com "R$" na frente de
# um valor em colones. O país dá a moeda sugerida e o código do WhatsApp; a
# moeda é escolha do motorista. O texto do recibo continua em português.
#
# código ISO 4217 -> (nome, símbolo, casas decimais, teto por recibo)
MOEDAS = {
    "BRL": ("Real brasileiro", "R$", 2, MAX_RECEIPT_AMOUNT),
    "USD": ("Dólar americano", "US$", 2, MAX_RECEIPT_AMOUNT),
    "EUR": ("Euro", "€", 2, MAX_RECEIPT_AMOUNT),
    "ARS": ("Peso argentino", "$", 2, MAX_RECEIPT_AMOUNT_GRANDE),
    "BOB": ("Boliviano", "Bs", 2, MAX_RECEIPT_AMOUNT),
    "CLP": ("Peso chileno", "$", 0, MAX_RECEIPT_AMOUNT_GRANDE),
    "COP": ("Peso colombiano", "$", 2, MAX_RECEIPT_AMOUNT_GRANDE),
    "CRC": ("Colón costarriquenho", "₡", 2, MAX_RECEIPT_AMOUNT_GRANDE),
    "DOP": ("Peso dominicano", "RD$", 2, MAX_RECEIPT_AMOUNT),
    "GTQ": ("Quetzal", "Q", 2, MAX_RECEIPT_AMOUNT),
    "HNL": ("Lempira", "L", 2, MAX_RECEIPT_AMOUNT),
    "MXN": ("Peso mexicano", "$", 2, MAX_RECEIPT_AMOUNT),
    "NIO": ("Córdoba", "C$", 2, MAX_RECEIPT_AMOUNT),
    "PEN": ("Sol peruano", "S/", 2, MAX_RECEIPT_AMOUNT),
    "PYG": ("Guarani", "₲", 0, MAX_RECEIPT_AMOUNT_GRANDE),
    "UYU": ("Peso uruguaio", "$", 2, MAX_RECEIPT_AMOUNT),
    "VES": ("Bolívar", "Bs.", 2, MAX_RECEIPT_AMOUNT_GRANDE),
}
MOEDA_PADRAO = "BRL"

# código ISO 3166-1 -> (nome, moeda sugerida, código de discagem). "ZZ" é o
# código reservado da ISO para "desconhecido": quem não acha o país escolhe a
# moeda à mão e digita o WhatsApp do passageiro com o + na frente.
PAISES = {
    "BR": ("Brasil", "BRL", "55"),
    "AR": ("Argentina", "ARS", "54"),
    "BO": ("Bolívia", "BOB", "591"),
    "CL": ("Chile", "CLP", "56"),
    "CO": ("Colômbia", "COP", "57"),
    "CR": ("Costa Rica", "CRC", "506"),
    "EC": ("Equador", "USD", "593"),
    "SV": ("El Salvador", "USD", "503"),
    "ES": ("Espanha", "EUR", "34"),
    "US": ("Estados Unidos", "USD", "1"),
    "GT": ("Guatemala", "GTQ", "502"),
    "HN": ("Honduras", "HNL", "504"),
    "MX": ("México", "MXN", "52"),
    "NI": ("Nicarágua", "NIO", "505"),
    "PA": ("Panamá", "USD", "507"),
    "PY": ("Paraguai", "PYG", "595"),
    "PE": ("Peru", "PEN", "51"),
    "PT": ("Portugal", "EUR", "351"),
    "DO": ("República Dominicana", "DOP", "1"),
    "UY": ("Uruguai", "UYU", "598"),
    "VE": ("Venezuela", "VES", "58"),
    "ZZ": ("Outro país", "USD", ""),
}
PAIS_PADRAO = "BR"

# 14 dígitos hex = 2^56. Com 10 (2^40) a chance de colisão passava de 36% em
# 1 milhão de recibos, e uma colisão derruba a emissão com erro 500.
RID_LENGTH = 14

# Recibo que ainda nao chegou: a pagina publica pergunta de novo a cada 10 s,
# por ate 10 minutos. E o caso do QR lido enquanto o app ainda esta sem sinal.
RECIBO_ESPERA_SEGUNDOS = 10
RECIBO_ESPERA_TENTATIVAS = 60

# Origens do WebView do Capacitor. Só a /api/* responde a elas, e só com
# Bearer token — nunca com cookie, para não abrir caminho de CSRF.
ORIGENS_APP = ("capacitor://localhost", "https://localhost", "http://localhost")

# Sem teto por campo, um POST de 200 KB por campo passava direto para o banco.
# (rótulo, tamanho máximo) — o rótulo entra na mensagem de erro.
RECEIPT_FIELD_LIMITS = {
    "passageiro": ("O nome do passageiro", 120),
    "origem": ("A origem", 200),
    "destino": ("O destino", 200),
    "observacoes": ("As observações", 500),
    "forma_pagamento": ("A forma de pagamento", 40),
    "email_passageiro": ("O e-mail do passageiro", 254),
    "whatsapp_passageiro": ("O WhatsApp do passageiro", 20),
    "documento_passageiro": ("O CPF/CNPJ do passageiro", 18),
    "razao_social": ("A razão social", 140),
    "data": ("A data", 10),
    "hora": ("A hora", 5),
    "hora_fim": ("A hora de chegada", 5),
    "km": ("A distância", 12),
    "nome_motorista": ("O nome do motorista", 120),
    "placa": ("A placa", 10),
    "whatsapp_motorista": ("O WhatsApp do motorista", 20),
    "cidade_motorista": ("A cidade", 80),
    "modelo_veiculo": ("O modelo do veículo", 80),
}

SIGNUP_FIELD_LIMITS = {
    "nome_completo": ("O nome completo", 120),
    "email": ("O e-mail", 254),
    "senha": ("A senha", 200),
    "whatsapp": ("O WhatsApp", 20),
    "cpf": ("O CPF", 14),
    "cidade": ("A cidade", 80),
    "placa": ("A placa", 10),
    "modelo_veiculo": ("O modelo do veículo", 80),
    "prefixo_taxi": ("O prefixo do táxi", 20),
    "numero_alvara": ("O número do alvará", 40),
    "indicacao": ("O código de indicação", 12),
    # Cooperativa ou associação de táxi (só no perfil; opcional).
    "cooperativa": ("O nome da cooperativa", 120),
    "cnpj_cooperativa": ("O CNPJ da cooperativa", 18),
}

# Teto por lote na limpeza, para a rotina não estourar o tempo da função
# quando houver backlog grande.
CLEANUP_BATCH_LIMIT = 500

# Teto diario de recibos enviados por e-mail, por motorista. Existe para
# proteger a reputacao do dominio: um endereco que dispara em volume vira spam
# para os provedores, e ai nem o e-mail de senha chega mais. Bater no teto nao
# impede emitir recibo — so deixa de mandar o e-mail daquele.
EMAIL_DAILY_LIMIT = 60
# A Política de Privacidade promete remover recibos sem cadastro em 12 meses.
GUEST_RETENTION_DAYS = 365
COUNTER_RETENTION_DAYS = 7
# Registros de acesso (login, renovação de sessão e cadastro: data, hora e IP),
# guardados por 6 meses como o Marco Civil (art. 15) exige de provedor de
# aplicação com fins econômicos. Migração 0016. A Política de Privacidade e a
# página de exclusão dizem exatamente isto.
ACCESS_LOG_RETENTION_DAYS = 180
# Histórico no painel do site: a partir desta data o plano Grátis vê só os
# recibos do mês atual; o Pro vê todos. Decisão do Anderson em 2026-11-10,
# com os 15 dias de aviso que os Termos (seção 11) prometem. Os recibos
# continuam guardados e os links públicos continuam abrindo.
HISTORICO_GRATIS_DESDE = "2026-11-26"
# past_due mantém o plano enquanto a Stripe tenta novas cobranças.
ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trialing", "past_due")

RESET_TOKEN_MAX_AGE = 3600  # 1 hora
# Pedidos de redefinicao por dia, por e-mail e por IP. Nao e economia: cada
# pedido e um e-mail, e um script disparando queima a reputacao do dominio —
# derrubando junto o e-mail de senha de quem precisa de verdade. O teto por IP e
# largo porque operadora de celular poe milhares de aparelhos atras do mesmo IP.
RESET_DAILY_LIMIT_EMAIL = 5
RESET_DAILY_LIMIT_IP = 30
# Tentativas de login por dia: por conta (força bruta numa senha) e por IP.
# O teto por IP é largo porque operadora de celular põe milhares de aparelhos
# atrás do mesmo endereço; o que segura o ataque é o teto por conta. Cadastro
# por IP idem: uma tarde num ponto de táxi cadastra dezenas na mesma rede.
LOGIN_DAILY_LIMIT_EMAIL = 30
LOGIN_DAILY_LIMIT_IP = 1000
CADASTRO_DAILY_LIMIT_IP = 200

# Estimativa de corrida (/api/estimativa). A rota vem da Routes API do Google,
# que cobra cada consulta acima da cota gratis do mes. O teto diario por
# motorista existe para um aparelho em laco, ou um token vazado, nao gastar a
# cota de todos; 40 e mais do que um turno inteiro de corridas.
ESTIMATIVA_DIARIA_LIMITE = 40
# Teto de todos os motoristas juntos, por dia. Criar conta e gratis, entao o
# teto por motorista sozinho nao fecha a conta do Google: 300/dia cabe na
# cota gratis do SKU com transito (5.000/mes) e segura um abuso ate alguem olhar.
ESTIMATIVA_DIARIA_GLOBAL = 300
ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

# Caixa do app 1.8 (POST /api/lancamentos, migração 0022). O teto diário conta
# ITENS, não pedidos: um motorista real lança umas 40 por dia, e 500 segura um
# aparelho em laço ou um token vazado. A primeira carga de quem já tem mais de
# 500 recibos termina no dia seguinte, e isso é aceitável (o app reenvia).
LANCAMENTOS_DIARIOS = 500
LANCAMENTOS_POR_LOTE = 100
# Lançamento apagado no app fica no servidor como túmulo (é ele que impede o
# recibo apagado de voltar ao caixa) e sai de vez depois disto. A Política de
# Privacidade promete "em até 30 dias".
LANCAMENTO_APAGADO_DIAS = 30

# Painel /admin. Tentativas de login por dia: o painel mostra CPF e telefone de
# todos os motoristas, e a senha inicial e curta por decisao do dono — o teto
# e o que impede um script de adivinhar. A sessao do admin morre sozinha em
# 12 horas; a do motorista dura 90 dias porque um recibo nao vale um CPF alheio.
ADMIN_LOGIN_DAILY_LIMIT_IP = 20
ADMIN_LOGIN_DAILY_LIMIT_EMAIL = 10         # por e-mail vindo do mesmo IP
ADMIN_LOGIN_DAILY_LIMIT_EMAIL_TOTAL = 100  # por e-mail, somando todos os IPs
ADMIN_SESSION_HOURS = 12
ADMIN_IDLE_MINUTES = 60
ADMIN_PASSWORD_MIN = 8
ADMIN_PAGE_SIZE = 50
RESET_TOKEN_SALT = "recibo-taxi-redefinir-senha"

# Automação de e-mails para quem ainda não assinou (/admin/emails). Fora desta
# faixa (horário de Brasília) o motor não manda nada, mesmo com e-mail vencido:
# oferta às 23h irrita e vai para o spam.
EMAIL_AUTO_HORA_INICIO = 8
EMAIL_AUTO_HORA_FIM = 20  # inclusive: a última leva sai entre 20h e 21h
# Quem ficou para trás mais que isto não recebe mais aquele e-mail. É o que
# impede que ligar o motor mande a régua inteira de uma vez para a base antiga:
# quem se cadastrou há 40 dias não recebe o "dia 3", o "dia 7" e o "dia 30".
EMAIL_AUTO_JANELA_DIAS = 3
EMAIL_AUTO_JANELA_CAMPANHA_DIAS = 7
# Por execução do cron. Com a pausa entre envios, cabe folgado no tempo da função.
EMAIL_AUTO_LOTE = 60
EMAIL_AUTO_PAUSA_S = 0.6  # o Resend aceita 2 pedidos por segundo por conta
EMAIL_AUTO_TESTES_DIA = 30  # e-mails de teste por administrador por dia
# Nunca entram na automação: contas da suíte (TLD reservada) e contas nossas,
# como a de demonstração da Apple — o domínio não recebe e-mail, e cada
# devolução pesa na reputação de quem manda.
EMAIL_AUTO_SUFIXOS_EXCLUIDOS = (".invalid", "@recibotaxi.com.br")
DESCADASTRO_SALT = "recibo-taxi-descadastro-email"
# Avisos internos para a equipe: cadastro novo e assinatura nova do Pro.
# EMAIL_AVISOS muda o destino; com valor vazio, desliga os dois. O teto diário
# do cadastro existe para um robô cadastrando em massa não gastar a cota do
# Resend que o e-mail de senha e o de recibo também usam.
EMAIL_AVISOS_PADRAO = "suporte@recibotaxi.com.br"
EMAIL_AVISO_CADASTRO_DIA = 50
AVISO_CADASTRO_CHAVE = "aviso_cadastro"      # contador diário (a suíte troca)
AVISO_ASSINATURA_CHAVE = "aviso_assinatura"  # um aviso por evento de webhook (a suíte troca)
PRECO_PRO_MENSAL = "R$ 9,90"    # R$ 19,90 até 2026-11-10 (decisão do Anderson)
PRECO_PRO_ANUAL = "R$ 59,90"    # R$ 119,90 até 2026-11-10; dá R$ 4,99 por mês
# Anual pago uma vez (Pix ou cartão), fora das lojas: oferta que sai pelo
# WhatsApp e pela página /oferta/anual, que o app nunca aponta (regra 3.1.3(b)
# da Apple: comprado fora vale no app, desde que o app não leve até a oferta).
# O valor é decisão do Anderson (2026-10-19: 59,90 ou 49,90); trocar aqui e o
# Price na Stripe basta.
PRECO_PRO_ANUAL_PIX = "R$ 59,90"
PRO_PREPAGO_DIAS = 365
# Plano escolhido por quem clicou em "Assinar" antes de ter conta: aparece no
# cadastro e no login, e depois deles a pessoa vai direto para o pagamento.
PLANOS_ROTULO = {
    "pro": f"Pro mensal ({PRECO_PRO_MENSAL} por mês)",
    "pro_anual": f"Pro anual ({PRECO_PRO_ANUAL} por ano)",
    "pro_anual_pix": f"Pro anual por Pix ({PRECO_PRO_ANUAL_PIX}, pagamento único)",
}

# Venda pelo painel (pedido do Anderson em 2026-11-19: um vendedor liga para os
# motoristas e, fechado o negócio, o painel manda o link). Dois links assinados
# com a SECRET_KEY, cada um com o seu sal e o seu prazo:
# - /pagar/<token>: leva ao pagamento de UM motorista, sem login. Só abre o
#   pagamento; não dá acesso à conta, então um link encaminhado para a pessoa
#   errada no máximo deixa alguém pagar o Pro de outro.
# - /criar-senha/<token>: só para conta criada pelo painel que ainda não tem
#   senha. Cria a senha, entra no site e, se veio com plano, segue para o
#   pagamento. Morre no primeiro uso (a digital da senha muda).
# Prévia do WhatsApp e antivírus de e-mail abrem os links sozinhos: o GET só
# mostra a página; quem cobra ou grava é o POST.
LINK_PAGAMENTO_SALT = "recibo-taxi-link-de-pagamento"
LINK_PAGAMENTO_DIAS = 7
CRIAR_SENHA_SALT = "recibo-taxi-criar-senha"
CRIAR_SENHA_DIAS = 7
# Erros na confirmação pelos 4 últimos dígitos do WhatsApp, por link: 5 em
# 10.000 combinações. Depois disso, só com um link novo do painel.
CRIAR_SENHA_TENTATIVAS = 5
# Ordem e texto das opções do painel. O preço vem das constantes acima; o valor
# cobrado é o do Price na Stripe.
PLANOS_DO_LINK = {
    "pro_anual_pix": f"Anual no Pix ou cartão: {PRECO_PRO_ANUAL_PIX}, pagamento único, vale 12 meses e não renova",
    "pro_anual": f"Anual no cartão: {PRECO_PRO_ANUAL} por ano, renova sozinho",
    "pro": f"Mensal no cartão: {PRECO_PRO_MENSAL} por mês, renova sozinho",
}
# Política e Termos de 2026-11-21 (venda por telefone com representante). A
# própria Política promete 15 dias de aviso para mudança relevante: as ligações e
# o representante de vendas só começam nesta data, e o aviso vai por e-mail.
# Versão de 2026-11-27: o Caixa do app 1.8 (lançamentos com cópia no servidor).
POLITICA_ATUALIZADA_EM = "27 de novembro de 2026"   # rascunho: acertar no dia do merge
CONTATO_DE_VENDAS_A_PARTIR = "06/12/2026"
# Caixa do app 1.8: a data de publicação da Política + os 15 dias de aviso que
# ela promete (seção 11). Rascunho: acertar no dia do merge.
CAIXA_A_PARTIR = "12/12/2026"
# Integração com o PilotPostAi (CRM e atendimento da mesma empresa): cada
# motorista vira cliente da conta do suporte lá (migração 0021). Um trigger em
# `drivers` enfileira criação, alteração, "parar ofertas" e exclusão em
# crm_sync; o envio (/tarefas/crm a cada 15 min, e logo depois do cadastro)
# manda o estado ATUAL do motorista, não o evento antigo. Desligado sem
# CRM_URL + CRM_SECRET na Vercel. Vai só o que a Política dá ao atendimento:
# nome, WhatsApp, e-mail, cidade, UF, plano, canal e opt-out. Nunca CPF,
# placa, recibos ou pagamentos.
CRM_POR_RODADA = 50
# Marca, no app_metadata do Supabase Auth, a conta que o painel criou. O
# app_metadata o motorista não consegue editar (o user_metadata, sim).
MARCA_CRIADO_PELO_PAINEL = "criado_pelo_painel"
# Conta interna (do Anderson, da equipe, de teste com cartão real): fica fora do
# faturamento da visão geral. Mesmo lugar da marca acima, que só o servidor escreve.
MARCA_CONTA_INTERNA = "conta_interna"

# Indicação: o colega indicado emite o primeiro recibo e quem indicou ganha um
# mês de Pro (até INDICACAO_MAX_MESES, um por colega ativo). Só quem indica
# ganha; o indicado não (decisão do Anderson, 2026-11-10). Teto por colega:
# sem teto, criar contas falsas renderia Pro para sempre.
INDICACAO_MESES_POR_INDICADO = 1
INDICACAO_MAX_MESES = 3
# Cupom de 70% por 12 meses no Pro mensal do site: ENCERRADO em 2026-11-10,
# junto com a queda do preço (R$ 19,90 → R$ 9,90). Ninguém ganha mais; quem
# já tinha ganhado (referral_coupon_granted_at sem used_at) continua podendo
# usar no site, como os Termos prometem. As constantes ficam só para isso. O
# desconto equivalente das lojas (código INDICA70 na Apple, oferta indicacao70
# no Google) saiu da sessão do app; o Anderson desativa os dois nos consoles.
INDICACAO_CUPOM_PCT = 70
INDICACAO_CUPOM_MESES = 12
INDICACAO_CUPOM_ID = "INDICACAO70"   # id do cupom na Stripe; criado sob demanda
INDICACAO_ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # sem 0/O, 1/I
# Aberturas da página do convite (/i/CÓDIGO) por IP e dia. Ver a rota.
CONVITE_DIARIO_LIMITE_IP = 300
INDICACAO_TAMANHO = 6
# Pro que não é assinatura: vive em drivers.pro_until e o cron rebaixa.
# "cortesia" é o Pro liberado pelo painel /admin (parceria com cooperativa,
# teste com um criador): mesma mecânica do pré-pago, sem pagamento nem indicação.
STATUS_PREPAGO = ("prepago", "indicacao", "cortesia")
# Vai depois do botão, em todo e-mail automático: no texto ela ficaria antes dele.
EMAIL_ASSINATURA = "Equipe Recibo Táxi"

# Os scripts inline carregam um nonce por requisição, então script-src dispensa
# 'unsafe-inline'. style-src ainda precisa: o Bootstrap injeta estilos inline em
# componentes como o collapse, e o risco de um estilo injetado é bem menor.
CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net 'nonce-{nonce}'; "
    "style-src 'self' https://cdn.jsdelivr.net https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com data:; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self' https://checkout.stripe.com https://billing.stripe.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'"
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Tudo desligado: o app não usa nenhuma dessas APIs. Menos permissão
    # declarada é menos coisa a justificar na revisão das lojas.
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=()",
}
# A versao web do app (/app/, so para desenvolvimento) precisa da localizacao
# para a tela da corrida; o site continua sem.
PERMISSIONS_POLICY_APP = "camera=(), microphone=(), payment=()"

STRIPE_PRICE_IDS = {
    "pro": "STRIPE_PRO_PRICE_ID",
    # Anual = o MESMO Pro, cobrado uma vez por ano: R$ 59,90, o mesmo valor no
    # site, na App Store e no Google Play (decisão do Anderson em 2026-10-03).
    # Antes era R$ 119,40, metade exata de 12 mensalidades, mas a Apple não tem
    # esse ponto de preço; 59,90 é o mais próximo. É um Price a mais no mesmo
    # produto do Stripe:
    # o metadata.plan do checkout continua "pro", então webhook, gates e a
    # tela de planos não distinguem ciclo — só o Stripe sabe.
    "pro_anual": "STRIPE_PRO_ANUAL_PRICE_ID",
    # Pagamento único (mode=payment na Stripe), não assinatura: o Pro vale
    # PRO_PREPAGO_DIAS a partir do pagamento e não renova sozinho.
    "pro_anual_pix": "STRIPE_PRO_ANUAL_PIX_PRICE_ID",
}
# Ciclo de cada chave de checkout. Tudo que não é "anual" é mensal.
PLAN_CYCLES = {"pro_anual": "anual", "pro_anual_pix": "anual"}
PLANOS_PREPAGOS = {"pro_anual_pix"}
# True = o anual renova sozinho a cada 12 meses (cancelável no portal, como o
# mensal). False = "pagamento único" literal: o webhook marca
# cancel_at_period_end e o Stripe encerra no fim do ano, derrubando pra free
# pelo customer.subscription.deleted de sempre. Trocar aqui basta.
ANUAL_RENOVA_AUTOMATICAMENTE = True


def plano_base(plan: str) -> str:
    """'pro_anual' → 'pro'. É o valor gravado em drivers.plan."""
    return (plan or "").split("_", 1)[0]


def to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat() + "Z"


def now_iso() -> str:
    return to_utc_iso(datetime.now(timezone.utc))


def today_br() -> str:
    return datetime.now(BR_TZ).strftime("%Y-%m-%d")


def month_range_utc(reference: datetime | None = None) -> tuple[str, str]:
    """Início e fim do mês corrente no fuso de Brasília, como ISO em UTC.

    Os recibos são gravados em UTC, mas o mês que vale para o motorista é o
    local — sem isso um recibo emitido às 22h do dia 31 cairia no mês seguinte.
    """
    local = (reference or datetime.now(BR_TZ)).astimezone(BR_TZ)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return to_utc_iso(start), to_utc_iso(end)


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def email_valido(value: str) -> bool:
    """Confere so o que evita erro bobo: uma arroba, um ponto no dominio, sem
    espaco e dentro do tamanho da RFC.

    Nao tenta validar se o endereco existe. Isso quem responde e o provedor do
    destinatario, e o motorista digitou o que o passageiro ditou — recusar um
    endereco plausivel na tela custaria mais que um e-mail devolvido.
    """
    endereco = normalize_email(value)
    if not endereco or len(endereco) > 254 or any(c.isspace() for c in endereco):
        return False
    if endereco.count("@") != 1:
        return False
    usuario, _, dominio = endereco.partition("@")
    return bool(usuario) and "." in dominio and not dominio.startswith(".") \
        and not dominio.endswith(".")


def sanitize_phone(value: str) -> str:
    return "".join(char for char in (value or "") if char.isdigit())


def phone_e164(value: str, pais: str = PAIS_PADRAO) -> str:
    """Telefone no formato que o wa.me exige: so digitos, com codigo do pais.

    Sem o 55 na frente, o link abre o WhatsApp sem destinatario e o motorista
    precisa ter o passageiro salvo na agenda — exatamente o que este app existe
    para evitar.

    A classificacao e por tamanho, nao por prefixo, e isso importa: existe o DDD
    55 (Santa Maria/RS). "5598765432" tem 10 digitos, entao e DDD 55 + fixo, e
    vira "555598765432". Quem olhasse so o prefixo trataria como codigo de pais
    e mandaria a mensagem para um numero que nao existe.

    `pais` e o do motorista: o passageiro de uma corrida na Costa Rica dita um
    numero de la. Com o + na frente, o numero ja e internacional em qualquer pais.
    """
    bruto = (value or "").strip()
    digitos = sanitize_phone(bruto)
    if bruto.startswith("+"):
        return digitos if 8 <= len(digitos) <= 15 else ""
    if pais != "BR":
        return _telefone_de_fora(digitos, pais)
    if digitos.startswith("00"):      # prefixo de discagem internacional
        digitos = digitos[2:]
    digitos = digitos.lstrip("0")     # zero de DDD interurbano
    if len(digitos) in (10, 11):      # DDD + numero, sem pais
        return "55" + digitos
    if len(digitos) in (12, 13):      # ja veio com o codigo do pais
        return digitos
    return ""                         # curto ou longo demais: nao da link


def _telefone_de_fora(digitos: str, pais: str) -> str:
    """phone_e164 fora do Brasil, onde nao ha a regra de tamanho do DDD.

    Numero que ja comeca com o codigo do pais e tem tamanho de numero
    internacional fica como esta; o resto ganha o codigo na frente. Sem pais
    conhecido ("Outro pais"), so da link quem digitou com 00.
    """
    ddi = PAISES.get(pais, ("", "", ""))[2]
    if digitos.startswith("00"):
        digitos = digitos[2:]
    elif ddi and not (digitos.startswith(ddi) and len(digitos) >= len(ddi) + 7):
        digitos = ddi + digitos.lstrip("0")
    elif not ddi:
        return ""
    return digitos if 8 <= len(digitos) <= 15 else ""


def razao_social_de(documento: str, valor: str) -> str:
    """Razao social so existe quando o documento e CNPJ.

    A regra mora aqui, e nao so na tela do app: o servidor nao confia no
    JavaScript que esconde o campo, e um CPF com nome de empresa junto seria
    um recibo que a contabilidade recusa. Quatorze digitos e CNPJ; qualquer
    outra coisa devolve vazio.
    """
    if len(re.sub(r"\D", "", documento or "")) != 14:
        return ""
    return " ".join((valor or "").split())


def format_document_br(value: str, pais: str = PAIS_PADRAO) -> str:
    """Pontua CPF e CNPJ; devolve como veio se nao reconhecer o tamanho.

    Nao valida digito verificador de proposito: o campo e opcional e serve para
    o passageiro prestar contas. Recusar um documento estrangeiro, ou um CPF
    que o passageiro ditou errado, so trocaria um recibo util por um erro na
    tela do taxista, com o passageiro esperando dentro do carro.

    Fora do Brasil, 11 digitos nao sao CPF: o documento fica como veio.
    """
    if pais != "BR":
        return (value or "").strip()
    d = sanitize_phone(value)          # mesma limpeza: so digitos
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return (value or "").strip()


def format_date_br(date_value: str) -> str:
    if not date_value:
        return "-"
    try:
        return datetime.strptime(date_value, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return date_value


def dados_da_moeda(moeda: str | None) -> tuple[str, str, int, Decimal]:
    """(nome, símbolo, casas, teto) da moeda. Código fora da lista não quebra:
    vira o próprio código como símbolo, com centavos e o teto do real."""
    codigo = (moeda or MOEDA_PADRAO).upper()
    return MOEDAS.get(codigo) or (codigo, codigo, 2, MAX_RECEIPT_AMOUNT)


def formatar_numero(valor, casas: int = 2) -> str:
    """1234.5 -> "1.234,50": ponto no milhar e vírgula nos centavos, como o
    resto do app, que é em português. O símbolo muda; a pontuação não."""
    n = Decimal(str(valor)).quantize(Decimal(1).scaleb(-casas))
    return f"{n:,.{casas}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def formatar_valor(valor, moeda: str | None = None) -> str:
    """Valor com o símbolo da moeda: "R$ 1.234,50", "₡ 15.000,00"."""
    if valor is None or valor == "":
        return "-"
    _, simbolo, casas, _ = dados_da_moeda(moeda)
    try:
        return f"{simbolo} {formatar_numero(valor, casas)}"
    except (InvalidOperation, ValueError):
        return "-"


def _separadores_do_valor(bruto: str) -> str:
    """Troca a pontuação digitada pela do Decimal ("1.500,50" -> "1500.50").

    Não depende do país, porque cada um escreve de um jeito ("1.500,50" no
    Brasil, "1,500.50" no México, "15 000" na Costa Rica):
      - com ponto e vírgula, o que vem por último separa os centavos;
      - um separador só, repetido ("1.500.000"), é de milhar;
      - um separador só, uma vez, seguido de exatamente 3 dígitos ("1.500",
        "15,000"), é de milhar — dinheiro não tem 3 casas, e era aqui que
        "1.500" virava R$ 1,50 no banco enquanto o app mostrava 1.500;
      - nos outros casos ("45.50", "45,5"), separa os centavos.
    """
    if "," in bruto and "." in bruto:
        decimal_sep = "," if bruto.rfind(",") > bruto.rfind(".") else "."
        milhar_sep = "." if decimal_sep == "," else ","
        return bruto.replace(milhar_sep, "").replace(decimal_sep, ".")
    for sep in (",", "."):
        if sep not in bruto:
            continue
        partes = bruto.split(sep)
        inteiro = partes[0].lstrip("-")
        de_milhar = all(len(p) == 3 and p.isdigit() for p in partes[1:]) \
            and 1 <= len(inteiro) <= 3 and inteiro.isdigit() and inteiro != "0"
        if len(partes) > 2 or de_milhar:
            return bruto.replace(sep, "") if de_milhar else bruto  # "1.2.3": inválido
        return bruto.replace(sep, ".")
    return bruto


def normalize_money(value: str, moeda: str | None = None) -> tuple[str, str]:
    """Valor digitado -> (valor para o banco, valor para exibir sem símbolo)."""
    _, simbolo, casas, teto = dados_da_moeda(moeda)
    # Espaço de qualquer tipo sai (a Costa Rica escreve "15 000"), e o símbolo
    # da moeda só sai das pontas: "12abc34" continua inválido. O símbolo da
    # moeda sai inteiro antes, porque "Bs." termina em ponto.
    bruto = re.sub(r"\s", "", str(value or ""))
    for marca in (simbolo, (moeda or MOEDA_PADRAO).upper()):
        if bruto.upper().startswith(marca.upper()):
            bruto = bruto[len(marca):]
        if bruto.upper().endswith(marca.upper()):
            bruto = bruto[:-len(marca)]
    bruto = re.sub(r"[^\d,.-]+$", "", re.sub(r"^[^\d,.-]+", "", bruto))
    if not bruto:
        raise ValueError("Informe o valor da corrida.")

    try:
        amount = Decimal(_separadores_do_valor(bruto))
    except InvalidOperation as exc:
        raise ValueError("Informe um valor válido (ex: 45,00).") from exc

    # Decimal aceita "NaN" e "Infinity" como valores válidos.
    if not amount.is_finite():
        raise ValueError("Informe um valor válido (ex: 45,00).")

    amount = amount.quantize(Decimal(1).scaleb(-casas))

    if amount <= 0:
        raise ValueError("O valor da corrida precisa ser maior que zero.")
    if amount > teto:
        raise ValueError("Valor acima do limite para um recibo de corrida.")

    return str(amount), formatar_numero(amount, casas)


def normalize_km(value) -> str | None:
    """"12,4", "12.4" ou "12" → "12.4" para o banco; vazio → None.

    Opcional de proposito: quem nao sabe o km nao pode ficar sem recibo. Mas
    o que vier tem de ser numero: "uns 12" vira erro na tela, nao lixo no PDF.
    """
    bruto = re.sub(r"\s", "", str(value or "")).lower().replace("km", "")
    if not bruto:
        return None
    try:
        km = Decimal(bruto.replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Informe a distância em km, só o número (ex.: 12,4).") from exc
    if not km.is_finite() or km <= 0 or km > Decimal("9999.9"):
        raise ValueError("Informe a distância em km, só o número (ex.: 12,4).")
    # Arredondamento de escola (12,45 → 12,5), não o "para o par" do Decimal.
    return str(km.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def formatar_km(valor) -> str:
    """"12.4" → "12,4 km"; vazio → ""."""
    if valor is None or valor == "":
        return ""
    try:
        return f"{formatar_numero(valor, 1)} km"
    except (InvalidOperation, ValueError):
        return ""


def hora_valida(valor: str) -> str:
    """"HH:MM" ou vazio; qualquer outra coisa vira vazio em vez de quebrar o recibo."""
    valor = (valor or "").strip()
    return valor if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", valor) else ""


def pais_e_moeda(pais: str | None, moeda: str | None) -> tuple[str, str]:
    """Confere o país e a moeda escolhidos. Vazio vira o padrão: Brasil e a
    moeda do país. Fora da lista levanta ValueError com a frase para a tela."""
    codigo_pais = (pais or "").strip().upper() or PAIS_PADRAO
    if codigo_pais not in PAISES:
        raise ValueError("Escolha o país na lista.")
    codigo_moeda = (moeda or "").strip().upper() or PAISES[codigo_pais][1]
    if codigo_moeda not in MOEDAS:
        raise ValueError("Escolha a moeda na lista.")
    return codigo_pais, codigo_moeda


def snapshot_do_motorista(user: dict) -> dict:
    """Cópia dos dados do motorista que vai dentro do recibo, para o recibo
    entregue não mudar quando ele editar o cadastro depois."""
    return {
        "full_name": user["full_name"],
        "email": user["email"],
        "whatsapp": user.get("whatsapp", ""),
        "city": user.get("city", ""),
        "plate": user.get("plate", ""),
        "vehicle_model": user.get("vehicle_model", ""),
        "taxi_prefix": user.get("taxi_prefix", ""),
        "license_number": user.get("license_number", ""),
        # Cooperativa ou associação de táxi, com o CNPJ: é o que a empresa do
        # passageiro procura no recibo para aceitar o reembolso (2026-11-12).
        "coop_name": user.get("coop_name") or "",
        "coop_cnpj": user.get("coop_cnpj") or "",
        "country": user.get("country") or PAIS_PADRAO,
    }


def cooperativa_do_recibo(receipt: dict) -> str:
    """'Nome · CNPJ 00.000.000/0001-00', ou vazio quando o motorista não informou."""
    retrato = receipt.get("driver_snapshot") or {}
    nome, cnpj = (retrato.get("coop_name") or "").strip(), (retrato.get("coop_cnpj") or "").strip()
    if not nome and not cnpj:
        return ""
    return " · ".join(p for p in (nome, f"CNPJ {cnpj}" if cnpj else "") if p)


def pais_do_recibo(receipt: dict) -> str:
    """País do motorista quando o recibo foi emitido. Recibo de antes da
    migração 0010 não guardou o país, e todos eram do Brasil."""
    return (receipt.get("driver_snapshot") or {}).get("country") or PAIS_PADRAO


def rotulo_documento(pais: str) -> str:
    return "CPF/CNPJ" if pais == "BR" else "Documento"


def horario_da_corrida(receipt: dict) -> str:
    """"08:10 às 08:45", "saída 08:10" ou "" — para mensagem, página e e-mail."""
    saida, chegada = receipt.get("trip_time") or "", receipt.get("trip_end_time") or ""
    if saida and chegada:
        return f"{saida} às {chegada}"
    if saida:
        return f"saída {saida}"
    if chegada:
        return f"chegada {chegada}"
    return ""


def compose_receipt_message(receipt: dict, public_url: str) -> str:
    valor = receipt.get("valor_formatado") or formatar_valor(
        receipt.get("amount_value"), receipt.get("currency"))
    lines = [
        f"✅ Recibo #{receipt['rid']}",
        # O nome e opcional: sem ele, nada de "Passageiro: -".
        *([f"👤 Passageiro: {receipt['passenger']}"] if receipt.get("passenger") else []),
        # Documento e razao social so entram quando existem: sao o que faz o
        # recibo valer para a empresa lancar a despesa. O app ja mandava no
        # WhatsApp; sem isto, o mesmo recibo por e-mail saia sem eles.
        # CPF mascarado: a mensagem é encaminhável; o PDF do app leva o inteiro.
        *([f"🧾 {rotulo_documento(pais_do_recibo(receipt))}: "
           f"{mascarar_documento(receipt['passenger_document'], pais_do_recibo(receipt))}"]
          if receipt.get("passenger_document") else []),
        *([f"🏢 Razão social: {receipt['passenger_company']}"]
          if receipt.get("passenger_company") else []),
        f"📅 Data: {receipt.get('trip_date_display') or '-'}",
        # Saida, chegada e km: o que a empresa do passageiro olha para aprovar
        # a despesa. So entram quando existem.
        *([f"🕐 Horário: {horario_da_corrida(receipt)}"] if horario_da_corrida(receipt) else []),
        f"📍 Origem: {receipt.get('origin') or '-'}",
        f"🏁 Destino: {receipt.get('destination') or '-'}",
        *([f"📏 Distância: {receipt['km_display']}"] if receipt.get("km_display") else []),
        f"💰 Valor: {valor}",
        f"💳 Pagamento: {receipt.get('payment_method') or '-'}",
        # Cooperativa do motorista, com o CNPJ, quando ele informou no perfil.
        *([f"🚖 Cooperativa: {cooperativa_do_recibo(receipt)}"] if cooperativa_do_recibo(receipt) else []),
        "",
        f"🔗 Acesse o recibo: {public_url}",
    ]
    # O recibo e repassado entre passageiros; o telefone impresso traz corrida
    # nova para o motorista sem custo de divulgacao.
    fone_motorista = (receipt.get("driver_snapshot") or {}).get("whatsapp") or ""
    if fone_motorista:
        lines += ["", f"🚕 Precisou de corrida? Me chame no WhatsApp: {fone_motorista}"]
    return "\n".join(lines)


def build_whatsapp_link(receipt: dict, public_url: str, with_recipient: bool = True) -> str:
    """Link de compartilhamento.

    with_recipient=False omite o telefone do passageiro: a página do recibo é
    pública e qualquer visitante com o link leria o destinatário no href.
    """
    phone = phone_e164(receipt.get("passenger_whatsapp", ""), pais_do_recibo(receipt)) \
        if with_recipient else ""
    message = quote(compose_receipt_message(receipt, public_url))
    if phone:
        return f"https://wa.me/{phone}?text={message}"
    return f"https://wa.me/?text={message}"


def build_email_link(receipt: dict, public_url: str, with_recipient: bool = True) -> str:
    """Idem: sem with_recipient o mailto vai sem o e-mail do passageiro."""
    raw_target = (receipt.get("passenger_email") or "").strip() if with_recipient else ""
    target = quote(raw_target)
    subject = quote(f"Recibo #{receipt['rid']} — Recibo Táxi")
    body = quote(compose_receipt_message(receipt, public_url))
    return f"mailto:{target}?subject={subject}&body={body}"


def enviar_recibo_por_email(receipt: dict) -> bool:
    """Manda o recibo para o e-mail do passageiro. Devolve se o provedor aceitou.

    O corpo e o mesmo texto do WhatsApp, com o link da pagina publica — e la que
    o passageiro ve o recibo formatado, imprime ou salva em PDF. Texto puro, e
    nao HTML, porque o que importa e chegar: e-mail transacional simples passa
    por qualquer filtro e qualquer leitor.
    """
    destino = normalize_email(receipt.get("passenger_email", ""))
    if not email_valido(destino):
        return False

    motorista = (receipt.get("driver_snapshot") or {}).get("full_name") or ""
    abertura = f"Segue o recibo da sua corrida com {motorista}." if motorista \
        else "Segue o recibo da sua corrida."
    corpo = "\n".join([
        "Olá!",
        "",
        abertura,
        "",
        compose_receipt_message(receipt, public_receipt_url(receipt["rid"])),
        "",
        "Este recibo não é documento fiscal.",
        "Você recebeu este e-mail porque este endereço foi informado no momento "
        "da corrida. Não é preciso responder.",
    ])
    # Tempo curto: este envio acontece dentro da requisicao que grava o recibo,
    # e o aparelho espera essa resposta para liberar o botao de enviar.
    return send_email(destino, f"Recibo da sua corrida — #{receipt['rid']}",
                      corpo, timeout=8)


def pode_enviar_email(driver_id: str) -> bool:
    """Consome uma unidade do teto diario do motorista."""
    usados = get_store().bump_counter(f"email:{driver_id}:{today_br()}")
    return usados <= EMAIL_DAILY_LIMIT


def chave_de_contador(valor: str) -> str:
    """E-mail (ou outro dado pessoal) numa chave de rate_limits: só o hash. A
    tabela dura 7 dias e aparece no painel; não precisa guardar o e-mail em
    claro, e o pedido de redefinição grava até e-mail de quem nem é cliente."""
    return hashlib.sha256(valor.strip().lower().encode("utf-8")).hexdigest()[:16]


def passou_do_teto(prefixo: str, chave: str, limite: int) -> bool:
    """Soma 1 ao contador do dia e diz se passou do teto. Banco fora do ar não
    bloqueia ninguém: o teto é proteção contra abuso, não requisito."""
    try:
        return get_store().bump_counter(f"{prefixo}:{chave}:{today_br()}") > limite
    except Exception as exc:
        app.logger.warning("contador %s indisponível: %s", prefixo, exc)
        return False


def registrar_acesso(driver_id: str, canal: str) -> None:
    """Grava o acesso sem nunca impedir o login: sem a tabela (migração 0016
    ainda não aplicada) ou com o banco lento, só avisa no log."""
    try:
        get_store().registrar_acesso(str(driver_id), client_ip(), canal)
    except Exception as exc:
        app.logger.warning("registro de acesso não gravado (migração 0016 aplicada?): %s", exc)


def _chaves_de_login(email: str) -> list[tuple[str, int]]:
    """Contadores de FALHAS de login do dia. O par conta+IP é o que segura força
    bruta sem deixar um estranho trancar a conta de alguém só com o e-mail;
    o teto por conta sozinha, bem mais largo, é a rede de segurança."""
    dia = today_br()
    h = chave_de_contador(email)
    return [(f"login:ei:{h}:{client_ip()}:{dia}", LOGIN_DAILY_LIMIT_EMAIL),
            (f"login:e:{h}:{dia}", LOGIN_DAILY_LIMIT_EMAIL * 5),
            (f"login:ip:{client_ip()}:{dia}", LOGIN_DAILY_LIMIT_IP)]


def login_bloqueado(email: str) -> bool:
    """Já passou do teto de falhas hoje? Só lê; quem soma é registrar_falha_de_login."""
    if not email:
        return False
    try:
        store = get_store()
        return any(store.get_counter(chave) >= limite for chave, limite in _chaves_de_login(email))
    except Exception as exc:
        app.logger.warning("teto de login indisponível: %s", exc)
        return False


def registrar_falha_de_login(email: str) -> None:
    if not email:
        return
    try:
        store = get_store()
        for chave, _ in _chaves_de_login(email):
            store.bump_counter(chave)
    except Exception as exc:
        app.logger.warning("contador de login indisponível: %s", exc)


def zerar_falhas_de_login(email: str) -> None:
    """Login certo ou senha redefinida: a conta volta a ter o dia inteiro.
    O contador do IP fica: um login certo não pode limpar a força bruta de
    um endereço contra outras contas."""
    try:
        store = get_store()
        for chave, _ in _chaves_de_login(email):
            if not chave.startswith("login:ip:"):
                store.reset_counter(chave)
    except Exception as exc:
        app.logger.warning("contador de login indisponível: %s", exc)


def pode_pedir_redefinicao(email: str) -> bool:
    """Consome uma unidade dos tetos diarios de redefinicao (e-mail e IP)."""
    store = get_store()
    dia = today_br()
    por_email = store.bump_counter(f"reset:e:{chave_de_contador(email)}:{dia}")
    por_ip = store.bump_counter(f"reset:ip:{client_ip()}:{dia}")
    return por_email <= RESET_DAILY_LIMIT_EMAIL and por_ip <= RESET_DAILY_LIMIT_IP


def cpf_valido(texto: str) -> bool:
    """Dígitos verificadores do CPF. Só para motorista do Brasil: em outro país
    o campo guarda outro documento."""
    digitos = re.sub(r"\D", "", texto or "")
    if len(digitos) != 11 or digitos == digitos[0] * 11:
        return False
    for tamanho in (9, 10):
        soma = sum(int(d) * peso for d, peso in zip(digitos[:tamanho], range(tamanho + 1, 1, -1)))
        esperado = (soma * 10) % 11 % 10
        if esperado != int(digitos[tamanho]):
            return False
    return True


def cnpj_limpo(texto: str) -> str:
    """Só letras e números, em maiúsculas: '12.abc.345/01de-35' → '12ABC34501DE35'."""
    return re.sub(r"[^0-9A-Za-z]", "", texto or "").upper()


def cnpj_valido(texto: str) -> bool:
    """Dígitos verificadores do CNPJ, numérico ou alfanumérico.

    Desde julho de 2026 a Receita dá às novas inscrições um CNPJ com letras
    nas 12 primeiras posições (os 2 verificadores seguem números). A conta é
    a mesma de sempre, com cada caractere valendo o código ASCII menos 48 —
    para um dígito, o próprio dígito. Exemplo oficial: 12.ABC.345/01DE-35.
    """
    s = cnpj_limpo(texto)
    if not re.fullmatch(r"[0-9A-Z]{12}\d{2}", s) or s == s[0] * 14:
        return False
    valores = [ord(c) - 48 for c in s]
    for tamanho in (12, 13):
        pesos = list(range(tamanho - 7, 1, -1)) + list(range(9, 1, -1))
        soma = sum(v * p for v, p in zip(valores[:tamanho], pesos))
        esperado = 0 if soma % 11 < 2 else 11 - soma % 11
        if esperado != valores[tamanho]:
            return False
    return True


def cnpj_pontuado(texto: str) -> str:
    """00.000.000/0000-00, também para o CNPJ alfanumérico."""
    s = cnpj_limpo(texto)
    return f"{s[:2]}.{s[2:5]}.{s[5:8]}/{s[8:12]}-{s[12:]}"


class CooperativaInvalida(ValueError):
    """Nome e CNPJ da cooperativa não fecham; `codigo` é o erro da API."""

    def __init__(self, codigo: str, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo


def cooperativa_informada(nome: str, cnpj: str, pais: str) -> tuple[str, str]:
    """Normaliza a cooperativa do perfil ou do cadastro: os dois juntos ou
    nenhum; CNPJ conferido pelos dígitos e devolvido pontuado. Fora do Brasil
    não há CNPJ: o documento fica como veio."""
    nome = " ".join((nome or "").split())
    cnpj = (cnpj or "").strip()
    if bool(nome) != bool(cnpj):
        raise CooperativaInvalida("cooperativa_incompleta",
                                  "Cooperativa: preencha o nome e o CNPJ, ou deixe os dois em branco.")
    if cnpj and pais == PAIS_PADRAO:
        if not cnpj_valido(cnpj):
            raise CooperativaInvalida("cnpj_invalido", "Confira o CNPJ da cooperativa: os dígitos não conferem.")
        cnpj = cnpj_pontuado(cnpj)
    return nome, cnpj


def guardar_cooperativa(user_id: str, coop_name: str, coop_cnpj: str) -> None:
    """Grava a cooperativa de um cadastro recém-criado. A linha em `drivers`
    nasce pelo trigger do Auth, que só conhece os campos antigos; sem nada
    informado, não toca no banco. Falha aqui não derruba o cadastro: o
    motorista completa em Meu perfil."""
    if not coop_name and not coop_cnpj:
        return
    try:
        get_store().update_user(user_id, {"coop_name": coop_name, "coop_cnpj": coop_cnpj})
    except Exception as exc:
        app.logger.warning("cooperativa do cadastro %s não gravada: %s", user_id, exc)


def canal_de_cadastro(valor) -> str:
    """O canal de divulgação que chegou com o cadastro: o `?c=` que o /baixar
    repassa ao /cadastro do site, ou o utm_source que o Google Play entrega ao
    app no primeiro acesso (Install Referrer, app 1.9+). Mesmo padrão do
    /baixar?c=; "" quando não serve."""
    canal = str(valor or "").strip().lower()
    return canal if CANAL_DOWNLOAD_RE.match(canal) else ""


def guardar_extras_do_cadastro(user_id: str, canal: str = "", uf: str = "") -> None:
    """Grava o canal de divulgação (migração 0018) e a UF (0019) de um cadastro
    recém-criado. Como a cooperativa: a linha em `drivers` nasce pelo trigger
    do Auth, que não conhece as colunas novas, e falha aqui não derruba o
    cadastro — só deixa os dois de fora, com aviso no log."""
    campos = {k: v for k, v in (("signup_channel", canal), ("state", uf)) if v}
    if not campos:
        return
    try:
        get_store().update_user(user_id, campos)
    except Exception as exc:
        app.logger.warning("canal/UF do cadastro %s não gravados (migrações 0018/0019 aplicadas?): %s", user_id, exc)


def chave_de_reconfirmacao(user_id: str) -> str:
    return f"reauth:{user_id}:{today_br()}"


def conferir_senha_atual(user: dict, senha: str) -> str:
    """Senha de quem já está logado (perfil e exclusão de conta, site e app).

    'bloqueada': passou de 30 erros hoje nesta conta; 'errada': conta um
    erro; 'certa': zera. O contador é só desta conta e só de quem já entrou
    (reauth:<id>:<dia>), separado do teto do login: o do login conta também
    por e-mail, e um estranho que só sabe o e-mail da pessoa trancaria a
    exclusão da conta dela (a Apple exige que excluir seja sempre possível).
    Sem teto nenhum, quem tem o token ou a sessão testava senhas à vontade.
    """
    chave = chave_de_reconfirmacao(user["_id"])
    store = get_store()
    # Conta a tentativa ANTES de conferir, num passo só: ler e somar depois
    # deixava pedidos simultâneos passarem todos pelo teto. A senha certa zera.
    try:
        if store.bump_counter(chave) > LOGIN_DAILY_LIMIT_EMAIL:
            return "bloqueada"
    except Exception as exc:
        app.logger.warning("teto de senha indisponível: %s", exc)
    if not auth_conferir_senha(user["email"], senha):
        return "errada"
    try:
        store.reset_counter(chave)
    except Exception as exc:
        app.logger.warning("contador de senha indisponível: %s", exc)
    return "certa"


MSG_MUITAS_TENTATIVAS = ("Muitas tentativas com a senha errada hoje. Tente amanhã ou redefina a senha "
                         "em \"Esqueci minha senha\".")


def enviar_link_de_redefinicao(user: dict) -> bool:
    """Manda o link de nova senha. Usado pelo site e pelo app.

    O link abre a pagina do site mesmo quando o pedido vem do app: a senha
    vive no Supabase Auth e a troca acontece no servidor. O motorista cria a
    senha nova no navegador e volta ao app para entrar.
    """
    if senha_pendente(user):
        # Conta do painel que ainda não tem senha: o e-mail foi ditado por
        # telefone e ninguém o confirmou. O link de 1 hora entraria na conta só
        # com a caixa de e-mail; vai o de criar senha, que pede os 4 primeiros
        # números do CPF (ver prova_de_posse). O nonce fixo faz os links pedidos
        # por aqui dividirem um teto de tentativas só (senão cada pedido
        # renovaria o teto).
        return enviar_link_criar_senha(user, gerar_link_criar_senha(user, nonce=f"r-{user['_id']}"))
    link = absolute_url("redefinir_senha", token=build_reset_token(user))
    return send_email(
        user["email"],
        f"Redefinição de senha — {APP_NAME}",
        (
            # Conta feita pelo painel só com o e-mail não tem nome: o split()[0] estourava.
            f"Olá, {_primeiro_nome(user.get('full_name', ''))}.\n\n"
            f"Recebemos um pedido para redefinir a senha da sua conta no {APP_NAME}.\n"
            f"Abra o link abaixo para criar uma nova senha:\n\n"
            f"{link}\n\n"
            "O link vale por 1 hora e só pode ser usado uma vez.\n"
            "Se não foi você quem pediu, ignore este e-mail — sua senha continua a mesma."
        ),
    )


def destino_dos_avisos(email_do_motorista: str) -> str:
    """Para onde vai um aviso interno, ou "" quando ele não deve sair.

    Contas da suíte e contas nossas não avisam: cada execução da suíte cria
    dezenas de cadastros e simula compras.
    """
    destino = os.environ.get("EMAIL_AVISOS", EMAIL_AVISOS_PADRAO).strip()
    email = normalize_email(email_do_motorista)
    if not destino or any(email.endswith(sufixo) for sufixo in EMAIL_AUTO_SUFIXOS_EXCLUIDOS):
        return ""
    return destino


def avisar_novo_cadastro(user_id: str, dados: dict, origem: str) -> bool:
    """Avisa a equipe, por e-mail, de cada motorista que acabou de se cadastrar.

    Roda dentro do cadastro, mas nunca o derruba: qualquer falha vira log. O CPF
    fica de fora de propósito — o painel também não mostra o número inteiro.
    """
    destino = destino_dos_avisos(dados.get("email", ""))
    if not destino:
        return False
    email = normalize_email(dados.get("email", ""))
    try:
        n_hoje = get_store().bump_counter(f"{AVISO_CADASTRO_CHAVE}:{today_br()}")
        if n_hoje > EMAIL_AVISO_CADASTRO_DIA:
            app.logger.warning("Aviso de cadastro novo: teto de %s por dia atingido; %s não avisado.",
                               EMAIL_AVISO_CADASTRO_DIA, user_id)
            return False
        nome = (dados.get("full_name") or "").strip() or "(sem nome)"
        cidade = (dados.get("city") or "").strip()
        veiculo = " · ".join(v for v in ((dados.get("plate") or "").strip(),
                                          (dados.get("vehicle_model") or "").strip()) if v)
        corpo = "\n".join([
            f"Novo motorista no {APP_NAME}, cadastrado pelo {origem}.",
            "",
            f"Nome: {nome}",
            f"E-mail: {email}",
            f"WhatsApp: {(dados.get('whatsapp') or '').strip() or '-'}",
            f"Cidade: {cidade or '-'}",
            f"País · moeda: {PAISES.get(dados.get('country') or PAIS_PADRAO, ('-',))[0]}"
            f" · {dados.get('currency') or MOEDA_PADRAO}",
            f"Placa · veículo: {veiculo or '-'}",
            f"Canal: {(dados.get('canal') or '').strip() or '-'}",
            f"Quando: {datetime.now(BR_TZ).strftime('%d/%m/%Y %H:%M')} (horário de Brasília)",
            f"É o cadastro nº {n_hoje} de hoje.",
            "",
            f"Ficha no painel: {absolute_url('admin_motorista', driver_id=user_id)}",
        ])
        assunto = f"Novo cadastro: {nome}" + (f" ({cidade})" if cidade else "")
        # Tempo curto: o motorista está esperando a resposta do cadastro.
        return send_email(destino, assunto, corpo, timeout=4)
    except Exception as exc:
        app.logger.error("Aviso de cadastro novo falhou para %s: %s", user_id, exc)
        return False


def ciclo_do_produto(product_id: str) -> str:
    """Mensal ou anual pelo id do produto na loja.

    No Google Play o anual é um plano base dentro da mesma assinatura: o id
    chega como "br.com.recibotaxi.app.pro.mensal:anual". Por isso vale o que
    vem depois dos dois-pontos.
    """
    parte = (product_id or "").lower().rsplit(":", 1)[-1]
    return "anual" if any(p in parte for p in ("anual", "annual", "year")) else "mensal"


def valor_da_compra(preco, moeda) -> str:
    try:
        n = float(preco)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return filtro_brl(n) if (moeda or "").upper() == "BRL" else f"{n:.2f} {(moeda or '').upper()}".strip()


def avisar_nova_assinatura(motorista: dict, ciclo: str, onde: str, valor: str = "",
                           teste: bool = False, evento_id: str = "") -> bool:
    """Avisa a equipe, por e-mail, de cada assinatura nova do Pro: plano e loja.

    Roda dentro do webhook e nunca o derruba. Webhook reenviado pela loja não
    repete o aviso: o id do evento vira um contador. Compra de sandbox ou da
    Stripe em modo teste sai marcada [TESTE] — ninguém foi cobrado.
    """
    destino = destino_dos_avisos(motorista.get("email", ""))
    if not destino:
        return False
    try:
        if evento_id and get_store().bump_counter(f"{AVISO_ASSINATURA_CHAVE}:{evento_id}") > 1:
            return False
        plano = "Pro anual" if ciclo == "anual" else "Pro mensal"
        nome = (motorista.get("full_name") or "").strip() or "(sem nome)"
        corpo = "\n".join([
            f"{nome} assinou o {plano}, pelo {onde}."
            + (" Compra de TESTE (sandbox ou modo teste): ninguém foi cobrado." if teste else ""),
            "",
            f"Plano: {plano}",
            f"Onde: {onde}",
            f"Valor: {valor or '-'}",
            f"Nome: {nome}",
            f"E-mail: {normalize_email(motorista.get('email', ''))}",
            f"WhatsApp: {(motorista.get('whatsapp') or '').strip() or '-'}",
            f"Cidade: {(motorista.get('city') or '').strip() or '-'}",
            f"Quando: {datetime.now(BR_TZ).strftime('%d/%m/%Y %H:%M')} (horário de Brasília)",
            "",
            f"Ficha no painel: {absolute_url('admin_motorista', driver_id=motorista['_id'])}",
        ])
        assunto = ("[TESTE] " if teste else "") + f"Nova assinatura: {plano} — {nome}"
        return send_email(destino, assunto, corpo, timeout=4)
    except Exception as exc:
        app.logger.error("Aviso de assinatura nova falhou para %s: %s", motorista.get("_id"), exc)
        return False


def avisar_assinatura_duplicada(motorista: dict, anterior: str, nova: str, teste: bool = False) -> bool:
    """Duas assinaturas da Stripe para o mesmo motorista: as duas cobram.

    Acontece se ele pagar duas sessões abertas ao mesmo tempo (o link do painel
    e o site, ou dois links). O banco guarda só a última, e o webhook passa a
    ignorar os eventos da outra; alguém precisa cancelar e devolver uma delas.
    """
    destino = destino_dos_avisos(motorista.get("email", ""))
    if not destino:
        return False
    try:
        nome = (motorista.get("full_name") or "").strip() or "(sem nome)"
        corpo = "\n".join([
            f"{nome} ficou com DUAS assinaturas do Pro na Stripe, e as duas cobram.",
            "",
            f"Anterior: https://dashboard.stripe.com/subscriptions/{anterior}",
            f"Nova (é a que o app e o painel usam): https://dashboard.stripe.com/subscriptions/{nova}",
            "",
            "Cancele a anterior na Stripe e devolva o que ela cobrou a mais.",
            f"Ficha no painel: {absolute_url('admin_motorista', driver_id=motorista['_id'])}",
        ])
        assunto = ("[TESTE] " if teste else "") + f"Atenção: assinatura em dobro — {nome}"
        return send_email(destino, assunto, corpo, timeout=4)
    except Exception as exc:
        app.logger.error("Aviso de assinatura em dobro falhou para %s: %s", motorista.get("_id"), exc)
        return False


def absolute_url(endpoint: str, **values) -> str:
    base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    if base_url:
        return f"{base_url}{url_for(endpoint, **values)}"
    return url_for(endpoint, _external=True, **values)


def public_receipt_url(rid: str) -> str:
    return absolute_url("recibo_view", rid=rid)


def field_limit_error(limits: dict[str, tuple[str, int]]) -> str | None:
    """Primeira mensagem de erro de tamanho, ou None se tudo couber."""
    for field, (label, maximo) in limits.items():
        if len(request.form.get(field, "")) > maximo:
            return f"{label} passa do limite de {maximo} caracteres."
    return None


def client_ip() -> str:
    """IP do cliente, já corrigido pelo ProxyFix.

    Atrás de CDN isto é best-effort: se o proxy não sobrescrever o
    X-Forwarded-For, o valor pode ser forjado. Serve para conter abuso
    casual (cadastro, pedido de nova senha, login do painel); contra um
    atacante determinado o caminho é o WAF da Vercel, que age antes da
    função rodar.
    """
    return (request.remote_addr or "desconhecido").strip()


def get_stripe():
    if stripe_lib is None:
        return None
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return None
    stripe_lib.api_key = key
    return stripe_lib


# ── Store ───────────────────────────────────────────────────────────────────

class PostgresStore:
    """Store em Postgres (Supabase).

    Devolve dicionários no mesmo formato que os outros stores — inclusive o
    `_id` e os campos derivados `amount_display`/`trip_date_display` — para o
    resto do app não precisar saber de onde os dados vieram.
    """

    kind = "postgres"
    label = "Postgres"

    # Colunas do motorista, já com o alias que o app espera.
    _DRIVER_COLS = """
        id as _id, email, password_changed_at, full_name, cpf,
        whatsapp, city, plate, vehicle_model, taxi_prefix, license_number,
        coop_name, coop_cnpj, state,
        plan, stripe_customer_id, stripe_subscription_id, subscription_status,
        country, currency, referral_code, referred_by, pro_until, pro_reminder_sent_at,
        referral_coupon_granted_at, referral_coupon_used_at, created_at, updated_at
    """

    _RECEIPT_COLS = """
        rid, rid as _id, driver_id, is_guest, passenger, passenger_email,
        passenger_whatsapp, passenger_document, passenger_company,
        trip_date, trip_time, trip_end_time, distance_km,
        origin, destination, amount, currency, payment_method, notes, driver_snapshot,
        created_at
    """

    def __init__(self, dsn: str) -> None:
        if ConnectionPool is None:
            raise RuntimeError(
                "A dependência 'psycopg' não está instalada. "
                "Rode 'pip install -r requirements.txt'."
            )
        # prepare_threshold=None: o Supavisor em transaction mode não suporta
        # prepared statements, e o psycopg 3 os cria sozinho depois de 5
        # execuções da mesma query — o erro só apareceria depois, em produção.
        self.pool = ConnectionPool(
            dsn,
            min_size=0,
            max_size=int(os.environ.get("DB_POOL_MAX", "4")),
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
        )
        # Em serverless o pool sobrevive entre invocações (Fluid Compute);
        # no fim do processo, fechar evita threads penduradas.
        atexit.register(self.pool.close)

    # ── conversões ──────────────────────────────────────────────────────────
    @staticmethod
    def _driver_out(row: dict | None) -> dict | None:
        if not row:
            return None
        out = dict(row)
        # O resto do app trata _id como string: ele vai para a sessão, para o
        # token de reset (que é JSON) e para comparações com driver_id.
        out["_id"] = str(out["_id"])
        for campo in ("created_at", "updated_at", "password_changed_at", "pro_until",
                      "pro_reminder_sent_at", "referral_coupon_granted_at", "referral_coupon_used_at"):
            valor = out.get(campo)
            out[campo] = to_utc_iso(valor) if valor else ""
        out["referred_by"] = str(out["referred_by"]) if out.get("referred_by") else ""
        return out

    @staticmethod
    def _receipt_out(row: dict | None) -> dict | None:
        if not row:
            return None
        out = dict(row)
        amount = out.pop("amount", None)
        if amount is not None:
            out["amount_value"] = str(amount)
            out["amount_display"] = formatar_numero(amount, dados_da_moeda(out.get("currency"))[2])
            out["valor_formatado"] = formatar_valor(amount, out.get("currency"))
        km = out.get("distance_km")
        out["distance_km"] = str(km) if km is not None else ""
        out["km_display"] = formatar_km(km)
        out["trip_end_time"] = out.get("trip_end_time") or ""
        trip_date = out.get("trip_date")
        out["trip_date"] = trip_date.isoformat() if trip_date else ""
        out["trip_date_display"] = format_date_br(out["trip_date"])
        out["driver_id"] = str(out["driver_id"]) if out.get("driver_id") else None
        out["created_at"] = to_utc_iso(out["created_at"]) if out.get("created_at") else ""
        return out

    # ── motoristas ──────────────────────────────────────────────────────────
    # Não há create_user: o perfil nasce do trigger on_auth_user_created
    # quando o usuário é criado no Supabase Auth.

    def update_user(self, user_id: str, updates: dict) -> None:
        if not updates:
            return
        # Só colunas conhecidas entram no UPDATE.
        permitidas = {
            "email", "password_changed_at", "full_name", "cpf",
            "whatsapp", "city", "plate", "vehicle_model", "taxi_prefix",
            "license_number", "coop_name", "coop_cnpj", "plan", "stripe_customer_id",
            "stripe_subscription_id", "subscription_status",
            "country", "currency", "referral_code", "referred_by", "pro_until",
            "pro_reminder_sent_at", "referral_coupon_granted_at", "referral_coupon_used_at",
            "signup_channel", "state",
        }
        campos = {k: v for k, v in updates.items() if k in permitidas}
        if not campos:
            return
        sets = ", ".join(f"{k} = %({k})s" for k in campos)
        campos["_uid"] = user_id
        with self.pool.connection() as conn:
            conn.execute(
                f"update public.drivers set {sets}, updated_at = now() where id = %(_uid)s",
                campos,
            )

    def get_user_by_email(self, email: str) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"select {self._DRIVER_COLS} from public.drivers where lower(email) = lower(%s)",
                (email,),
            ).fetchone()
        return self._driver_out(row)

    def get_user_by_referral_code(self, codigo: str) -> dict | None:
        codigo = (codigo or "").strip().upper()
        if not codigo:
            return None
        with self.pool.connection() as conn:
            row = conn.execute(
                f"select {self._DRIVER_COLS} from public.drivers where referral_code = %s",
                (codigo,),
            ).fetchone()
        return self._driver_out(row)

    def get_user_by_id(self, user_id: str | None) -> dict | None:
        if not user_id:
            return None
        with self.pool.connection() as conn:
            try:
                row = conn.execute(
                    f"select {self._DRIVER_COLS} from public.drivers where id = %s",
                    (user_id,),
                ).fetchone()
            except psycopg.errors.InvalidTextRepresentation:
                return None  # id que não é uuid
        return self._driver_out(row)

    def get_user_by_stripe_customer(self, customer_id: str | None) -> dict | None:
        if not customer_id:
            return None
        with self.pool.connection() as conn:
            row = conn.execute(
                f"select {self._DRIVER_COLS} from public.drivers where stripe_customer_id = %s",
                (customer_id,),
            ).fetchone()
        return self._driver_out(row)

    def delete_user(self, user_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("delete from public.drivers where id = %s", (user_id,))

    def delete_receipts_by_driver(self, driver_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("delete from public.receipts where driver_id = %s", (driver_id,))

    # ── recibos ─────────────────────────────────────────────────────────────
    def create_receipt(self, payload: dict, quota_limit: int | None = None) -> dict | None:
        """Cria o recibo. Com quota_limit, consome a cota na MESMA transação.

        Devolve None se a cota do mês já acabou. Cota e insert juntos evitam
        as duas janelas: furar o teto, e queimar cota sem gravar o recibo.
        """
        dados = dict(payload)
        dados.setdefault("is_guest", False)
        dados["driver_id"] = dados.get("driver_id") or None
        dados["amount"] = dados.pop("amount_value", "0")
        dados["currency"] = (dados.get("currency") or MOEDA_PADRAO).upper()
        dados.pop("amount_display", None)
        dados.pop("valor_formatado", None)
        dados.pop("trip_date_display", None)
        dados.pop("_id", None)
        dados["driver_snapshot"] = Json(dados.get("driver_snapshot") or {})
        dados["trip_time"] = dados.get("trip_time") or ""
        # Versoes antigas do app nao mandam chegada nem km (migracao 0013).
        dados["trip_end_time"] = dados.get("trip_end_time") or ""
        dados["distance_km"] = dados.get("distance_km") or None
        dados.pop("km_display", None)
        # Campo novo: as versoes do app ja instaladas no aparelho do motorista
        # nao enviam documento. Sem o default, cada recibo dessas versoes
        # quebraria no insert em vez de gravar sem o campo opcional.
        dados["passenger_document"] = dados.get("passenger_document") or ""
        dados["passenger_company"] = dados.get("passenger_company") or ""

        # Colunas que so entram no insert quando tem valor; sem elas, vale o
        # default da coluna. created_at explícito é usado por testes e
        # importação; via_corrida so vem do caminho da corrida do app
        # (migração 0008), e o recibo de sempre segue pelo insert de antes.
        col_extra = val_extra = ""
        created_at = dados.pop("created_at", None)
        if created_at:
            dados["created_at"] = created_at
            col_extra += ", created_at"
            val_extra += ", %(created_at)s"
        # So o booleano true marca, aqui e nao so na rota da API: quem chamar
        # o store com o campo cru do JSON ("false", "0") nao pode marcar.
        if dados.pop("via_corrida", None) is True:
            col_extra += ", via_corrida"
            val_extra += ", true"

        with self.pool.connection() as conn:
            if quota_limit is not None and dados["driver_id"]:
                liberado = conn.execute(
                    "select public.consume_receipt_quota(%s, %s)",
                    (dados["driver_id"], quota_limit),
                ).fetchone()["consume_receipt_quota"]
                if not liberado:
                    return None

            row = conn.execute(
                f"""insert into public.receipts
                    (rid, driver_id, is_guest, passenger, passenger_email,
                     passenger_whatsapp, passenger_document, passenger_company,
                     trip_date, trip_time, trip_end_time, distance_km,
                     origin, destination, amount, currency, payment_method, notes,
                     driver_snapshot{col_extra})
                    values (%(rid)s, %(driver_id)s, %(is_guest)s, %(passenger)s,
                            %(passenger_email)s, %(passenger_whatsapp)s,
                            %(passenger_document)s, %(passenger_company)s,
                            %(trip_date)s,
                            %(trip_time)s, %(trip_end_time)s, %(distance_km)s,
                            %(origin)s, %(destination)s, %(amount)s,
                            %(currency)s, %(payment_method)s, %(notes)s,
                            %(driver_snapshot)s{val_extra})
                    returning {self._RECEIPT_COLS}""",
                dados,
            ).fetchone()
        return self._receipt_out(row)

    def create_receipt_idempotente(
        self, payload: dict, quota_limit: int | None
    ) -> tuple[dict | None, bool]:
        """Cria o recibo com um rid escolhido pelo CLIENTE.

        Devolve (recibo, criado_agora). Se o rid já existe e é do mesmo
        motorista, devolve o existente com criado_agora=False e **não gasta
        cota** — é retentativa da fila offline, não recibo novo.

        Devolve (None, False) quando a cota do mês acabou.
        """
        rid = payload["rid"]
        existente = self.get_receipt(rid)
        if existente:
            mesmo_dono = str(existente.get("driver_id") or "") == str(
                payload.get("driver_id") or ""
            )
            return (existente, False) if mesmo_dono else (None, False)

        criado = self.create_receipt(payload, quota_limit=quota_limit)
        return (criado, criado is not None)

    def get_receipt(self, rid: str) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"select {self._RECEIPT_COLS} from public.receipts where rid = %s",
                (rid,),
            ).fetchone()
        return self._receipt_out(row)

    def list_receipts_by_driver(self, driver_id: str, limit: int | None = None) -> list[dict]:
        sql = f"""select {self._RECEIPT_COLS} from public.receipts
                  where driver_id = %s order by created_at desc"""
        params: tuple = (driver_id,)
        if limit is not None:
            sql += " limit %s"
            params = (driver_id, limit)
        with self.pool.connection() as conn:
            linhas = conn.execute(sql, params).fetchall()
        return [self._receipt_out(l) for l in linhas]

    def count_receipts_in_range(
        self, driver_id: str, start_iso: str, end_iso: str, cap: int
    ) -> int:
        with self.pool.connection() as conn:
            return conn.execute(
                """select count(*) as n from public.receipts
                   where driver_id = %s and created_at >= %s and created_at < %s""",
                (driver_id, start_iso, end_iso),
            ).fetchone()["n"]

    # ── administradores (/admin) ────────────────────────────────────────────
    _ADMIN_COLS = "id as _id, email, password_hash, created_at, password_changed_at, last_login_at"

    def get_admin_by_email(self, email: str) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"select {self._ADMIN_COLS} from public.admin_users where lower(email) = lower(%s)",
                (email,),
            ).fetchone()
        return dict(row) if row else None

    def get_admin_by_id(self, admin_id: str | None) -> dict | None:
        if not admin_id:
            return None
        with self.pool.connection() as conn:
            row = conn.execute(
                f"select {self._ADMIN_COLS} from public.admin_users where id = %s",
                (admin_id,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_admin(self, email: str, password_hash: str) -> dict:
        """Cria o administrador, ou redefine a senha se o e-mail ja existir."""
        with self.pool.connection() as conn:
            row = conn.execute(
                f"""insert into public.admin_users (email, password_hash)
                    values (lower(%s), %s)
                    on conflict (lower(email)) do update
                      set password_hash = excluded.password_hash,
                          password_changed_at = now()
                    returning {self._ADMIN_COLS}""",
                (email, password_hash),
            ).fetchone()
        return dict(row)

    def set_admin_password(self, admin_id: str, password_hash: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """update public.admin_users
                      set password_hash = %s, password_changed_at = now()
                    where id = %s""",
                (password_hash, admin_id),
            )

    def touch_admin_login(self, admin_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "update public.admin_users set last_login_at = now() where id = %s",
                (admin_id,),
            )

    def delete_admin(self, admin_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("delete from public.admin_users where id = %s", (admin_id,))

    # ── contadores e limpeza ────────────────────────────────────────────────
    def bump_counter(self, key: str) -> int:
        with self.pool.connection() as conn:
            return conn.execute(
                "select public.bump_counter(%s) as n", (key,)
            ).fetchone()["n"]

    def reset_counter(self, key: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("delete from public.rate_limits where key = %s", (key,))

    def get_counter(self, key: str) -> int:
        with self.pool.connection() as conn:
            linha = conn.execute("select count from public.rate_limits where key = %s", (key,)).fetchone()
        return int(linha["count"]) if linha else 0

    def registrar_acesso(self, driver_id: str, ip: str, canal: str) -> None:
        """Data, hora e IP de um acesso (migração 0016). Sem FK: o registro
        sobrevive à exclusão da conta pelos 6 meses que a lei pede."""
        with self.pool.connection() as conn:
            conn.execute(
                "insert into public.access_logs (driver_id, ip, channel) values (%s, %s, %s)",
                (driver_id, (ip or "")[:64], canal[:20]))

    def purge_access_logs_before(self, cutoff_iso: str) -> int:
        with self.pool.connection() as conn:
            return conn.execute("delete from public.access_logs where at < %s", (cutoff_iso,)).rowcount

    def bump_download_click(self, day: str, channel: str, store: str) -> None:
        """Soma um clique no /baixar por dia, canal e loja (migração 0014).

        Só contagem: nem IP, nem agente, nem quem clicou. Com o volume de hoje
        as lojas escondem campanha pequena em "Outros"; este é o número que
        sempre existe.
        """
        with self.pool.connection() as conn:
            conn.execute(
                """insert into public.download_clicks (day, channel, store, clicks)
                   values (%s, %s, %s, 1)
                   on conflict (day, channel, store)
                   do update set clicks = public.download_clicks.clicks + 1""",
                (day, channel, store))

    def download_clicks_since(self, day: str) -> list[dict]:
        with self.pool.connection() as conn:
            return [dict(r) for r in conn.execute(
                """select channel, store, sum(clicks) as n from public.download_clicks
                    where day >= %s group by channel, store order by n desc, channel, store""",
                (day,)).fetchall()]

    def purge_guest_receipts_before(self, cutoff_iso: str, limit: int) -> tuple[int, bool]:
        with self.pool.connection() as conn:
            apagados = conn.execute(
                """delete from public.receipts where rid in (
                     select rid from public.receipts
                      where is_guest and created_at < %s limit %s)
                   returning rid""",
                (cutoff_iso, limit),
            ).fetchall()
            sobrou = conn.execute(
                """select exists(select 1 from public.receipts
                                  where is_guest and created_at < %s) as e""",
                (cutoff_iso,),
            ).fetchone()["e"]
        return len(apagados), bool(sobrou)

    def purge_counters_before(self, cutoff_iso: str, limit: int) -> tuple[int, bool]:
        with self.pool.connection() as conn:
            apagados = conn.execute(
                """delete from public.rate_limits where key in (
                     select key from public.rate_limits
                      where created_at < %s limit %s)
                   returning key""",
                (cutoff_iso, limit),
            ).fetchall()
            sobrou = conn.execute(
                "select exists(select 1 from public.rate_limits where created_at < %s) as e",
                (cutoff_iso,),
            ).fetchone()["e"]
        return len(apagados), bool(sobrou)

    def bump_counter_by(self, key: str, n: int) -> int:
        """Soma n ao contador numa ida só ao banco e devolve o total.

        O bump_counter (função SQL da 0001) só soma 1 e fica como está para os
        outros tetos. O de lançamentos conta itens do lote, e somar um por um
        seriam até 100 idas ao banco por pedido.
        """
        with self.pool.connection() as conn:
            return conn.execute(
                """insert into public.rate_limits as r (key, count) values (%s, %s)
                   on conflict (key) do update set count = r.count + excluded.count
                   returning r.count""",
                (key, n),
            ).fetchone()["count"]

    # ── caixa (migração 0022) ───────────────────────────────────────────────
    def upsert_ledger_entries(self, driver_id: str, itens: list[dict]) -> None:
        """Grava os lançamentos de um motorista num comando só (uma transação).

        `itens` vem de ler_lote_de_lancamentos: já conferido e sem id repetido
        (repetido no mesmo comando, o ON CONFLICT estoura com "cannot affect row
        a second time"). A versão decide: a guardada só é trocada por uma igual
        ou maior, então o reenvio atrasado de uma versão velha não desfaz uma
        edição. O driver_id vem do token, nunca do corpo do pedido.
        """
        with self.pool.connection() as conn:
            conn.execute(
                """insert into public.ledger_entries
                       (driver_id, id, kind, category, amount, currency, entry_date, entry_time,
                        payment_method, source, rid, note, distance_km, liters, version, deleted_at)
                   select %s::uuid, x.id, x.kind, x.category, x.amount, x.currency, x.entry_date,
                          x.entry_time, x.payment_method, x.source, x.rid, x.note, x.distance_km,
                          x.liters, x.version, case when x.apagado then now() end
                     from jsonb_to_recordset(%s::jsonb) as x(
                          id text, kind text, category text, amount numeric, currency text,
                          entry_date date, entry_time text, payment_method text, source text,
                          rid text, note text, distance_km numeric, liters numeric,
                          version integer, apagado boolean)
                   on conflict (driver_id, id) do update set
                          kind = excluded.kind, category = excluded.category,
                          amount = excluded.amount, currency = excluded.currency,
                          entry_date = excluded.entry_date, entry_time = excluded.entry_time,
                          payment_method = excluded.payment_method, source = excluded.source,
                          rid = excluded.rid, note = excluded.note,
                          distance_km = excluded.distance_km, liters = excluded.liters,
                          version = excluded.version,
                          -- Apagar de novo guarda a data da primeira vez: é dela que
                          -- a limpeza conta os 30 dias. Desapagar (versão maior) zera.
                          deleted_at = case when excluded.deleted_at is null then null
                                            else coalesce(ledger_entries.deleted_at, excluded.deleted_at) end,
                          updated_at = now()
                    where ledger_entries.version <= excluded.version""",
                (driver_id, Json(itens)),
            )

    def list_ledger_entries_by_driver(self, driver_id: str) -> list[dict]:
        """Todos os lançamentos do motorista, apagados inclusive: o túmulo também
        é dado. A suíte confere por aqui; o GET da fase 2 devolve isto ao app."""
        with self.pool.connection() as conn:
            return [dict(l) for l in conn.execute(
                """select id, kind, category, amount, currency, entry_date, entry_time,
                          payment_method, source, rid, note, distance_km, liters,
                          version, deleted_at, created_at, updated_at
                     from public.ledger_entries where driver_id = %s
                    order by entry_date, created_at, id""",
                (driver_id,),
            ).fetchall()]

    def purge_deleted_ledger_before(self, cutoff_iso: str, limit: int) -> tuple[int, bool]:
        """Apaga de vez os lançamentos apagados no app antes do corte, em lotes."""
        with self.pool.connection() as conn:
            apagados = conn.execute(
                """delete from public.ledger_entries where (driver_id, id) in (
                     select driver_id, id from public.ledger_entries
                      where deleted_at < %s limit %s)
                   returning id""",
                (cutoff_iso, limit),
            ).fetchall()
            sobrou = conn.execute(
                "select exists(select 1 from public.ledger_entries where deleted_at < %s) as e",
                (cutoff_iso,),
            ).fetchone()["e"]
        return len(apagados), bool(sobrou)


# ── Store singleton ──────────────────────────────────────────────────────────

_STORE = None


def get_store():
    global _STORE
    if _STORE is not None:
        return _STORE

    # SUPABASE_DB_URL vence DATABASE_URL para produção não cair no banco local.
    dsn = (
        os.environ.get("SUPABASE_DB_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )
    if not dsn:
        raise RuntimeError(
            "Nenhum banco configurado. Defina SUPABASE_DB_URL (produção) ou "
            "DATABASE_URL (desenvolvimento) — sem um deles os dados não persistem."
        )

    _STORE = PostgresStore(dsn)
    return _STORE


# ── App ──────────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=None)
# x_for=1: sem isso request.remote_addr seria o IP do proxy da Vercel,
# e qualquer lógica por IP olharia sempre para o mesmo endereço.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

secret_key = os.environ.get("SECRET_KEY", "").strip()
if not secret_key:
    if os.environ.get("VERCEL"):
        raise RuntimeError("Defina a variável SECRET_KEY antes de publicar na Vercel.")
    secret_key = "dev-secret-key"

app.config.update(
    SECRET_KEY=secret_key,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
    # Sem PERMANENT_SESSION_LIFETIME o cookie sai sem Expires e vira cookie de
    # sessão do navegador: some quando o processo fecha. Num WebView isso
    # significa deslogar o motorista toda vez que ele abre o app.
    PERMANENT_SESSION_LIFETIME=timedelta(days=90),
    # Nenhum formulário do app precisa de mais que isto.
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)


# ── Supabase Auth ────────────────────────────────────────────────────────────
#
# A senha vive no Supabase Auth; `drivers` é só o perfil, ligado por chave
# estrangeira. A sessão do Flask continua sendo a fonte da verdade de "quem
# está logado" — ela já é assinada pela SECRET_KEY. Assim uma página comum não
# paga ida à rede: só cadastro, login e troca de senha falam com o Auth.

_ADMIN_CLIENT = None


def supabase_admin():
    """Client com a chave de serviço. Ignora RLS — nunca exponha em template.

    Fica em cache de módulo porque nunca carrega sessão de usuário. Um client
    que faz sign_in muta os próprios headers e vazaria a sessão de um usuário
    para o request de outro; este não faz.
    """
    global _ADMIN_CLIENT
    if _ADMIN_CLIENT is not None:
        return _ADMIN_CLIENT

    if create_client is None:
        raise RuntimeError(
            "A dependência 'supabase' não está instalada. "
            "Rode 'pip install -r requirements.txt'."
        )

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (
        os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    )
    if not url or not key:
        raise RuntimeError(
            "Defina SUPABASE_URL e SUPABASE_SECRET_KEY para a autenticação funcionar."
        )

    # auto_refresh_token cria threading.Timer de renovação; numa função
    # serverless a thread fica pendurada sem servir para nada.
    _ADMIN_CLIENT = create_client(
        url, key, options=ClientOptions(auto_refresh_token=False, persist_session=False)
    )
    return _ADMIN_CLIENT


def supabase_public():
    """Client novo a cada uso, com a chave pública.

    Novo de propósito: sign_in_with_password muta os headers do objeto, então
    um client compartilhado entregaria a sessão de um usuário a outro.
    """
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Defina SUPABASE_URL e SUPABASE_PUBLISHABLE_KEY.")
    return create_client(
        url, key, options=ClientOptions(auto_refresh_token=False, persist_session=False)
    )


class AuthIndisponivel(Exception):
    """O Supabase Auth não respondeu (rede, tempo esgotado, 5xx).

    Não é credencial errada. Tratar como se fosse derrubava o motorista: o app
    recebia 401, tentava renovar, recebia 401 de novo e apagava a sessão (ver a
    auditoria de 2026-11-10). O errorhandler transforma em 503 "indisponivel".
    """


def _auth_recusou_credencial(exc: Exception) -> bool:
    """A exceção do Auth diz que token ou senha é inválido (4xx)? Qualquer
    outra coisa (rede, timeout, 5xx, erro "retryable") é o serviço fora do ar."""
    if AuthRetryableError is not None and isinstance(exc, AuthRetryableError):
        return False
    status = getattr(exc, "status", None)
    try:
        # 429 é o teto do próprio GoTrue, compartilhado por todos os motoristas
        # porque as chamadas saem dos IPs da Vercel: é indisponibilidade, não
        # credencial errada. Só os códigos que o GoTrue usa para token ou senha
        # inválidos contam como recusa.
        return status is not None and int(status) in (400, 401, 403, 404, 422)
    except (TypeError, ValueError):
        return False


@app.errorhandler(AuthIndisponivel)
def _auth_indisponivel(exc):
    """503 em vez de 401: o app mantém a sessão e tenta de novo depois."""
    app.logger.warning("Supabase Auth indisponível: %s", exc.__cause__ or exc)
    if request.path.startswith("/api/"):
        return jsonify({"erro": "indisponivel",
                        "mensagem": "O serviço de login está fora do ar. Tente de novo em instantes."}), 503
    flash("O serviço de login está fora do ar. Nada foi alterado; tente de novo em instantes.", "danger")
    destino = request.referrer if request.referrer and request.referrer.startswith(request.host_url) else url_for("index")
    return redirect(destino, code=303)


def auth_criar_usuario(email: str, password: str, metadata: dict,
                       app_metadata: dict | None = None) -> str:
    """Cria o usuário já confirmado e devolve o id.

    Usa a admin API em vez de sign_up por dois motivos: não dispara e-mail de
    confirmação (o fluxo do app loga direto após o cadastro) e não passa pelo
    limite de 30 cadastros a cada 5 minutos por IP.

    O perfil em `drivers` nasce pelo trigger on_auth_user_created.
    `app_metadata` (só o servidor escreve) marca a conta criada pelo painel.
    """
    atributos = {
        "email": email,
        "password": password,
        "email_confirm": True,
        "user_metadata": metadata,
    }
    if app_metadata:
        atributos["app_metadata"] = app_metadata
    try:
        resposta = supabase_admin().auth.admin.create_user(atributos)
    except Exception as exc:
        texto = str(exc).lower()
        if "already" in texto or "registered" in texto or "exists" in texto:
            raise ValueError("Já existe uma conta com este e-mail.") from exc
        app.logger.error("Falha ao criar usuário no Auth: %s", exc)
        raise RuntimeError("Não foi possível criar a conta agora.") from exc

    if not resposta or not resposta.user:
        raise RuntimeError("Não foi possível criar a conta agora.")
    return str(resposta.user.id)


def auth_conferir_senha(email: str, password: str) -> str | None:
    """Devolve o id do usuário se a senha confere, ou None."""
    if not email or not password:
        return None
    try:
        resposta = supabase_public().auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except Exception as exc:
        if _auth_recusou_credencial(exc):
            return None  # credencial inválida é o caso comum; não faz log
        raise AuthIndisponivel() from exc
    return str(resposta.user.id) if resposta and resposta.user else None


def auth_entrar_com_token(email: str, password: str) -> dict | None:
    """Login para o app nativo: devolve os tokens, não cria sessão de cookie.

    O WebView do Capacitor roda em capacitor://localhost, uma origem diferente
    da API — o cookie de sessão não viajaria. Por isso o app guarda o token.
    """
    try:
        resposta = supabase_public().auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except Exception as exc:
        if _auth_recusou_credencial(exc):
            return None
        raise AuthIndisponivel() from exc
    if not resposta or not resposta.session or not resposta.user:
        return None
    return {
        "user_id": str(resposta.user.id),
        "access_token": resposta.session.access_token,
        "refresh_token": resposta.session.refresh_token,
        "expires_at": resposta.session.expires_at,
    }


def auth_renovar_sessao(refresh_token: str) -> dict | None:
    """Troca o refresh token por um access token novo.

    O access token do Supabase vale uma hora. Sem esta rota o app parava de
    sincronizar em silencio depois desse tempo: o 401 fazia a fila desistir, o
    motorista seguia emitindo recibo que nunca subia, e o passageiro recebia
    link que nunca ia funcionar. So o botao "Sair", manual, destravava.
    """
    if not refresh_token:
        return None
    try:
        resposta = supabase_public().auth.refresh_session(refresh_token)
    except Exception as exc:
        if _auth_recusou_credencial(exc):
            return None
        raise AuthIndisponivel() from exc
    if not resposta or not resposta.session or not resposta.user:
        return None
    return {
        "user_id": str(resposta.user.id),
        "access_token": resposta.session.access_token,
        "refresh_token": resposta.session.refresh_token,
        "expires_at": resposta.session.expires_at,
    }


def auth_usuario_do_token(token: str) -> str | None:
    """Valida o JWT no Supabase e devolve o id do usuário."""
    if not token:
        return None
    try:
        resposta = supabase_admin().auth.get_user(token)
    except Exception as exc:
        if _auth_recusou_credencial(exc):
            return None
        raise AuthIndisponivel() from exc
    return str(resposta.user.id) if resposta and resposta.user else None


def auth_definir_senha(user_id: str, password: str) -> None:
    supabase_admin().auth.admin.update_user_by_id(user_id, {"password": password})


def auth_definir_email(user_id: str, email: str) -> None:
    supabase_admin().auth.admin.update_user_by_id(
        user_id, {"email": email, "email_confirm": True}
    )


def auth_excluir_usuario(user_id: str) -> None:
    """Apaga no Auth. O cascade leva perfil, recibos e cotas junto."""
    supabase_admin().auth.admin.delete_user(user_id)


# ── E-mail transacional ──────────────────────────────────────────────────────

def remetente_padrao() -> str:
    """Endereço do remetente. onboarding@resend.dev é o domínio compartilhado
    do Resend — serve para desenvolvimento, mas só entrega para o dono da conta.
    """
    return os.environ.get("EMAIL_FROM", "").strip() or "onboarding@resend.dev"


def enviar_por_resend(to_address: str, subject: str, body: str, timeout: int = 15) -> bool:
    """Envia pela API HTTP do Resend.

    HTTP em vez do relay SMTP de propósito: numa função serverless, o handshake
    do SMTP (EHLO, STARTTLS, AUTH, MAIL FROM, RCPT TO, DATA) são várias idas e
    voltas; aqui é um POST só.
    """
    ok, _, _, _ = chamar_resend(
        {
            "from": f"{APP_NAME} <{remetente_padrao()}>",
            "to": [to_address],
            "subject": subject,
            "text": body,
        },
        timeout=timeout,
    )
    return ok


def chamar_resend(payload: dict, timeout: int = 15, idempotency_key: str = "") -> tuple[bool, str, str, bool]:
    """POST /emails no Resend. Devolve (aceito, id do Resend, erro, vale_tentar_de_novo).

    "Vale tentar de novo" quando o problema não é deste e-mail: 429, 5xx, falha
    de rede, e também 401/403 — chave ou domínio mal configurados, que voltam a
    funcionar quando alguém corrigir. Os outros 4xx (endereço inválido, campo
    recusado) não melhoram tentando outra vez.
    """
    chave = os.environ.get("RESEND_API_KEY", "").strip()
    if not chave:
        return False, "", "RESEND_API_KEY ausente", False

    destino = ", ".join(payload.get("to") or [])
    cabecalhos = {
        "Authorization": f"Bearer {chave}",
        "Content-Type": "application/json",
        # Sem User-Agent próprio o Cloudflare do Resend devolve 403 com
        # "error code: 1010" — bloqueia o padrão do urllib.
        "User-Agent": f"{APP_NAME.replace(' ', '-')}/1.0",
    }
    if idempotency_key:
        # O Resend ignora o segundo pedido com a mesma chave por 24 h: se a
        # função morrer entre enviar e anotar, a próxima tentativa não duplica.
        cabecalhos["Idempotency-Key"] = idempotency_key

    requisicao = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=cabecalhos,
    )

    try:
        with urllib.request.urlopen(requisicao, timeout=timeout) as resposta:
            corpo = resposta.read() or b"{}"
            try:
                provider_id = str(json.loads(corpo).get("id") or "")
            except ValueError:
                provider_id = ""
            return 200 <= resposta.status < 300, provider_id, "", False
    except urllib.error.HTTPError as exc:
        detalhe = (exc.read() or b"")[:200].decode("utf-8", "replace")
        app.logger.error(
            "Resend recusou o e-mail para %s (HTTP %s): %s", destino, exc.code, detalhe
        )
        return False, "", f"HTTP {exc.code}: {detalhe}", exc.code in (401, 403, 429) or exc.code >= 500
    except Exception as exc:
        app.logger.error("Falha ao chamar o Resend para %s: %s", filtro_mascarar_email(destino), exc)
        return False, "", str(exc)[:200], True


def send_email(to_address: str, subject: str, body: str, timeout: int = 15) -> bool:
    """Envia por Resend; se não houver chave, cai para SMTP; sem nenhum, só loga."""
    if os.environ.get("RESEND_API_KEY", "").strip():
        return enviar_por_resend(to_address, subject, body, timeout=timeout)

    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        app.logger.warning(
            "Nenhum provedor de e-mail configurado — '%s' para %s não foi enviado.",
            subject,
            to_address,
        )
        return False

    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip() or username

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{APP_NAME} <{sender}>"
    message["To"] = to_address
    message.set_content(body)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.starttls()
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        return True
    except Exception as exc:
        app.logger.error("Falha ao enviar e-mail para %s: %s", filtro_mascarar_email(to_address), exc)
        return False


# ── Token de redefinição de senha ────────────────────────────────────────────

def _password_fingerprint(user: dict) -> str:
    """Digital que muda quando a senha muda.

    Antes vinha do hash da senha, que morava aqui. Agora a senha vive no
    Supabase Auth, então a marca é o password_changed_at — atualizado toda vez
    que a senha é trocada. Continua garantindo token de uso único.
    """
    marca = (user.get("password_changed_at") or "") + "|" + str(user.get("_id", ""))
    return hashlib.sha256(marca.encode("utf-8")).hexdigest()[:16]


def build_reset_token(user: dict) -> str:
    serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=RESET_TOKEN_SALT)
    return serializer.dumps({"uid": user["_id"], "fp": _password_fingerprint(user)})


def load_reset_token(token: str) -> dict | None:
    """Devolve o usuário do token, ou None se inválido, expirado ou já usado."""
    serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=RESET_TOKEN_SALT)
    try:
        data = serializer.loads(token, max_age=RESET_TOKEN_MAX_AGE)
    except BadSignature:
        return None

    user = get_store().get_user_by_id(data.get("uid"))
    if not user:
        return None

    # A digital vem do password_changed_at: trocar a senha invalida o token.
    expected = _password_fingerprint(user)
    if not hmac.compare_digest(expected, str(data.get("fp", ""))):
        return None
    return user


def corpo_json() -> dict:
    """O JSON do pedido, ou {} quando nao ha um objeto la.

    `get_json(silent=True) or {}` deixava passar lista, texto e numero, e o
    `.get` seguinte derrubava a rota com 500.
    """
    dados = request.get_json(silent=True)
    return dados if isinstance(dados, dict) else {}


def api_login_required(view):
    """Aceita sessão de cookie (navegador) ou Bearer token (app nativo)."""

    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.get("user"):
            return view(*args, **kwargs)

        cabecalho = request.headers.get("Authorization", "")
        if cabecalho.startswith("Bearer "):
            user_id = auth_usuario_do_token(cabecalho[7:].strip())
            if user_id:
                usuario = get_store().get_user_by_id(user_id)
                if usuario:
                    g.user = usuario
                    return view(*args, **kwargs)

        return jsonify({"erro": "nao_autenticado"}), 401

    return wrapped_view


# Página que pediu o login, para voltar a ela depois de entrar (o link "Meu
# perfil" do app abre o site sem sessão). Vale por meia hora: um destino de
# ontem não sequestra o login de hoje.
DESTINO_DO_LOGIN_VALIDADE = 30 * 60


def destino_depois_do_login() -> str:
    """O caminho guardado por login_required, se ainda vale; senão, vazio."""
    guardado = session.pop("depois_do_login", None) or {}
    caminho = guardado.get("caminho", "") if isinstance(guardado, dict) else ""
    recente = isinstance(guardado, dict) and time.time() - float(guardado.get("em") or 0) < DESTINO_DO_LOGIN_VALIDADE
    # Só caminho interno deste site: nada de "//outro.site" nem barra invertida.
    if recente and caminho.startswith("/") and not caminho.startswith("//") and "\\" not in caminho:
        return caminho
    return ""


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not g.user:
            if request.method == "GET":
                session["depois_do_login"] = {"caminho": request.path, "em": int(time.time())}
            flash("Entre com sua conta para continuar.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


@app.before_request
def assign_csp_nonce() -> None:
    g.csp_nonce = secrets.token_urlsafe(16)
    # Vale para toda resposta: o cookie ganha Expires e sobrevive ao reinício
    # do app. Sem isto, PERMANENT_SESSION_LIFETIME não é aplicado.
    session.permanent = True


@app.after_request
def apply_cors_do_app(response):
    """CORS restrito à /api/*, só para as origens do app nativo."""
    origem = request.headers.get("Origin", "")
    if origem in ORIGENS_APP and request.path.startswith("/api/"):
        response.headers["Access-Control-Allow-Origin"] = origem
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Vary"] = "Origin"
        # Sem Allow-Credentials de propósito: o app usa Bearer, não cookie.
    return response


@app.after_request
def apply_security_headers(response):
    if request.path.startswith("/app/"):
        response.headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY_APP)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    response.headers.setdefault(
        "Content-Security-Policy",
        CSP_TEMPLATE.format(nonce=g.get("csp_nonce", "")),
    )
    return response


@app.before_request
def load_current_user() -> None:
    user_id = session.get("user_id")
    g.user = None
    if not user_id:
        return
    user = get_store().get_user_by_id(user_id)
    if not user:
        session.pop("user_id", None)
        return

    # Se a senha mudou depois que esta sessão nasceu, a sessão morre — é o que
    # torna a redefinição capaz de expulsar um cookie roubado.
    if (user.get("password_changed_at") or "") != (session.get("pw_stamp") or ""):
        session.clear()
        return

    g.user = user


def plano_pedido(valor: str) -> str:
    """O plano pedido na URL ou no formulário, ou "" se não for um plano à venda."""
    valor = (valor or "").strip()
    return valor if valor in STRIPE_PRICE_IDS else ""


@app.context_processor
def inject_plano_pedido() -> dict:
    plano = plano_pedido(request.values.get("plano", "")) if has_request_context() else ""
    return {"plano_pedido": plano, "plano_pedido_rotulo": PLANOS_ROTULO.get(plano, "")}


@app.context_processor
def inject_globals() -> dict:
    store = get_store()
    return {
        "app_name": APP_NAME,
        "current_user": g.get("user"),
        "storage_mode": store.kind,
        "storage_label": store.label,
        "current_year": datetime.now(BR_TZ).year,
        "csp_nonce": g.get("csp_nonce", ""),
        "stripe_configured": bool(os.environ.get("STRIPE_SECRET_KEY")),
        # O site so vende o anual com o Price dele configurado. Sem isso, o card do
        # anual leva ao app, onde a loja vende — e nao a um checkout que responde
        # "plano indisponivel".
        "anual_no_site": bool(os.environ.get("STRIPE_SECRET_KEY", "").strip()
                              and os.environ.get(STRIPE_PRICE_IDS["pro_anual"], "").strip()),
        "stripe_pub_key": os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        "free_monthly_limit": FREE_MONTHLY_LIMIT,
        "app_store_url": APP_STORE_URL,
        "play_store_url": PLAY_STORE_URL,
        "app_store_id": APP_STORE_ID,
        # Botões das lojas no site: o clique conta como canal "site" nos
        # consoles das lojas (o link direto não passa pelo /baixar).
        "app_store_url_site": link_appstore_com_canal("site"),
        "play_store_url_site": link_play_com_canal("site"),
        "site_url": SITE_URL,
        # Canonical sem a query: ?plano=pro e ?indicacao=X são a mesma página.
        "canonical_url": SITE_URL + (request.path if has_request_context() else "/"),
        "og_image_url": SITE_URL + "/static/img/og-recibo-taxi.png",
        "paises": PAISES,
        "ufs": UFS,
        "moedas": MOEDAS,
        "preco_pro_anual_pix": PRECO_PRO_ANUAL_PIX,
        "preco_pro_mensal": PRECO_PRO_MENSAL,
        "preco_pro_anual": PRECO_PRO_ANUAL,
        "indicacao": {"pct": INDICACAO_CUPOM_PCT, "meses_cupom": INDICACAO_CUPOM_MESES,
                      "meses_por_indicado": INDICACAO_MESES_POR_INDICADO, "max_meses": INDICACAO_MAX_MESES},
    }


# ── Static & health ──────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return "OK", 200


@app.get("/favicon.ico")
def favicon():
    return redirect(url_for("public_static", filename="img/RECIBO.png"), code=307)


@app.get("/app/")
@app.get("/app/<path:filename>")
def app_offline(filename: str = "index.html"):
    """Serve o bundle do app nativo, para testar no navegador.

    No Capacitor estes mesmos arquivos vão empacotados no aparelho — este
    caminho existe só para desenvolvimento.
    """
    pasta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mobile", "www")
    resposta = send_from_directory(pasta, filename)
    # Responde em produção também; não é página para aparecer na busca.
    resposta.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resposta


@app.get("/robots.txt")
def robots_txt():
    corpo = "\n".join([
        "User-agent: *",
        "Disallow: /admin",
        "Disallow: /api/",
        "Disallow: /app/",
        # A ida à loja só conta gente; a página /baixar fica aberta.
        "Disallow: /baixar/ir",
        "Disallow: /dashboard",
        "Disallow: /perfil",
        "Disallow: /recibo/",
        "Allow: /",
        f"Sitemap: {SITE_URL}/sitemap.xml",
        "",
    ])
    return Response(corpo, mimetype="text/plain")


# Páginas públicas que valem para a busca. Login, cadastro e senha ficam de
# fora: não são conteúdo. O recibo tem noindex.
SITEMAP_ROTAS = ("index", "planos", "indicar", "termos", "privacidade")


@app.get("/sitemap.xml")
def sitemap_xml():
    urls = "".join(
        f"<url><loc>{SITE_URL}{url_for(rota)}</loc></url>" for rota in SITEMAP_ROTAS)
    corpo = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             f"{urls}</urlset>")
    return Response(corpo, mimetype="application/xml")


@app.get("/static/<path:filename>")
def public_static(filename: str):
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "static")
    return send_from_directory(static_dir, filename)


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    if g.user:
        return redirect(url_for("dashboard"))
    return render_template("index.html")


# Robôs que abrem o link sem ser gente: o AdsBot do Google (que confere o
# anúncio, com agente de iPhone), buscadores e as prévias de link do WhatsApp e
# das redes. Não entram na contagem do /baixar. Lista fechada de propósito:
# "bot" solto pegaria celular de verdade (o Android da marca CUBOT).
ROBO_RE = re.compile(
    r"Googlebot|AdsBot|Mediapartners-Google|Google-InspectionTool|bingbot|Applebot"
    r"|facebookexternalhit|meta-externalagent|^WhatsApp/|TelegramBot|Twitterbot|Slackbot"
    r"|Discordbot|YandexBot|DuckDuckBot|Baiduspider|AhrefsBot|SemrushBot|crawler|spider",
    re.IGNORECASE)


def _canal_e_codigo_do_baixar() -> tuple[str, str]:
    """`?c=CANAL` e `?i=CÓDIGO` do /baixar, já conferidos ("" quando não servem).

    Sem `c`, vale o `utm_source` do link, no mesmo padrão: os anúncios da Meta
    chegam com `utm_source=ig` (ou `fb`, `an`, `msg`) e sem `c`, e até
    2026-11-25 caíam todos em "direto". Fora do padrão, "direto".
    """
    canal = canal_de_cadastro(request.args.get("c")) or canal_de_cadastro(request.args.get("utm_source"))
    # `i` = código de quem indicou (vem da página do convite). Só com canal, e
    # só no formato do código; segue ao Play em utm_content.
    codigo = request.args.get("i", "").strip().upper()
    if not (canal and codigo_de_indicacao_valido(codigo)):
        codigo = ""
    return canal, codigo


def _loja_do_aparelho() -> str:
    """'appstore' no iPhone, 'play' no Android, "" no computador."""
    agente = request.headers.get("User-Agent", "")
    if re.search(r"iPhone|iPad|iPod", agente):
        return "appstore"
    if "Android" in agente:
        return "play"
    return ""


def _agente_e_robo(agente: str) -> bool:
    """Robô conhecido, ou agente que navegador nenhum manda.

    Todo navegador (e todo WebView) começa o User-Agent com "Mozilla/"; curl,
    python-requests, Go, Java, as prévias de link e o agente vazio, não. Até
    2026-11-24 só a lista fechada ficava de fora e o resto caía em
    "computador": o painel chegou a 1.000 cliques de "computador" em 30 dias,
    sem anúncio nenhum para computador.
    """
    return bool(ROBO_RE.search(agente)) or "Mozilla/" not in agente


def _contar_baixar(canal: str, loja: str) -> None:
    # Robô conta à parte, como loja "robo": somado, dá para ver quanto do
    # tráfego é máquina; fora da conta, ele sumia no meio de "computador".
    if _agente_e_robo(request.headers.get("User-Agent", "")):
        loja = "robo"
    try:
        get_store().bump_download_click(today_br(), canal or "direto", loja)
    except Exception as exc:  # contar nunca pode impedir a página nem a ida à loja
        app.logger.warning("/baixar: clique não contado (migração 0014 aplicada?): %s", exc)


@app.get("/baixar")
def baixar_pagina():
    """Página com os botões das lojas: o link impresso no cartão, no cartaz e no anúncio.

    Não redireciona. Até 2026-11-22 este endereço mandava o celular direto à
    loja, e o Google Ads reprovou o anúncio de Pesquisa por "destino
    incompatível": o anúncio mostra recibotaxi.com.br e o celular caía em
    play.google.com. Agora o destino fica no site; a loja só abre no toque.

    O botão da loja do aparelho vem primeiro (no computador, os dois lado a
    lado, mais um QR que leva o celular ao /baixar/ir) e leva ao /baixar/ir
    com o canal e o código, que conta e segue à loja. A visita é somada como
    loja "pagina": o painel mostra quantos abriram a página e quantos foram à
    loja, por canal.
    """
    canal, codigo = _canal_e_codigo_do_baixar()
    _contar_baixar(canal, "pagina")
    extra = {"c": canal or None, "i": codigo or None}
    loja_do_aparelho = _loja_do_aparelho()
    return render_template(
        "baixar.html",
        loja_do_aparelho=loja_do_aparelho,
        link_appstore=url_for("baixar_app", loja="appstore", **extra),
        link_play=url_for("baixar_app", loja="play", **extra),
        link_cadastro=url_for("cadastro", indicacao=codigo or None, c=canal or None),
        # No computador, o QR leva o celular ao /baixar/ir com o mesmo canal e
        # código: a loja abre no aparelho certo e o toque conta como Android ou
        # iPhone, não como "computador".
        qr=qr_svg(absolute_url("baixar_app", **extra)) if not loja_do_aparelho else "",
    )


@app.get("/baixar/ir")
def baixar_app():
    """Manda para a loja do celular de quem clicou, sem página no meio.

    É o destino dos botões "Baixar o app" do próprio site (convite, criar
    senha, pagamento, planos) e dos links dos e-mails e do WhatsApp: quem
    tocou já decidiu baixar, então vai direto. No iPhone cai na App Store, no
    Android no Google Play — e, com o app instalado, a loja mostra "Abrir". No
    computador vai para a seção do app na página inicial. `?loja=play` ou
    `?loja=appstore` (os botões da página /baixar) escolhe a loja na mão.

    Nunca é o destino de anúncio: sai do domínio (ver baixar_pagina).

    `?c=CANAL` diz de onde veio o clique (cartão no ponto de táxi, cooperativa,
    anúncio, e-mail). O canal segue para a loja — no Play dentro do `referrer`,
    que o Play Console mostra como "Origem do UTM"; na App Store em `ct`, que
    o App Store Connect mostra em Analytics → Campaigns — e é somado aqui por
    dia, canal e loja. Sem `c` nem `utm_source`, ou com um valor fora do
    padrão, vai à ficha pura da loja.
    """
    canal, codigo = _canal_e_codigo_do_baixar()
    loja = request.args.get("loja", "")
    if loja not in ("appstore", "play"):
        loja = _loja_do_aparelho()

    if loja == "appstore":
        destino = link_appstore_com_canal(canal) if canal else APP_STORE_URL
    elif loja == "play":
        destino = link_play_com_canal(canal, codigo) if canal else PLAY_STORE_URL
    else:
        loja, destino = "site", url_for("index") + "#app"

    _contar_baixar(canal, loja)
    return redirect(destino)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("planos") if plano_pedido(request.values.get("plano", "")) else url_for("dashboard"))

    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))
        password = request.form.get("senha", "")
        if login_bloqueado(email):
            flash("Muitas tentativas de entrar hoje. Tente de novo amanhã ou use \"Esqueci minha senha\".", "danger")
            return render_template("login.html", form=request.form), 429
        user_id = auth_conferir_senha(email, password)
        user = get_store().get_user_by_id(user_id) if user_id else None
        if not user:
            registrar_falha_de_login(email)
            flash("E-mail ou senha inválidos.", "danger")
            return render_template("login.html", form=request.form), 401
        zerar_falhas_de_login(email)
        session["user_id"] = user["_id"]
        session["pw_stamp"] = user.get("password_changed_at") or ""
        registrar_acesso(user["_id"], "site")
        plano = plano_pedido(request.form.get("plano", ""))
        if plano:
            # Clicou em "Assinar" antes de entrar: segue direto para o pagamento.
            return iniciar_checkout(user, plano)
        flash("Bem-vindo de volta!", "success")
        return redirect(destino_depois_do_login() or url_for("dashboard"))

    return render_template("login.html")


@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    """Cadastro pelo site. Pede nome, e-mail, senha, WhatsApp, UF e cidade
    (desde 2026-11-25; a cidade vem da lista do IBGE, com a UF); CPF e placa
    vêm no /completar-cadastro, antes do primeiro recibo. Se o formulário
    mandar esses campos mesmo assim (um parceiro, um teste), eles valem e são
    conferidos como antes: CPF pelo dígito, no Brasil.

    `?c=CANAL` é o canal de divulgação que o /baixar repassa; fica gravado no
    cadastro (migração 0018) para o painel contar cadastros por canal.
    """
    if g.user:
        return redirect(url_for("planos") if plano_pedido(request.values.get("plano", "")) else url_for("dashboard"))
    canal = canal_de_cadastro(request.args.get("c") or request.form.get("canal"))

    if request.method == "POST":
        def recusar(mensagem: str, status: int = 400):
            flash(mensagem, "danger")
            return render_template("register.html", form=request.form, canal=canal), status

        erro_tamanho = field_limit_error(SIGNUP_FIELD_LIMITS)
        if erro_tamanho:
            return recusar(erro_tamanho)

        full_name = request.form.get("nome_completo", "").strip()
        email = normalize_email(request.form.get("email", ""))
        password = request.form.get("senha", "")
        whatsapp = request.form.get("whatsapp", "").strip()
        uf = request.form.get("uf", "").strip().upper()
        city = request.form.get("cidade", "").strip()
        # Fora do formulário curto; valem quando vêm.
        cpf = request.form.get("cpf", "").strip()
        plate = request.form.get("placa", "").strip().upper()
        vehicle_model = request.form.get("modelo_veiculo", "").strip()
        taxi_prefix = request.form.get("prefixo_taxi", "").strip()
        license_number = request.form.get("numero_alvara", "").strip()

        if not all([full_name, email, password, whatsapp, city]):
            return recusar("Preencha nome, e-mail, senha, WhatsApp e cidade.")
        if not email_valido(email):
            return recusar("Confira o e-mail: ele não parece válido.")
        if len(password) < 8:
            return recusar("A senha precisa ter pelo menos 8 caracteres.")
        try:
            pais, moeda = pais_e_moeda(request.form.get("pais"), request.form.get("moeda"))
        except ValueError as exc:
            return recusar(str(exc))
        # Com DDD, como no painel e no /criar-senha: é o número da lista do
        # vendedor e do link do WhatsApp.
        if not phone_e164(whatsapp, pais):
            return recusar("Confira o WhatsApp: com DDD (fora do Brasil, com + e o código do país).")
        if pais == PAIS_PADRAO:
            # Cidade da lista oficial, com a UF: "sao paulo" vira "São Paulo".
            uf, city = cidade_do_brasil(uf, city)
            if not city:
                return recusar("Escolha a UF e a cidade na lista.")
        else:
            uf = ""
        if cpf and pais == PAIS_PADRAO and not cpf_valido(cpf):
            return recusar("Confira o CPF: os dígitos não conferem.")
        # Cooperativa (opcional, pedido de um taxista): conferida aqui, gravada
        # depois de a conta nascer — o trigger do Auth não conhece os campos.
        try:
            coop_name, coop_cnpj = cooperativa_informada(
                request.form.get("cooperativa", ""), request.form.get("cnpj_cooperativa", ""), pais)
        except CooperativaInvalida as exc:
            return recusar(str(exc))
        if passou_do_teto("cadastro:ip", client_ip(), CADASTRO_DAILY_LIMIT_IP):
            return recusar("Muitos cadastros a partir desta rede hoje. Tente de novo amanhã ou fale com o suporte.", 429)
        # Código de quem indicou: opcional, mas, se veio, tem de existir —
        # engolir um código errado deixaria a indicação sem dono.
        codigo_indicacao = request.form.get("indicacao", "").strip().upper()
        indicador = get_store().get_user_by_referral_code(codigo_indicacao) if codigo_indicacao else None
        if codigo_indicacao and not indicador:
            return recusar("Código de indicação não encontrado. Confira com quem te indicou ou deixe em branco.")

        # O usuário nasce no Supabase Auth; o trigger on_auth_user_created
        # cria o perfil em `drivers` a partir deste metadata.
        try:
            user_id = auth_criar_usuario(
                email,
                password,
                {
                    "full_name": full_name,
                    "cpf": cpf,
                    "whatsapp": whatsapp,
                    "city": city,
                    "plate": plate,
                    "vehicle_model": vehicle_model,
                    "taxi_prefix": taxi_prefix,
                    "license_number": license_number,
                    "country": pais,
                    "currency": moeda,
                },
            )
        except ValueError as exc:
            return recusar(str(exc), 409)
        except RuntimeError as exc:
            return recusar(str(exc), 503)

        guardar_cooperativa(user_id, coop_name, coop_cnpj)
        guardar_extras_do_cadastro(user_id, canal=canal, uf=uf)
        registrar_indicacao(indicador, user_id)
        avisar_novo_cadastro(user_id, {
            "email": email, "full_name": full_name, "whatsapp": whatsapp, "city": city,
            "plate": plate, "vehicle_model": vehicle_model, "country": pais, "currency": moeda,
            "canal": canal,
        }, "site")
        sincronizar_crm_agora(user_id)
        session["user_id"] = user_id
        session["pw_stamp"] = ""
        registrar_acesso(user_id, "site_cadastro")
        plano = plano_pedido(request.form.get("plano", ""))
        novo = get_store().get_user_by_id(user_id) if plano else None
        if novo:
            # Clicou em "Assinar" antes de ter conta: segue direto para o
            # pagamento, sem precisar achar o botão de novo. Os dados do
            # recibo ficam para o painel, que avisa.
            flash("Conta criada!", "success")
            return iniciar_checkout(novo, plano)
        if not all([cpf, plate]):
            flash("Conta criada! Agora complete os dados que saem no recibo.", "success")
            return redirect(url_for("completar_cadastro"))
        flash("Conta criada! Já pode emitir e salvar seus recibos.", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html", canal=canal)


@app.route("/completar-cadastro", methods=["GET", "POST"])
@login_required
def completar_cadastro():
    """Nome, CPF, cidade e placa de quem fez o cadastro curto do site (ou de
    quem o painel criou só com o e-mail): são os dados que saem em cada recibo.
    Mesmos campos e regras do /criar-senha. Conta completa volta ao painel."""
    pede = o_que_o_primeiro_acesso_pede(g.user)
    if not pede["faltam"]:
        return redirect(url_for("dashboard"))
    contexto = {"pede": pede, "faltam": rotulos_que_faltam(g.user),
                "documento": rotulo_do_documento(g.user), "pais": g.user.get("country") or PAIS_PADRAO}
    if request.method == "POST":
        erro, mudancas = dados_do_primeiro_acesso(g.user, pede)
        if erro:
            flash(erro, "danger")
            return render_template("completar_cadastro.html", preenchido=request.form, **contexto), 400
        try:
            get_store().update_user(g.user["_id"], mudancas)
        except Exception as exc:
            app.logger.error("completar cadastro: não salvou os dados de %s: %s", g.user["_id"], exc)
            flash("Não conseguimos salvar agora. Tente de novo em alguns instantes.", "danger")
            return render_template("completar_cadastro.html", preenchido=request.form, **contexto), 503
        app.logger.info("motorista %s completou o cadastro pelo site", g.user["_id"])
        flash("Cadastro completo! Já pode emitir e salvar seus recibos.", "success")
        return redirect(url_for("dashboard"))
    return render_template("completar_cadastro.html", preenchido={}, **contexto)


@app.get("/sair")
def sair():
    session.clear()
    flash("Sessão encerrada com sucesso.", "info")
    return redirect(url_for("index"))


@app.route("/recuperar-senha", methods=["GET", "POST"])
def recuperar_senha():
    # Quem está logado também pode pedir o link: é a saída que a mensagem do
    # teto de senha e o "Use o link de redefinição" do /perfil indicam. Antes
    # o site o mandava ao painel, e a saída não existia.

    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))
        if email and not pode_pedir_redefinicao(email):
            flash("Muitos pedidos para este e-mail hoje. Tente novamente mais tarde.", "danger")
            return render_template("recuperar_senha.html"), 429

        user = get_store().get_user_by_email(email) if email else None
        if user:
            enviar_link_de_redefinicao(user)

        # Resposta idêntica exista ou não a conta, para não revelar cadastros.
        flash(
            "Se existir uma conta com este e-mail, enviamos um link de redefinição. "
            "O link vale por 1 hora.",
            "info",
        )
        return redirect(url_for("login") if not g.user else url_for("dashboard"))

    return render_template("recuperar_senha.html", email_logado=(g.user or {}).get("email", ""))


@app.route("/redefinir-senha/<token>", methods=["GET", "POST"])
def redefinir_senha(token: str):
    user = load_reset_token(token)
    # Conta do painel ainda sem senha só ganha senha pelo /criar-senha, que
    # pede o WhatsApp: pedir de novo manda esse link (enviar_link_de_redefinicao).
    if user and senha_pendente(user):
        user = None
    if not user:
        flash("Link inválido ou expirado. Peça um novo link de redefinição.", "danger")
        return redirect(url_for("recuperar_senha"))

    if request.method == "POST":
        password = request.form.get("senha", "")
        confirmation = request.form.get("confirmar_senha", "")

        if len(password) < 8:
            flash("A senha precisa ter pelo menos 8 caracteres.", "danger")
            return render_template("redefinir_senha.html", token=token), 400

        if password != confirmation:
            flash("As senhas não conferem.", "danger")
            return render_template("redefinir_senha.html", token=token), 400

        # A senha vai para o Auth; o carimbo fica no perfil e é o que derruba
        # as outras sessões e invalida este mesmo token.
        auth_definir_senha(user["_id"], password)
        get_store().update_user(user["_id"], {"password_changed_at": now_iso()})
        zerar_falhas_de_login(user["email"])  # a saída prometida pelo 429
        try:
            get_store().reset_counter(chave_de_reconfirmacao(user["_id"]))
        except Exception as exc:
            app.logger.warning("contador de senha indisponível: %s", exc)
        session.clear()
        flash("Senha redefinida! Entre com a nova senha.", "success")
        return redirect(url_for("login"))

    return render_template("redefinir_senha.html", token=token)


@app.route("/perfil", methods=["GET", "POST"])
@login_required
def perfil():
    """Correção de dados cadastrais — o que a LGPD chama de direito de retificação."""
    if request.method == "POST":
        erro_tamanho = field_limit_error(SIGNUP_FIELD_LIMITS)
        if erro_tamanho:
            flash(erro_tamanho, "danger")
            return render_template("perfil.html", form=request.form), 400

        # Alterar dados da conta pede a senha atual: sem isso, uma sessão
        # sequestrada trocaria o e-mail e tomaria a conta em silêncio.
        senha = conferir_senha_atual(g.user, request.form.get("senha_atual", ""))
        if senha == "bloqueada":
            flash(MSG_MUITAS_TENTATIVAS, "danger")
            return render_template("perfil.html", form=request.form), 429
        if senha == "errada":
            flash("Senha incorreta. Nenhuma alteração foi salva.", "danger")
            return render_template("perfil.html", form=request.form), 401

        full_name = request.form.get("nome_completo", "").strip()
        email = normalize_email(request.form.get("email", ""))
        whatsapp = request.form.get("whatsapp", "").strip()
        cpf = request.form.get("cpf", "").strip()
        city = request.form.get("cidade", "").strip()
        plate = request.form.get("placa", "").strip().upper()

        if not all([full_name, email, whatsapp, cpf, city, plate]):
            flash("Preencha todos os campos obrigatórios.", "danger")
            return render_template("perfil.html", form=request.form), 400

        # Campo ausente mantém o que está salvo: vazio não pode virar Brasil e
        # trocar a moeda de quem é de fora sem ele pedir.
        try:
            pais, moeda = pais_e_moeda(request.form.get("pais") or g.user.get("country"),
                                       request.form.get("moeda") or g.user.get("currency"))
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template("perfil.html", form=request.form), 400

        # Mesma regra do cadastro; conta antiga com CPF errado só é cobrada quando mexer nele.
        if pais == PAIS_PADRAO and cpf != (g.user.get("cpf") or "") and not cpf_valido(cpf):
            flash("Confira o CPF: os dígitos não conferem.", "danger")
            return render_template("perfil.html", form=request.form), 400

        # Cooperativa: os dois juntos ou nenhum; CNPJ conferido e pontuado.
        try:
            coop_name, coop_cnpj = cooperativa_informada(
                request.form.get("cooperativa", ""), request.form.get("cnpj_cooperativa", ""), pais)
        except CooperativaInvalida as exc:
            flash(str(exc), "danger")
            return render_template("perfil.html", form=request.form), 400

        if email != g.user["email"]:
            if not email_valido(email):
                flash("Confira o e-mail: ele não parece válido.", "danger")
                return render_template("perfil.html", form=request.form), 400
            existente = get_store().get_user_by_email(email)
            if existente and existente["_id"] != g.user["_id"]:
                flash("Já existe uma conta com este e-mail.", "danger")
                return render_template("perfil.html", form=request.form), 409
            # O e-mail é o login: tem de mudar no Auth também, senão o
            # usuário salvaria o perfil e não conseguiria mais entrar.
            try:
                auth_definir_email(g.user["_id"], email)
            except Exception as exc:
                app.logger.error("Falha ao trocar o e-mail no Auth: %s", exc)
                flash("Não conseguimos alterar o e-mail agora. Tente mais tarde.", "danger")
                return render_template("perfil.html", form=request.form), 503

        get_store().update_user(
            g.user["_id"],
            {
                "full_name": full_name,
                "email": email,
                "whatsapp": whatsapp,
                "cpf": cpf,
                "city": city,
                "plate": plate,
                "vehicle_model": request.form.get("modelo_veiculo", "").strip(),
                "taxi_prefix": request.form.get("prefixo_taxi", "").strip(),
                "license_number": request.form.get("numero_alvara", "").strip(),
                "coop_name": coop_name,
                "coop_cnpj": coop_cnpj,
                "country": pais,
                "currency": moeda,
                "updated_at": now_iso(),
            },
        )
        flash("Cadastro atualizado.", "success")
        return redirect(url_for("dashboard"))

    return render_template("perfil.html", form=g.user)


@app.post("/excluir-conta")
@login_required
def excluir_conta():
    if request.form.get("confirmacao", "").strip().upper() != "EXCLUIR":
        flash('Digite EXCLUIR para confirmar a remoção da conta.', "danger")
        return redirect(url_for("dashboard"))

    senha = conferir_senha_atual(g.user, request.form.get("senha", ""))
    if senha == "bloqueada":
        flash(MSG_MUITAS_TENTATIVAS, "danger")
        return redirect(url_for("dashboard"))
    if senha == "errada":
        flash("Senha incorreta. A conta não foi excluída.", "danger")
        return redirect(url_for("dashboard"))

    # Quem assinou pela loja cancela na loja: a Apple e a Google não deixam o
    # servidor cancelar por fora, e apagar aqui não para a cobrança de lá.
    # Mesma trava do app e do painel.
    if assina_pela_loja(g.user):
        flash("Sua assinatura é pela App Store ou pelo Google Play. Cancele lá primeiro "
              "e depois exclua a conta; apagar agora não pararia a cobrança da loja.", "danger")
        return redirect(url_for("dashboard"))

    # Apagar a conta sem conseguir parar a cobrança deixaria uma assinatura
    # órfã, cobrando alguém que já não tem como cancelá-la pelo app.
    subscription_id = g.user.get("stripe_subscription_id")
    if subscription_id:
        stripe = get_stripe()
        cancelada = False
        if stripe:
            try:
                stripe.Subscription.cancel(subscription_id)
                cancelada = True
            except Exception as exc:
                app.logger.error(
                    "Não foi possível cancelar a assinatura %s: %s", subscription_id, exc
                )

        if not cancelada:
            flash(
                "Não conseguimos cancelar sua assinatura agora, então a conta "
                "não foi excluída — apagá-la deixaria a cobrança ativa sem você "
                "poder pará-la. Tente de novo em alguns minutos ou fale com o "
                "suporte.",
                "danger",
            )
            return redirect(url_for("dashboard"))

    # Apagar no Auth basta: o cascade leva perfil, recibos e cotas.
    auth_excluir_usuario(g.user["_id"])
    sincronizar_crm_agora(g.user["_id"])  # o trigger já anotou a exclusão; o CRM anonimiza na hora
    session.clear()

    flash(
        "Sua conta e todos os seus recibos foram excluídos definitivamente.",
        "info",
    )
    return redirect(url_for("index"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

def historico_limitado_ao_mes(usuario: dict) -> bool:
    """Grátis vê só o mês atual no painel a partir de HISTORICO_GRATIS_DESDE."""
    return usuario.get("plan", "free") not in PAID_PLANS and today_br() >= HISTORICO_GRATIS_DESDE


@app.get("/dashboard")
@login_required
def dashboard():
    receipts = []
    for receipt in get_store().list_receipts_by_driver(g.user["_id"]):
        share_url = public_receipt_url(receipt["rid"])
        item = dict(receipt)
        item["public_url"] = share_url
        item["whatsapp_link"] = build_whatsapp_link(item, share_url)
        item["email_link"] = build_email_link(item, share_url)
        receipts.append(item)

    month_start, month_end = month_range_utc()
    receipts_this_month = sum(
        1 for r in receipts if month_start <= r.get("created_at", "") < month_end
    )
    # Histórico de meses anteriores é do Pro. O Grátis vê o mês atual (para
    # reenviar o recibo que o passageiro perdeu); os outros continuam guardados
    # e os links públicos continuam abrindo.
    recibos_anteriores = len(receipts) - receipts_this_month
    total_recibos = len(receipts)
    if historico_limitado_ao_mes(g.user):
        receipts = [r for r in receipts if month_start <= r.get("created_at", "") < month_end]

    if request.args.get("subscribed") == "1":
        # O plano só muda quando o webhook chega, alguns segundos depois — não
        # dá para anunciar qual é sem arriscar dizer "Plano Free ativado".
        if g.user.get("plan") in PAID_PLANS:
            flash(
                f"✅ Plano {g.user['plan'].capitalize()} ativado com sucesso! "
                "Seja bem-vindo.",
                "success",
            )
        else:
            flash(
                "✅ Pagamento confirmado! Seu plano é ativado em alguns "
                "segundos — atualize a página se ainda aparecer como Grátis.",
                "success",
            )

    user_plan = g.user.get("plan", "free")
    _, simbolo, casas, _ = dados_da_moeda(g.user.get("currency"))
    return render_template(
        "dashboard.html",
        faltam_no_cadastro=rotulos_que_faltam(g.user),
        receipts=receipts,
        receipts_this_month=receipts_this_month,
        recibos_anteriores=recibos_anteriores,
        total_recibos=total_recibos,
        historico_limitado=historico_limitado_ao_mes(g.user),
        historico_gratis_desde=HISTORICO_GRATIS_DESDE,
        today=today_br(),
        user_plan=user_plan,
        monthly_limit=None if user_plan in PAID_PLANS else FREE_MONTHLY_LIMIT,
        moeda_simbolo=simbolo,
        valor_exemplo=formatar_numero(0, casas),
        pro_until=_instante(g.user.get("pro_until")) if g.user.get("subscription_status") in STATUS_PREPAGO else None,
        prepago=g.user.get("subscription_status") in STATUS_PREPAGO,
        assina_na_loja=assina_pela_loja(g.user),
    )


@app.post("/recibo")
@login_required
def recibo_criar():
    # Cadastro curto do site: nome, CPF, cidade e placa vêm antes do primeiro
    # recibo, ou o recibo sairia sem quem o emitiu.
    faltam = rotulos_que_faltam(g.user)
    if faltam:
        flash("Falta completar seu cadastro (" + ", ".join(faltam) + "): esses dados saem no recibo.", "warning")
        return redirect(url_for("completar_cadastro"))
    erro_tamanho = field_limit_error(RECEIPT_FIELD_LIMITS)
    if erro_tamanho:
        flash(erro_tamanho, "danger")
        return redirect(url_for("dashboard"))

    passenger = request.form.get("passageiro", "").strip()
    trip_date = request.form.get("data", "").strip()
    origin = request.form.get("origem", "").strip()
    destination = request.form.get("destino", "").strip()
    payment_method = request.form.get("forma_pagamento", "").strip() or "Pix"

    # O nome do passageiro e opcional: quem recebe pelo QR muitas vezes nem o
    # diz. Data, origem e destino continuam obrigatorios.
    if not all([trip_date, origin, destination]):
        flash("Preencha os dados principais da corrida.", "danger")
        return redirect(url_for("dashboard"))

    moeda = g.user.get("currency") or MOEDA_PADRAO
    try:
        amount_value, amount_display = normalize_money(request.form.get("valor", ""), moeda)
        distance_km = normalize_km(request.form.get("km", ""))
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("dashboard"))

    rid = uuid4().hex[:RID_LENGTH].upper()
    receipt = {
        "_id": rid,
        "rid": rid,
        "driver_id": g.user["_id"],
        "passenger": passenger,
        "passenger_email": normalize_email(request.form.get("email_passageiro", "")),
        "passenger_whatsapp": request.form.get("whatsapp_passageiro", "").strip(),
        "passenger_document": format_document_br(
            request.form.get("documento_passageiro", ""), g.user.get("country") or PAIS_PADRAO),
        "passenger_company": razao_social_de(
            request.form.get("documento_passageiro", ""),
            request.form.get("razao_social", "")),
        "trip_date": trip_date,
        "trip_date_display": format_date_br(trip_date),
        "trip_time": hora_valida(request.form.get("hora", "")),
        "trip_end_time": hora_valida(request.form.get("hora_fim", "")),
        "distance_km": distance_km,
        "origin": origin,
        "destination": destination,
        "amount_value": amount_value,
        "amount_display": amount_display,
        "currency": moeda,
        "payment_method": payment_method,
        "notes": request.form.get("observacoes", "").strip(),
        "created_at": now_iso(),
        "driver_snapshot": snapshot_do_motorista(g.user),
    }

    # Cota e gravação na mesma operação: sem a janela entre contar e inserir,
    # duas emissões simultâneas não furam mais o teto do plano Grátis.
    quota = None if g.user.get("plan", "free") in PAID_PLANS else FREE_MONTHLY_LIMIT
    salvo = get_store().create_receipt(receipt, quota_limit=quota)
    if salvo is None:
        flash(
            f"Você atingiu o limite de {FREE_MONTHLY_LIMIT} recibos deste mês do "
            "plano Grátis. Faça upgrade para emitir recibos ilimitados.",
            "warning",
        )
        return redirect(url_for("planos"))

    ativar_indicacao(g.user)
    flash("Recibo gerado e salvo na sua conta!", "success")
    return redirect(url_for("recibo_view", rid=rid, created="1"))


# ── Gerador público (desligado) ───────────────────────────────────────────────

@app.get("/gerar")
@login_required
def gerador():
    """Emitir recibo no site exige conta desde 2026-09-22.

    O gerador sem cadastro foi desligado; a rota fica só para os links antigos
    (favoritos, WhatsApp, resultado do Google) não caírem em 404. Sem sessão o
    login_required manda para o login; com sessão, o painel já tem o formulário.
    """
    return redirect(url_for("dashboard"))


# ── Pricing & Stripe ──────────────────────────────────────────────────────────

@app.get("/planos")
def planos():
    return render_template("planos.html")


# ── Indicação e Pro pré-pago ─────────────────────────────────────────────────
#
# Duas formas de Pro que não são assinatura: o anual pago uma vez (Pix ou
# cartão, fora das lojas) e o mês ganho por indicar um colega. As duas vivem
# em drivers.pro_until (migração 0012); o cron diário rebaixa quando a data
# passa. Quem tem assinatura (Stripe ou loja) não depende de pro_until: se
# também tiver dias pré-pagos, eles valem quando a assinatura acabar.

def _instante(valor) -> datetime | None:
    """ISO (como o store devolve) ou datetime → datetime com fuso; vazio → None."""
    if not valor:
        return None
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None


def assina_pela_loja(usuario: dict) -> bool:
    """Pro vindo da App Store ou do Google Play: pago, sem id da Stripe e sem
    ser pré-pago (indicação, anual por Pix, cortesia). Só esse o servidor não
    consegue cancelar, então só esse trava a exclusão da conta."""
    return (usuario.get("plan") in PAID_PLANS and not usuario.get("stripe_subscription_id")
            and usuario.get("subscription_status") not in STATUS_PREPAGO)


def status_prepago_ao_encerrar(usuario: dict) -> str:
    """Status que fica quando a assinatura acaba e sobram dias pré-pagos:
    preserva indicação e cortesia; só o que foi pago é 'prepago' (e recebe o
    lembrete de renovação)."""
    atual = usuario.get("subscription_status") or ""
    return atual if atual in STATUS_PREPAGO else "prepago"


def pro_prepago_vigente(usuario: dict) -> bool:
    fim = _instante(usuario.get("pro_until"))
    return bool(fim and fim > datetime.now(timezone.utc))


def conceder_pro_prepago(usuario: dict, dias: int, status: str) -> datetime:
    """Soma `dias` ao Pro pré-pago e devolve o fim novo.

    Quem ainda tem dias não perde nenhum: a soma parte do fim atual. Assinante
    (Stripe ou loja) continua assinante e os dias ficam guardados; quem não é
    vira Pro pré-pago. Pagar ("prepago") vale mais que ganhar ("indicacao"):
    um Pro de indicação que compra o anual passa a prepago, e o contrário não.
    """
    agora = datetime.now(timezone.utc)
    base = _instante(usuario.get("pro_until")) or agora
    fim = max(base, agora) + timedelta(days=dias)
    updates: dict = {"pro_until": to_utc_iso(fim), "pro_reminder_sent_at": None}
    atual = usuario.get("subscription_status") or ""
    if usuario.get("plan") == "free" or atual in STATUS_PREPAGO:
        updates["plan"] = "pro"
        updates["subscription_status"] = "prepago" if "prepago" in (status, atual) else status
    get_store().update_user(usuario["_id"], updates)
    usuario.update(updates)
    return fim


def expirar_pro_prepago() -> int:
    """Rebaixa quem tinha Pro pré-pago e passou da data. Roda no cron diário."""
    with get_store().pool.connection() as conn:
        return conn.execute(
            """update public.drivers
                  set plan = 'free', subscription_status = 'expirado', updated_at = now()
                where plan = 'pro' and subscription_status = any(%s)
                  and pro_until < now()""", (list(STATUS_PREPAGO),)).rowcount


def lembrar_vencimento_prepago() -> int:
    """Avisa por e-mail quem pagou o anual e vence em ~15 dias. Uma vez só.

    Só o anual pago: o mês de indicação é presente, e um aviso de "vence" para
    presente soa como cobrança.
    """
    store = get_store()
    with store.pool.connection() as conn:
        linhas = conn.execute(
            """select id, email, full_name, pro_until from public.drivers
                where plan = 'pro' and subscription_status = 'prepago'
                  and pro_reminder_sent_at is null
                  and pro_until between now() + interval '14 days' and now() + interval '16 days'
                limit 50""").fetchall()
    enviados = 0
    for d in linhas:
        if any(d["email"].endswith(s) for s in EMAIL_AUTO_SUFIXOS_EXCLUIDOS):
            continue
        fim = d["pro_until"].astimezone(BR_TZ).strftime("%d/%m/%Y")
        corpo = "\n".join([
            f"Olá, {(d['full_name'] or '').split()[0] if d['full_name'] else ''}.".replace("Olá, .", "Olá."),
            "",
            f"Seu {APP_NAME} Pro anual vale até {fim}. Depois dessa data a conta volta ao plano "
            f"Grátis, com {FREE_MONTHLY_LIMIT} recibos por mês.",
            "",
            f"Para renovar por mais um ano ({PRECO_PRO_ANUAL_PIX}, Pix ou cartão), abra:",
            absolute_url("oferta_anual"),
            "",
            "Os dias que ainda faltam não se perdem: o ano novo começa a contar no fim do atual.",
        ])
        if send_email(d["email"], f"Seu {APP_NAME} Pro vence em {fim}", corpo, timeout=8):
            store.update_user(str(d["id"]), {"pro_reminder_sent_at": now_iso()})
            enviados += 1
    return enviados


def codigo_de_indicacao(usuario: dict) -> str:
    """Código do motorista para indicar colegas; nasce na primeira vez que é pedido."""
    codigo = (usuario.get("referral_code") or "").strip()
    if codigo:
        return codigo
    store = get_store()
    for _ in range(10):
        tentativa = "".join(secrets.choice(INDICACAO_ALFABETO) for _ in range(INDICACAO_TAMANHO))
        if store.get_user_by_referral_code(tentativa):
            continue
        try:
            store.update_user(usuario["_id"], {"referral_code": tentativa})
        except psycopg.errors.UniqueViolation:
            continue  # outro motorista tirou o mesmo código no mesmo instante
        usuario["referral_code"] = tentativa
        return tentativa
    raise RuntimeError("não foi possível gerar um código de indicação")


def codigo_de_indicacao_valido(codigo: str) -> bool:
    return len(codigo) == INDICACAO_TAMANHO and all(ch in INDICACAO_ALFABETO for ch in codigo)


def link_de_indicacao(codigo: str) -> str:
    """O convite abre a página /i/CÓDIGO, que leva à loja do aparelho, e não ao
    formulário do site: o que alimenta a posição nas lojas é instalação. O app
    pega este link na sessão, então os apps já instalados passam a mandar o
    link novo sem atualização."""
    return absolute_url("convite", codigo=codigo)


def mensagem_de_indicacao(usuario: dict, codigo: str) -> str:
    """Texto pronto para o WhatsApp: o botão "Indicar" abre o wa.me com ele.

    Diz só o que é verdade: quem indica ganha o mês de Pro; o colega não ganha
    nada além do app (a versão antiga dizia "a gente ganha"). Sem "10 segundos",
    que ninguém mediu.
    """
    nome = (usuario.get("full_name") or "").split()[0] if usuario.get("full_name") else "um colega"
    return (
        f"Oi! Uso o {APP_NAME} para dar recibo de corrida pelo celular: sai na hora, funciona "
        f"sem internet e vai pelo WhatsApp do passageiro. É grátis para começar. Baixa por aqui "
        f"e, ao criar a conta, coloca o meu código *{codigo}* (assim eu ganho um mês de Pro): "
        f"{link_de_indicacao(codigo)} — {nome}"
    )


@app.get("/i/<codigo>")
def convite(codigo: str):
    """Página do convite: quem recebeu o link de um colega cai aqui.

    Mostra quem convidou, o código em letras grandes e o botão da loja do
    aparelho (/baixar/ir?c=indicacao&i=CÓDIGO, que conta o clique e leva o código
    no referrer do Play). Quem prefere o computador cria a conta pelo site com
    o código já preenchido. Código que não existe dá 404: link adivinhado não
    revela nada.
    """
    codigo = codigo.strip().upper()
    if not codigo_de_indicacao_valido(codigo):
        abort(404)
    # Código existente devolve o primeiro nome de quem indicou; tentar códigos
    # em massa não pode virar lista de motoristas. O teto por IP é largo para
    # o uso real (um link por conversa) e apertado para um script.
    try:
        tentativas = get_store().bump_counter(f"convite:{request.remote_addr}:{today_br()}")
    except Exception:
        tentativas = 0
    if tentativas > CONVITE_DIARIO_LIMITE_IP:
        abort(429)
    indicador = get_store().get_user_by_referral_code(codigo)
    if not indicador:
        abort(404)
    partes = (indicador.get("full_name") or "").split()
    nome = partes[0] if partes else "Um colega"
    return render_template(
        "convite.html", codigo=codigo, nome=nome,
        link_baixar=url_for("baixar_app", c="indicacao", i=codigo),
        link_cadastro=url_for("cadastro", indicacao=codigo),
    )


def registrar_indicacao(indicador: dict | None, novo_id: str) -> None:
    """Liga o cadastro novo a quem indicou. Chamado logo depois de criar a conta."""
    if not indicador or str(indicador["_id"]) == str(novo_id):
        return
    store = get_store()
    store.update_user(novo_id, {"referred_by": indicador["_id"]})
    with store.pool.connection() as conn:
        conn.execute(
            """insert into public.referrals (referrer_id, referred_id) values (%s, %s)
               on conflict (referred_id) do nothing""", (indicador["_id"], novo_id))


def ativar_indicacao(usuario: dict) -> None:
    """Primeiro recibo de quem foi indicado: libera a recompensa de quem indicou.

    O UPDATE com `activated_at is null` é o que garante uma ativação só, mesmo
    com dois recibos ao mesmo tempo. Nunca derruba a emissão: falha vira log.
    """
    if not usuario.get("referred_by"):
        return
    try:
        store = get_store()
        with store.pool.connection() as conn:
            linha = conn.execute(
                """update public.referrals set activated_at = now()
                    where referred_id = %s and activated_at is null returning referrer_id""",
                (usuario["_id"],)).fetchone()
        if linha:
            recompensar_indicador(str(linha["referrer_id"]), usuario)
    except Exception as exc:
        app.logger.error("Indicação de %s não ativada: %s", usuario.get("_id"), exc)


def recompensar_indicador(indicador_id: str, indicado: dict) -> None:
    store = get_store()
    indicador = store.get_user_by_id(indicador_id)
    if not indicador:
        return
    with store.pool.connection() as conn:
        ativas = conn.execute(
            "select count(*) as n from public.referrals where referrer_id = %s and activated_at is not null",
            (indicador_id,)).fetchone()["n"]
    recompensas = []
    fim = None
    if ativas > INDICACAO_MAX_MESES:
        recompensas.append("teto_de_meses")
    elif indicador.get("plan") == "free" or indicador.get("subscription_status") in STATUS_PREPAGO:
        fim = conceder_pro_prepago(indicador, 30 * INDICACAO_MESES_POR_INDICADO, "indicacao")
        recompensas.append("mes_pro")
    else:
        recompensas.append("mes_pro_nao_aplicado_assinante")
    with store.pool.connection() as conn:
        conn.execute("update public.referrals set reward = %s where referred_id = %s",
                     (",".join(recompensas), indicado["_id"]))
    avisar_indicador(indicador, fim)


def avisar_indicador(indicador: dict, fim: datetime | None) -> bool:
    email = normalize_email(indicador.get("email", ""))
    if any(email.endswith(s) for s in EMAIL_AUTO_SUFIXOS_EXCLUIDOS):
        return False
    nome = (indicador.get("full_name") or "").split()[0] if indicador.get("full_name") else ""
    linhas = [f"Olá, {nome}." if nome else "Olá.", "",
              "Um colega que você indicou começou a usar o Recibo Táxi. Obrigado!", ""]
    if fim:
        linhas.append(f"Você ganhou {INDICACAO_MESES_POR_INDICADO} mês de Pro: recibos ilimitados até "
                      f"{fim.astimezone(BR_TZ).strftime('%d/%m/%Y')}.")
    linhas += ["", f"Indique mais colegas em {absolute_url('indicar')}."]
    try:
        return send_email(email, "Seu colega começou a usar o Recibo Táxi", "\n".join(linhas), timeout=8)
    except Exception as exc:
        app.logger.error("Aviso de indicação para %s falhou: %s", indicador.get("_id"), exc)
        return False


def tem_cupom_de_indicacao(usuario: dict) -> bool:
    return bool(usuario.get("referral_coupon_granted_at")) and not usuario.get("referral_coupon_used_at")


def cupom_de_indicacao(stripe) -> str | None:
    """Id do cupom na Stripe. Cria na primeira vez; None se a Stripe falhar."""
    try:
        stripe.Coupon.retrieve(INDICACAO_CUPOM_ID)
        return INDICACAO_CUPOM_ID
    except Exception:
        pass
    try:
        stripe.Coupon.create(
            id=INDICACAO_CUPOM_ID, percent_off=INDICACAO_CUPOM_PCT, duration="repeating",
            duration_in_months=INDICACAO_CUPOM_MESES,
            name=f"Indicação: {INDICACAO_CUPOM_PCT}% por {INDICACAO_CUPOM_MESES} meses")
        return INDICACAO_CUPOM_ID
    except Exception as exc:
        app.logger.error("Cupom de indicação indisponível na Stripe: %s", exc)
        return None


def resumo_de_indicacoes(usuario: dict) -> dict:
    """O que a página /indicar mostra: colegas indicados e o que já rendeu."""
    with get_store().pool.connection() as conn:
        linhas = conn.execute(
            """select d.full_name, r.created_at, r.activated_at, r.reward
                 from public.referrals r join public.drivers d on d.id = r.referred_id
                where r.referrer_id = %s order by r.created_at desc limit 100""",
            (usuario["_id"],)).fetchall()
    colegas = []
    for l in linhas:
        partes = (l["full_name"] or "").split()
        colegas.append({
            "nome": (partes[0] + (f" {partes[-1][0]}." if len(partes) > 1 else "")) if partes else "(sem nome)",
            "quando": l["created_at"], "ativo": bool(l["activated_at"]),
            "meses": "mes_pro" in (l["reward"] or ""),
        })
    return {
        "colegas": colegas,
        "ativos": sum(1 for c in colegas if c["ativo"]),
        "meses_ganhos": sum(1 for c in colegas if c["meses"]),
        "cupom": "usado" if usuario.get("referral_coupon_used_at")
                 else "liberado" if usuario.get("referral_coupon_granted_at") else "",
    }


@app.get("/indicar")
def indicar():
    """Código, link e regras do programa de indicação. Sem login mostra só as regras."""
    if not g.user:
        return render_template("indicar.html", resumo=None, codigo="", link="", whatsapp="")
    codigo = codigo_de_indicacao(g.user)
    return render_template(
        "indicar.html", resumo=resumo_de_indicacoes(g.user), codigo=codigo,
        link=link_de_indicacao(codigo),
        whatsapp="https://wa.me/?text=" + quote(mensagem_de_indicacao(g.user, codigo)),
        pro_until=_instante(g.user.get("pro_until")),
    )


@app.get("/oferta/anual")
def oferta_anual():
    """Pro anual pago uma vez, por Pix ou cartão. Não aparece no menu nem no app:
    é o link que vai pelo WhatsApp e pelo e-mail de renovação."""
    return render_template("oferta_anual.html",
                           disponivel=bool(os.environ.get(STRIPE_PRICE_IDS["pro_anual_pix"], "").strip()),
                           pro_until=_instante((g.user or {}).get("pro_until")))


@app.post("/assinar/<plan>")
@login_required
def assinar(plan: str):
    return iniciar_checkout(g.user, plan)


def iniciar_checkout(usuario: dict, plan: str):
    """Abre o checkout da Stripe para `usuario` e devolve o redirect.

    Chamado pelo botão "Assinar" de quem está logado e logo depois do cadastro
    ou do login de quem clicou em assinar antes de ter conta.
    """
    stripe = get_stripe()
    if not stripe:
        flash("Pagamentos ainda não configurados. Entre em contato conosco.", "warning")
        return redirect(url_for("planos"))

    price_env = STRIPE_PRICE_IDS.get(plan)
    if not price_env:
        abort(400)

    price_id = os.environ.get(price_env, "").strip()
    if not price_id:
        flash("Este plano não está disponível no momento.", "warning")
        return redirect(url_for("planos"))

    # Quem já é Pro não abre um segundo checkout: seria uma assinatura
    # paralela, cobrando duas vezes. Vale também para quem assinou pela loja —
    # antes só a Stripe era checada, e um Pro do app conseguia pagar de novo
    # aqui.
    # Pro pré-pago (anual por Pix ou mês de indicação) pode comprar de novo:
    # outro ano soma ao fim do atual, e uma assinatura começa a valer já, com
    # os dias pré-pagos guardados para quando ela acabar (é assim que quem
    # ganhou o mês de indicação usa o cupom antes de o mês acabar). Assinante
    # (Stripe ou loja) não — pagaria duas vezes pela mesma coisa.
    prepago = usuario.get("subscription_status") in STATUS_PREPAGO
    if usuario.get("plan") in PAID_PLANS and not prepago:
        # Exceção: mensal → anual na Stripe é troca de Price na assinatura que
        # já existe. O Stripe zera o ciclo, credita os dias não usados do mês e
        # cobra o ano na hora (always_invoice) — sem segunda assinatura.
        if plan == "pro_anual" and usuario.get("stripe_subscription_id"):
            return _migrar_para_anual(stripe, price_id, usuario)
        if usuario.get("stripe_customer_id"):
            flash(
                "Você já tem uma assinatura ativa. Use 'Gerenciar plano' para "
                "trocar de plano ou cancelar.",
                "info",
            )
        else:
            flash(
                "Você já é Pro pelo app (App Store ou Google Play). Para trocar "
                "de plano ou cancelar, use a loja do seu celular.",
                "info",
            )
        return redirect(url_for("dashboard"))

    # Volta para o mesmo domínio em que a pessoa está (www.recibotaxi.com.br
    # ou recibo-taxi.vercel.app): o cookie da sessão é de um domínio só, e
    # voltar para o outro deixava quem acabou de pagar deslogado.
    success_url = url_for("dashboard", subscribed=1, _external=True)
    cancel_url = url_for("oferta_anual" if plan in PLANOS_PREPAGOS else "planos", _external=True)

    try:
        checkout = criar_sessao_de_checkout(
            stripe, usuario, plan, price_id, success_url=success_url, cancel_url=cancel_url,
            # O domínio entra na chave: a mesma chave com outra success_url
            # seria recusada pela Stripe por "parâmetros diferentes".
            idempotency_key=f"assinar:{usuario['_id']}:{plan}:{request.host}",
        )
    except CupomIndisponivel:
        flash("Não conseguimos aplicar o seu desconto de indicação agora. "
              "Tente de novo em alguns instantes ou fale com o suporte.", "danger")
        return redirect(url_for("planos"))
    except Exception as exc:
        # A mensagem crua da Stripe pode conter identificadores internos.
        app.logger.error("Falha ao criar checkout para %s: %s", usuario["_id"], exc)
        flash(
            "Não conseguimos iniciar o pagamento agora. Tente de novo em "
            "alguns instantes ou fale com o suporte.",
            "danger",
        )
        return redirect(url_for("planos"))

    return redirect(checkout.url, code=303)


class CupomIndisponivel(Exception):
    """O cupom de indicação não pôde ser lido nem criado na Stripe."""


# Chaves do metadata que o webhook lê para decidir o que liberar. Quem chama
# criar_sessao_de_checkout com metadata_extra não consegue sobrescrevê-las.
METADATA_DO_WEBHOOK = ("user_id", "plan", "ciclo", "modo", "cupom")


def criar_sessao_de_checkout(stripe, usuario: dict, plan: str, price_id: str, *, success_url: str,
                             cancel_url: str, idempotency_key: str, metadata_extra: dict | None = None):
    """Cria o Customer (se faltar) e a sessão do Checkout. Devolve a sessão.

    Serve ao botão "Assinar" do site (iniciar_checkout) e ao link de pagamento
    do painel (/pagar). Levanta CupomIndisponivel quando o desconto de
    indicação devia entrar e a Stripe não o entrega; qualquer outra falha da
    Stripe sobe como veio, para quem chamou decidir a mensagem.
    """
    extra = {k: str(v) for k, v in (metadata_extra or {}).items() if k not in METADATA_DO_WEBHOOK}
    metadata = {**extra, "user_id": usuario["_id"], "plan": plano_base(plan),
                "ciclo": PLAN_CYCLES.get(plan, "mensal")}
    extras: dict = {}
    if plan in PLANOS_PREPAGOS:
        # Pagamento único: o webhook soma PRO_PREPAGO_DIAS ao pro_until.
        metadata["modo"] = "prepago"
        extras["mode"] = "payment"
    else:
        extras["mode"] = "subscription"
        # Cupom de indicação: só no mensal, e só uma vez. A Stripe aplica os
        # 12 meses sozinha; o webhook marca o cupom como usado.
        if plan == "pro" and tem_cupom_de_indicacao(usuario):
            cupom = cupom_de_indicacao(stripe)
            if not cupom:
                raise CupomIndisponivel()
            extras["discounts"] = [{"coupon": cupom}]
            metadata["cupom"] = "indicacao"
        if extra:
            # Renovação e cancelamento chegam como eventos da assinatura, que só
            # carregam o metadata dela: é assim que a venda continua rastreável.
            extras["subscription_data"] = {"metadata": dict(extra)}

    # Sem o parâmetro `customer`, o Checkout cria um Customer NOVO a cada
    # sessão — customer_email só preenche o campo. Dois checkouts virariam
    # dois clientes, com duas assinaturas cobrando em paralelo.
    customer_id = usuario.get("stripe_customer_id")
    if not customer_id:
        customer = stripe.Customer.create(
            email=usuario["email"],
            name=usuario.get("full_name") or None,
            metadata={"user_id": usuario["_id"]},
            # Dois toques em "Pagar" antes de o id ser gravado criavam dois
            # clientes; a sessão do segundo, com a mesma chave e outro cliente,
            # era recusada pela Stripe, e o link ficava com erro por 24 h.
            idempotency_key=f"cliente:{usuario['_id']}",
        )
        customer_id = customer.id
        get_store().update_user(usuario["_id"], {"stripe_customer_id": customer_id})

    # Sem payment_method_types: as versões novas da API recusam o parâmetro.
    # As formas de pagamento vêm de Configurações → Formas de pagamento no
    # painel da Stripe; no modo assinatura ele só oferece as compatíveis.
    return stripe.checkout.Session.create(
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=metadata,
        locale="pt-BR",
        idempotency_key=idempotency_key,
        **extras,
    )


def _migrar_para_anual(stripe, price_id: str, usuario: dict):
    """Troca o Price da assinatura ativa pelo anual. Só é chamado por iniciar_checkout."""
    try:
        sub = stripe.Subscription.retrieve(usuario["stripe_subscription_id"])
        item = sub["items"]["data"][0]
        if item["price"]["id"] == price_id:
            flash("Você já está no plano anual.", "info")
            return redirect(url_for("dashboard"))
        stripe.Subscription.modify(
            sub["id"],
            items=[{"id": item["id"], "price": price_id}],
            proration_behavior="always_invoice",
            cancel_at_period_end=not ANUAL_RENOVA_AUTOMATICAMENTE,
            metadata={"plan": "pro", "ciclo": "anual"},
            idempotency_key=f"anual:{usuario['_id']}:{sub['id']}",
        )
    except Exception as exc:
        app.logger.error("Falha ao migrar %s para o anual: %s", usuario["_id"], exc)
        flash(
            "Não conseguimos trocar para o plano anual agora. Tente de novo "
            "em alguns instantes ou fale com o suporte.",
            "danger",
        )
        return redirect(url_for("planos"))
    flash(
        "Pronto! Sua assinatura passou para o plano anual: R$ 59,90 por ano, "
        "com o que sobrou do mês já descontado.",
        "success",
    )
    return redirect(url_for("dashboard"))


@app.post("/portal-cliente")
@login_required
def portal_cliente():
    stripe = get_stripe()
    if not stripe:
        abort(503)

    customer_id = g.user.get("stripe_customer_id")
    if not customer_id:
        flash("Você não possui uma assinatura ativa para gerenciar.", "warning")
        return redirect(url_for("dashboard"))

    # Mesmo domínio em que a pessoa está, pelo mesmo motivo do checkout.
    return_url = url_for("dashboard", _external=True)
    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id, return_url=return_url
        )
    except Exception as exc:
        # Sem isto, um erro da Stripe deixa quem paga sem caminho para cancelar.
        app.logger.error("Falha ao abrir o portal de %s: %s", g.user["_id"], exc)
        flash(
            "Não conseguimos abrir o portal de assinatura agora. Tente de novo "
            "em alguns instantes ou fale com o suporte.",
            "danger",
        )
        return redirect(url_for("dashboard"))

    return redirect(portal.url, code=303)


def liberar_pro_prepago(obj: dict, evento: dict) -> tuple[str, str | None]:
    """Checkout de pagamento único pago: soma um ano ao Pro do motorista.

    Devolve (resultado, driver_id) para o registro de eventos. A sessão da
    Stripe chega por dois eventos (completed e async_payment_succeeded) e a
    Stripe reenvia o que não teve 200: o id da sessão vira um contador, e só
    a primeira passagem soma os dias.
    """
    meta = obj.get("metadata") or {}
    user_id = str(meta.get("user_id") or "")
    if not user_id:
        return "ignorado: sem_user_id", None
    if (obj.get("payment_status") or "paid") != "paid":
        return "aguardando_pagamento", None
    store = get_store()
    motorista = store.get_user_by_id(user_id)
    if not motorista:
        return "ignorado: motorista_desconhecido", None
    if store.bump_counter(f"prepago:{obj.get('id') or evento.get('id')}") > 1:
        return "repetido", motorista["_id"]
    fim = conceder_pro_prepago(motorista, PRO_PREPAGO_DIAS, "prepago")
    if obj.get("customer") and not motorista.get("stripe_customer_id"):
        store.update_user(motorista["_id"], {"stripe_customer_id": obj["customer"]})
    marcar_link_pago(meta)
    avisar_nova_assinatura(
        motorista, "anual", onde_foi_vendido(meta, "site (Pix ou cartão, pagamento único)"),
        valor_da_compra((obj.get("amount_total") or 0) / 100, obj.get("currency")),
        teste=not evento.get("livemode", False), evento_id=str(evento.get("id") or ""))
    return f"pro (pré-pago até {fim.astimezone(BR_TZ).strftime('%d/%m/%Y')})", motorista["_id"]


def onde_foi_vendido(meta: dict, padrao: str) -> str:
    """Rótulo do aviso de venda: o link do painel aparece separado do site."""
    if meta.get("origem") == "link_painel":
        return "link de pagamento do painel" + (" (Pix ou cartão, pagamento único)"
                                               if meta.get("modo") == "prepago" else " (Stripe)")
    return padrao


def marcar_link_pago(meta: dict) -> None:
    """Pagamento que veio de um /pagar: o link para de oferecer o botão.

    O contador vive em rate_limits, que o cron limpa 7 dias depois de criado, e
    o link pode durar até o fim do 7º dia (mais a folga do /pronto). Por isso
    link_ja_pago também procura o pagamento no registro de cobranças, que fica.
    """
    nonce = str(meta.get("link") or "")
    if meta.get("origem") != "link_painel" or not nonce:
        return
    try:
        get_store().bump_counter(f"link_pago:{nonce}")
    except Exception as exc:
        app.logger.warning("não marquei o link de pagamento como pago: %s", exc)


def link_ja_pago(nonce: str) -> bool:
    """O pagamento deste /pagar já foi confirmado pelo webhook?

    Primeiro o contador (rápido); se ele já foi limpo, o registro de cobranças,
    onde o webhook guarda o metadata da sessão e o que liberou ("pro...").
    """
    if not nonce:
        return False
    try:
        if get_store().get_counter(f"link_pago:{nonce}") > 0:
            return True
    except Exception as exc:
        app.logger.warning("contador do link de pagamento indisponível: %s", exc)
    try:
        with get_store().pool.connection() as conn:
            linha = conn.execute(
                """select exists(select 1 from public.billing_events
                                   where source = 'stripe' and driver_id is not null
                                     and payload -> 'object' -> 'metadata' ->> 'link' = %s
                                     and outcome like 'pro%%') as pago""", (nonce,)).fetchone()
        return bool(linha and linha["pago"])
    except Exception as exc:
        app.logger.warning("não consegui procurar o link %s nas cobranças: %s", nonce[:6], exc)
        return False


def assinatura_de_outra_compra(usuario: dict | None, obj: dict) -> bool:
    """O evento é de uma assinatura da Stripe que não é a gravada no motorista.

    Só decide quando os dois ids existem: evento sem id (como os da suíte) ou
    motorista sem assinatura gravada seguem o caminho de sempre.
    """
    if not usuario:
        return False
    gravada = usuario.get("stripe_subscription_id") or ""
    do_evento = str(obj.get("id") or "")
    return bool(gravada and do_evento.startswith("sub_") and do_evento != gravada)


@app.post("/webhook/stripe")
def stripe_webhook():
    stripe = get_stripe()
    if not stripe:
        abort(503)

    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

    # construct_event só para validar a assinatura: o retorno é um StripeObject,
    # que não tem .get() e mudou de formato entre versões do SDK. O corpo já
    # chegou como JSON — tratá-lo como dict puro é estável e suficiente.
    try:
        stripe.Webhook.construct_event(payload, sig, secret)
        evento = json.loads(payload)
    except Exception:
        abort(400)

    obj = evento.get("data", {}).get("object") or {}
    tipo = evento.get("type", "")
    store = get_store()
    # O que foi feito e com quem, para o registro de eventos no fim.
    resultado, motorista_id = "sem_efeito", None

    meta_sessao = obj.get("metadata") or {}
    sessao_prepaga = meta_sessao.get("modo") == "prepago" or obj.get("mode") == "payment"

    if tipo in ("checkout.session.completed", "checkout.session.async_payment_succeeded") and sessao_prepaga:
        # Pix confirma em segundos, mas pode chegar como async_payment_succeeded
        # depois de um completed ainda "unpaid"; os dois caem aqui.
        resultado, motorista_id = liberar_pro_prepago(obj, evento)

    elif tipo == "checkout.session.completed":
        user_id = meta_sessao.get("user_id")
        # plano_base por segurança: um checkout antigo/manual com "pro_anual"
        # no metadata não pode gravar um plano que os gates não conhecem.
        plan = plano_base(meta_sessao.get("plan", "pro")) or "pro"
        resultado = "ignorado: sem_user_id"
        if user_id:
            # A assinatura que estava gravada antes desta: se for outra, o
            # motorista pagou duas sessões e as duas assinaturas cobram.
            anterior = (store.get_user_by_id(user_id) or {}).get("stripe_subscription_id") or ""
            store.update_user(user_id, {
                "plan": plan,
                "stripe_customer_id": obj.get("customer"),
                "stripe_subscription_id": obj.get("subscription"),
                "subscription_status": "active",
                # O cupom de indicação vale uma assinatura; a Stripe cuida dos 12 meses.
                **({"referral_coupon_used_at": now_iso()} if meta_sessao.get("cupom") == "indicacao" else {}),
            })
            motorista = store.get_user_by_id(user_id)
            resultado = "ignorado: motorista_desconhecido"
            if motorista:
                motorista_id, resultado = motorista["_id"], plan
                if meta_sessao.get("cupom") == "indicacao":
                    resultado += " (cupom de indicação)"
                if anterior and obj.get("subscription") and anterior != obj.get("subscription"):
                    resultado += f" — EM DOBRO com {anterior[:40]}"
                    app.logger.error("assinatura em dobro para %s: %s e %s", user_id, anterior, obj.get("subscription"))
                    avisar_assinatura_duplicada(motorista, anterior, str(obj.get("subscription")),
                                                teste=not evento.get("livemode", False))
                marcar_link_pago(meta_sessao)
                avisar_nova_assinatura(
                    motorista, "anual" if meta_sessao.get("ciclo") == "anual" else "mensal",
                    onde_foi_vendido(meta_sessao, "site (Stripe)"),
                    valor_da_compra((obj.get("amount_total") or 0) / 100, obj.get("currency")),
                    # Sem livemode = evento de teste (inclusive o da suíte).
                    teste=not evento.get("livemode", False), evento_id=str(evento.get("id") or ""))
        # "Pagamento único" literal: o anual não renova. O Stripe encerra no
        # fim dos 12 meses e o subscription.deleted abaixo derruba pra free.
        if (not ANUAL_RENOVA_AUTOMATICAMENTE
                and meta_sessao.get("ciclo") == "anual"
                and obj.get("subscription")):
            try:
                stripe.Subscription.modify(obj["subscription"], cancel_at_period_end=True)
            except Exception as exc:
                app.logger.error("cancel_at_period_end falhou para %s: %s", user_id, exc)

    elif tipo in ("customer.subscription.deleted", "customer.subscription.updated") \
            and (dono := store.get_user_by_stripe_customer(obj.get("customer"))) \
            and assinatura_de_outra_compra(dono, obj):
        # O cliente tem mais de uma assinatura (pagou pelo site e pelo link, por
        # exemplo) e o evento é da que não está gravada: encerrar aquela não
        # pode derrubar a que vale.
        motorista_id = dono["_id"]
        resultado = f"ignorado: outra_assinatura ({str(obj.get('id'))[:40]})"
        app.logger.warning("webhook Stripe: %s de %s ignorado; a assinatura gravada é outra",
                           tipo, obj.get("id"))

    elif tipo == "customer.subscription.deleted":
        user = store.get_user_by_stripe_customer(obj.get("customer"))
        resultado = "ignorado: cliente_desconhecido"
        if user:
            motorista_id = user["_id"]
            if pro_prepago_vigente(user):
                # Tinha dias pré-pagos guardados: a assinatura acabou, o Pro não.
                store.update_user(user["_id"], {"stripe_subscription_id": None,
                                                "subscription_status": status_prepago_ao_encerrar(user)})
                resultado = "mantido_prepago"
            else:
                store.update_user(user["_id"], {
                    "plan": "free",
                    "stripe_subscription_id": None,
                    "subscription_status": "canceled",
                })
                resultado = "free"

    elif tipo == "customer.subscription.updated":
        user = store.get_user_by_stripe_customer(obj.get("customer"))
        resultado = "ignorado: cliente_desconhecido"
        if user:
            status = obj.get("status") or "inactive"
            motorista_id = user["_id"]
            if status in ACTIVE_SUBSCRIPTION_STATUSES:
                store.update_user(user["_id"], {"subscription_status": status})
                resultado = f"status: {status}"
            elif pro_prepago_vigente(user):
                store.update_user(user["_id"], {"subscription_status": status_prepago_ao_encerrar(user)})
                resultado = f"mantido_prepago ({status})"
            else:
                # Assinatura encerrada: o id sai junto, para não parecer viva
                # depois (pro_vem_da_stripe olha só a presença do id).
                store.update_user(user["_id"], {
                    "plan": "free",
                    "subscription_status": status,
                    "stripe_subscription_id": None,
                })
                resultado = f"free ({status})"

    meta = obj.get("metadata") or {}
    registrar_evento_cobranca(
        "stripe", tipo, resultado, event_id=str(evento.get("id") or ""), driver_id=motorista_id,
        app_user_id=str(obj.get("customer") or ""), store="STRIPE",
        environment="PRODUCTION" if evento.get("livemode") else "TEST",
        product_id=str(meta.get("plan") or ""), cycle=str(meta.get("ciclo") or ""),
        price=(obj.get("amount_total") or 0) / 100 if obj.get("amount_total") else None,
        currency=str(obj.get("currency") or ""),
        # So o que o painel usa: o objeto inteiro da Stripe passa de 5 KB.
        payload={"id": evento.get("id"), "type": tipo, "livemode": evento.get("livemode"),
                 "object": {k: obj.get(k) for k in (
                     "id", "customer", "subscription", "status", "metadata", "amount_total",
                     "currency", "cancel_at_period_end", "current_period_end") if k in obj}})
    return jsonify({"ok": True})


# ── Venda pelo painel: link de pagamento e criar senha ───────────────────────

def _serializador(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=salt)


def planos_do_link_disponiveis() -> dict[str, str]:
    """Planos que o painel pode vender agora: Stripe configurada e Price na Vercel.

    Oferecer um plano sem Price deixaria o motorista num "não está disponível"
    depois de ter aceitado pagar.
    """
    if not get_stripe():
        return {}
    return {plano: rotulo for plano, rotulo in PLANOS_DO_LINK.items()
            if os.environ.get(STRIPE_PRICE_IDS[plano], "").strip()}


def gerar_link_de_pagamento(driver_id: str, plano: str) -> tuple[str, datetime]:
    """URL pública do /pagar para um motorista e um plano, e quando ela vence.

    O `n` é a identidade do link: entra na chave de idempotência da Stripe e na
    marca de "já pago". Dois links gerados para o mesmo motorista são dois.
    """
    token = _serializador(LINK_PAGAMENTO_SALT).dumps(
        {"u": str(driver_id), "p": plano, "n": secrets.token_urlsafe(9)})
    return absolute_url("pagar_link", token=token), _fim_do_link(datetime.now(timezone.utc))


def _fim_do_link(emitido: datetime) -> datetime:
    """"Vale até DD/MM" vale o dia inteiro, no horário de Brasília: o motorista
    lê a data, não a hora em que o painel gerou o link."""
    return (emitido + timedelta(days=LINK_PAGAMENTO_DIAS)).astimezone(BR_TZ).replace(
        hour=23, minute=59, second=59, microsecond=0)


def ler_link_de_pagamento(token: str, folga_dias: int = 0) -> tuple[dict | None, str]:
    """(dados do link, "") se ele vale; (None, "vencido" ou "invalido") se não."""
    try:
        # O max_age é só o teto da assinatura; quem decide é o fim do dia abaixo.
        dados, emitido = _serializador(LINK_PAGAMENTO_SALT).loads(
            token, max_age=(LINK_PAGAMENTO_DIAS + 1 + folga_dias) * 86400, return_timestamp=True)
    except SignatureExpired:
        return None, "vencido"
    except BadSignature:
        return None, "invalido"
    if not isinstance(dados, dict) or dados.get("p") not in PLANOS_DO_LINK \
            or not dados.get("u") or not dados.get("n"):
        return None, "invalido"
    vence = _fim_do_link(emitido)
    if datetime.now(timezone.utc) > vence + timedelta(days=folga_dias):
        return None, "vencido"
    return {"uid": str(dados["u"]), "plano": dados["p"], "nonce": str(dados["n"]), "vence": vence}, ""


def situacao_do_link(motorista: dict, plano: str, nonce: str) -> str:
    """O que a página do link oferece, na ordem em que as regras valem.

    "pago" vence tudo: o mesmo link não cobra duas vezes (o Pix anual, por
    regra, deixaria comprar outro ano). Quem já assina pelo site ou pela loja
    não paga de novo; quem tem Pro pré-pago (Pix, cortesia, indicação) pode,
    como no botão do site.
    """
    if link_ja_pago(nonce):
        return "pago"
    if plano not in planos_do_link_disponiveis():
        return "indisponivel"
    return ja_assina(motorista) or "pode_pagar"


def _contexto_do_link(motorista: dict, link: dict) -> dict:
    plano = link["plano"]
    prepago = motorista.get("subscription_status") in STATUS_PREPAGO
    return {
        "plano": plano,
        "rotulo": PLANOS_ROTULO.get(plano, ""),
        "descricao": PLANOS_DO_LINK.get(plano, ""),
        "renova": plano not in PLANOS_PREPAGOS,
        "primeiro_nome": _primeiro_nome(motorista.get("full_name", "")),
        "email_mascarado": filtro_mascarar_email(motorista.get("email", "")),
        "vence": link.get("vence"),
        "pro_ativo": motorista.get("plan") in PAID_PLANS,
        "pro_until": _instante(motorista.get("pro_until")) if prepago else None,
    }


def _pagina_com_token(template: str, status: int = 200, **contexto):
    # O token é a credencial: não pode ir para o canonical nem para o og:url.
    return render_template(template, canonical_url=SITE_URL + "/", **contexto), status


@app.after_request
def paginas_com_token_sem_rastro(response):
    """Páginas com credencial na URL: fora da busca, do cache e do Referer."""
    if request.path.startswith(("/pagar/", "/criar-senha/", "/redefinir-senha/")):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _carregar_link_de_pagamento(token: str, folga_dias: int = 0):
    """(link, motorista, None) ou (None, None, resposta de erro pronta)."""
    link, erro = ler_link_de_pagamento(token, folga_dias)
    if not link:
        return None, None, _pagina_com_token("pagar.html", 410 if erro == "vencido" else 404,
                                             token=token, estado=erro)
    motorista = get_store().get_user_by_id(link["uid"])
    if not motorista:
        return None, None, _pagina_com_token("pagar.html", 404, token=token, estado="invalido")
    return link, motorista, None


@app.get("/pagar/<token>")
def pagar_link(token: str):
    """Link de pagamento que o painel gerou para um motorista.

    Só mostra o plano, o preço e as regras. A sessão da Stripe nasce no POST:
    a prévia do WhatsApp e o antivírus do e-mail também abrem este endereço.
    """
    link, motorista, erro = _carregar_link_de_pagamento(token)
    if erro:
        return erro
    estado = situacao_do_link(motorista, link["plano"], link["nonce"])
    return _pagina_com_token("pagar.html", token=token, estado=estado,
                             **_contexto_do_link(motorista, link))


@app.post("/pagar/<token>")
def pagar_link_confirmar(token: str):
    """O motorista confirmou: abre o Checkout da Stripe na conta dele, sem login.

    O webhook de sempre libera o Pro pelo user_id do metadata; o "origem" e o
    "link" no metadata marcam a venda do painel e deixam o link como pago.
    """
    link, motorista, erro = _carregar_link_de_pagamento(token)
    if erro:
        return erro
    contexto = _contexto_do_link(motorista, link)
    estado = situacao_do_link(motorista, link["plano"], link["nonce"])
    if estado != "pode_pagar":
        return _pagina_com_token("pagar.html", 409, token=token, estado=estado, **contexto)
    plano = link["plano"]
    try:
        checkout = criar_sessao_de_checkout(
            get_stripe(), motorista, plano, os.environ.get(STRIPE_PRICE_IDS[plano], "").strip(),
            success_url=url_for("pagar_pronto", token=token, _external=True),
            cancel_url=url_for("pagar_link", token=token, _external=True),
            # Uma chave por link e domínio: tocar de novo em 24 h devolve a
            # mesma sessão, e o link não colide com o "Assinar" do site.
            idempotency_key=f"pagar:{link['nonce']}:{motorista['_id']}:{plano}:{request.host}",
            metadata_extra={"origem": "link_painel", "link": link["nonce"]},
        )
    except Exception as exc:
        # CupomIndisponivel também cai aqui: a página só diz para tentar de novo.
        app.logger.error("Falha ao criar checkout do link para %s: %s", motorista["_id"], exc)
        return _pagina_com_token("pagar.html", 503, token=token, estado="erro", **contexto)
    app.logger.info("link de pagamento: %s foi para a Stripe (%s)", motorista["_id"], plano)
    return redirect(checkout.url, code=303)


@app.get("/pagar/<token>/pronto")
def pagar_pronto(token: str):
    """Volta da Stripe depois de pagar. Pública: o motorista do link não tem
    sessão no site, e o /dashboard pediria login logo depois de ele pagar."""
    # Dois dias de folga: quem pagou no último dia do link ainda vê esta página.
    link, motorista, erro = _carregar_link_de_pagamento(token, folga_dias=2)
    if erro:
        return erro
    contexto = _contexto_do_link(motorista, link)
    # "Confirmado" é ESTE pagamento, não o plano de agora: quem já tinha Pro
    # (cortesia, indicação) veria a data antiga como se fosse a nova. O webhook
    # só marca o link depois de gravar o Pro, então a data já é a nova.
    contexto["confirmado"] = link_ja_pago(link["nonce"])
    return _pagina_com_token("pagar.html", token=token, estado="pronto", **contexto)


def _marca_do_auth(driver_id: str, chave: str) -> bool:
    """Lê uma marca booleana do app_metadata do Auth (só o servidor a escreve)."""
    try:
        with get_store().pool.connection() as conn:
            linha = conn.execute(
                "select coalesce(raw_app_meta_data ->> %s, '') = 'true' as marca from auth.users where id = %s",
                (chave, str(driver_id))).fetchone()
        return bool(linha and linha["marca"])
    except Exception as exc:
        app.logger.warning("não consegui ler a marca %s de %s: %s", chave, driver_id, exc)
        return False


def criado_pelo_painel(driver_id: str) -> bool:
    """A conta nasceu pelo painel (marca no app_metadata do Auth)?"""
    return _marca_do_auth(driver_id, MARCA_CRIADO_PELO_PAINEL)


def conta_interna(driver_id: str) -> bool:
    """Conta da casa (dono, equipe, teste com cartão real): fora do faturamento."""
    return _marca_do_auth(driver_id, MARCA_CONTA_INTERNA)


def auth_marcar_conta_interna(driver_id: str, interna: bool) -> None:
    """Grava a marca no app_metadata. O GoTrue junta as chaves, não substitui."""
    supabase_admin().auth.admin.update_user_by_id(str(driver_id), {"app_metadata": {MARCA_CONTA_INTERNA: interna}})


def senha_pendente(usuario: dict) -> bool:
    """Conta criada pelo painel que ainda não tem a senha do motorista.

    O carimbo password_changed_at só é gravado quando alguém cria ou redefine a
    senha; enquanto ele estiver vazio, ninguém além do servidor sabe a senha.
    """
    return not usuario.get("password_changed_at") and criado_pelo_painel(usuario["_id"])


def gerar_link_criar_senha(usuario: dict, plano: str = "", nonce: str | None = None) -> str:
    """URL do /criar-senha. A digital da senha faz o link morrer no primeiro uso.

    O `n` identifica o link no teto de tentativas da confirmação pelo WhatsApp:
    cada link do painel tem o seu (só o admin gera); os que saem pelo "Esqueci
    minha senha" usam um fixo por conta, para pedir de novo não renovar o teto.
    """
    token = _serializador(CRIAR_SENHA_SALT).dumps(
        {"u": str(usuario["_id"]), "fp": _password_fingerprint(usuario),
         "p": plano if plano in PLANOS_DO_LINK else "", "n": nonce or secrets.token_urlsafe(6)})
    return absolute_url("criar_senha", token=token)


def ler_link_criar_senha(token: str) -> dict:
    """{"usuario": dict ou None, "uid", "plano", "nonce", "motivo"}.

    motivo: "" (vale), "vencido", "usado" (a senha já existe) ou "invalido".
    """
    vazio = {"usuario": None, "uid": "", "plano": "", "nonce": "", "motivo": "invalido"}
    try:
        dados = _serializador(CRIAR_SENHA_SALT).loads(token, max_age=CRIAR_SENHA_DIAS * 86400)
    except SignatureExpired:
        return {**vazio, "motivo": "vencido"}
    except BadSignature:
        return vazio
    if not isinstance(dados, dict) or not dados.get("u"):
        return vazio
    uid = str(dados["u"])
    plano = dados.get("p") if dados.get("p") in PLANOS_DO_LINK else ""
    nonce = str(dados.get("n") or "")
    usuario = get_store().get_user_by_id(uid)
    if not usuario:
        return vazio
    if not hmac.compare_digest(_password_fingerprint(usuario), str(dados.get("fp", ""))):
        return {**vazio, "uid": uid, "plano": plano, "motivo": "usado"}
    # Só o painel gera este link, e só para conta que ele criou; conferir de
    # novo aqui impede que um link desses vire porta para uma conta antiga.
    if not criado_pelo_painel(uid):
        return vazio
    return {"usuario": usuario, "uid": uid, "plano": plano, "nonce": nonce, "motivo": ""}


def prova_de_posse(usuario: dict) -> tuple[str, str]:
    """O que o /criar-senha pede para confirmar que é o motorista: ("cpf" ou
    "whatsapp", os 4 dígitos certos), ou ("", "") se não houver como provar.

    O e-mail foi ditado por telefone e ninguém o confirmou; se ele saiu errado,
    o dono daquela caixa pode receber o link (pelo botão do painel ou pelo
    "Esqueci minha senha", que para estas contas manda este mesmo link). Vale
    também para um link encaminhado.
    - No Brasil, os 4 primeiros números do CPF: o CPF passou pelo dígito
      verificador no cadastro (erro de digitação quase nunca passa) e não vai
      em nenhuma mensagem, nem na página. O WhatsApp não servia: digitado
      errado, levava o link e a resposta a um estranho, e travava o motorista.
    - Fora do Brasil o documento é texto livre: os 4 últimos do WhatsApp.
    - Sem esse dado, ("", ""): desde 2026-11-22 o painel só exige o e-mail, e o
      link não pede nada (ver criar_senha).
    Só dígitos ASCII: o compare_digest recusa texto com outros caracteres.
    """
    if (usuario.get("country") or PAIS_PADRAO) == PAIS_PADRAO:
        cpf = re.sub(r"[^0-9]", "", usuario.get("cpf") or "")
        return ("cpf", cpf[:4]) if len(cpf) == 11 else ("", "")
    digitos = re.sub(r"[^0-9]", "", usuario.get("whatsapp") or "")
    return ("whatsapp", digitos[-4:]) if len(digitos) >= 4 else ("", "")


# O que o /criar-senha pede quando a conta não tem: os obrigatórios do
# cadastro do site, menos o e-mail (é o login). (campo do formulário, coluna
# em drivers, rótulo na ficha)
OBRIGATORIOS_DO_PRIMEIRO_ACESSO = (("nome_completo", "full_name", "nome"), ("whatsapp", "whatsapp", "WhatsApp"),
                                   ("cpf", "cpf", "CPF"), ("cidade", "city", "cidade"), ("placa", "plate", "placa"))
OPCIONAIS_DO_PRIMEIRO_ACESSO = (("modelo_veiculo", "vehicle_model"), ("prefixo_taxi", "taxi_prefix"),
                                ("numero_alvara", "license_number"))


def campos_que_faltam(usuario: dict) -> list[str]:
    """Os obrigatórios do cadastro que a conta ainda não tem (nomes do
    formulário): são os dados que saem em cada recibo.

    Quem usa: o /criar-senha (conta feita pelo painel só com o e-mail), o
    /completar-cadastro (cadastro curto do site), o aviso do painel do
    motorista, o POST /recibo e o /api/recibos (que não emitem sem eles) e o
    /api/sessao (`cadastro_incompleto`). Mudar a tupla muda tudo isso.
    """
    return [campo for campo, coluna, _ in OBRIGATORIOS_DO_PRIMEIRO_ACESSO if not (usuario.get(coluna) or "").strip()]


def rotulos_que_faltam(usuario: dict) -> list[str]:
    """Os mesmos campos de campos_que_faltam, com o nome que a ficha mostra."""
    rotulos = {campo: rotulo for campo, _, rotulo in OBRIGATORIOS_DO_PRIMEIRO_ACESSO}
    if (usuario.get("country") or PAIS_PADRAO) != PAIS_PADRAO:
        rotulos["cpf"] = "documento"
    return [rotulos[campo] for campo in campos_que_faltam(usuario)]


def rotulo_do_documento(usuario: dict) -> str:
    """"CPF" no Brasil; fora, o documento de identidade do país."""
    return "CPF" if (usuario.get("country") or PAIS_PADRAO) == PAIS_PADRAO else "Documento de identidade"


def o_que_o_primeiro_acesso_pede(usuario: dict) -> dict:
    """Campos do cadastro que o /criar-senha mostra. Só o que está vazio: um
    dado salvo não aparece na página (o CPF é a prova do link, e quem abre o
    link ainda não provou nada). Conta completa: nenhum, como sempre foi."""
    faltam = campos_que_faltam(usuario)
    if not faltam:
        return {"faltam": [], "opcionais": [], "cooperativa": False}
    return {"faltam": faltam,
            "opcionais": [campo for campo, coluna in OPCIONAIS_DO_PRIMEIRO_ACESSO if not (usuario.get(coluna) or "").strip()],
            "cooperativa": not (usuario.get("coop_name") or "").strip()}


def dados_do_primeiro_acesso(usuario: dict, pede: dict) -> tuple[str, dict]:
    """Confere o que o /criar-senha pediu: (erro, {}) ou ("", mudanças em drivers).

    As regras são as do cadastro do site: obrigatórios preenchidos, CPF pelo
    dígito verificador (no Brasil) e cooperativa com CNPJ. O WhatsApp também
    precisa de DDD, como no painel: é o número da lista do vendedor.
    """
    if not pede["faltam"]:
        return "", {}
    erro_tamanho = field_limit_error(SIGNUP_FIELD_LIMITS)
    if erro_tamanho:
        return erro_tamanho, {}
    pais = usuario.get("country") or PAIS_PADRAO
    valores = {campo: request.form.get(campo, "").strip() for campo in pede["faltam"] + pede["opcionais"]}
    if "placa" in valores:
        valores["placa"] = valores["placa"].upper()
    if not all(valores[campo] for campo in pede["faltam"]):
        return "Preencha todos os campos marcados com *.", {}
    if "whatsapp" in valores and not phone_e164(valores["whatsapp"], pais):
        return "Confira o WhatsApp: com DDD (fora do Brasil, com + e o código do país).", {}
    if "cpf" in valores and pais == PAIS_PADRAO and not cpf_valido(valores["cpf"]):
        return "Confira o CPF: os dígitos não conferem.", {}
    uf = ""
    if "cidade" in valores and pais == PAIS_PADRAO:
        uf, valores["cidade"] = cidade_do_brasil(request.form.get("uf", ""), valores["cidade"])
        if not valores["cidade"]:
            return "Escolha a UF e a cidade na lista.", {}
    colunas = {campo: coluna for campo, coluna, _ in OBRIGATORIOS_DO_PRIMEIRO_ACESSO}
    colunas.update(OPCIONAIS_DO_PRIMEIRO_ACESSO)
    mudancas = {colunas[campo]: valor for campo, valor in valores.items() if valor}
    if uf:
        mudancas["state"] = uf
    if pede["cooperativa"]:
        try:
            coop_name, coop_cnpj = cooperativa_informada(
                request.form.get("cooperativa", ""), request.form.get("cnpj_cooperativa", ""), pais)
        except CooperativaInvalida as exc:
            return str(exc), {}
        if coop_name:
            mudancas.update(coop_name=coop_name, coop_cnpj=coop_cnpj)
    return "", mudancas


def plano_do_primeiro_acesso(usuario: dict, plano: str) -> str:
    """O plano do link de senha só vale para quem ainda está no Grátis.

    O link pode ter saído dias antes; se nesse meio-tempo ele pagou pelo link
    de pagamento (ou ganhou cortesia), mandá-lo ao pagamento de novo cobraria
    outro ano de Pix ou, no anual do cartão, a troca imediata do plano.
    """
    if plano not in planos_do_link_disponiveis() or usuario.get("plan") in PAID_PLANS:
        return ""
    return plano


def enviar_link_criar_senha(usuario: dict, link: str, plano: str = "") -> bool:
    """E-mail com o link de criar senha. Texto puro, como o de redefinição."""
    linhas = [
        f"Olá, {_primeiro_nome(usuario.get('full_name', ''))}!",
        "",
        f"A equipe do {APP_NAME} criou a sua conta, como combinado com você.",
        ("Falta completar o seu cadastro e criar a sua senha, neste link:" if campos_que_faltam(usuario)
         else "Falta só criar a sua senha, neste link:"),
        "",
        link,
        "",
    ]
    if plano in PLANOS_ROTULO:
        linhas += [f"Logo depois de criar a senha, você vai direto para o pagamento do {PLANOS_ROTULO[plano]}.", ""]
    linhas += [
        f"O link vale por {CRIAR_SENHA_DIAS} dias e só pode ser usado uma vez.",
        f"Para entrar no app, use este e-mail ({usuario.get('email', '')}) e a senha que você criar.",
        f"Baixe o app: {absolute_url('baixar_app', c='criar-senha')}",
        "",
        f"Não pediu esta conta? Escreva para {EMAIL_AVISOS_PADRAO} que a gente apaga.",
    ]
    try:
        return send_email(usuario["email"], f"Sua conta no {APP_NAME} está pronta: crie sua senha",
                          "\n".join(linhas))
    except Exception as exc:
        app.logger.error("Falha ao enviar o link de criar senha para %s: %s", usuario.get("_id"), exc)
        return False


def _pagina_criar_senha(token: str, estado: str, status: int = 200, **contexto):
    return _pagina_com_token("criar_senha.html", status, token=token, estado=estado, **contexto)


@app.route("/criar-senha/<token>", methods=["GET", "POST"])
def criar_senha(token: str):
    """Primeiro acesso de quem o painel cadastrou: cria a senha e entra.

    Diferente do /redefinir-senha, termina com o motorista logado no site: o
    link pode trazer um plano, e o pagamento precisa da sessão para voltar ao
    painel dele.
    """
    link = ler_link_criar_senha(token)
    usuario = link["usuario"]
    if not usuario:
        if link["motivo"] == "usado":
            # Já usado por ele mesmo (voltou à página, tocou duas vezes): mostra
            # o "tudo pronto" em vez de um erro.
            if g.user and g.user["_id"] == link["uid"]:
                plano = plano_do_primeiro_acesso(g.user, link["plano"])
                return _pagina_criar_senha(token, "pronto", usuario=g.user, email=g.user.get("email", ""),
                                           primeiro_nome=_primeiro_nome(g.user.get("full_name", "")),
                                           plano=plano, rotulo=PLANOS_ROTULO.get(plano, ""))
            # A senha foi criada, mas a resposta não chegou a este navegador
            # (recarregou, a rede caiu, abriu o link em outro lugar): o caminho
            # é entrar com a senha nova, levando o plano para o pagamento.
            dono = get_store().get_user_by_id(link["uid"]) or {}
            plano = plano_do_primeiro_acesso(dono, link["plano"]) if dono else ""
            if request.method == "POST":
                flash("Sua senha já foi criada. Entre com ela para continuar.", "info")
                return redirect(url_for("login", plano=plano or None))
            return _pagina_criar_senha(token, "usado", 410, plano=plano, rotulo=PLANOS_ROTULO.get(plano, ""))
        status = 404 if link["motivo"] == "invalido" else 410
        return _pagina_criar_senha(token, link["motivo"], status)

    plano = plano_do_primeiro_acesso(usuario, link["plano"])
    # Conta feita só com o e-mail (sem CPF; fora do Brasil, sem WhatsApp): não
    # há o que perguntar, e quem abre o link cria a senha. É a garantia do
    # "Esqueci minha senha" de qualquer conta: a posse da caixa de e-mail (ou
    # do WhatsApp) para onde o link foi. Sem senha, a conta ainda não tem
    # recibo nenhum; a ficha avisa o admin antes de ele mandar o link.
    prova, digitos_certos = prova_de_posse(usuario)
    # Faltando dados (conta do painel só com o e-mail), ele completa o
    # cadastro aqui, no site, junto com a senha; depois vai para o app.
    pede = o_que_o_primeiro_acesso_pede(usuario)
    chave_tentativas = f"criar_senha_falha:{link['nonce'] or usuario['_id']}"
    contexto = {"email": usuario.get("email", ""), "primeiro_nome": _primeiro_nome(usuario.get("full_name", "")),
                "plano": plano, "rotulo": PLANOS_ROTULO.get(plano, ""), "prova": prova,
                "pro_ativo": usuario.get("plan") in PAID_PLANS, "pede": pede,
                "documento": rotulo_do_documento(usuario), "pais": usuario.get("country") or PAIS_PADRAO,
                # Volta o que ele digitou quando algo não confere (nunca a senha nem os números).
                "preenchido": request.form if request.method == "POST" else {}}
    try:
        tentativas = get_store().get_counter(chave_tentativas)
    except Exception as exc:
        app.logger.warning("contador do criar senha indisponível: %s", exc)
        tentativas = 0
    if tentativas >= CRIAR_SENHA_TENTATIVAS:
        return _pagina_criar_senha(token, "bloqueado", 429)

    if request.method == "POST":
        senha = request.form.get("senha", "")
        confirmacao = request.form.get("confirmar_senha", "")
        # O cadastro é conferido antes da prova: errar um campo não gasta tentativa.
        erro, mudancas = dados_do_primeiro_acesso(usuario, pede)
        if erro:
            pass
        elif len(senha) < 8:
            erro = "A senha precisa ter pelo menos 8 caracteres."
        elif len(senha) > SIGNUP_FIELD_LIMITS["senha"][1]:
            erro = f"A senha passa do limite de {SIGNUP_FIELD_LIMITS['senha'][1]} caracteres."
        elif senha != confirmacao:
            erro = "As senhas não conferem."
        elif prova:
            # A tentativa conta ANTES de conferir, num passo só: ler e somar
            # depois deixava pedidos simultâneos passarem todos pelo teto (o
            # mesmo cuidado do conferir_senha_atual). Erro de senha não gasta.
            # Sem o contador, não confere: conferir sem contar tiraria o teto.
            try:
                tentativa = get_store().bump_counter(chave_tentativas)
            except Exception as exc:
                app.logger.warning("contador do criar senha indisponível: %s", exc)
                flash("Não conseguimos conferir agora. Tente de novo em alguns instantes.", "danger")
                return _pagina_criar_senha(token, "formulario", 503, **contexto)
            if tentativa > CRIAR_SENHA_TENTATIVAS:
                return _pagina_criar_senha(token, "bloqueado", 429)
            so_digitos = re.sub(r"[^0-9]", "", request.form.get("digitos", ""))
            # Quem digitar o CPF inteiro também passa: valem os 4 primeiros.
            digitado = so_digitos[:4] if prova == "cpf" else so_digitos[-4:]
            if not hmac.compare_digest(digitado.encode(), digitos_certos.encode()):
                erro = ("Os 4 primeiros números do CPF não conferem com o do cadastro." if prova == "cpf"
                        else "Os 4 últimos números do WhatsApp não conferem com o do cadastro.")
        if erro:
            flash(erro, "danger")
            return _pagina_criar_senha(token, "formulario", 400, **contexto)

        store = get_store()
        try:
            auth_definir_senha(usuario["_id"], senha)
            # Os dados do cadastro vão no mesmo passo do carimbo da senha: se
            # este falhar, o link continua valendo e ele tenta de novo.
            store.update_user(usuario["_id"], {**mudancas, "password_changed_at": now_iso(),
                                               **({"updated_at": now_iso()} if mudancas else {})})
        except Exception as exc:
            app.logger.error("criar senha: não salvou a senha de %s: %s", usuario["_id"], exc)
            flash("Não conseguimos salvar a senha agora. Tente de novo em alguns instantes.", "danger")
            return _pagina_criar_senha(token, "formulario", 503, **contexto)
        zerar_falhas_de_login(usuario["email"])
        try:
            store.reset_counter(chave_de_reconfirmacao(usuario["_id"]))
        except Exception as exc:
            app.logger.warning("contador de senha indisponível: %s", exc)

        # Entra já logado. O carimbo novo vai para a sessão, ou a próxima
        # página (a volta da Stripe, por exemplo) derrubaria a sessão. Qualquer
        # sessão anterior neste navegador (de outra conta ou do painel) sai.
        usuario = store.get_user_by_id(usuario["_id"]) or usuario
        session.clear()
        session["user_id"] = usuario["_id"]
        session["pw_stamp"] = usuario.get("password_changed_at") or ""
        registrar_acesso(usuario["_id"], "site_criar_senha")
        app.logger.info("motorista %s criou a senha pelo link do painel%s", usuario["_id"],
                        " e completou o cadastro" if mudancas else "")
        if plano:
            # O aviso aparece na volta da Stripe, no painel: por isso é neutro.
            flash("Senha criada! No celular, entre no app Recibo Táxi com este e-mail e esta senha.",
                  "success")
            return iniciar_checkout(usuario, plano)
        return redirect(url_for("criar_senha", token=token))

    return _pagina_criar_senha(token, "formulario", **contexto)


# ── API do app (fila offline) ─────────────────────────────────────────────────

@app.route("/api/<path:_qualquer>", methods=["OPTIONS"])
def api_preflight(_qualquer: str):
    """Responde ao preflight do navegador. O CORS vem do after_request."""
    return ("", 204)


@app.post("/api/login")
def api_login():
    """Login do app nativo. Devolve tokens em vez de criar cookie."""
    dados = corpo_json()
    email = normalize_email(str(dados.get("email", "")))
    if login_bloqueado(email):
        return jsonify({"erro": "muitas_tentativas",
                        "mensagem": "Muitas tentativas de entrar hoje. Tente amanhã ou use Esqueci minha senha."}), 429
    tokens = auth_entrar_com_token(email, str(dados.get("senha", "")))
    if not tokens:
        registrar_falha_de_login(email)
        return jsonify({"erro": "credenciais_invalidas"}), 401

    usuario = get_store().get_user_by_id(tokens["user_id"])
    if not usuario:
        return jsonify({"erro": "perfil_ausente"}), 401

    zerar_falhas_de_login(email)
    registrar_acesso(tokens["user_id"], "app")
    return jsonify(tokens)


@app.post("/api/recuperar-senha")
def api_recuperar_senha():
    """Pedido de nova senha pelo app.

    Sem isto o motorista que esquecia a senha nao tinha saida dentro do app: a
    tela de login so oferecia entrar ou criar conta. A resposta e a mesma exista
    ou nao a conta — dizer "nao encontrado" contaria a um estranho quem esta
    cadastrado.
    """
    dados = corpo_json()
    email = normalize_email(str(dados.get("email", "")))
    if not email_valido(email):
        return jsonify({"erro": "email_invalido"}), 400
    if not pode_pedir_redefinicao(email):
        return jsonify({"erro": "muitos_pedidos"}), 429

    user = get_store().get_user_by_email(email)
    if user:
        try:
            enviar_link_de_redefinicao(user)
        except Exception as exc:
            app.logger.error("Falha ao enviar link de redefinicao: %s", exc)

    return jsonify({"enviado": True})


@app.post("/api/excluir-conta")
@api_login_required
def api_excluir_conta():
    """Exclusao de conta pelo proprio app.

    As duas lojas exigem que quem cria conta dentro do app consiga apagar
    dentro do app — Apple na diretriz 5.1.1(v), Google na politica de exclusao
    de dados. Mandar o motorista para o site nao cumpre.

    Pede a senha, e nao so o token: token vazado num aparelho emprestado nao
    pode apagar a conta e todos os recibos de alguem.
    """
    dados = corpo_json()
    senha = conferir_senha_atual(g.user, str(dados.get("senha", "")))
    if senha == "bloqueada":
        return jsonify({"erro": "muitas_tentativas", "mensagem": MSG_MUITAS_TENTATIVAS}), 429
    if senha == "errada":
        return jsonify({"erro": "senha_incorreta"}), 403

    # Apagar sem conseguir parar a cobranca deixaria assinatura orfa, cobrando
    # alguem que ja nao tem como cancelar. Mesma regra do site.
    assinatura = g.user.get("stripe_subscription_id")
    if assinatura:
        stripe = get_stripe()
        try:
            if not stripe:
                raise RuntimeError("Stripe nao configurada")
            stripe.Subscription.cancel(assinatura)
        except Exception as exc:
            app.logger.error("Nao cancelou a assinatura %s: %s", assinatura, exc)
            return jsonify({"erro": "assinatura_ativa"}), 409

    # Quem assinou pela loja cancela na loja: a Apple e a Google nao deixam o
    # servidor cancelar por fora, e apagar aqui nao para a cobranca de la.
    # Pro pre-pago (indicacao, anual por Pix, cortesia) nao tem cobranca
    # nenhuma: pode apagar.
    if assina_pela_loja(g.user):
        return jsonify({"erro": "assinatura_na_loja"}), 409

    auth_excluir_usuario(g.user["_id"])
    sincronizar_crm_agora(g.user["_id"])
    return jsonify({"excluida": True})


@app.post("/api/recibos/<rid>/email")
@api_login_required
def api_enviar_recibo_por_email(rid: str):
    """Manda o recibo para o e-mail do passageiro.

    Este e o UNICO lugar que envia recibo por e-mail. Emitir nao envia nada:
    preencher o campo e uma coisa, mandar e outra, e cada envio custa — no
    plano gratuito do Resend sao 100 por dia somados todos os motoristas.
    """
    recibo = get_store().get_receipt(str(rid).strip().upper())
    # Mesma resposta para recibo inexistente e recibo de outro motorista: dizer
    # qual dos dois e contaria a um estranho que aquele codigo existe.
    if not recibo or str(recibo.get("driver_id") or "") != str(g.user["_id"]):
        return jsonify({"erro": "nao_encontrado"}), 404

    if not email_valido(recibo.get("passenger_email", "")):
        return jsonify({"erro": "sem_email"}), 400

    if not pode_enviar_email(g.user["_id"]):
        return jsonify({"erro": "limite_diario", "limite": EMAIL_DAILY_LIMIT}), 429

    try:
        enviou = enviar_recibo_por_email(recibo)
    except Exception as exc:
        app.logger.error("Falha ao enviar recibo %s: %s", recibo.get("rid"), exc)
        enviou = False
    if not enviou:
        return jsonify({"erro": "falha_no_envio"}), 502

    return jsonify({"enviado": True, "para": recibo["passenger_email"]})


@app.post("/api/refresh")
def api_refresh():
    """Renova a sessao do app. Nao exige o access token — ele ja expirou."""
    dados = corpo_json()
    tokens = auth_renovar_sessao(str(dados.get("refresh_token", "")).strip())
    if not tokens:
        return jsonify({"erro": "refresh_invalido"}), 401

    # Se o perfil sumiu (conta apagada), renovar nao adianta: manda para login.
    if not get_store().get_user_by_id(tokens["user_id"]):
        return jsonify({"erro": "perfil_ausente"}), 401

    registrar_acesso(tokens["user_id"], "app_refresh")
    return jsonify(tokens)


@app.post("/api/cadastro")
def api_cadastro():
    """Cadastro pelo app nativo. Devolve os tokens já logado.

    Existe porque mandar o motorista para o site abriria o Safari — além da
    experiência ruim, é um dos padrões que a Apple rejeita (Guideline 4.2).
    """
    dados = corpo_json()

    campos = {k: str(dados.get(k, "") or "").strip() for k in (
        "nome_completo", "email", "senha", "whatsapp", "cpf", "cidade",
        "placa", "modelo_veiculo", "prefixo_taxi", "numero_alvara",
        "cooperativa", "cnpj_cooperativa")}
    campos["email"] = normalize_email(campos["email"])
    campos["placa"] = campos["placa"].upper()

    for nome, (rotulo, maximo) in SIGNUP_FIELD_LIMITS.items():
        if len(campos.get(nome, "")) > maximo:
            return jsonify({"erro": "campo_longo", "campo": nome, "maximo": maximo}), 400

    obrigatorios = ["nome_completo", "email", "senha", "whatsapp", "cpf", "cidade", "placa"]
    faltando = [c for c in obrigatorios if not campos[c]]
    if faltando:
        return jsonify({"erro": "campos_obrigatorios", "campos": faltando}), 400

    if len(campos["senha"]) < 8:
        return jsonify({"erro": "senha_curta", "minimo": 8}), 400

    if not email_valido(campos["email"]):
        return jsonify({"erro": "email_invalido", "mensagem": "Confira o e-mail: ele não parece válido."}), 400

    # As versões do app de antes da 0010 não mandam país nem moeda: vale o Brasil.
    try:
        pais, moeda = pais_e_moeda(str(dados.get("pais") or ""), str(dados.get("moeda") or ""))
    except ValueError as exc:
        return jsonify({"erro": "pais_ou_moeda_invalido", "mensagem": str(exc)}), 400

    if pais == PAIS_PADRAO and not cpf_valido(campos["cpf"]):
        return jsonify({"erro": "cpf_invalido", "mensagem": "Confira o CPF: os dígitos não conferem."}), 400

    # Cooperativa (opcional; o app 1.5 manda os dois campos).
    try:
        coop_name, coop_cnpj = cooperativa_informada(campos["cooperativa"], campos["cnpj_cooperativa"], pais)
    except CooperativaInvalida as exc:
        return jsonify({"erro": exc.codigo, "mensagem": str(exc)}), 400

    if passou_do_teto("cadastro:ip", client_ip(), CADASTRO_DAILY_LIMIT_IP):
        return jsonify({"erro": "muitas_tentativas",
                        "mensagem": "Muitos cadastros a partir desta rede hoje. Tente amanhã ou fale com o suporte."}), 429

    codigo_indicacao = str(dados.get("indicacao") or "").strip().upper()
    indicador = get_store().get_user_by_referral_code(codigo_indicacao) if codigo_indicacao else None
    if codigo_indicacao and not indicador:
        return jsonify({"erro": "codigo_indicacao_invalido"}), 400

    try:
        user_id = auth_criar_usuario(
            campos["email"],
            campos["senha"],
            {k: campos[k] for k in (
                "nome_completo", "cpf", "whatsapp", "cidade", "placa",
                "modelo_veiculo", "prefixo_taxi", "numero_alvara")}
            | {"full_name": campos["nome_completo"], "city": campos["cidade"],
               "plate": campos["placa"], "vehicle_model": campos["modelo_veiculo"],
               "taxi_prefix": campos["prefixo_taxi"],
               "license_number": campos["numero_alvara"],
               "country": pais, "currency": moeda},
        )
    except ValueError as exc:
        return jsonify({"erro": "email_em_uso", "mensagem": str(exc)}), 409
    except RuntimeError:
        return jsonify({"erro": "indisponivel"}), 503

    guardar_cooperativa(user_id, coop_name, coop_cnpj)
    # `canal`: o utm_source que o Google Play entrega ao app no primeiro acesso
    # (Install Referrer; app 1.9+). Opcional; fora do padrão, fica vazio.
    canal = canal_de_cadastro(dados.get("canal"))
    guardar_extras_do_cadastro(user_id, canal=canal)
    registrar_indicacao(indicador, user_id)
    avisar_novo_cadastro(user_id, {
        "email": campos["email"], "full_name": campos["nome_completo"], "whatsapp": campos["whatsapp"],
        "city": campos["cidade"], "plate": campos["placa"], "vehicle_model": campos["modelo_veiculo"],
        "country": pais, "currency": moeda, "canal": canal,
    }, "app")
    sincronizar_crm_agora(user_id)
    tokens = auth_entrar_com_token(campos["email"], campos["senha"])
    if not tokens:
        return jsonify({"erro": "criado_sem_login"}), 500
    registrar_acesso(tokens["user_id"], "app_cadastro")
    return jsonify(tokens), 201


@app.get("/api/sessao")
@api_login_required
def api_sessao():
    """Quem sou eu — o app usa para saber se a sessão ainda vale."""
    mes_inicio, mes_fim = month_range_utc()
    plano = g.user.get("plan", "free")
    moeda = g.user.get("currency") or MOEDA_PADRAO
    _, simbolo, casas, _ = dados_da_moeda(moeda)
    return jsonify(
        {
            "id": g.user["_id"],
            "nome": g.user.get("full_name", ""),
            "email": g.user.get("email", ""),
            # Conta do cadastro curto do site que ainda não completou os dados
            # do recibo (rótulos): o app 1.9+ manda completar antes de emitir.
            "cadastro_incompleto": rotulos_que_faltam(g.user),
            "plano": plano,
            # O app imprime o recibo offline: guarda a moeda e o jeito de
            # escrevê-la, para o PDF sair igual à página do recibo.
            "pais": g.user.get("country") or PAIS_PADRAO,
            # Codigo de discagem do pais: o app monta o wa.me do passageiro com ele.
            "ddi": PAISES.get(g.user.get("country") or PAIS_PADRAO, ("", "", ""))[2],
            "moeda": moeda,
            # Pro pré-pago (anual por Pix ou mês de indicação) vence; o app mostra a data.
            # So para Pro pre-pago: assinante tambem pode ter dias guardados, e o
            # app mostraria "Seu Pro vale ate X, depois e so assinar" a quem assina.
            "pro_until": (g.user.get("pro_until") or None)
                         if g.user.get("subscription_status") in STATUS_PREPAGO else None,
            "codigo_indicacao": codigo_de_indicacao(g.user),
            "link_indicacao": link_de_indicacao(codigo_de_indicacao(g.user)),
            # Desconto de indicação nas lojas: encerrado em 2026-11-10. O app 1.3
            # lê `cupom_indicacao` e, sem ele, esconde o bloco (app.js, cupomDeIndicacao).
            "moeda_simbolo": simbolo,
            "moeda_casas": casas,
            # Este servidor aceita recibo sem nome do passageiro. O app 1.4 so
            # devolve a fila o recibo sem nome recusado por um servidor antigo
            # quando ve este sinal; sem ele, o reenvio seria recusado de novo.
            "passageiro_opcional": True,
            "limite_mensal": None if plano in PAID_PLANS else FREE_MONTHLY_LIMIT,
            # O app precisa saber a origem: quem assinou pela Stripe no site
            # gerencia lá; quem assinou pelo app gerencia na loja.
            # Pela assinatura viva, e nao pelo cliente da Stripe: o cliente nasce
            # ao abrir o checkout no site e fica para sempre, e quem desistiu ali e
            # assinou pela loja lia no app que a assinatura era do site.
            "origem_assinatura": (
                "stripe" if g.user.get("stripe_subscription_id")
                and g.user.get("subscription_status") in ACTIVE_SUBSCRIPTION_STATUSES
                else "loja" if assina_pela_loja(g.user)
                else "prepago" if plano in PAID_PLANS
                else None
            ),
            "usados_no_mes": get_store().count_receipts_in_range(
                g.user["_id"], mes_inicio, mes_fim, 10_000
            ),
            "motorista": motorista_do_app(g.user),
        }
    )


def motorista_do_app(user: dict) -> dict:
    """Os dados do motorista que o app guarda e imprime no recibo offline.
    Mesmo formato na /api/sessao e na resposta de /api/perfil."""
    return {
        "full_name": user.get("full_name", ""),
        "email": user.get("email", ""),
        "whatsapp": user.get("whatsapp", ""),
        "city": user.get("city", ""),
        "plate": user.get("plate", ""),
        "vehicle_model": user.get("vehicle_model", ""),
        "taxi_prefix": user.get("taxi_prefix", ""),
        "license_number": user.get("license_number", ""),
        "coop_name": user.get("coop_name") or "",
        "coop_cnpj": user.get("coop_cnpj") or "",
    }


# Campos que o app edita (versão 1.6). E-mail, CPF, país e moeda ficam no
# site: o e-mail é o login no Supabase Auth, e país e moeda mudam o jeito de
# escrever o valor dos recibos que ainda estão na fila do aparelho.
PERFIL_DO_APP = ("nome_completo", "whatsapp", "cidade", "placa", "modelo_veiculo",
                 "prefixo_taxi", "numero_alvara", "cooperativa", "cnpj_cooperativa")
PERFIL_OBRIGATORIOS = ("nome_completo", "whatsapp", "cidade", "placa")


@app.post("/api/perfil")
@api_login_required
def api_perfil():
    """Correção dos dados do motorista pelo app (a mesma de Meu perfil no site).

    Pede a senha atual, como o site: um token esquecido num aparelho
    emprestado não pode trocar o nome e o telefone que saem nos recibos de
    alguém. Senha errada é 403, e não 401: o app trata 401 como token
    vencido e tentaria renovar a sessão. Conta no teto de quem já entrou
    (conferir_senha_atual: 30 por dia nesta conta, separado do /api/login),
    e a validação dos campos vem antes, para um CNPJ digitado errado não
    gastar tentativa de senha.
    """
    dados = corpo_json()
    campos = {k: str(dados.get(k, "") or "").strip() for k in PERFIL_DO_APP}
    campos["placa"] = campos["placa"].upper()

    for nome in PERFIL_DO_APP:
        rotulo, maximo = SIGNUP_FIELD_LIMITS[nome]
        if len(campos[nome]) > maximo:
            return jsonify({"erro": "campo_longo", "campo": nome, "maximo": maximo,
                            "mensagem": f"{rotulo} passa do limite de {maximo} caracteres."}), 400

    faltando = [c for c in PERFIL_OBRIGATORIOS if not campos[c]]
    if faltando:
        return jsonify({"erro": "campos_obrigatorios", "campos": faltando}), 400

    # CPF só entra quando a conta ainda não tem um (cadastro curto do site;
    # app 1.9+). Conta com CPF: o campo é ignorado, como sempre foi — o
    # gravado é a prova do /criar-senha e só muda no site.
    cpf = "" if (g.user.get("cpf") or "").strip() else str(dados.get("cpf", "") or "").strip()
    if len(cpf) > SIGNUP_FIELD_LIMITS["cpf"][1]:
        return jsonify({"erro": "campo_longo", "campo": "cpf", "maximo": SIGNUP_FIELD_LIMITS["cpf"][1]}), 400
    if cpf and (g.user.get("country") or PAIS_PADRAO) == PAIS_PADRAO and not cpf_valido(cpf):
        return jsonify({"erro": "cpf_invalido", "mensagem": "Confira o CPF: os dígitos não conferem."}), 400

    try:
        coop_name, coop_cnpj = cooperativa_informada(
            campos["cooperativa"], campos["cnpj_cooperativa"], g.user.get("country") or PAIS_PADRAO)
    except CooperativaInvalida as exc:
        return jsonify({"erro": exc.codigo, "mensagem": str(exc)}), 400

    senha = conferir_senha_atual(g.user, str(dados.get("senha_atual", "")))
    if senha == "bloqueada":
        return jsonify({"erro": "muitas_tentativas", "mensagem": MSG_MUITAS_TENTATIVAS}), 429
    if senha == "errada":
        return jsonify({"erro": "senha_incorreta"}), 403

    get_store().update_user(g.user["_id"], {
        **({"cpf": cpf} if cpf else {}),
        "full_name": campos["nome_completo"],
        "whatsapp": campos["whatsapp"],
        "city": campos["cidade"],
        "plate": campos["placa"],
        "vehicle_model": campos["modelo_veiculo"],
        "taxi_prefix": campos["prefixo_taxi"],
        "license_number": campos["numero_alvara"],
        "coop_name": coop_name,
        "coop_cnpj": coop_cnpj,
        "updated_at": now_iso(),
    })
    atualizado = get_store().get_user_by_id(g.user["_id"]) or g.user
    return jsonify({"motorista": motorista_do_app(atualizado)})


@app.get("/api/paises")
def api_paises():
    """Listas de país e moeda para a tela de cadastro do app.

    Ficam no servidor, e não no app, para incluir um país sem esperar a
    revisão das lojas. Público: é a mesma lista do formulário do site.
    """
    return jsonify({
        "paises": [{"codigo": c, "nome": p[0], "moeda": p[1], "ddi": p[2]}
                   for c, p in PAISES.items()],
        "moedas": [{"codigo": c, "nome": m[0], "simbolo": m[1], "casas": m[2]}
                   for c, m in MOEDAS.items()],
    })


@app.post("/api/recibos")
@api_login_required
def api_criar_recibo():
    """Recebe um recibo emitido OFFLINE, com rid escolhido pelo aparelho.

    É idempotente de propósito: a fila do app reenvia o que não teve resposta
    confirmada, e sem isso uma resposta perdida viraria recibo duplicado.
    """
    dados = corpo_json()

    rid = str(dados.get("rid", "")).strip().upper()
    if not re.fullmatch(r"[0-9A-F]{%d}" % RID_LENGTH, rid):
        return jsonify({"erro": "rid_invalido"}), 400

    # Cadastro curto do site sem os dados do recibo: o retrato do motorista
    # fica congelado no recibo, então não emite. O app guarda o recibo como
    # recusado (o 1.9+ manda completar em "Meus dados").
    faltam = rotulos_que_faltam(g.user)
    if faltam:
        return jsonify({"erro": "cadastro_incompleto", "faltam": faltam,
                        "mensagem": "Complete seu cadastro (" + ", ".join(faltam)
                        + ") em Meus dados ou no site antes de emitir."}), 409

    passageiro = dados.get("passageiro")
    campos = {
        # Opcional desde 2026-10. Antes, null, false ou 0 caiam na checagem de
        # obrigatorios; agora virariam o texto "None" no recibo.
        "passageiro": passageiro if isinstance(passageiro, str) else "",
        "data": dados.get("data", ""),
        "origem": dados.get("origem", ""),
        "destino": dados.get("destino", ""),
        "valor": dados.get("valor", ""),
        "forma_pagamento": dados.get("forma_pagamento", ""),
        "observacoes": dados.get("observacoes", ""),
        "email_passageiro": dados.get("email_passageiro", ""),
        "whatsapp_passageiro": dados.get("whatsapp_passageiro", ""),
        "documento_passageiro": dados.get("documento_passageiro", ""),
        "razao_social": dados.get("razao_social", ""),
        "hora": dados.get("hora", ""),
        "hora_fim": dados.get("hora_fim", ""),
        "km": dados.get("km", ""),
    }
    for nome, (rotulo, maximo) in RECEIPT_FIELD_LIMITS.items():
        if len(str(campos.get(nome, "") or "")) > maximo:
            return jsonify({"erro": "campo_longo", "campo": nome, "maximo": maximo}), 400

    # Sem o nome do passageiro desde 2026-10 (o app 1.4 deixa em branco).
    if not all([campos["data"], campos["origem"], campos["destino"]]):
        return jsonify({"erro": "campos_obrigatorios"}), 400

    # A versao do app que ja sabe a moeda manda a que imprimiu no PDF — o
    # recibo pode ter sido emitido offline, antes de o motorista trocar de
    # moeda no site. As versoes antigas nao mandam: vale a do cadastro.
    moeda = str(dados.get("moeda") or "").strip().upper()
    if moeda not in MOEDAS:
        moeda = g.user.get("currency") or MOEDA_PADRAO
    try:
        amount_value, amount_display = normalize_money(str(campos["valor"]), moeda)
    except ValueError as exc:
        return jsonify({"erro": "valor_invalido", "mensagem": str(exc)}), 400
    try:
        distance_km = normalize_km(campos["km"])
    except ValueError as exc:
        return jsonify({"erro": "km_invalido", "mensagem": str(exc)}), 400

    receipt = {
        "_id": rid,
        "rid": rid,
        "driver_id": g.user["_id"],
        "is_guest": False,
        "passenger": str(campos["passageiro"]).strip(),
        "passenger_email": normalize_email(str(campos["email_passageiro"])),
        "passenger_whatsapp": str(campos["whatsapp_passageiro"]).strip(),
        "passenger_document": format_document_br(
            str(campos["documento_passageiro"]), g.user.get("country") or PAIS_PADRAO),
        "passenger_company": razao_social_de(
            str(campos["documento_passageiro"]), str(campos["razao_social"])),
        "trip_date": str(campos["data"]).strip(),
        "trip_time": hora_valida(str(campos["hora"])),
        "trip_end_time": hora_valida(str(campos["hora_fim"])),
        "distance_km": distance_km,
        "origin": str(campos["origem"]).strip(),
        "destination": str(campos["destino"]).strip(),
        "amount_value": amount_value,
        "amount_display": amount_display,
        "currency": moeda,
        "payment_method": str(campos["forma_pagamento"]).strip() or "Pix",
        "notes": str(campos["observacoes"]).strip(),
        # So o booleano true conta: "sim", 1 ou qualquer outra coisa nao marca.
        "via_corrida": dados.get("via_corrida") is True,
        "driver_snapshot": snapshot_do_motorista(g.user),
    }
    # created_at do cliente (recibo emitido offline e sincronizado depois): só
    # ISO-8601, nem no futuro nem antes de 30 dias; fora disso vale a hora do
    # servidor. Texto inválido dava 500 e data retroativa descasava a cota.
    if dados.get("created_at"):
        instante = _instante(str(dados["created_at"]))
        agora = datetime.now(timezone.utc)
        # Não antes do mês corrente: a cota é consumida no mês do servidor, e
        # um recibo datado do mês passado descasaria a contagem do painel.
        piso = max(agora - timedelta(days=30), _instante(month_range_utc()[0]) or agora)
        if instante and piso <= instante <= agora + timedelta(minutes=5):
            receipt["created_at"] = to_utc_iso(instante)

    quota = None if g.user.get("plan", "free") in PAID_PLANS else FREE_MONTHLY_LIMIT
    salvo, criado_agora = get_store().create_receipt_idempotente(receipt, quota)

    if salvo is None:
        # Ou a cota acabou, ou o rid pertence a outro motorista.
        existente = get_store().get_receipt(rid)
        if existente:
            # O mesmo motorista mandando o mesmo rid duas vezes ao mesmo tempo
            # (a fila e o "voltar ao app" do aparelho), no ultimo recibo da
            # cota: o primeiro pedido gravou, o segundo nao achou nada antes e
            # ficou sem vaga. E um reenvio como outro qualquer, nao um rid de
            # outra pessoa — o app marcaria "codigo ja usado" para sempre.
            if existente.get("driver_id") == g.user["_id"]:
                return jsonify({"rid": rid, "url": public_receipt_url(rid), "criado_agora": False}), 200
            return jsonify({"erro": "rid_de_outro_motorista"}), 409
        return jsonify({"erro": "limite_mensal", "limite": FREE_MONTHLY_LIMIT}), 402

    if criado_agora:
        ativar_indicacao(g.user)

    # O e-mail NAO sai daqui. Quem decide e o motorista, tocando em "Enviar por
    # e-mail" na tela do recibo (POST /api/recibos/<rid>/email). Preencher o
    # campo e uma coisa; mandar e outra — o passageiro pode ter dito o endereco
    # so para o motorista guardar.
    return (
        jsonify(
            {
                "rid": salvo["rid"],
                "url": public_receipt_url(salvo["rid"]),
                "criado_agora": criado_agora,
            }
        ),
        201 if criado_agora else 200,
    )


# ── Caixa do motorista (app 1.8) ──────────────────────────────────────────────
#
# O Caixa mora no aparelho, offline como os recibos. O servidor guarda uma
# cópia de cada lançamento (migração 0022) para o motorista não perder o que
# lançou; restaurar em aparelho novo (GET) é a fase 2. Contrato no README,
# seção "Caixa (app 1.8)". O trajeto e o passageiro nunca chegam aqui, e o log
# leva só contagens e códigos: nada de valor, categoria ou observação.

LANCAMENTO_TIPOS = {"receita": "income", "despesa": "expense"}
LANCAMENTO_ORIGENS = {"recibo": "receipt", "corrida": "ride", "manual": "manual"}
# fullmatch, e não ^...$: o $ do Python aceita um \n no fim, e o check do banco
# recusaria a linha, derrubando o lote inteiro.
_LANCAMENTO_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
_LANCAMENTO_CATEGORIA_RE = re.compile(r"[a-z][a-z0-9_]{0,29}")
_LANCAMENTO_DATA_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_LANCAMENTO_RID_RE = re.compile(r"[0-9A-F]{%d}" % RID_LENGTH)
_LANCAMENTO_DATA_MINIMA = datetime(2000, 1, 1).date()
_LANCAMENTO_VERSAO_MAXIMA = 2**31 - 1   # integer do Postgres
_LITROS_MAXIMO = Decimal("999.99")


class LancamentoRecusado(ValueError):
    """Item do lote fora do contrato. O código vai para `recusados`."""


def _versao_do_lancamento(valor) -> int | None:
    """Inteiro de verdade de 1 a 2^31-1, ou None. bool fica de fora: para o
    Python, True é 1."""
    if type(valor) is int and 1 <= valor <= _LANCAMENTO_VERSAO_MAXIMA:
        return valor
    return None


def _texto_do_lancamento(valor, maximo: int) -> str | None:
    """Forma de pagamento ou observação pronta para o banco; None se passou do tamanho.

    O que não é texto vira vazio. NUL e surrogate solto saem antes: o Postgres
    recusa os dois, e um item assim derrubaria o lote inteiro (503), que o app
    reenviaria para sempre.
    """
    if not isinstance(valor, str):
        return ""
    texto = valor.replace("\x00", "").encode("utf-8", "replace").decode("utf-8").strip()
    return texto if len(texto) <= maximo else None


def _litros_do_lancamento(valor) -> str | None:
    """"30,5", "30.5" ou 30.5 → "30.50"; vazio → None. Fora de 0 < x ≤ 999,99
    com até 2 casas levanta ValueError."""
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return None
    if isinstance(valor, bool) or not isinstance(valor, (int, float, str)):
        raise ValueError("litros")
    texto = valor.strip().replace(",", ".") if isinstance(valor, str) else str(valor)
    try:
        litros = Decimal(texto)
    except InvalidOperation as exc:
        raise ValueError("litros") from exc
    # A ordem importa: NaN e número enorme param antes do quantize, que estoura com eles.
    if (not litros.is_finite() or litros <= 0 or litros > _LITROS_MAXIMO
            or litros != litros.quantize(Decimal("0.01"))):
        raise ValueError("litros")
    return str(litros.quantize(Decimal("0.01")))


def ler_lancamento(item: dict, moeda_do_cadastro: str, hoje) -> dict:
    """Confere um item (já com id válido) do POST /api/lancamentos.

    Devolve a linha pronta para upsert_ledger_entries ou levanta
    LancamentoRecusado com o código do primeiro campo errado, na ordem da
    tabela do contrato. Nada aqui pode estourar com tipo inesperado: lista no
    lugar de texto vira código de erro, nunca 500.
    """
    lid = item["id"]
    tipo = item.get("tipo")
    if not isinstance(tipo, str) or tipo not in LANCAMENTO_TIPOS:
        raise LancamentoRecusado("tipo_invalido")
    categoria = item.get("categoria")
    if not (isinstance(categoria, str) and _LANCAMENTO_CATEGORIA_RE.fullmatch(categoria)):
        raise LancamentoRecusado("categoria_invalida")

    # A moeda vem antes do valor porque o teto é dela. Fora da lista vale a do
    # cadastro, como em /api/recibos.
    moeda = str(item.get("moeda") or "").strip().upper()
    if moeda not in MOEDAS:
        moeda = moeda_do_cadastro
    _, _, casas, teto = dados_da_moeda(moeda)
    # Dinheiro chega em inteiro na menor unidade: 45.0 (float) e True (bool)
    # ficam de fora, senão o arredondamento de float volta pela porta dos fundos.
    centavos = item.get("centavos")
    if type(centavos) is not int or not 1 <= centavos <= teto.scaleb(casas):
        raise LancamentoRecusado("valor_invalido")

    data = item.get("data")
    if not (isinstance(data, str) and _LANCAMENTO_DATA_RE.fullmatch(data)):
        raise LancamentoRecusado("data_invalida")
    try:
        dia = datetime.strptime(data, "%Y-%m-%d").date()
    except ValueError as exc:
        raise LancamentoRecusado("data_invalida") from exc
    # Dois dias de folga: o relógio do aparelho e o fuso de quem roda fora do Brasil.
    if not _LANCAMENTO_DATA_MINIMA <= dia <= hoje + timedelta(days=2):
        raise LancamentoRecusado("data_invalida")

    hora = item.get("hora")
    hora = hora_valida(hora if isinstance(hora, str) else "")

    forma = _texto_do_lancamento(item.get("forma"), 40)
    if forma is None:
        raise LancamentoRecusado("campo_longo")

    origem = item.get("origem")
    if not isinstance(origem, str) or origem not in LANCAMENTO_ORIGENS:
        raise LancamentoRecusado("origem_invalida")

    # O recibo de origem não precisa existir no servidor (ver a migração 0022),
    # mas o lançamento do recibo tem id fixo, r-<rid>: é o que impede o mesmo
    # recibo de entrar duas vezes no caixa.
    rid = item.get("rid")
    if rid is None or rid == "":
        rid = None
    elif not (isinstance(rid, str) and _LANCAMENTO_RID_RE.fullmatch(rid)):
        raise LancamentoRecusado("rid_invalido")
    if origem == "recibo" and (rid is None or lid != f"r-{rid}"):
        raise LancamentoRecusado("rid_invalido")

    obs = _texto_do_lancamento(item.get("obs"), 120)
    if obs is None:
        raise LancamentoRecusado("campo_longo")

    try:
        km = normalize_km(item.get("km"))
    except ValueError as exc:
        raise LancamentoRecusado("km_invalido") from exc
    try:
        litros = _litros_do_lancamento(item.get("litros"))
    except ValueError as exc:
        raise LancamentoRecusado("litros_invalido") from exc

    versao = _versao_do_lancamento(item.get("versao"))
    if versao is None:
        raise LancamentoRecusado("versao_invalida")

    return {
        "id": lid, "kind": LANCAMENTO_TIPOS[tipo], "category": categoria,
        "amount": str(Decimal(centavos).scaleb(-casas)), "currency": moeda,
        "entry_date": data, "entry_time": hora, "payment_method": forma,
        "source": LANCAMENTO_ORIGENS[origem], "rid": rid, "note": obs,
        "distance_km": km, "liters": litros, "version": versao,
        # Só o booleano true apaga: "true", 1 ou qualquer outra coisa não.
        "apagado": item.get("apagado") is True,
    }


def ler_lote_de_lancamentos(itens: list, moeda_do_cadastro: str, hoje) -> tuple[list, list, list]:
    """(aceitos, recusados, linhas) de um lote, na ordem em que os ids apareceram.

    O mesmo id duas vezes no lote: fica o de maior versão (empate: o último),
    e o id inteiro segue a sorte dele — aceito, com todos os repetidos, se ele
    for válido; recusado com o erro dele, se não. Sem isso o upsert recebia o
    id duas vezes e estourava com "cannot affect row a second time".
    """
    vencedor: dict[str, tuple] = {}   # id -> (versão para comparar, linha, erro)
    vagas: list = []                  # id (str) ou recusa de item sem id que preste (dict)
    for item in itens:
        if not isinstance(item, dict):
            vagas.append({"id": None, "erro": "item_invalido"})
            continue
        lid = item.get("id")
        if not (isinstance(lid, str) and _LANCAMENTO_ID_RE.fullmatch(lid)):
            vagas.append({"id": lid[:40] if isinstance(lid, str) else None, "erro": "id_invalido"})
            continue
        try:
            linha, erro = ler_lancamento(item, moeda_do_cadastro, hoje), ""
        except LancamentoRecusado as exc:
            linha, erro = None, str(exc)
        # Versão inválida perde de qualquer válida; >= faz o último vencer no empate.
        versao = _versao_do_lancamento(item.get("versao")) or 0
        if lid not in vencedor:
            vagas.append(lid)
            vencedor[lid] = (versao, linha, erro)
        elif versao >= vencedor[lid][0]:
            vencedor[lid] = (versao, linha, erro)

    aceitos, recusados, linhas = [], [], []
    for vaga in vagas:
        if isinstance(vaga, dict):
            recusados.append(vaga)
            continue
        _, linha, erro = vencedor[vaga]
        if linha is not None:
            aceitos.append(vaga)
            linhas.append(linha)
        else:
            recusados.append({"id": vaga, "erro": erro})
    return aceitos, recusados, linhas


def _codigo_do_erro(exc: Exception) -> str:
    """Classe e SQLSTATE do erro, sem a mensagem: a do Postgres pode citar a
    linha que falhou ("Failing row contains ..."), com valor e observação."""
    codigo = getattr(exc, "sqlstate", None)
    return f"{type(exc).__name__} ({codigo})" if codigo else type(exc).__name__


@app.post("/api/lancamentos")
@api_login_required
def api_lancamentos():
    """Recebe a cópia dos lançamentos do Caixa (app 1.8+), em lotes de até 100.

    Idempotente como /api/recibos: a fila do app reenvia o que ficou sem
    resposta, e a versão de cada lançamento decide quem vale. O item errado vai
    para `recusados` e os outros seguem; só lote malformado, teto do dia e
    banco fora do ar recusam o pedido inteiro, e aí nada é gravado.
    """
    lista = corpo_json().get("lancamentos")
    if not isinstance(lista, list) or not 1 <= len(lista) <= LANCAMENTOS_POR_LOTE:
        return jsonify({"erro": "lote_invalido", "maximo": LANCAMENTOS_POR_LOTE}), 400

    motorista = g.user["_id"]
    store = get_store()
    # O contador sobe pelo número de itens ANTES de conferir, como a estimativa
    # conta pedidos: lote cheio de lixo também gasta o teto.
    try:
        usados = store.bump_counter_by(f"lancamentos:{motorista}:{today_br()}", len(lista))
    except Exception as exc:
        app.logger.error("Caixa: contador fora do ar (lote de %s): %s", len(lista), _codigo_do_erro(exc))
        return jsonify({"erro": "indisponivel"}), 503
    if usados > LANCAMENTOS_DIARIOS:
        return jsonify({"erro": "muitos_lancamentos", "limite": LANCAMENTOS_DIARIOS}), 429

    aceitos, recusados, linhas = ler_lote_de_lancamentos(
        lista, g.user.get("currency") or MOEDA_PADRAO, datetime.now(BR_TZ).date())
    if linhas:
        try:
            store.upsert_ledger_entries(motorista, linhas)
        except Exception as exc:
            # O app guarda o lote como pendente e tenta de novo mais tarde.
            app.logger.error("Caixa: lote de %s não gravado (%s válidos): %s",
                             len(lista), len(linhas), _codigo_do_erro(exc))
            return jsonify({"erro": "indisponivel"}), 503
    if recusados:
        app.logger.warning("Caixa: %s aceito(s), %s recusado(s): %s", len(aceitos), len(recusados),
                           ", ".join(sorted({r["erro"] for r in recusados})))
    return jsonify({"aceitos": aceitos, "recusados": recusados})


# ── Estimativa de corrida ─────────────────────────────────────────────────────
#
# O app mostra uma FAIXA de preco, nunca um valor a cobrar: na cidade grande
# quem cobra e o taximetro aferido, e a Portaria Inmetro 124/2022 so admite
# taximetro ligado ao sensor do carro. Por isso o servidor devolve so a rota —
# km e minutos — e a conta com a tarifa acontece no aparelho, com a tabela que
# o proprio motorista digitou.
#
# A consulta passa por aqui, e nao sai direto do app, por tres motivos: a chave
# do Google nao pode morar dentro do app, o teto diario precisa de um lugar que
# o aparelho nao controla, e trocar de fornecedor nao pode exigir versao nova
# nas lojas.

class RotaIndisponivel(Exception):
    """O Google nao respondeu como devia: rede, cota, chave recusada."""


def _segundos_da_rota(valor) -> int:
    """A Routes API devolve duracao como texto: "1873s"."""
    try:
        return max(0, round(float(str(valor or "0").rstrip("s"))))
    except ValueError:
        return 0


def ler_rota(corpo: dict) -> dict | None:
    """Km e minutos da primeira rota da resposta. None quando nao ha rota.

    Sem rota, o Google responde sem a lista "routes". Com rota, a lista vem —
    e um campo zerado vem OMITIDO (JSON do proto3): a corrida de zero metros,
    com o motorista ja no destino, chega como {"routes": [{}]} e e uma rota
    de 0 km, nao um destino que nao existe.
    """
    rotas = (corpo or {}).get("routes") or []
    if not rotas or not isinstance(rotas[0], dict):
        return None
    metros = rotas[0].get("distanceMeters") or 0
    if not isinstance(metros, (int, float)) or isinstance(metros, bool) or metros < 0:
        return None
    segundos = _segundos_da_rota(rotas[0].get("duration"))
    # Sem a duracao sem transito, a diferenca vira zero: a faixa sai sem hora
    # parada, em vez de inventar uma.
    sem_transito = _segundos_da_rota(rotas[0].get("staticDuration")) or segundos
    return {
        "km": round(metros / 1000, 1),
        "minutos": round(segundos / 60),
        "minutos_sem_transito": round(sem_transito / 60),
    }


def endereco_do_destino(destino: str, cidade: str) -> str:
    """Poe a cidade no fim do destino quando o motorista nao pos.

    "Rua das Flores, 10" existe em centenas de cidades. Sem a cidade, o Google
    pode escolher outra, e a estimativa sai com centenas de km.
    """
    cidade = (cidade or "").strip()
    if not cidade or cidade.casefold() in destino.casefold():
        return destino
    return f"{destino}, {cidade}"


def consultar_rota(origem: dict, destino: str, timeout: int = 8) -> dict | None:
    """Rota de carro da origem ate o destino escrito, pela Routes API.

    `origem` e {"lat", "lng"}, do GPS do aparelho, ou {"endereco"}, digitado.
    Devolve o que `ler_rota` devolve, ou None quando o Google nao achou
    caminho ate o destino. Levanta RotaIndisponivel quando a falha nao e do
    destino.
    """
    if "lat" in origem:
        ponto = {"location": {"latLng": {"latitude": origem["lat"], "longitude": origem["lng"]}}}
    else:
        ponto = {"address": origem["endereco"]}

    corpo = {
        "origin": ponto,
        "destination": {"address": destino},
        "travelMode": "DRIVE",
        # Com transito: a diferenca para a duracao sem transito e o tempo que o
        # taximetro cobraria como hora parada.
        "routingPreference": "TRAFFIC_AWARE",
        "languageCode": "pt-BR",
        "regionCode": "BR",
        "units": "METRIC",
    }
    requisicao = urllib.request.Request(
        ROUTES_API_URL,
        data=json.dumps(corpo).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": os.environ.get("GOOGLE_MAPS_API_KEY", "").strip(),
            # Obrigatoria: sem a mascara a Routes API recusa o pedido.
            "X-Goog-FieldMask": "routes.distanceMeters,routes.duration,routes.staticDuration",
        },
    )

    try:
        with urllib.request.urlopen(requisicao, timeout=timeout) as resposta:
            return ler_rota(json.loads(resposta.read() or b"{}"))
    except urllib.error.HTTPError as exc:
        # So codigo, status e motivo vao para o log. A mensagem pode repetir o
        # endereco, e nem a posicao nem o destino ficam guardados.
        try:
            erro = json.loads(exc.read() or b"{}").get("error") or {}
        except (ValueError, AttributeError):
            erro = {}
        status = str(erro.get("status") or "")
        motivos = [str(d.get("reason") or "") for d in (erro.get("details") or []) if isinstance(d, dict)]
        mensagem = str(erro.get("message") or "").lower()
        if not culpa_do_destino(exc.code, status, motivos, mensagem):
            app.logger.error("Routes API falhou (HTTP %s %s %s)", exc.code, status, ",".join(motivos))
            raise RotaIndisponivel(f"HTTP {exc.code} {status}") from exc
        app.logger.warning("Routes API nao achou rota (HTTP %s %s)", exc.code, status)
        return None
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # OSError cobre URLError, timeout e conexao derrubada; HTTPException, a
        # resposta cortada no meio (IncompleteRead). Antes so os dois primeiros
        # eram tratados e o resto virava 500.
        app.logger.error("Routes API sem resposta: %s", type(exc).__name__)
        raise RotaIndisponivel(type(exc).__name__) from exc


def culpa_do_destino(codigo: int, status: str, motivos: list[str], mensagem: str) -> bool:
    """O erro do Google e do endereco pedido, e nao da nossa configuracao?

    O Google responde 400 tanto para endereco que nao entende quanto para
    chave invalida ou restrita (INVALID_ARGUMENT com reason API_KEY_*). Tratar
    todo 400 como "destino nao encontrado" escondia a chave errada: o
    motorista via "nao encontrei esse destino", o log so tinha um aviso e o
    teto diario era consumido.
    """
    if codigo == 404:
        return True
    if codigo != 400:
        return False
    if status in ("PERMISSION_DENIED", "UNAUTHENTICATED"):
        return False
    if any(m.startswith("API_KEY") or m.startswith("SERVICE_DISABLED") for m in motivos):
        return False
    return any(p in mensagem for p in ("address", "waypoint", "origin", "destination", "location"))


def _texto_curto(valor, maximo: int) -> str:
    """Espacos repetidos viram um so; o que nao for texto vira vazio.

    Lista ou dicionario no lugar do texto viravam "['a', 'b']" e seguiam para
    o Google como endereco.
    """
    if not isinstance(valor, str):
        return ""
    texto = re.sub(r"\s+", " ", valor).strip()
    return texto if len(texto) <= maximo else ""


def _coordenada(valor, limite: float) -> float | None:
    """Numero dentro de [-limite, limite], ou None.

    Booleano fica de fora de proposito: float(True) e 1.0, e {"lat": true}
    virava uma origem no golfo da Guine.
    """
    if isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if -limite <= numero <= limite else None


@app.post("/api/estimativa")
@api_login_required
def api_estimativa():
    """Distancia e tempo da corrida, para o app estimar o valor.

    A origem vem como coordenada, do GPS do aparelho, ou como texto, quando o
    motorista negou a localizacao. Nada disto e gravado: nem a posicao nem o
    destino vao para o banco ou para o log. O unico rastro e o contador do
    teto diario.
    """
    dados = corpo_json()

    destino = _texto_curto(dados.get("destino"), RECEIPT_FIELD_LIMITS["destino"][1])
    if len(destino) < 3:
        return jsonify({"erro": "destino_invalido"}), 400

    origem = None
    lat, lng = _coordenada(dados.get("lat"), 90), _coordenada(dados.get("lng"), 180)
    if lat is not None and lng is not None:
        origem = {"lat": lat, "lng": lng}
    if origem is None:
        texto = _texto_curto(dados.get("origem"), RECEIPT_FIELD_LIMITS["origem"][1])
        if len(texto) >= 3:
            origem = {"endereco": texto}
    if origem is None:
        return jsonify({"erro": "origem_invalida"}), 400

    if not os.environ.get("GOOGLE_MAPS_API_KEY", "").strip():
        return jsonify({"erro": "indisponivel"}), 503

    usadas = get_store().bump_counter(f"estimativa:{g.user['_id']}:{today_br()}")
    if usadas > ESTIMATIVA_DIARIA_LIMITE:
        return jsonify({"erro": "limite_diario", "limite": ESTIMATIVA_DIARIA_LIMITE}), 429
    # "todos" nao e id de motorista: o painel, que junta a chave com drivers,
    # ignora este contador sozinho.
    if get_store().bump_counter(f"estimativa:todos:{today_br()}") > ESTIMATIVA_DIARIA_GLOBAL:
        app.logger.error("Estimativa: teto global de %s por dia atingido", ESTIMATIVA_DIARIA_GLOBAL)
        return jsonify({"erro": "indisponivel_hoje"}), 429

    # A cidade de onde o aparelho esta vence a do cadastro: o motorista pode
    # estar rodando fora da cidade em que se cadastrou.
    cidade = (_texto_curto(dados.get("cidade"), SIGNUP_FIELD_LIMITS["cidade"][1])
              or g.user.get("city", ""))
    try:
        rota = consultar_rota(origem, endereco_do_destino(destino, cidade))
    except RotaIndisponivel:
        return jsonify({"erro": "falha_na_consulta"}), 502
    if not rota:
        return jsonify({"erro": "destino_nao_encontrado"}), 404
    return jsonify(rota)


# ── Webhook do RevenueCat (assinatura pelo app) ───────────────────────────────
#
# Três fontes podem conceder o Pro: Stripe (site), App Store e Google Play
# (app). Todas convergem para a mesma coluna `plan` — o app e o site só olham
# para ela, e nunca precisam saber de onde a assinatura veio.

# Eventos que concedem acesso, e os que tiram. TRANSFER fica fora dos dois:
# ele mexe em duas contas e vem sem app_user_id (ver transferir_assinatura_rc).
RC_EVENTOS_ATIVA = {
    "INITIAL_PURCHASE", "RENEWAL", "UNCANCELLATION",
    "PRODUCT_CHANGE", "SUBSCRIPTION_EXTENDED",
}
RC_EVENTOS_ENCERRA = {"EXPIRATION", "SUBSCRIPTION_PAUSED"}
RC_LOJAS = {"APP_STORE": "App Store (iPhone)", "PLAY_STORE": "Google Play (Android)"}
# Nome curto da loja para o painel. A chave vem do RevenueCat (`store`) ou e
# "STRIPE", que o webhook da Stripe grava por conta propria.
LOJAS_NOME = {"APP_STORE": "App Store", "PLAY_STORE": "Google Play", "STRIPE": "Stripe (site)",
              "MAC_APP_STORE": "Mac App Store", "AMAZON": "Amazon", "PROMOTIONAL": "Promocional"}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def nome_da_loja(store: str, source: str = "") -> str:
    chave = (store or "").upper()
    return LOJAS_NOME.get(chave) or chave or ("RevenueCat" if source == "revenuecat" else source)


def _decimal_ou_none(valor):
    try:
        n = Decimal(str(valor))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return n if n.is_finite() else None


def registrar_evento_cobranca(source: str, tipo: str, outcome: str, *, event_id: str = "",
                              seq: int = 0, driver_id: str | None = None, app_user_id: str = "",
                              store: str = "", environment: str = "", product_id: str = "",
                              cycle: str = "", price=None, currency: str = "",
                              payload: dict | None = None) -> None:
    """Guarda o webhook como chegou e o que o servidor fez com ele (migracao 0011).

    Nunca derruba o webhook: se a tabela ainda nao existe ou o banco falhou, o
    plano do motorista ja mudou e a origem recebe o 200 do mesmo jeito — senao
    ela reenviaria para sempre. Reenvio do mesmo evento (mesmo id e posicao)
    cai no indice unico e nao duplica.
    """
    try:
        with get_store().pool.connection() as conn:
            conn.execute(
                """insert into public.billing_events
                     (source, event_id, seq, type, driver_id, app_user_id, store, environment,
                      product_id, cycle, price, currency, outcome, payload)
                   values (%(source)s, %(event_id)s, %(seq)s, %(type)s, %(driver_id)s, %(app_user_id)s,
                           %(store)s, %(environment)s, %(product_id)s, %(cycle)s, %(price)s,
                           %(currency)s, %(outcome)s, %(payload)s)
                   on conflict (source, event_id, seq) where event_id <> '' do nothing""",
                {
                    "source": source, "event_id": str(event_id or "")[:200], "seq": seq,
                    "type": str(tipo or "")[:80], "driver_id": driver_id,
                    "app_user_id": str(app_user_id or "")[:200], "store": str(store or "")[:40],
                    "environment": str(environment or "")[:20], "product_id": str(product_id or "")[:200],
                    "cycle": str(cycle or "")[:10], "price": _decimal_ou_none(price),
                    "currency": str(currency or "")[:10].upper(), "outcome": str(outcome or "")[:200],
                    "payload": Json(payload if isinstance(payload, dict) else {}),
                })
    except Exception as exc:
        app.logger.error("Evento de cobrança não registrado (%s %s): %s", source, tipo, exc)


def motorista_por_app_user_id(valor: str) -> dict | None:
    """Motorista por um App User ID do RevenueCat.

    Com cara de uuid, e o proprio id do motorista (o app faz logIn com ele).
    Sem, e um id anonimo do SDK ($RCAnonymousID:...); ai vale o que o registro
    de eventos ja aprendeu: um anonimo que chegou com o motorista em `aliases`
    ficou ligado a ele.
    """
    valor = str(valor or "").strip()
    if not valor:
        return None
    store = get_store()
    if _UUID_RE.match(valor):
        return store.get_user_by_id(valor)
    try:
        with store.pool.connection() as conn:
            linha = conn.execute(
                """select driver_id from public.billing_events
                    where app_user_id = %s and driver_id is not null
                    order by received_at desc limit 1""", (valor,)).fetchone()
    except Exception:
        return None  # migracao 0011 ainda nao aplicada
    return store.get_user_by_id(str(linha["driver_id"])) if linha else None


def motorista_do_evento_rc(evento: dict) -> tuple[dict | None, str]:
    """(motorista, id que bateu) ou (None, "").

    O SDK nasce anonimo e so depois o app faz logIn; reinstalado o app, a
    compra pode ficar num anonimo que o RevenueCat nao juntou a conta. Por
    isso ele manda app_user_id, original_app_user_id e aliases, e recomenda
    procurar em todos. Antes so o primeiro era olhado, e a compra virava
    "motorista desconhecido" com o id do motorista logo ali em aliases.
    """
    aliases = evento.get("aliases")
    candidatos = [evento.get("app_user_id"), evento.get("original_app_user_id"),
                  *(aliases if isinstance(aliases, list) else [])]
    vistos: set = set()
    for valor in candidatos:
        valor = str(valor or "").strip()
        if not valor or valor.lower() in vistos:
            continue
        vistos.add(valor.lower())
        usuario = motorista_por_app_user_id(valor)
        if usuario:
            return usuario, valor
    return None, ""


def compra_cita_desconto_antigo(evento: dict) -> bool:
    """A compra na loja usou o desconto de indicação encerrado em 2026-11-10.

    Apple: `offer_code` traz o código resgatado. Google: a oferta aparece no
    identificador do produto, em `offer_code` ou em `discount_identifier`.
    """
    textos = " ".join(str(evento.get(k) or "") for k in ("offer_code", "product_id", "discount_identifier")).lower()
    return "indica70" in textos or "indicacao70" in textos


def pro_vem_da_stripe(usuario: dict) -> bool:
    """Assinatura ativa na Stripe. Evento da loja nao mexe nela: sao
    assinaturas independentes (Guideline 3.1.3b).

    O customer_id sozinho nao conta: ele fica depois do cancelamento, e um
    ex-assinante do site que passou a assinar pelo app precisa ser rebaixado
    quando a loja mandar o EXPIRATION — antes ele ficava Pro para sempre.
    """
    return bool(usuario.get("stripe_subscription_id"))


def ativar_pro_pela_loja(user_id: str) -> None:
    """Pro pela loja.

    O id da Stripe só fica se a assinatura de lá estiver viva (quem assina nos
    dois lugares não pode ter a da Stripe esquecida pelo servidor). Id de
    assinatura já encerrada ou perdida é apagado: pro_vem_da_stripe olha só a
    presença do id, e um id obsoleto seguraria o Pro para sempre quando a
    loja expirasse."""
    store = get_store()
    usuario = store.get_user_by_id(user_id) or {}
    updates: dict = {"plan": "pro", "subscription_status": "active"}
    if not (usuario.get("stripe_subscription_id")
            and usuario.get("subscription_status") in ACTIVE_SUBSCRIPTION_STATUSES):
        updates["stripe_subscription_id"] = None
    store.update_user(user_id, updates)


def _registrar_rc(evento: dict, outcome: str, *, driver_id: str | None = None,
                  app_user_id: str = "", seq: int = 0) -> None:
    produto = str(evento.get("product_id") or "")
    registrar_evento_cobranca(
        "revenuecat", str(evento.get("type") or ""), outcome,
        event_id=str(evento.get("id") or ""), seq=seq, driver_id=driver_id,
        app_user_id=app_user_id, store=str(evento.get("store") or ""),
        environment=str(evento.get("environment") or "").upper(), product_id=produto,
        cycle=ciclo_do_produto(produto) if produto else "",
        price=evento.get("price_in_purchased_currency"), currency=str(evento.get("currency") or ""),
        payload=evento)


def transferir_assinatura_rc(evento: dict) -> dict:
    """TRANSFER: a assinatura passou de um App User ID para outro.

    Acontece quando alguem toca em "Restaurar compras" logado em outra conta:
    a loja so reconhece um dono por recibo, e o RevenueCat move a assinatura.
    O evento traz transferred_from e transferred_to, e o app_user_id pode nem
    vir. Antes ele caia no caminho comum e era ignorado — quem recebeu nao
    virava Pro e quem perdeu continuava Pro.
    """
    def motoristas(chave: str) -> dict:
        ids = evento.get(chave)
        achados: dict = {}
        for valor in (ids if isinstance(ids, list) else []):
            usuario = motorista_por_app_user_id(str(valor or ""))
            if usuario:
                achados[usuario["_id"]] = usuario
        return achados

    recebem = motoristas("transferred_to")
    perdem = {k: u for k, u in motoristas("transferred_from").items() if k not in recebem}

    if not recebem:
        # Destino desconhecido: quase sempre um App User ID anonimo de um
        # aparelho recem-instalado, antes do logIn (o SDK ja sincroniza o
        # recibo da loja). Quem "perdeu" continua sendo cobrado pela loja,
        # entao continua Pro; o evento seguinte do anonimo, com aliases, ou o
        # TRANSFER de volta acertam o resto. Rebaixar aqui tiraria o Pro de
        # quem esta pagando.
        destinos = evento.get("transferred_to")
        _registrar_rc(evento, "ignorado: destino_desconhecido",
                      app_user_id=", ".join(str(d) for d in (destinos if isinstance(destinos, list) else [])))
        for seq, usuario in enumerate(perdem.values(), start=1):
            _registrar_rc(evento, "mantido (destino desconhecido)", driver_id=usuario["_id"],
                          app_user_id=usuario["_id"], seq=seq)
        return {"receberam": [], "perderam": [], "mantidos": list(perdem)}

    feitos: list[tuple[dict, str]] = []
    for usuario in recebem.values():
        ativar_pro_pela_loja(usuario["_id"])
        feitos.append((usuario, "pro (recebeu a assinatura)"))
    for usuario in perdem.values():
        if pro_vem_da_stripe(usuario):
            feitos.append((usuario, "mantido_stripe (a da loja foi para outra conta)"))
        elif pro_prepago_vigente(usuario):
            get_store().update_user(usuario["_id"], {"subscription_status": status_prepago_ao_encerrar(usuario)})
            feitos.append((usuario, "mantido_prepago (a da loja foi para outra conta)"))
        else:
            get_store().update_user(usuario["_id"], {"plan": "free", "subscription_status": "transferred"})
            feitos.append((usuario, "free (a assinatura foi para outra conta)"))
    for seq, (usuario, outcome) in enumerate(feitos):
        _registrar_rc(evento, outcome, driver_id=usuario["_id"], app_user_id=usuario["_id"], seq=seq)
    return {"receberam": list(recebem), "perderam": list(perdem)}


@app.post("/webhook/revenuecat")
def webhook_revenuecat():
    """Recebe mudanças de assinatura feitas dentro do app."""
    segredo = os.environ.get("REVENUECAT_WEBHOOK_SECRET", "").strip()
    if not segredo:
        app.logger.warning("REVENUECAT_WEBHOOK_SECRET ausente — webhook desativado.")
        abort(503)

    # O painel do RevenueCat manda o campo "Authorization header value" como
    # veio: com "Bearer " na frente ou sem, com espaco sobrando ou nao. Exigir
    # a forma exata custou um 401 em producao no primeiro evento de verdade.
    # Aceitar as duas formas nao afrouxa nada: o segredo continua sendo
    # comparado inteiro, em tempo constante.
    enviado = request.headers.get("Authorization", "").strip()
    if enviado[:7].lower() == "bearer ":
        enviado = enviado[7:].strip()
    if not hmac.compare_digest(enviado.encode("utf-8", "replace"), segredo.encode("utf-8")):
        abort(401)

    evento = corpo_json().get("event")
    evento = evento if isinstance(evento, dict) else {}
    tipo = str(evento.get("type", ""))

    # Compra de teste (sandbox: TestFlight, teste interno do Play e a própria
    # revisão da Apple) não vira Pro de verdade. Mas ignorar também não serve:
    # o revisor da Apple compra em sandbox e precisa ver o Pro ativo na hora.
    # Então a compra de sandbox vira Pro de cortesia só até a expiração que o
    # próprio evento traz (assinatura de sandbox dura minutos); o cron rebaixa.
    # As contas de teste em RC_SANDBOX_PERMITIDOS seguem o fluxo completo.
    if str(evento.get("environment") or "").upper() == "SANDBOX" and tipo not in ("TEST", "TRANSFER"):
        usuario_sb, _ = motorista_do_evento_rc(evento)
        permitidos = {e.strip().lower() for e in os.environ.get("RC_SANDBOX_PERMITIDOS", "").split(",") if e.strip()}
        if not usuario_sb or normalize_email(usuario_sb.get("email", "")) not in permitidos:
            resultado_sb = "ignorado: sandbox"
            if usuario_sb and tipo in RC_EVENTOS_ATIVA and not pro_vem_da_stripe(usuario_sb) \
                    and usuario_sb.get("subscription_status") not in ACTIVE_SUBSCRIPTION_STATUSES:
                fim_ms = evento.get("expiration_at_ms")
                fim = datetime.fromtimestamp(int(fim_ms) / 1000, timezone.utc) if fim_ms else \
                    datetime.now(timezone.utc) + timedelta(hours=1)
                fim = min(fim, datetime.now(timezone.utc) + timedelta(days=1))
                get_store().update_user(usuario_sb["_id"], {
                    "plan": "pro", "subscription_status": "cortesia", "pro_until": to_utc_iso(fim)})
                resultado_sb = f"sandbox: cortesia até {fim.isoformat()}"
            _registrar_rc(evento, resultado_sb, driver_id=(usuario_sb or {}).get("_id"),
                          app_user_id=str(evento.get("app_user_id") or ""))
            return jsonify({"ok": True, "ignorado": "sandbox", "resultado": resultado_sb})

    if tipo == "TRANSFER":
        return jsonify({"ok": True, "evento": tipo, "resultado": transferir_assinatura_rc(evento)})

    app_user_id = str(evento.get("app_user_id") or "").strip()
    usuario, id_batido = motorista_do_evento_rc(evento)
    if not usuario:
        motivo = "motorista_desconhecido" if app_user_id else "sem_app_user_id"
        if app_user_id:
            app.logger.warning("RevenueCat: motorista %s não encontrado.", app_user_id)
        _registrar_rc(evento, f"ignorado: {motivo}", app_user_id=app_user_id)
        return jsonify({"ok": True, "ignorado": motivo})

    store = get_store()
    user_id = usuario["_id"]
    if tipo == "CANCELLATION":
        # Cancelou, mas segue com acesso até o fim do período pago. Quem tira o
        # acesso é o EXPIRATION, depois.
        store.update_user(user_id, {"subscription_status": "canceled_pendente"})
        resultado = "cancelamento_agendado"

    elif tipo in RC_EVENTOS_ATIVA:
        ativar_pro_pela_loja(user_id)
        resultado = "pro"
        # Enquanto o código INDICA70 (Apple) e a oferta indicacao70 (Google) não
        # forem desativados nos consoles, uma compra com eles ainda pode passar:
        # quem tinha o cupom antigo do site gasta-o aqui, para não usar duas vezes.
        if tem_cupom_de_indicacao(usuario) and compra_cita_desconto_antigo(evento):
            store.update_user(user_id, {"referral_coupon_used_at": now_iso()})
            resultado += " (cupom de indicação antigo)"
        if tipo == "INITIAL_PURCHASE":
            loja = str(evento.get("store") or "")
            avisar_nova_assinatura(
                usuario, ciclo_do_produto(str(evento.get("product_id") or "")),
                RC_LOJAS.get(loja, loja or "app"),
                valor_da_compra(evento.get("price_in_purchased_currency"), evento.get("currency")),
                teste=str(evento.get("environment") or "").upper() == "SANDBOX",
                evento_id=str(evento.get("id") or ""))

    elif tipo in RC_EVENTOS_ENCERRA:
        if pro_vem_da_stripe(usuario):
            resultado = "mantido_stripe"
        elif pro_prepago_vigente(usuario):
            # Dias pré-pagos guardados (anual por Pix ou mês de indicação)
            # entram em vigor quando a assinatura da loja acaba.
            store.update_user(user_id, {"subscription_status": status_prepago_ao_encerrar(usuario)})
            resultado = "mantido_prepago"
        else:
            store.update_user(user_id, {"plan": "free", "subscription_status": tipo.lower()})
            resultado = "free"

    else:
        # BILLING_ISSUE (periodo de carencia), TEST (botao do painel do
        # RevenueCat) e o que mais vier: fica registrado, sem mexer no plano.
        resultado = "sem_efeito"

    if id_batido and id_batido.lower() != app_user_id.lower():
        resultado += " (pelo alias)"
    _registrar_rc(evento, resultado, driver_id=user_id, app_user_id=app_user_id or id_batido)
    return jsonify({"ok": True, "evento": tipo, "resultado": resultado})


# ── Manutenção ────────────────────────────────────────────────────────────────

def exigir_cron() -> None:
    """Só o Cron da Vercel passa. Sem CRON_SECRET a rota fica fechada (503)."""
    secret = os.environ.get("CRON_SECRET", "").strip()
    if not secret:
        app.logger.warning("CRON_SECRET não definido — %s desativada.", request.path)
        abort(503)

    # compare_digest levanta TypeError com str não-ASCII; comparar bytes evita
    # que um header esquisito vire 500 em vez de 401.
    enviado = request.headers.get("Authorization", "").encode("utf-8", "replace")
    if not hmac.compare_digest(enviado, f"Bearer {secret}".encode("utf-8")):
        abort(401)


@app.get("/tarefas/limpeza")
def tarefa_limpeza():
    """Apaga recibos sem conta (do antigo gerador público) vencidos, contadores antigos
    e lançamentos do Caixa apagados há mais de LANCAMENTO_APAGADO_DIAS.

    Chamada pelo Cron da Vercel, que envia 'Authorization: Bearer $CRON_SECRET'.
    """
    exigir_cron()

    store = get_store()
    now = datetime.now(timezone.utc)
    receipts_removed, receipts_left = store.purge_guest_receipts_before(
        to_utc_iso(now - timedelta(days=GUEST_RETENTION_DAYS)), CLEANUP_BATCH_LIMIT
    )
    counters_removed, counters_left = store.purge_counters_before(
        to_utc_iso(now - timedelta(days=COUNTER_RETENTION_DAYS)), CLEANUP_BATCH_LIMIT
    )
    try:
        acessos_removidos = store.purge_access_logs_before(
            to_utc_iso(now - timedelta(days=ACCESS_LOG_RETENTION_DAYS)))
    except Exception as exc:  # migração 0016 ainda não aplicada
        app.logger.warning("limpeza de access_logs pulada: %s", exc)
        acessos_removidos = 0
    # Caixa (migração 0022): o lançamento apagado no app fica como túmulo e sai
    # de vez em 30 dias, como a Política de Privacidade promete.
    try:
        lancamentos_removidos, lancamentos_left = store.purge_deleted_ledger_before(
            to_utc_iso(now - timedelta(days=LANCAMENTO_APAGADO_DIAS)), CLEANUP_BATCH_LIMIT)
    except Exception as exc:  # migração 0022 ainda não aplicada
        app.logger.warning("limpeza de lançamentos apagados pulada: %s", _codigo_do_erro(exc))
        lancamentos_removidos, lancamentos_left = 0, False
    # Pro pré-pago (migração 0012): quem passou da data volta ao Grátis, e quem
    # pagou o anual recebe o aviso de renovação 15 dias antes. Falha aqui não
    # derruba a limpeza: vira log, e amanhã o cron tenta de novo.
    pro_expirados = lembretes = 0
    try:
        pro_expirados = expirar_pro_prepago()
        lembretes = lembrar_vencimento_prepago()
    except Exception as exc:
        app.logger.error("Pro pré-pago: expiração/lembretes falharam: %s", exc)

    incompleto = receipts_left or counters_left or lancamentos_left
    app.logger.info(
        "Limpeza: %s recibos sem cadastro, %s contadores e %s lançamentos apagados removidos.%s",
        receipts_removed,
        counters_removed,
        lancamentos_removidos,
        " Ainda há backlog — a próxima execução continua." if incompleto else "",
    )
    return jsonify(
        {
            "ok": True,
            "recibos_removidos": receipts_removed,
            "contadores_removidos": counters_removed,
            "acessos_removidos": acessos_removidos,
            "lancamentos_removidos": lancamentos_removidos,
            "pro_expirados": pro_expirados,
            "lembretes_enviados": lembretes,
            "backlog_restante": incompleto,
        }
    )


# ── Automação de e-mails ──────────────────────────────────────────────────────
#
# Régua de e-mails para quem tem conta e ainda está no Grátis. Cada modelo
# (email_templates) é um e-mail com a sua programação; o cron /tarefas/emails
# roda de hora em hora, acha quem está na vez de cada modelo e manda.
#
# Garantias, da mais importante para a menos:
#   1. Nunca o mesmo e-mail duas vezes: a linha de email_sends nasce antes do
#      envio, com unique (modelo, motorista, dedupe_key).
#   2. Assinou, parou: a consulta só pega plan = 'free' na hora do envio.
#   3. Descadastrou, parou: link no rodapé e List-Unsubscribe de um clique.
#   4. No máximo um e-mail por motorista a cada N horas (email_settings).
#   5. Teto diário somando todos os motoristas, para não tomar a cota do
#      Resend que o e-mail de senha e o de recibo também usam.

EMAIL_GATILHOS = {
    # chave: (rótulo no painel, unidade do número)
    "signup": ("Dias depois do cadastro", "dias"),
    "no_receipt": ("Dias depois do cadastro, se ainda não emitiu nenhum recibo", "dias"),
    "monthly_usage": ("Quando chegar a N recibos no mês (uma vez por mês)", "recibos"),
    "inactive": ("Dias sem emitir recibo (só quem já emitiu algum)", "dias"),
    "scheduled": ("Campanha em data marcada", "dias de cadastro, no mínimo"),
}

EMAIL_VARIAVEIS = {
    "nome": "Primeiro nome do motorista",
    "cidade": "Cidade do cadastro",
    "recibos_mes": "Recibos emitidos neste mês",
    "restantes": "Recibos grátis que ainda restam no mês",
    "recibos_total": "Recibos emitidos desde o cadastro",
    "limite": f"Recibos por mês no Grátis (hoje {FREE_MONTHLY_LIMIT})",
    "mes": "Nome do mês atual",
    "preco": f"Preço do Pro mensal ({PRECO_PRO_MENSAL})",
    "link_app": "Abre a loja do app certa para o celular (App Store ou Google Play)",
    "link_planos": "Página de planos no site",
    "link_painel": "Painel do motorista no site",
    "link_iphone": "App na App Store",
    "link_android": "App no Google Play",
}

MESES_PT = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
            "agosto", "setembro", "outubro", "novembro", "dezembro")

_VARIAVEL_RE = re.compile(r"\{\{\s*([A-Za-z_]+)\s*\}\}")
_TZ_SQL = "'America/Sao_Paulo'"


def descrever_gatilho(modelo: dict) -> str:
    """Frase curta para a lista do painel: "3 dias depois do cadastro, a partir das 10h"."""
    n = int(modelo.get("trigger_value") or 0)
    hora = f"a partir das {int(modelo.get('send_hour') or 0)}h"
    gatilho = modelo.get("trigger")
    if gatilho == "signup":
        quando = "no dia do cadastro" if n == 0 else f"{n} dia{'s' if n != 1 else ''} depois do cadastro"
        return f"{quando}, {hora}"
    if gatilho == "no_receipt":
        return f"{n} dia{'s' if n != 1 else ''} depois do cadastro, sem nenhum recibo, {hora}"
    if gatilho == "monthly_usage":
        return f"ao chegar a {n} recibo{'s' if n != 1 else ''} no mês, {hora}"
    if gatilho == "inactive":
        return f"{n} dia{'s' if n != 1 else ''} sem emitir recibo, {hora}"
    if gatilho == "scheduled":
        quando = modelo.get("scheduled_at")
        data = filtro_data_br(quando) if quando else "data não marcada"
        return f"campanha em {data}" + (f", para quem tem {n}+ dias de cadastro" if n else "")
    return gatilho or "-"


def variaveis_desconhecidas(*textos: str) -> list[str]:
    return sorted({v for t in textos for v in _VARIAVEL_RE.findall(t or "") if v not in EMAIL_VARIAVEIS})


def _preencher(texto: str, valores: dict) -> str:
    return _VARIAVEL_RE.sub(lambda m: str(valores.get(m.group(1), m.group(0))), texto or "")


def _primeiro_nome(nome_completo: str) -> str:
    primeiro = (nome_completo or "").strip().split(" ")[0] if (nome_completo or "").strip() else ""
    # Cadastro todo em maiúsculas (ou minúsculas) vira "Carlos", não "CARLOS".
    if primeiro.isupper() or primeiro.islower():
        primeiro = primeiro.capitalize()
    return primeiro or "motorista"


def valores_do_email(motorista: dict, agora: datetime | None = None) -> dict:
    """Valores das {{variáveis}} para um motorista."""
    agora_br = (agora or datetime.now(timezone.utc)).astimezone(BR_TZ)
    recibos_mes = int(motorista.get("recibos_mes") or 0)
    return {
        "nome": _primeiro_nome(motorista.get("full_name", "")),
        "cidade": (motorista.get("city") or "").strip() or "sua cidade",
        "recibos_mes": recibos_mes,
        "restantes": max(FREE_MONTHLY_LIMIT - recibos_mes, 0),
        "recibos_total": int(motorista.get("recibos_total") or 0),
        "limite": FREE_MONTHLY_LIMIT,
        "mes": MESES_PT[agora_br.month - 1],
        "preco": PRECO_PRO_MENSAL,
        "link_app": absolute_url("baixar_app", c="email"),
        "link_planos": absolute_url("planos"),
        "link_painel": absolute_url("dashboard"),
        "link_iphone": APP_STORE_URL,
        "link_android": PLAY_STORE_URL,
    }


# Motorista de mentira para a prévia e para o e-mail de teste do painel.
MOTORISTA_EXEMPLO = {"full_name": "Carlos Pereira", "city": "Cascavel", "recibos_mes": 4, "recibos_total": 11}


def _formatar_linha(texto: str) -> str:
    """Escapa e aplica o pouco de formatação aceito: **negrito** e links."""
    s = str(escape(texto))
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return re.sub(r"(https?://[^\s<]+)", r'<a href="\1" style="color:#c2402f;">\1</a>', s)


def corpo_em_html(corpo: str) -> Markup:
    """Texto do modelo em HTML: linha em branco separa parágrafo, "- " vira lista."""
    estilo_p = "margin:0 0 16px 0;"
    partes = []
    for bloco in re.split(r"\n\s*\n", (corpo or "").strip()):
        paragrafo, itens = [], []

        def fechar_paragrafo():
            if paragrafo:
                partes.append(f'<p style="{estilo_p}">' + "<br>".join(paragrafo) + "</p>")
                paragrafo.clear()

        def fechar_lista():
            if itens:
                partes.append('<ul style="margin:0 0 16px 0;padding-left:22px;">'
                              + "".join(f'<li style="margin:0 0 6px 0;">{i}</li>' for i in itens) + "</ul>")
                itens.clear()

        for linha in bloco.split("\n"):
            linha = linha.rstrip()
            if linha.lstrip().startswith("- "):
                fechar_paragrafo()
                itens.append(_formatar_linha(linha.lstrip()[2:]))
            elif linha.strip():
                fechar_lista()
                paragrafo.append(_formatar_linha(linha.strip()))
        fechar_paragrafo()
        fechar_lista()
    return Markup("".join(partes))


def token_descadastro(driver_id: str) -> str:
    # Sem validade de propósito: link de descadastro tem que funcionar no
    # e-mail de meses atrás.
    return URLSafeSerializer(app.config["SECRET_KEY"], salt=DESCADASTRO_SALT).dumps(str(driver_id))


def ler_token_descadastro(token: str) -> str | None:
    try:
        return str(URLSafeSerializer(app.config["SECRET_KEY"], salt=DESCADASTRO_SALT).loads(token))
    except Exception:
        return None


def montar_email_automacao(modelo: dict, valores: dict, link_sair: str) -> dict:
    """Assunto, HTML e texto puro prontos para o envio."""
    assunto = " ".join(_preencher(modelo["subject"], valores).split())
    corpo = _preencher(modelo["body"], valores)
    botao_texto = _preencher(modelo.get("button_text") or "", valores).strip()
    botao_link = _preencher(modelo.get("button_url") or "", valores).strip()
    if not (botao_texto and botao_link):
        botao_texto = botao_link = ""

    # Primeira frase depois da saudação: é o que o Gmail mostra ao lado do assunto.
    paragrafos = [p for p in re.split(r"\n\s*\n", corpo.strip()) if p.strip()]
    resumo = paragrafos[1] if len(paragrafos) > 1 else (paragrafos[0] if paragrafos else "")
    resumo = " ".join(resumo.replace("**", "").split())[:140]

    html = render_template(
        "emails/automacao.html",
        assunto=assunto, resumo=resumo, corpo=corpo_em_html(corpo),
        botao_texto=botao_texto, botao_link=botao_link, link_sair=link_sair,
        link_iphone=APP_STORE_URL, link_android=PLAY_STORE_URL, assinatura=EMAIL_ASSINATURA,
    )
    texto = corpo.replace("**", "")
    if botao_texto:
        texto += f"\n\n{botao_texto}: {botao_link}"
    texto += f"\n\n{EMAIL_ASSINATURA}"
    texto += (
        "\n\n—\n"
        f"Você recebe este e-mail porque tem cadastro no {APP_NAME}.\n"
        f"Para não receber mais: {link_sair}\n"
    )
    return {"assunto": assunto, "html": html, "texto": texto, "link_sair": link_sair}


def remetente_automacao() -> str:
    return os.environ.get("EMAIL_FROM_AUTOMACAO", "").strip() or remetente_padrao()


def endereco_de_resposta() -> str:
    """Para onde vão as respostas. Sem isto elas voltam para o nao-responda@,
    que não tem caixa — e vários modelos pedem "responda este e-mail"."""
    return os.environ.get("EMAIL_REPLY_TO", "").strip()


def enviar_email_automacao(destino: str, msg: dict, idempotency_key: str = "") -> tuple[bool, str, str, bool]:
    payload = {
        "from": f"{APP_NAME} <{remetente_automacao()}>",
        "to": [destino],
        "subject": msg["assunto"],
        "html": msg["html"],
        "text": msg["texto"],
        # Descadastro de um clique (RFC 8058): o Gmail e o Yahoo mostram o botão
        # "Cancelar inscrição" ao lado do remetente e fazem o POST sozinhos.
        "headers": {
            "List-Unsubscribe": f"<{msg['link_sair']}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        "tags": [{"name": "categoria", "value": "automacao"}],
    }
    if endereco_de_resposta():
        payload["reply_to"] = endereco_de_resposta()
    return chamar_resend(payload, timeout=10, idempotency_key=idempotency_key)


def config_email(conn) -> dict:
    row = conn.execute(
        "select enabled, daily_cap, min_gap_hours, updated_at from public.email_settings where id"
    ).fetchone()
    return dict(row) if row else {"enabled": False, "daily_cap": 0, "min_gap_hours": 24, "updated_at": None}


def _sql_email_aceito(coluna: str) -> str:
    # Os sufixos são constantes do código, nunca dado de fora.
    return " and ".join(f"lower({coluna}) not like '%%{sufixo}'" for sufixo in EMAIL_AUTO_SUFIXOS_EXCLUIDOS)


def _sql_candidatos(gatilho: str, restringir: bool) -> str:
    """SELECT de quem está na vez de um modelo. Tudo que vem do pedido é parâmetro."""
    def vence(base: str) -> str:
        # Dia (em Brasília) do evento + N dias + a hora do modelo.
        return (f"((date_trunc('day', {base} at time zone {_TZ_SQL}) "
                f"+ make_interval(days => %(n)s, hours => %(hora)s)) at time zone {_TZ_SQL})")

    janela = "make_interval(days => %(janela)s)"
    chave = "''"
    if gatilho in ("signup", "no_receipt"):
        v = vence("d.created_at")
        condicao = f"{v} <= %(agora)s and {v} > %(agora)s - {janela}"
        if gatilho == "no_receipt":
            condicao += " and rc.total = 0"
    elif gatilho == "inactive":
        v = vence("rc.ultimo")
        condicao = f"rc.ultimo is not null and {v} <= %(agora)s and {v} > %(agora)s - {janela}"
        # Volta a valer se ele emitir de novo e parar outra vez.
        chave = f"to_char(rc.ultimo at time zone {_TZ_SQL}, 'YYYY-MM-DD')"
    elif gatilho == "monthly_usage":
        # Só o modelo de maior N que ele já alcançou, entre os desta passada:
        # quem chegou a 6 recebe o "limite atingido", e não também o "faltam 0".
        condicao = f"""coalesce(q.used, 0) >= %(n)s
            and extract(hour from %(agora)s at time zone {_TZ_SQL}) >= %(hora)s
            and not exists (select 1 from public.email_templates t2
                             where t2.id = any(%(passada)s::uuid[]) and t2.trigger = 'monthly_usage'
                               and t2.trigger_value > %(n)s and coalesce(q.used, 0) >= t2.trigger_value)"""
        chave = "to_char(public.br_period(%(agora)s), 'YYYY-MM')"
    elif gatilho == "scheduled":
        condicao = (f"%(agendado)s::timestamptz <= %(agora)s and %(agendado)s::timestamptz > %(agora)s - {janela}"
                    " and d.created_at <= %(agora)s - make_interval(days => %(n)s)")
    else:
        raise ValueError(f"gatilho desconhecido: {gatilho}")

    quem = "d.id = any(%(ids)s::uuid[])" if restringir else _sql_email_aceito("d.email")

    return f"""
        select d.id, d.email, d.full_name, d.city,
               coalesce(q.used, 0) as recibos_mes, rc.total as recibos_total,
               {chave} as dedupe_key
          from public.drivers d
          left join public.receipt_quotas q
                 on q.driver_id = d.id and q.period = public.br_period(%(agora)s)
          cross join lateral (
                select count(*) as total, max(r.created_at) as ultimo
                  from public.receipts r where r.driver_id = d.id) rc
         where d.plan = 'free'
           and d.marketing_optout_at is null
           and d.email like '%%_@_%%'
           -- Conta do painel ainda sem senha: o e-mail foi ditado por telefone e
           -- pode ser de outra pessoa. A régua começa quando ele cria a senha.
           and not (d.password_changed_at is null and exists (
                    select 1 from auth.users u where u.id = d.id
                       and coalesce(u.raw_app_meta_data ->> '{MARCA_CRIADO_PELO_PAINEL}', '') = 'true'))
           and {quem}
           and {condicao}
           and not exists (select 1 from public.email_sends s
                            where s.driver_id = d.id
                              and s.created_at > %(agora)s - make_interval(hours => %(intervalo)s))
           and not exists (select 1 from public.email_sends s
                            where s.template_id = %(modelo)s and s.driver_id = d.id
                              and s.dedupe_key = {chave})
         order by d.created_at
         limit %(lote)s
    """


def candidatos_do_modelo(conn, modelo: dict, agora: datetime, intervalo_horas: int,
                         lote: int, restringir_a: list[str] | None = None,
                         passada: list | None = None) -> list[dict]:
    """Quem está na vez de `modelo`. `passada`: ids dos modelos rodando juntos."""
    janela = EMAIL_AUTO_JANELA_CAMPANHA_DIAS if modelo["trigger"] == "scheduled" else EMAIL_AUTO_JANELA_DIAS
    params = {
        "agora": agora, "n": int(modelo["trigger_value"]), "hora": int(modelo["send_hour"]),
        "janela": janela, "intervalo": int(intervalo_horas), "modelo": modelo["id"],
        "agendado": modelo.get("scheduled_at"), "lote": int(lote),
        "ids": list(restringir_a or []), "passada": list(passada or [modelo["id"]]),
    }
    if modelo["trigger"] == "scheduled" and not modelo.get("scheduled_at"):
        return []
    sql = _sql_candidatos(modelo["trigger"], restringir_a is not None)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def rodar_automacao_email(agora: datetime | None = None, enviar=None, restringir_a: list[str] | None = None,
                          modelos: list[str] | None = None, ignorar_desligado: bool = False,
                          pausa: float = EMAIL_AUTO_PAUSA_S) -> dict:
    """Uma passada do motor. Chamada pelo cron e pelo botão "Rodar agora".

    `enviar`, `restringir_a`, `modelos` e `ignorar_desligado` existem para a
    suíte: ela roda contra o banco de produção e nunca pode mandar e-mail de
    verdade nem mexer no interruptor do motor. Com `modelos`, roda só aqueles,
    mesmo desativados: a suíte cria os seus desligados, e assim o cron de
    produção nunca os pega.

    Precisa de contexto de requisição (url_for e render_template): o cron e o
    painel já têm; fora deles, use app.test_request_context().
    """
    agora = agora or datetime.now(timezone.utc)
    store = get_store()
    resultado = {"ok": True, "enviados": 0, "falhas": 0, "motivo": "", "por_modelo": {}}

    with store.pool.connection() as conn:
        cfg = config_email(conn)
        if not cfg["enabled"] and not ignorar_desligado:
            return dict(resultado, motivo="desligado")
        hora_br = agora.astimezone(BR_TZ).hour
        if not (EMAIL_AUTO_HORA_INICIO <= hora_br <= EMAIL_AUTO_HORA_FIM):
            return dict(resultado, motivo="fora_do_horario")
        if enviar is None and not os.environ.get("RESEND_API_KEY", "").strip():
            return dict(resultado, ok=False, motivo="sem_resend")

        # O dia tem fim, e não só começo: a suíte roda com o relógio em 2020, e
        # "desde 15/01/2020" contava todo envio real de produção — passados 80,
        # o teto parecia batido e a suíte não mandava nada.
        inicio_dia = agora.astimezone(BR_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        usados_hoje = conn.execute(
            """select count(*) as n from public.email_sends
                where created_at >= %s and created_at < %s and status <> 'failed'""",
            (inicio_dia, inicio_dia + timedelta(days=1))).fetchone()["n"]
        lista = conn.execute(
            """select * from public.email_templates
                where (%(todos)s and active) or id = any(%(ids)s::uuid[])
                order by position, created_at""",
            {"todos": modelos is None, "ids": list(modelos or [])}).fetchall()

    restante = min(int(cfg["daily_cap"]) - int(usados_hoje), EMAIL_AUTO_LOTE)
    if restante <= 0:
        return dict(resultado, motivo="teto_diario")

    enviar = enviar or enviar_email_automacao
    primeiro = True
    recusados: set = set()  # endereço recusado não tenta outro modelo nesta passada
    parar = False
    for modelo in lista:
        if parar:
            break
        if restante <= 0:
            resultado["motivo"] = "teto_diario"
            break
        with store.pool.connection() as conn:
            candidatos = candidatos_do_modelo(conn, modelo, agora, cfg["min_gap_hours"], restante, restringir_a,
                                              passada=[m["id"] for m in lista])
        enviados_modelo = 0
        for c in candidatos:
            if restante <= 0 or parar:
                break
            if c["id"] in recusados:
                continue
            # Reserva primeiro, manda depois. Outra execução que chegue junto
            # bate no unique e pula.
            with store.pool.connection() as conn:
                reserva = conn.execute(
                    """insert into public.email_sends (template_id, driver_id, dedupe_key, created_at)
                       values (%s, %s, %s, %s) on conflict do nothing returning id""",
                    (modelo["id"], c["id"], c["dedupe_key"], agora)).fetchone()
            if not reserva:
                continue
            if not primeiro and pausa:
                time.sleep(pausa)
            primeiro = False

            msg = montar_email_automacao(
                modelo, valores_do_email(c, agora),
                absolute_url("email_descadastro", token=token_descadastro(c["id"])))
            ok, provider_id, erro, transitorio = enviar(c["email"], msg, f"auto-{reserva['id']}-{c['id']}")
            with store.pool.connection() as conn:
                if ok:
                    conn.execute(
                        "update public.email_sends set status = 'sent', sent_at = now(), provider_id = %s where id = %s",
                        (provider_id or None, reserva["id"]))
                elif transitorio:
                    # Problema no caminho: solta a reserva para a próxima hora.
                    conn.execute("delete from public.email_sends where id = %s", (reserva["id"],))
                else:
                    conn.execute(
                        "update public.email_sends set status = 'failed', error = %s where id = %s",
                        ((erro or "")[:300], reserva["id"]))
            if ok:
                resultado["enviados"] += 1
                enviados_modelo += 1
                restante -= 1
            else:
                resultado["falhas"] += 1
                recusados.add(c["id"])
                if transitorio:
                    # Resend fora do ar, limitando ou mal configurado: insistir só piora.
                    resultado["motivo"] = "provedor_indisponivel"
                    parar = True
        if enviados_modelo:
            resultado["por_modelo"][modelo["name"]] = enviados_modelo

    app.logger.info("Automação de e-mails: %s enviados, %s falhas%s.", resultado["enviados"],
                    resultado["falhas"], f" ({resultado['motivo']})" if resultado["motivo"] else "")
    return resultado


def _migracao_email_pendente(exc: Exception) -> bool:
    return psycopg is not None and isinstance(exc, (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn))


@app.get("/tarefas/emails")
def tarefa_emails():
    """Passada de hora em hora do motor de e-mails. Chamada pelo Cron da Vercel."""
    exigir_cron()
    try:
        return jsonify(rodar_automacao_email())
    except Exception as exc:
        if _migracao_email_pendente(exc):
            app.logger.warning("Automação de e-mails: migração 0007 ainda não aplicada.")
            return jsonify({"ok": False, "motivo": "migracao_0007_pendente"})
        raise


# ---------------------------------------------------------------------------
# CRM (PilotPostAi): fila crm_sync → POST /internal/integracoes/recibo-taxi/clientes
# ---------------------------------------------------------------------------

def crm_config() -> dict:
    """Endereço e segredo do PilotPostAi, lidos na hora (variáveis da Vercel)."""
    return {"url": os.environ.get("CRM_URL", "").strip(), "secret": os.environ.get("CRM_SECRET", "").strip()}


def crm_configurado(cfg: dict | None = None) -> bool:
    cfg = cfg or crm_config()
    return bool(cfg["url"] and cfg["secret"])


def _crm_evento(linhas: list) -> str:
    """Vários eventos pendentes do mesmo motorista viram um: exclusão vence,
    depois criação, depois "parar ofertas"; o resto é alteração."""
    eventos = {l["event"] for l in linhas}
    if "deleted" in eventos:
        return "driver.deleted"
    if "created" in eventos:
        return "driver.created"
    if "opted_out" in eventos:
        return "driver.opted_out"
    return "driver.updated"


def payload_crm(conn, driver_id: str, evento: str, event_id: str) -> dict | None:
    """O que o PilotPostAi recebe: o estado atual do motorista, só os campos que
    a Política dá ao atendimento. None se o motorista sumiu sem o evento de
    exclusão ainda (ele vem logo atrás)."""
    agora = datetime.now(timezone.utc).isoformat()
    if evento == "driver.deleted":
        return {"event": evento, "event_id": event_id, "occurred_at": agora, "external_id": str(driver_id)}
    linha = conn.execute(
        """select id, full_name, email, whatsapp, city, state, country, plan, subscription_status,
                  signup_channel, created_at, marketing_optout_at
             from public.drivers where id = %s""", (str(driver_id),)).fetchone()
    if not linha:
        return None
    criado = linha["created_at"]
    return {
        "event": evento,
        "event_id": event_id,
        "occurred_at": agora,
        "external_id": str(linha["id"]),
        "name": (linha["full_name"] or "").strip(),
        "phone": phone_e164(linha["whatsapp"] or "", linha["country"] or PAIS_PADRAO),
        "email": normalize_email(linha["email"] or ""),
        "city": (linha["city"] or "").strip(),
        "state": (linha["state"] or "").strip().upper(),
        "plan": linha["plan"] or "free",
        "plan_status": linha["subscription_status"] or "",
        "signup_channel": linha["signup_channel"] or "",
        "signup_at": criado.isoformat() if hasattr(criado, "isoformat") else str(criado or ""),
        "whatsapp_opt_out": linha["marketing_optout_at"] is not None,
        # Termos e Política de hoje já cobrem as ofertas por WhatsApp com o
        # "SAIR"; o cadastro do site diz isso em letras. Quem pediu para parar
        # chega com o opt-out marcado, e o PilotPostAi zera o aceite.
        "marketing_consent": True,
    }


def montar_pedido_crm(cfg: dict, payload: dict) -> urllib.request.Request:
    """POST assinado como o PilotPostAi confere: Bearer e HMAC-SHA256 do corpo cru."""
    corpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assinatura = "sha256=" + hmac.new(cfg["secret"].encode("utf-8"), corpo, hashlib.sha256).hexdigest()
    return urllib.request.Request(
        cfg["url"], data=corpo, method="POST",
        headers={"Authorization": f"Bearer {cfg['secret']}", "X-Recibo-Signature": assinatura,
                 "Content-Type": "application/json", "User-Agent": "ReciboTaxi-CRM/1"})


def enviar_crm_http(cfg: dict, payload: dict, timeout: int = 10) -> tuple[int, str]:
    """(status HTTP, começo do corpo). 0 = nem chegou (rede, DNS, tempo)."""
    try:
        with urllib.request.urlopen(montar_pedido_crm(cfg, payload), timeout=timeout) as resposta:
            return resposta.status, resposta.read().decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:300]
    except Exception as exc:  # rede, DNS, tempo esgotado
        return 0, str(exc)[:300]


def processar_fila_crm(limite: int = CRM_POR_RODADA, driver_id: str | None = None, timeout: int = 10) -> dict:
    """Manda ao PilotPostAi o que está pendente em crm_sync, um envio por motorista.

    2xx marca tudo como enviado. 400 é corpo que nunca vai dar certo: marca
    como enviado com o erro anotado, sem repetir. O resto (401, 5xx, rede)
    fica na fila com espera dobrada a cada tentativa, até um dia.
    """
    cfg = crm_config()
    if not crm_configurado(cfg):
        return {"ok": True, "enviados": 0, "motivo": "nao_configurado"}
    enviados = falhas = descartados = 0
    with get_store().pool.connection() as conn:
        filtro = "and driver_id = %(did)s" if driver_id else ""
        pendentes = conn.execute(
            f"""select id, driver_id, event, attempts from public.crm_sync
                 where sent_at is null and next_try_at <= now() {filtro}
                 order by id limit %(n)s""",
            {"n": limite * 5, "did": str(driver_id) if driver_id else None}).fetchall()
        por_motorista: dict[str, list] = {}
        for l in pendentes:
            por_motorista.setdefault(str(l["driver_id"]), []).append(l)
        for did, linhas in list(por_motorista.items())[:limite]:
            ids = [l["id"] for l in linhas]
            evento = _crm_evento(linhas)
            payload = payload_crm(conn, did, evento, f"crm-{max(ids)}")
            if payload is None:
                continue  # motorista apagado sem o 'deleted' ainda: a próxima rodada pega
            status, corpo = enviar_crm_http(cfg, payload, timeout=timeout)
            if 200 <= status < 300:
                conn.execute("update public.crm_sync set sent_at = now(), last_error = '' where id = any(%s)", (ids,))
                enviados += 1
            elif status == 400:
                conn.execute("update public.crm_sync set sent_at = now(), last_error = %s where id = any(%s)",
                             (f"400: {corpo}"[:300], ids))
                descartados += 1
                app.logger.warning("CRM recusou o motorista %s: %s", did, corpo)
            else:
                tentativas = max(l["attempts"] for l in linhas) + 1
                espera_min = min(5 * 2 ** tentativas, 24 * 60)
                conn.execute(
                    """update public.crm_sync set attempts = %s, last_error = %s,
                              next_try_at = now() + make_interval(mins => %s::int) where id = any(%s)""",
                    (tentativas, f"{status}: {corpo}"[:300], espera_min, ids))
                falhas += 1
                app.logger.warning("CRM: envio do motorista %s falhou (%s); tentativa %s, de novo em %s min",
                                   did, status, tentativas, espera_min)
    if enviados or descartados:
        _ADMIN_CACHE.pop("dashboard", None)
    return {"ok": True, "enviados": enviados, "falhas": falhas, "descartados": descartados}


def sincronizar_crm_agora(driver_id) -> None:
    """Logo depois do cadastro, do "parar ofertas" e da exclusão: tenta mandar na
    hora, com prazo curto. O que falhar fica na fila para o cron. Nunca derruba
    a requisição que a chamou."""
    if not crm_configurado():
        return
    try:
        processar_fila_crm(limite=1, driver_id=str(driver_id), timeout=4)
    except Exception as exc:  # fila ausente (migração 0021), banco fora, etc.
        app.logger.warning("CRM: envio imediato de %s falhou: %s", driver_id, exc)


@app.get("/tarefas/crm")
def tarefa_crm():
    """Rodada da fila do CRM. Chamada pelo Cron da Vercel a cada 15 minutos."""
    exigir_cron()
    try:
        return jsonify(processar_fila_crm())
    except psycopg.errors.UndefinedTable:
        app.logger.warning("CRM: migração 0021 ainda não aplicada.")
        return jsonify({"ok": False, "motivo": "migracao_0021_pendente"})


@app.route("/emails/sair/<token>", methods=["GET", "POST"])
def email_descadastro(token: str):
    """Descadastro da automação. GET só mostra o botão; quem descadastra é o POST.

    GET não pode descadastrar: antivírus de e-mail e o "link seguro" do Outlook
    abrem todo link da mensagem antes da pessoa, e descadastrariam todo mundo.
    O POST serve ao botão da página e ao descadastro de um clique do Gmail
    (RFC 8058), que chega com o corpo "List-Unsubscribe=One-Click".
    """
    driver_id = ler_token_descadastro(token)
    store = get_store()
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "select email, marketing_optout_at from public.drivers where id = %s",
                (driver_id,)).fetchone() if driver_id else None
            if not row:
                return render_template("email_descadastro.html", estado="invalido"), 404

            estado = "ja_saiu" if row["marketing_optout_at"] else "confirmar"
            if request.method == "POST":
                if request.form.get("acao") == "voltar":
                    conn.execute("update public.drivers set marketing_optout_at = null where id = %s", (driver_id,))
                    estado = "voltou"
                else:
                    conn.execute(
                        "update public.drivers set marketing_optout_at = coalesce(marketing_optout_at, now()) where id = %s",
                        (driver_id,))
                    estado = "saiu"
    except psycopg.errors.InvalidTextRepresentation:
        return render_template("email_descadastro.html", estado="invalido"), 404
    except Exception as exc:
        if _migracao_email_pendente(exc):
            abort(503)
        raise
    return render_template("email_descadastro.html", estado=estado, token=token,
                           email=filtro_mascarar_email(row["email"]))


# ── Legal ─────────────────────────────────────────────────────────────────────

@app.get("/privacidade")
def privacidade():
    return render_template("privacidade.html", updated_at=POLITICA_ATUALIZADA_EM,
                           vendas_a_partir=CONTATO_DE_VENDAS_A_PARTIR,
                           caixa_a_partir=CAIXA_A_PARTIR)


@app.get("/termos")
def termos():
    return render_template("termos.html", updated_at=POLITICA_ATUALIZADA_EM,
                           vendas_a_partir=CONTATO_DE_VENDAS_A_PARTIR,
                           caixa_a_partir=CAIXA_A_PARTIR)


@app.get("/excluir-conta")
def pagina_excluir_conta():
    """Pagina publica de exclusao de conta.

    O Google Play exige as DUAS coisas de quem deixa criar conta no app: um
    caminho dentro do app e um link na web onde qualquer um possa pedir a
    exclusao — inclusive quem ja desinstalou e nao consegue mais entrar. O
    formulario do painel nao serve sozinho: ele fica atras do login. Esta
    pagina abre sem sessao e a URL vai declarada no formulario de Seguranca
    dos Dados do Play Console.

    Mesma URL do POST logo abaixo, que e quem realmente apaga: aqui e GET.
    """
    # 27/11: a lista do que é apagado ganhou o Caixa. Rascunho: acertar no dia
    # do merge, junto com POLITICA_ATUALIZADA_EM.
    return render_template("excluir_conta.html", updated_at="27 de novembro de 2026")


# ── Receipt view ──────────────────────────────────────────────────────────────

@app.get("/recibo/<rid>")
def recibo_view(rid: str):
    # O formato primeiro: codigo malformado nem chega ao banco (um NUL no
    # caminho derrubava a consulta com 500). Minusculas valem: quem digita o
    # codigo a partir do papel nem sempre respeita a caixa.
    rid = rid.upper()
    if not re.fullmatch(r"[0-9A-F]{%d}" % RID_LENGTH, rid):
        abort(404, description="Recibo não encontrado.")
    receipt = get_store().get_receipt(rid)
    if not receipt:
        # Codigo bem formado que nao existe. O servidor nao sabe por que: pode
        # ser o recibo que o motorista acabou de emitir sem sinal e mostrou no
        # QR, um link antigo ou um codigo inventado. A pagina nao afirma nada
        # disso; so tenta de novo por uns minutos e abre o recibo se ele chegar.
        tentativa = min(max(request.args.get("t", 0, type=int), 0), RECIBO_ESPERA_TENTATIVAS)
        proxima = (url_for("recibo_view", rid=rid, t=tentativa + 1)
                   if tentativa < RECIBO_ESPERA_TENTATIVAS else None)
        resposta = make_response(render_template(
            "recibo_aguardando.html", rid=rid, proxima=proxima,
            url_recibo=url_for("recibo_view", rid=rid),
            url_fim=url_for("recibo_view", rid=rid, t=RECIBO_ESPERA_TENTATIVAS),
            restantes=RECIBO_ESPERA_TENTATIVAS - tentativa,
            segundos=RECIBO_ESPERA_SEGUNDOS,
            minutos=RECIBO_ESPERA_SEGUNDOS * RECIBO_ESPERA_TENTATIVAS // 60), 404)
        # O "ainda nao chegou" nao pode ficar guardado em cache nenhum.
        resposta.headers["Cache-Control"] = "no-store"
        return resposta

    share_url = public_receipt_url(rid)
    is_owner = bool(g.user and g.user["_id"] == receipt.get("driver_id"))
    # O convidado que emitiu continua sendo o emissor enquanto durar a sessão.
    is_issuer = is_owner or rid in session.get("guest_receipts", [])

    context = dict(receipt)
    context["public_url"] = share_url
    context["whatsapp_link"] = build_whatsapp_link(context, share_url, with_recipient=is_issuer)
    context["email_link"] = build_email_link(context, share_url, with_recipient=is_issuer)
    context["is_owner"] = is_owner
    context["is_issuer"] = is_issuer
    context["is_guest"] = receipt.get("is_guest", False)
    context["just_created"] = request.args.get("created") == "1"
    context["rotulo_documento"] = rotulo_documento(pais_do_recibo(receipt))
    # Quem emitiu vê o documento inteiro; qualquer outro visitante, mascarado.
    context["documento_exibido"] = (receipt.get("passenger_document") or "") if is_issuer \
        else mascarar_documento(receipt.get("passenger_document") or "", pais_do_recibo(receipt))
    context["horario"] = horario_da_corrida(receipt)

    return render_template("recibo_view.html", dados=context)


# ── Painel administrativo (/admin) ───────────────────────────────────────────
#
# Conta separada da de motorista, com sessao propria (`admin_id`). O painel
# mostra CPF, telefone e placa de todo mundo, entao ele e mais fechado que o
# resto do site: tentativas de login com teto diario, sessao que morre em 12 h,
# CSRF em todo POST e noindex em toda resposta.

# Hash de mentira para e-mail inexistente: check_password_hash roda do mesmo
# jeito, entao o tempo de resposta nao conta quem e admin. Calculado uma vez —
# scrypt a cada tentativa errada seria custo sem motivo.
_ADMIN_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


def _admin_pw_stamp(admin: dict) -> str:
    return to_utc_iso(admin["password_changed_at"]) if admin.get("password_changed_at") else ""


def admin_atual() -> dict | None:
    """Administrador da sessao, ou None. Derruba sessao velha ou de senha trocada."""
    admin_id = session.get("admin_id")
    if not admin_id:
        return None
    agora = datetime.now(timezone.utc)
    entrou_em = session.get("admin_login_at")
    visto_em = session.get("admin_seen_at") or entrou_em
    if (not entrou_em
            or datetime.fromisoformat(entrou_em) + timedelta(hours=ADMIN_SESSION_HOURS) < agora
            or datetime.fromisoformat(visto_em) + timedelta(minutes=ADMIN_IDLE_MINUTES) < agora):
        encerrar_sessao_admin()
        return None
    # Renova a marca de atividade no maximo a cada 5 min, senao o cookie e
    # reescrito em toda resposta.
    if datetime.fromisoformat(visto_em) + timedelta(minutes=5) < agora:
        session["admin_seen_at"] = to_utc_iso(agora)
    admin = get_store().get_admin_by_id(admin_id)
    if not admin or _admin_pw_stamp(admin) != session.get("admin_pw_stamp", ""):
        encerrar_sessao_admin()
        return None
    return admin


def iniciar_sessao_admin(admin: dict, senha_digitada: str) -> None:
    # Sessao nova do zero: nao herda nada de um login de motorista no mesmo
    # navegador, e ganha um token CSRF proprio.
    session.clear()
    session["admin_id"] = admin["_id"]
    session["admin_login_at"] = to_utc_iso(datetime.now(timezone.utc))
    session["admin_seen_at"] = session["admin_login_at"]
    session["admin_pw_stamp"] = _admin_pw_stamp(admin)
    session["admin_csrf"] = secrets.token_urlsafe(32)
    # A senha inicial e "1234" por decisao do dono. O painel avisa em toda tela
    # ate ela ser trocada — o aviso vive na sessao para nao rodar scrypt a cada
    # pagina.
    session["admin_senha_fraca"] = len(senha_digitada) < ADMIN_PASSWORD_MIN
    get_store().touch_admin_login(admin["_id"])


def encerrar_sessao_admin() -> None:
    for chave in ("admin_id", "admin_login_at", "admin_seen_at", "admin_pw_stamp", "admin_csrf", "admin_senha_fraca"):
        session.pop(chave, None)


def admin_csrf_ok() -> bool:
    esperado = session.get("admin_csrf", "")
    enviado = request.form.get("csrf", "")
    return bool(esperado) and hmac.compare_digest(esperado, enviado)


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        admin = admin_atual()
        if not admin:
            # So GET volta para onde estava: o login redireciona com GET, e
            # mandar de volta para uma rota so-POST (sair, excluir) daria 405.
            proximo = request.path if request.method == "GET" else None
            return redirect(url_for("admin_login", proximo=proximo))
        if request.method == "POST" and not admin_csrf_ok():
            abort(400)
        g.admin = admin
        return view(*args, **kwargs)

    return wrapped_view


@app.after_request
def admin_noindex(response):
    # O painel nao existe para o Google, e a URL nao deve nem aparecer numa
    # busca. Vale para login, erro e tudo mais debaixo de /admin.
    if request.path == "/admin" or request.path.startswith("/admin/"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Cache-Control"] = "no-store"
        # As URLs do painel carregam id de motorista e ha links para o painel
        # da Stripe: nada disso deve viajar no Referer.
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.context_processor
def inject_admin() -> dict:
    return {
        "admin_user": g.get("admin"),
        "admin_csrf": session.get("admin_csrf", ""),
        "admin_senha_fraca": bool(session.get("admin_senha_fraca")),
    }


def _chave_login_admin(email: str, ip: str = "") -> str:
    """Contador do e-mail no dia; com `ip`, o do par e-mail + IP.

    O e-mail tentado nao vai em claro para a tabela de contadores.
    """
    digest = hashlib.sha256(f"{email}|{ip}".encode("utf-8")).hexdigest()[:16]
    return f"admin_login:{'par' if ip else 'email'}:{digest}:{today_br()}"


def pode_tentar_login_admin(email: str) -> bool:
    """Tres tetos por dia, do mais barato de estourar para o mais caro.

    O teto apertado e do par e-mail + IP: quem erra a senha dez vezes tranca a
    si mesmo, nao o dono da conta. Quando o teto era so por e-mail, dez
    tentativas de qualquer lugar trancavam o administrador ate a meia-noite.
    O teto total por e-mail continua existindo, bem mais alto, para segurar
    quem tenta adivinhar a senha trocando de IP.
    """
    store = get_store()
    ip = client_ip()
    # Cada teto estourado para aqui: IP bloqueado nao gasta o total do e-mail.
    if store.bump_counter(f"admin_login:ip:{ip}:{today_br()}") > ADMIN_LOGIN_DAILY_LIMIT_IP:
        return False
    if store.bump_counter(_chave_login_admin(email, ip)) > ADMIN_LOGIN_DAILY_LIMIT_EMAIL:
        return False
    return store.bump_counter(_chave_login_admin(email)) <= ADMIN_LOGIN_DAILY_LIMIT_EMAIL_TOTAL


def _proximo_seguro(valor: str) -> str:
    """So aceita caminho interno debaixo de /admin: nada de mandar para fora."""
    return valor if valor.startswith("/admin") and "//" not in valor else url_for("admin_dashboard")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if admin_atual():
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))
        senha = request.form.get("senha", "")
        if not email or not senha:
            flash("Informe e-mail e senha.", "danger")
            return render_template("admin/login.html", form={"email": email}), 400

        if not pode_tentar_login_admin(email):
            flash("Muitas tentativas hoje. Tente novamente mais tarde.", "danger")
            return render_template("admin/login.html", form={"email": email}), 429

        admin = get_store().get_admin_by_email(email)
        # check_password_hash roda mesmo sem conta, com um hash de mentira:
        # responder mais rapido para e-mail inexistente contaria quem e admin.
        hash_ = admin["password_hash"] if admin else _ADMIN_DUMMY_HASH
        if not admin or not check_password_hash(hash_, senha):
            app.logger.warning("admin: login recusado para %s de %s", _chave_login_admin(email), client_ip())
            flash("E-mail ou senha inválidos.", "danger")
            return render_template("admin/login.html", form={"email": email}), 401

        # Login certo zera as tentativas do par e-mail + IP: o dono errar a
        # senha nove vezes num dia nao pode trancar a conta dele.
        get_store().reset_counter(_chave_login_admin(email, client_ip()))
        iniciar_sessao_admin(admin, senha)
        app.logger.info("admin %s entrou de %s", admin["email"], client_ip())
        return redirect(_proximo_seguro(request.form.get("proximo", "")))

    return render_template("admin/login.html", form={}, proximo=request.args.get("proximo", ""))


@app.post("/admin/sair")
@admin_required
def admin_sair():
    encerrar_sessao_admin()
    flash("Você saiu do painel.", "info")
    return redirect(url_for("admin_login"))


@app.route("/admin/senha", methods=["GET", "POST"])
@admin_required
def admin_senha():
    if request.method == "POST":
        atual = request.form.get("senha_atual", "")
        nova = request.form.get("senha", "")
        confirmacao = request.form.get("confirmar_senha", "")

        if not check_password_hash(g.admin["password_hash"], atual):
            flash("A senha atual não confere.", "danger")
            return render_template("admin/senha.html"), 403
        if len(nova) < ADMIN_PASSWORD_MIN:
            flash(f"A nova senha precisa ter pelo menos {ADMIN_PASSWORD_MIN} caracteres.", "danger")
            return render_template("admin/senha.html"), 400
        if nova != confirmacao:
            flash("As senhas não conferem.", "danger")
            return render_template("admin/senha.html"), 400
        if nova == atual:
            flash("A nova senha é igual à atual.", "danger")
            return render_template("admin/senha.html"), 400

        get_store().set_admin_password(g.admin["_id"], generate_password_hash(nova))
        # Trocar a senha derruba as outras sessoes (o carimbo muda); esta
        # continua, com o carimbo novo.
        admin = get_store().get_admin_by_id(g.admin["_id"])
        iniciar_sessao_admin(admin, nova)
        flash("Senha alterada.", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("admin/senha.html")


# Cache em processo: na Vercel a instancia sobrevive entre invocacoes, e o
# painel sao ~35 idas ao banco. Um minuto de atraso nao muda decisao nenhuma.
_ADMIN_CACHE: dict = {}
ADMIN_CACHE_SECONDS = 60


@app.get("/admin")
@admin_required
def admin_dashboard():
    import time as _time

    agora = _time.monotonic()
    guardado = _ADMIN_CACHE.get("dashboard")
    if guardado and guardado[0] > agora and request.args.get("atualizar") != "1":
        dados = dict(guardado[1], cache=True)
    else:
        dados = montar_dashboard_admin()
        _ADMIN_CACHE["dashboard"] = (agora + ADMIN_CACHE_SECONDS, dados)
    return render_template("admin/dashboard.html", d=dados)


# ── Dashboard: consultas ─────────────────────────────────────────────────────
#
# Tudo numa conexao so, com um savepoint por secao: se uma consulta falhar (por
# exemplo, permissao em auth.*), a secao sai como None e a pagina continua de pe.
# Janelas sao calculadas em Python e passadas como constante, para o planner
# usar os indices em created_at. Dia e mes "de negocio" sao no fuso de Brasilia.

def _inicio_dia_br(dias_atras: int = 0) -> str:
    d = datetime.now(BR_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return to_utc_iso(d - timedelta(days=dias_atras))


def _inicio_mes_br(meses_atras: int = 0) -> str:
    hoje = datetime.now(BR_TZ)
    ano, mes = hoje.year, hoje.month - meses_atras
    while mes <= 0:
        mes += 12
        ano -= 1
    return to_utc_iso(datetime(ano, mes, 1, tzinfo=BR_TZ))


def _serie_dias(linhas: list[dict], dias: int, campos: tuple[str, ...]) -> list[dict]:
    """Preenche os dias sem linha com zero, do mais antigo para hoje."""
    por_dia = {str(l["dia"]): l for l in linhas}
    hoje = datetime.now(BR_TZ).date()
    saida = []
    for i in range(dias - 1, -1, -1):
        dia = hoje - timedelta(days=i)
        l = por_dia.get(dia.isoformat(), {})
        saida.append({"dia": dia, **{c: l.get(c) or 0 for c in campos}})
    return saida


def _serie_meses(linhas: list[dict], meses: int, campos: tuple[str, ...]) -> list[dict]:
    por_mes = {str(l["mes"]): l for l in linhas}
    hoje = datetime.now(BR_TZ)
    saida = []
    for i in range(meses - 1, -1, -1):
        ano, mes = hoje.year, hoje.month - i
        while mes <= 0:
            mes += 12
            ano -= 1
        chave = f"{ano:04d}-{mes:02d}-01"
        l = por_mes.get(chave, {})
        saida.append({"mes": f"{mes:02d}/{ano % 100:02d}", **{c: l.get(c) or 0 for c in campos}})
    return saida


def _com_maximo(serie: list[dict], campo: str) -> list[dict]:
    """Acrescenta pct (0-100) para desenhar barra em CSS sem JavaScript."""
    maior = max((float(l[campo] or 0) for l in serie), default=0) or 1
    for l in serie:
        l["pct"] = round(float(l[campo] or 0) / maior * 100)
    return serie


# ── Painel: faturamento mês a mês e cliques do /baixar (gráficos) ────────────
# Pedido do Anderson em 2026-11-21. Os gráficos são SVG montado aqui e no
# template (o painel não carrega biblioteca de gráfico); o JavaScript só faz o
# destaque ao passar o mouse, e a tabela embaixo de cada um vale sem ele.

# O gráfico mostra sempre 12 meses antes, o mês atual no centro e 12 depois
# (pedido do Anderson em 2026-11-21).
FATURAMENTO_MESES_PASSADOS = 12
FATURAMENTO_MESES_FUTUROS = 12
# Cobrança da Stripe a partir deste valor é do anual. Os preços são 9,90 e
# 19,90 (mensal) e 59,90 e 119,90 (anual); a troca do mensal pelo anual cobra o
# anual menos o que sobrou do mês, sempre acima disto.
FATURAMENTO_ANUAL_MINIMO = Decimal("30")
# Ordem de baixo para cima na pilha e nas legendas; a cor segue o tipo.
FATURAMENTO_TIPOS = (
    ("mensal", "Assinatura mensal"),
    ("anual", "Assinatura anual"),
    ("unico", "Anual pago uma vez (Pix ou cartão)"),
)
CLIQUES_DIAS = 60
CLIQUES_CANAIS = 6          # os demais somam em "outros"
GRAFICO_LARGURA, GRAFICO_ALTURA = 960, 300
GRAFICO_MARGEM = {"esq": 60, "dir": 16, "topo": 18, "base": 46}


def _somar_meses(ano: int, mes: int, n: int) -> tuple[int, int]:
    total = ano * 12 + (mes - 1) + n
    return total // 12, total % 12 + 1


def _datahora_mais_meses(quando: datetime, n: int) -> datetime:
    ano, mes = _somar_meses(quando.year, quando.month, n)
    return quando.replace(year=ano, month=mes, day=min(quando.day, calendar.monthrange(ano, mes)[1]))


def _chave_mes(quando: datetime) -> str:
    return quando.astimezone(BR_TZ).strftime("%Y-%m")


MESES_CURTOS = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")


def _campo(obj, *caminho, padrao=None):
    """obj[a][b]... de um objeto da Stripe (ou dict), sem estourar no que falta."""
    atual = obj
    for chave in caminho:
        try:
            atual = atual[chave]
        except (KeyError, IndexError, TypeError, AttributeError):
            return padrao
        if atual is None:
            return padrao
    return atual


# Só o que é do Recibo Táxi (pedido do Anderson em 2026-11-21): a mesma conta
# da Stripe ou do RevenueCat pode, um dia, atender outro app da AJR. Nas lojas,
# o id do produto começa assim; na Stripe, valem os produtos dos Prices
# configurados (os preços antigos ficaram nos mesmos produtos).
PRODUTO_LOJA_PREFIXO = "br.com.recibotaxi."
_PRODUTOS_STRIPE: dict[str, str] = {}   # price -> product; não muda, guarda por processo


def produtos_stripe_do_recibo(stripe) -> set[str]:
    """Ids dos produtos da Stripe que são o Recibo Táxi Pro, pelos Prices das variáveis."""
    produtos = set()
    for variavel in STRIPE_PRICE_IDS.values():
        preco = os.environ.get(variavel, "").strip()
        if not preco:
            continue
        if preco not in _PRODUTOS_STRIPE:
            produto = _campo(stripe.Price.retrieve(preco), "product")
            _PRODUTOS_STRIPE[preco] = str(_campo(produto, "id") or produto or "") if produto else ""
        if _PRODUTOS_STRIPE[preco]:
            produtos.add(_PRODUTOS_STRIPE[preco])
    return produtos


def _id_do_cliente(objeto) -> str:
    """Id do cliente de uma fatura ou assinatura (texto, ou objeto expandido)."""
    cliente = _campo(objeto, "customer")
    return str(_campo(cliente, "id") or cliente or "") if not isinstance(cliente, str) else cliente


def _produto_da_linha(linha) -> str:
    """Produto de uma linha de fatura: a API de 2025 em diante o põe em
    pricing.price_details; as anteriores, em price."""
    return str(_campo(linha, "pricing", "price_details", "product") or _campo(linha, "price", "product") or "")


def faturamento_da_stripe(desde: datetime, excluir_clientes=frozenset()) -> tuple[list[dict], list[dict], str]:
    """(cobranças pagas das assinaturas do site, próximas cobranças, erro).

    O webhook não assina invoice.*, então as renovações da Stripe não chegam
    ao servidor: o painel pergunta à própria Stripe. Faturas pagas trazem a
    primeira cobrança e as renovações; o pagamento único (Pix ou cartão) não
    gera fatura e vem do registro de cobranças. As assinaturas que não foram
    canceladas dizem quanto e quando cobram de novo.
    """
    stripe = get_stripe()
    if not stripe:
        return [], [], "a Stripe não está configurada"
    # Só o Recibo Táxi: a mesma conta da Stripe pode atender outro app da AJR.
    try:
        produtos = produtos_stripe_do_recibo(stripe)
    except Exception as exc:
        app.logger.warning("admin: não li os produtos do Recibo Táxi na Stripe: %s", exc)
        return [], [], "não consegui identificar os produtos do Recibo Táxi; o site ficou fora da conta"
    if not produtos:
        return [], [], "nenhum preço do Recibo Táxi configurado; o site ficou fora da conta"
    pagas, proximas = [], []
    try:
        for fatura in stripe.Invoice.list(status="paid", created={"gte": int(desde.timestamp())},
                                          limit=100).auto_paging_iter():
            valor = Decimal(int(_campo(fatura, "amount_paid", padrao=0))) / 100
            if valor <= 0 or not _campo(fatura, "livemode", padrao=False) \
                    or str(_campo(fatura, "currency", padrao="")).lower() != "brl":
                continue
            if not any(_produto_da_linha(linha) in produtos
                       for linha in (_campo(fatura, "lines", "data", padrao=[]) or [])):
                continue  # fatura de outro produto da mesma conta
            if _id_do_cliente(fatura) in excluir_clientes:
                continue  # conta interna
            momento = int(_campo(fatura, "status_transitions", "paid_at", padrao=0)
                          or _campo(fatura, "created", padrao=0))
            pagas.append({"mes": _chave_mes(datetime.fromtimestamp(momento, timezone.utc)), "valor": valor,
                          "tipo": "anual" if valor >= FATURAMENTO_ANUAL_MINIMO else "mensal",
                          "origem": "Stripe (site)"})
        # Sem `status`, a Stripe devolve as que não foram canceladas.
        for assinatura in stripe.Subscription.list(limit=100).auto_paging_iter():
            if _campo(assinatura, "status") not in ACTIVE_SUBSCRIPTION_STATUSES \
                    or _campo(assinatura, "cancel_at_period_end", padrao=False) \
                    or not _campo(assinatura, "livemode", padrao=False):
                continue
            item = _campo(assinatura, "items", "data", 0, padrao={})
            if str(_campo(item, "price", "product", padrao="")) not in produtos:
                continue  # assinatura de outro produto da mesma conta
            if _id_do_cliente(assinatura) in excluir_clientes:
                continue  # conta interna
            valor = Decimal(int(_campo(item, "price", "unit_amount", padrao=0))) \
                * int(_campo(item, "quantity", padrao=1)) / 100
            # Desde a API de 2025 o fim do período mora no item da assinatura.
            fim = _campo(item, "current_period_end") or _campo(assinatura, "current_period_end")
            if valor <= 0 or not fim or str(_campo(item, "price", "currency", padrao="brl")).lower() != "brl":
                continue
            proximas.append({
                "valor": valor, "proxima": datetime.fromtimestamp(int(fim), timezone.utc),
                "tipo": "anual" if _campo(item, "price", "recurring", "interval") == "year" else "mensal",
                "origem": "Stripe (site)"})
    except Exception as exc:
        app.logger.warning("admin: faturamento da Stripe indisponível: %s", exc)
        return pagas, proximas, "a Stripe não respondeu; o que veio do site pode estar faltando"
    return pagas, proximas, ""


def _escala(maximo: float, passos: int = 4) -> tuple[float, list[float]]:
    """Topo do eixo e marcas redondas (1, 2, 2,5, 5 × 10^n)."""
    if maximo <= 0:
        return 1.0, [0.0, 1.0]
    bruto = maximo / passos
    potencia = 10 ** math.floor(math.log10(bruto))
    passo = next(m * potencia for m in (1, 2, 2.5, 5, 10) if m * potencia >= bruto)
    topo = passo * math.ceil(maximo / passo - 1e-9)
    return topo, [passo * i for i in range(int(round(topo / passo)) + 1)]


def _barra_arredondada(x: float, topo: float, largura: float, base: float, raio: float = 4) -> str:
    """Caminho SVG de barra com o topo arredondado e a base reta."""
    altura = base - topo
    raio = max(0.0, min(raio, largura / 2, altura))
    return (f"M{x:.1f},{base:.1f}V{topo + raio:.1f}Q{x:.1f},{topo:.1f} {x + raio:.1f},{topo:.1f}"
            f"H{x + largura - raio:.1f}Q{x + largura:.1f},{topo:.1f} {x + largura:.1f},{topo + raio:.1f}"
            f"V{base:.1f}Z")


def montar_faturamento(d: dict, agora: datetime | None = None, da_stripe=None) -> dict:
    """Faturamento bruto mês a mês: o que entrou nos FATURAMENTO_MESES_PASSADOS
    meses anteriores, o mês atual no centro e o que as assinaturas ativas vão
    cobrar nos FATURAMENTO_MESES_FUTUROS meses seguintes.

    O mensal entra todo mês; o anual entra uma vez, no mês da cobrança (e de
    novo 12 meses depois, se renovar); o pago uma vez não se repete. A previsão
    supõe que ninguém cancela e ninguém novo entra.
    """
    agora = agora or datetime.now(timezone.utc)
    local = agora.astimezone(BR_TZ)
    mes_atual = (local.year, local.month)
    inicio = _somar_meses(*mes_atual, -FATURAMENTO_MESES_PASSADOS)
    zero = {tipo: Decimal(0) for tipo, _ in FATURAMENTO_TIPOS}
    realizado: dict[str, dict] = {}
    previsto: dict[str, dict] = {}

    def somar(alvo: dict, mes: str, tipo: str, valor) -> None:
        alvo.setdefault(mes, dict(zero))[tipo] += Decimal(str(valor or 0))

    for linha in d.get("fat_loja") or []:
        somar(realizado, linha["mes"], linha["tipo"], linha["valor"])
    for linha in d.get("fat_unico") or []:
        somar(realizado, linha["mes"], "unico", linha["valor"])
    internas = d.get("fat_internas") or []
    clientes_internos = frozenset(str(l["cliente"]) for l in internas if l.get("cliente"))
    pagas, proximas, erro = da_stripe if da_stripe is not None else faturamento_da_stripe(
        datetime(inicio[0], inicio[1], 1, tzinfo=BR_TZ), clientes_internos)
    for paga in pagas:
        somar(realizado, paga["mes"], paga["tipo"], paga["valor"])
    proximas = list(proximas)
    for linha in d.get("fat_loja_ativas") or []:
        if linha.get("vence_ms") and linha.get("price"):
            proximas.append({"valor": Decimal(str(linha["price"])), "origem": "loja",
                             "proxima": datetime.fromtimestamp(int(linha["vence_ms"]) / 1000, timezone.utc),
                             "tipo": "anual" if linha.get("cycle") == "anual" else "mensal"})

    fim = _somar_meses(*mes_atual, FATURAMENTO_MESES_FUTUROS)
    for cobranca in proximas:
        passo = 12 if cobranca["tipo"] == "anual" else 1
        quando = cobranca["proxima"]
        # Venceu sem a renovação chegar (webhook atrasado): conta como renovada.
        while quando < agora:
            quando = _datahora_mais_meses(quando, passo)
        while (quando.astimezone(BR_TZ).year, quando.astimezone(BR_TZ).month) <= fim:
            somar(previsto, _chave_mes(quando), cobranca["tipo"], cobranca["valor"])
            quando = _datahora_mais_meses(quando, passo)

    # Janela fixa: o mês atual fica sempre no centro do gráfico.
    primeiro = inicio
    meses = []
    ano, mes = primeiro
    while (ano, mes) <= fim:
        chave = f"{ano:04d}-{mes:02d}"
        r, p = realizado.get(chave, dict(zero)), previsto.get(chave, dict(zero))
        meses.append({
            "chave": chave, "rotulo": f"{MESES_CURTOS[mes - 1]}/{ano % 100:02d}",
            "nome": f"{MESES_CURTOS[mes - 1]} {ano}", "mes_curto": MESES_CURTOS[mes - 1], "ano": ano,
            "mostra_ano": mes == 1 or (ano, mes) == primeiro,
            "futuro": (ano, mes) > mes_atual, "atual": (ano, mes) == mes_atual,
            "realizado": r, "previsto": p,
            "total_realizado": sum(r.values()), "total_previsto": sum(p.values()),
        })
        ano, mes = _somar_meses(ano, mes, 1)
    for m in meses:
        m["total"] = m["total_realizado"] + m["total_previsto"]
        linhas = []
        for tipo, rotulo in FATURAMENTO_TIPOS:
            if m["realizado"][tipo]:
                linhas.append({"tipo": tipo, "previsto": False, "rotulo": rotulo,
                               "valor": filtro_brl(m["realizado"][tipo])})
            if m["previsto"][tipo]:
                linhas.append({"tipo": tipo, "previsto": True, "rotulo": f"{rotulo} (previsão)",
                               "valor": filtro_brl(m["previsto"][tipo])})
        m["dica"] = {"titulo": m["nome"] + (" · previsão" if m["futuro"] else " · até agora e previsão" if m["atual"] else ""),
                     "total": filtro_brl(m["total"]), "linhas": linhas}

    # Geometria do gráfico (coordenadas do viewBox).
    margem = GRAFICO_MARGEM
    largura_util = GRAFICO_LARGURA - margem["esq"] - margem["dir"]
    altura_util = GRAFICO_ALTURA - margem["topo"] - margem["base"]
    base = margem["topo"] + altura_util
    topo_eixo, marcas = _escala(float(max((m["total"] for m in meses), default=0)))
    vaga = largura_util / max(len(meses), 1)
    largura_barra = min(24.0, vaga * 0.6)
    for i, m in enumerate(meses):
        x = margem["esq"] + i * vaga + (vaga - largura_barra) / 2
        m["x"], m["x_centro"], m["vaga_x"], m["vaga"] = x, x + largura_barra / 2, margem["esq"] + i * vaga, vaga
        pilha = [(tipo, "realizado", m["realizado"][tipo]) for tipo, _ in FATURAMENTO_TIPOS] \
            + [(tipo, "previsto", m["previsto"][tipo]) for tipo, _ in FATURAMENTO_TIPOS]
        pilha = [(t, f, v) for t, f, v in pilha if v > 0]
        segmentos, y = [], base
        for k, (tipo, fase, valor) in enumerate(pilha):
            altura = float(valor) / topo_eixo * altura_util
            de, ate = y - altura, y
            # 2 unidades de fundo entre segmentos vizinhos (1 de cada lado).
            if k > 0 and altura > 3:
                ate -= 1
            if k < len(pilha) - 1 and altura > 3:
                de += 1
            ultimo = k == len(pilha) - 1
            segmentos.append({"tipo": tipo, "fase": fase, "y": de, "altura": max(ate - de, 0.5),
                              "caminho": _barra_arredondada(x, de, largura_barra, ate) if ultimo else ""})
            y -= altura
        m["segmentos"], m["y_topo"] = segmentos, y
    divisa = next((m["vaga_x"] + m["vaga"] for m in meses if m["atual"]), None)
    return {
        "meses": meses, "tipos": FATURAMENTO_TIPOS, "erro_stripe": erro,
        "marcas": [{"valor": v, "y": base - v / topo_eixo * altura_util} for v in marcas],
        "largura": GRAFICO_LARGURA, "altura": GRAFICO_ALTURA, "base": base, "margem": margem,
        "largura_barra": largura_barra, "divisa_x": divisa,
        # Tudo o que já entrou na janela: os 12 meses anteriores e o atual até agora.
        "ultimos_12m": sum((m["total_realizado"] for m in meses if not m["futuro"]), Decimal(0)),
        "este_mes": next((m["total_realizado"] for m in meses if m["atual"]), Decimal(0)),
        "previsto_12m": sum((m["total_previsto"] for m in meses), Decimal(0)),
        "assinaturas_previstas": len(proximas),
        # Seções do banco que falharam: o gráfico sairia menor sem dizer por quê.
        "faltando": [n for n in ("fat_loja", "fat_unico", "fat_loja_ativas", "fat_internas") if n in d and d[n] is None],
        "contas_internas": len(internas),
    }


def montar_cliques_por_dia(linhas: list[dict] | None, hoje=None) -> dict | None:
    """Cliques no /baixar por dia: idas à loja (total e por canal) e, à parte,
    quantos abriram a página (`abriram`, desde 2026-11-22; antes a visita já
    era a ida à loja)."""
    if linhas is None:
        return None
    hoje = hoje or datetime.now(BR_TZ).date()
    por_canal: dict[str, int] = {}
    abriram_no_dia: dict[str, int] = {}
    for linha in linhas:
        por_canal[linha["canal"]] = por_canal.get(linha["canal"], 0) + int(linha["n"] or 0)
        dia = str(linha["dia"])
        abriram_no_dia[dia] = abriram_no_dia.get(dia, 0) + int(linha.get("abriram") or 0)
    # Ordem fixa pelo volume do período: a cor segue o canal, não a posição do
    # dia. Canal sem ida à loja (só abriu a página) não ganha linha.
    ordem = sorted((c for c in por_canal if por_canal[c] > 0), key=lambda c: (-por_canal[c], c))
    canais = ordem[:CLIQUES_CANAIS]
    nomes = canais + (["outros"] if len(ordem) > len(canais) else [])
    # Começa no primeiro dia com clique: antes disso não havia contagem, e um
    # trecho zerado pareceria "ninguém clicou".
    primeiro = min((l["dia"] for l in linhas), default=hoje)
    primeiro = max(primeiro, hoje - timedelta(days=CLIQUES_DIAS - 1))
    dias = [primeiro + timedelta(days=i) for i in range((hoje - primeiro).days + 1)]
    valores: dict[tuple[str, str], int] = {}
    for linha in linhas:
        chave = (str(linha["dia"]), linha["canal"] if linha["canal"] in canais else "outros")
        valores[chave] = valores.get(chave, 0) + int(linha["n"] or 0)
    serie = []
    for dia in dias:
        por_dia = {nome: valores.get((dia.isoformat(), nome), 0) for nome in nomes}
        serie.append({"dia": dia, "rotulo": dia.strftime("%d/%m"), "canais": por_dia, "total": sum(por_dia.values()),
                      "abriram": abriram_no_dia.get(dia.isoformat(), 0)})

    margem = GRAFICO_MARGEM
    largura_util = GRAFICO_LARGURA - margem["esq"] - margem["dir"]
    altura_util = GRAFICO_ALTURA - margem["topo"] - margem["base"]
    base = margem["topo"] + altura_util
    topo_eixo, marcas = _escala(float(max((max(p["total"], p["abriram"]) for p in serie), default=0)))
    passo_x = largura_util / max(len(serie) - 1, 1)
    for i, p in enumerate(serie):
        p["x"] = margem["esq"] + i * passo_x if len(serie) > 1 else margem["esq"] + largura_util / 2

    def linha_de(chave_valor) -> str:
        return " ".join(f"{p['x']:.1f},{base - chave_valor(p) / topo_eixo * altura_util:.1f}" for p in serie)

    linhas_svg = [{"nome": "total", "rotulo": "Foram à loja (total)", "classe": "linha-total",
                   "pontos": linha_de(lambda p: p["total"]),
                   "fim_y": base - serie[-1]["total"] / topo_eixo * altura_util if serie else base,
                   "soma": sum(p["total"] for p in serie)}]
    for i, nome in enumerate(nomes):
        linhas_svg.append({"nome": nome, "rotulo": nome, "serie": i + 1,
                           "classe": "linha-outros" if nome == "outros" else f"linha-{i + 1}",
                           "pontos": linha_de(lambda p, n=nome: p["canais"][n]),
                           "soma": sum(p["canais"][nome] for p in serie)})
    # Tracejada: quem abriu a página (desde 2026-11-22). Não soma com as idas
    # à loja: quem tocou no botão está nas duas.
    linhas_svg.append({"nome": "abriram", "rotulo": "Abriram a página", "classe": "linha-abriram",
                       "pontos": linha_de(lambda p: p["abriram"]), "soma": sum(p["abriram"] for p in serie)})
    rotulos_x = [p for i, p in enumerate(serie) if i % 7 == (len(serie) - 1) % 7]
    # O que a dica mostra em cada dia (o JavaScript só lê; tudo está na tabela).
    dados_js = [{"x": round(p["x"], 1), "titulo": p["dia"].strftime("%d/%m/%Y"), "total": p["total"],
                 "abriram": p["abriram"], "canais": [[nome, p["canais"][nome]] for nome in nomes]} for p in serie]
    return {
        "serie": serie, "canais": nomes, "linhas": linhas_svg, "rotulos_x": rotulos_x, "dados_js": dados_js,
        "marcas": [{"valor": v, "y": base - v / topo_eixo * altura_util} for v in marcas],
        "largura": GRAFICO_LARGURA, "altura": GRAFICO_ALTURA, "base": base, "margem": margem,
        "total": sum(p["total"] for p in serie), "abriram": sum(p["abriram"] for p in serie), "dias": len(serie),
    }


_SQL_ADMIN = {
    "geral": """
        select count(*) as total,
               count(*) filter (where created_at >= %(ini_hoje)s) as novos_hoje,
               count(*) filter (where created_at >= %(ini_7d)s) as novos_7d,
               count(*) filter (where created_at >= %(ini_mes)s) as novos_mes,
               count(*) filter (where plan in ('pro','business')) as pagantes,
               count(*) filter (where plan='pro' and stripe_subscription_id is not null) as pro_stripe,
               count(*) filter (where plan='pro' and stripe_subscription_id is null
                                  and coalesce(subscription_status, '') not in ('prepago','indicacao','cortesia')) as pro_loja,
               count(*) filter (where plan='pro' and stripe_subscription_id is null
                                  and subscription_status in ('prepago','indicacao','cortesia')) as pro_prepago,
               count(*) filter (where plan='business') as business,
               count(*) filter (where subscription_status='past_due') as past_due,
               count(*) filter (where subscription_status='canceled_pendente') as cancel_pendente,
               count(*) filter (where plan='free' and subscription_status is not null) as ex_assinantes,
               count(*) filter (where plan='free') as gratis
          from public.drivers""",
    "recibos_mes": """
        select count(*) as mes,
               count(*) filter (where created_at >= %(ini_hoje)s) as hoje,
               count(*) filter (where is_guest) as mes_guest,
               coalesce(sum(amount) filter (where currency = 'BRL'), 0) as mes_valor
          from public.receipts where created_at >= %(ini_mes)s""",
    "recibos_estim": """
        select greatest(reltuples, 0)::bigint as n from pg_class where oid = 'public.receipts'::regclass""",
    "pagantes": """
        select id, full_name, email, city, plan, subscription_status,
               case when stripe_subscription_id is not null then 'Stripe'
                    when subscription_status in ('prepago','indicacao','cortesia') then 'Pré-pago'
                    when stripe_customer_id is not null then 'Loja (ex-Stripe)'
                    else 'Loja' end as origem,
               created_at, updated_at
          from public.drivers where plan in ('pro','business')
         order by updated_at desc limit 50""",
    "ex_assinantes": """
        select subscription_status as status, count(*) as n
          from public.drivers where plan='free' and subscription_status is not null
         group by 1 order by n desc""",
    "novos_por_dia": """
        select (created_at at time zone 'America/Sao_Paulo')::date as dia, count(*) as n
          from public.drivers where created_at >= %(ini_30d)s group by 1 order by 1""",
    "novos_por_mes": """
        select public.br_period(created_at) as mes, count(*) as n
          from public.drivers where created_at >= %(ini_12m)s group by 1 order by 1""",
    "ativacao": """
        select count(*) as total,
               count(*) filter (where ativo) as ativados,
               count(*) filter (where created_at >= %(ini_30d)s) as coorte_30d,
               count(*) filter (where created_at >= %(ini_30d)s and ativo) as coorte_30d_ativados,
               count(*) filter (where created_at >= %(ini_30d)s and ativo_24h) as coorte_30d_ativados_24h
          from (select d.created_at,
                       exists (select 1 from public.receipts r where r.driver_id = d.id) as ativo,
                       exists (select 1 from public.receipts r where r.driver_id = d.id
                                  and r.created_at < d.created_at + interval '24 hours') as ativo_24h
                  from public.drivers d) s""",
    "funil": """
        select count(*) as free_ativos_mes,
               count(*) filter (where q.used >= %(limite)s and d.plan='free') as no_teto,
               count(*) filter (where q.used >= %(limite)s and d.plan<>'free') as no_teto_converteram,
               count(*) filter (where q.used between %(limite)s-2 and %(limite)s-1 and d.plan='free') as quase_teto,
               count(*) filter (where q.used between 1 and %(limite)s-3 and d.plan='free') as uso_baixo
          from public.receipt_quotas q join public.drivers d on d.id = q.driver_id
         where q.period = public.br_period()""",
    "candidatos": """
        select d.id, d.full_name, d.email, d.whatsapp, d.city, q.used, d.created_at,
               (select count(*) from public.receipts r where r.driver_id = d.id) as recibos_total
          from public.receipt_quotas q join public.drivers d on d.id = q.driver_id
         where q.period = public.br_period() and q.used >= %(limite)s and d.plan = 'free'
         order by recibos_total desc limit 50""",
    "uso_por_dia": """
        select (created_at at time zone 'America/Sao_Paulo')::date as dia,
               count(*) filter (where not is_guest) as logado,
               count(*) filter (where is_guest) as guest,
               count(*) as n,
               coalesce(sum(amount) filter (where currency = 'BRL'), 0) as valor
          from public.receipts where created_at >= %(ini_30d)s group by 1 order by 1""",
    "uso_por_mes": """
        select public.br_period(created_at) as mes,
               count(*) filter (where not is_guest) as logado,
               count(*) filter (where is_guest) as guest,
               count(*) as n,
               coalesce(sum(amount) filter (where currency = 'BRL'), 0) as valor,
               count(distinct driver_id) as motoristas_ativos
          from public.receipts where created_at >= %(ini_12m)s group by 1 order by 1""",
    "pagamentos": """
        select payment_method as forma, count(*) as n,
               coalesce(sum(amount) filter (where currency = 'BRL'), 0) as valor
          from public.receipts where created_at >= %(ini_30d)s group by 1 order by n desc limit 6""",
    # Cadastros por canal de divulgação (migração 0018): o par do "cliques por
    # canal". Com o gasto de cada anúncio, dá o custo real por cadastro.
    "cadastros_canal": """
        select case when signup_channel = '' then '(sem canal)' else signup_channel end as canal,
               count(*) as n
          from public.drivers where created_at >= %(ini_30d)s
         group by 1 order by n desc, 1 limit 20""",
    # Cliques no /baixar por canal (migração 0014). `direto` é o link sem ?c=.
    "cliques_canal": """
        select channel || ' · ' || case store when 'play' then 'Android' when 'appstore' then 'iPhone'
                                               when 'pagina' then 'abriu a página'
                                               when 'robo' then 'robôs'
                                               else 'computador' end as canal,
               sum(clicks) as n
          from public.download_clicks where day >= %(dia_30d)s
         group by channel, store order by n desc, channel limit 20""",
    # O mesmo, dia a dia e só por canal (o aparelho fica de fora), para o gráfico.
    # `n` são as idas à loja; `abriram`, as visitas à página ('pagina', desde
    # 2026-11-22), à parte: somadas ao toque no botão contariam a mesma pessoa
    # duas vezes. Robô fica fora das duas.
    "cliques_dia": """
        select day as dia, channel as canal,
               coalesce(sum(clicks) filter (where store <> 'pagina'), 0) as n,
               coalesce(sum(clicks) filter (where store = 'pagina'), 0) as abriram
          from public.download_clicks where day >= %(dia_cliques)s and store <> 'robo'
         group by 1, 2 order by 1, 2""",
    # Fila do CRM (PilotPostAi, migração 0021): o que espera envio e o que falhou.
    "crm_fila": """
        select count(*) filter (where sent_at is null) as pendentes,
               count(*) filter (where sent_at is null and attempts > 0) as com_falha,
               count(*) filter (where sent_at >= now() - interval '24 hours' and last_error = '') as enviados_24h,
               count(*) filter (where sent_at is not null and last_error <> '') as descartados,
               (select last_error from public.crm_sync where last_error <> '' order by id desc limit 1) as ultimo_erro,
               (select max(sent_at) from public.crm_sync where last_error = '') as ultimo_envio
          from public.crm_sync""",
    # Faturamento que entrou pelas lojas (RevenueCat): compras e renovações de
    # produção, em reais. Reembolso chega como CANCELLATION com preço negativo.
    # PRODUCT_CHANGE fica fora: a cobrança da troca chega como compra ou renovação.
    "fat_loja": """
        select to_char(public.br_period(received_at), 'YYYY-MM') as mes,
               case when type = 'NON_RENEWING_PURCHASE' then 'unico'
                    when cycle = 'anual' then 'anual' else 'mensal' end as tipo,
               sum(price) as valor, count(*) as n
          from public.billing_events
         where source = 'revenuecat' and environment = 'PRODUCTION' and price is not null
           and upper(coalesce(currency, '')) = 'BRL' and product_id like %(produto_loja)s
           and (type in ('INITIAL_PURCHASE', 'RENEWAL', 'NON_RENEWING_PURCHASE')
                or (type = 'CANCELLATION' and price < 0))
           and received_at >= %(ini_fat)s
           and not exists (select 1 from auth.users u where u.id = billing_events.driver_id
                              and coalesce(u.raw_app_meta_data ->> 'conta_interna', '') = 'true')
         group by 1, 2""",
    # Anual pago uma vez pelo site (Pix ou cartão, mode=payment): o webhook grava
    # o valor e "pro (pré-pago até ...)" quando libera. Não gera fatura na Stripe.
    "fat_unico": """
        select to_char(public.br_period(received_at), 'YYYY-MM') as mes, sum(price) as valor, count(*) as n
          from public.billing_events
         where source = 'stripe' and environment = 'PRODUCTION' and price > 0
           and type in ('checkout.session.completed', 'checkout.session.async_payment_succeeded')
           and outcome like 'pro (pré-pago%%'
           and received_at >= %(ini_fat)s
           and not exists (select 1 from auth.users u where u.id = billing_events.driver_id
                              and coalesce(u.raw_app_meta_data ->> 'conta_interna', '') = 'true')
         group by 1""",
    # Quem assina pela loja e segue renovando: a última cobrança diz o valor, o
    # ciclo e o vencimento, que é a próxima cobrança. Cancelou a renovação
    # (canceled_pendente) ou é teste: fora da previsão.
    "fat_loja_ativas": """
        select distinct on (e.driver_id) e.driver_id, e.price, e.cycle,
               (e.payload ->> 'expiration_at_ms')::bigint as vence_ms
          from public.billing_events e
          join public.drivers d on d.id = e.driver_id
         where e.source = 'revenuecat' and e.environment = 'PRODUCTION'
           and e.type in ('INITIAL_PURCHASE', 'RENEWAL') and e.price > 0
           and upper(coalesce(e.currency, '')) = 'BRL' and e.product_id like %(produto_loja)s
           and d.plan = 'pro' and d.stripe_subscription_id is null
           and d.subscription_status in ('active', 'trialing', 'past_due')
           and not exists (select 1 from auth.users u where u.id = d.id and coalesce(u.raw_app_meta_data ->> 'conta_interna', '') = 'true')
         order by e.driver_id, e.received_at desc, e.seq desc""",
    # Contas internas: ficam fora do faturamento. O cliente da Stripe de cada
    # uma tira as faturas e assinaturas dela da conta do site.
    "fat_internas": """
        select d.id, d.stripe_customer_id as cliente
          from public.drivers d join auth.users u on u.id = d.id
         where coalesce(u.raw_app_meta_data ->> 'conta_interna', '') = 'true'""",
    "cidades_recibos": """
        select coalesce(nullif(initcap(trim(driver_snapshot->>'city')), ''), '(sem cidade)') as cidade,
               count(*) as n, count(distinct driver_id) as motoristas
          from public.receipts where created_at >= %(ini_30d)s group by 1 order by n desc limit 15""",
    "cidades_cadastro": """
        select initcap(trim(city)) as cidade, count(*) as n,
               count(*) filter (where plan <> 'free') as pagantes
          from public.drivers group by 1 order by n desc limit 15""",
    "ticket": """
        select round(avg(amount), 2) as media,
               percentile_cont(0.5) within group (order by amount) as mediana,
               min(amount) as minimo, max(amount) as maximo
          from public.receipts where created_at >= %(ini_30d)s and currency = 'BRL'""",
    "top_emissores": """
        select d.id, d.full_name, d.plan, count(*) as n,
               coalesce(sum(r.amount) filter (where r.currency = 'BRL'), 0) as valor
          from public.receipts r join public.drivers d on d.id = r.driver_id
         where r.created_at >= %(ini_30d)s
         group by d.id, d.full_name, d.plan order by n desc limit 10""",
    "por_hora": """
        select extract(hour from created_at at time zone 'America/Sao_Paulo')::int as hora, count(*) as n
          from public.receipts where created_at >= %(ini_30d)s group by 1 order by 1""",
    "ultimos_recibos": """
        select rid, is_guest, driver_id, driver_snapshot->>'full_name' as motorista,
               driver_snapshot->>'city' as cidade, passenger, amount, currency, payment_method, created_at
          from public.receipts order by created_at desc limit 20""",
    "logins": """
        select count(*) filter (where u.last_sign_in_at >= now() - interval '1 day') as login_24h,
               count(*) filter (where u.last_sign_in_at >= now() - interval '7 days') as login_7d,
               count(*) filter (where u.last_sign_in_at >= now() - interval '30 days') as login_30d,
               count(*) filter (where u.last_sign_in_at is null) as nunca_logou
          from auth.users u join public.drivers d on d.id = u.id""",
    "ultimos_logins": """
        select d.id, d.full_name, d.email, d.plan, u.last_sign_in_at
          from auth.users u join public.drivers d on d.id = u.id
         order by u.last_sign_in_at desc nulls last limit 20""",
    "sessoes": """
        select count(*) as sessoes, count(distinct user_id) as usuarios
          from auth.sessions
         where coalesce(refreshed_at, updated_at, created_at) >= now() - interval '7 days'
           and (not_after is null or not_after > now())""",
    "inativos_30d": """
        select count(*) as n from public.drivers d join auth.users u on u.id = d.id
         where d.created_at < now() - interval '30 days'
           and coalesce(u.last_sign_in_at, 'epoch') < now() - interval '30 days'
           and not exists (select 1 from public.receipts r
                            where r.driver_id = d.id and r.created_at >= now() - interval '30 days')""",
    # Uso do "Fazer corrida" (app 1.2). Mesma janela de 30 dias (meia-noite de
    # Brasilia) dos outros numeros de recibos do painel, para dar para comparar.
    "corrida": """
        select count(*) filter (where created_at >= %(ini_30d)s) as recibos_30d,
               count(distinct driver_id) filter (where created_at >= %(ini_30d)s) as motoristas_30d,
               count(*) as recibos_total
          from public.receipts where via_corrida""",
    # O contador antiabuso conta PEDIDOS, nao estimativas que deram certo: ele
    # sobe antes do teto e antes de o Google responder. Por isso o rotulo e
    # "pedidos", e cada dia e cortado no teto — acima dele so houve recusa. A
    # janela vai no SQL: a limpeza apaga os contadores velhos, mas so quando
    # roda. O join com drivers tira os contadores das contas da suite.
    "estimativas": """
        select coalesce(sum(least(l.count, %(teto_estimativa)s)), 0) as total_7d,
               count(distinct d.id) as motoristas_7d
          from public.rate_limits l
          join public.drivers d on d.id::text = split_part(l.key, ':', 2)
         where l.key like 'estimativa:%%' and l.created_at >= %(ini_7d)s""",
    # Caixa do app 1.8 (migração 0022). SÓ contagens: nem valor, nem categoria,
    # nem lista por motorista — a Política promete que a equipe vê quantos usam,
    # nunca os valores de ninguém. As janelas contam pela chegada ao servidor;
    # na primeira sincronização do 1.8 os recibos antigos entram de uma vez, e
    # "recibo" pula sem ser uso novo. Apagado fica fora das contagens por
    # origem e tipo. Conta interna conta, como no "Fazer corrida": ela só sai
    # do faturamento.
    "caixa": """
        select (select count(distinct driver_id) from public.ledger_entries) as motoristas,
               count(distinct driver_id) filter (where created_at >= %(ini_7d)s) as motoristas_7d,
               count(*) filter (where deleted_at is null) as total_30d,
               count(*) filter (where deleted_at is null and kind = 'income') as receitas_30d,
               count(*) filter (where deleted_at is null and kind = 'expense') as despesas_30d,
               count(*) filter (where deleted_at is null and source = 'receipt') as recibo_30d,
               count(*) filter (where deleted_at is null and source = 'ride') as corrida_30d,
               count(*) filter (where deleted_at is null and source = 'manual') as manual_30d
          from public.ledger_entries where created_at >= %(ini_30d)s""",
    "pagantes_parados": """
        select d.id, d.full_name, d.email, d.whatsapp, d.plan,
               (select max(created_at) from public.receipts r where r.driver_id = d.id) as ultimo_recibo
          from public.drivers d
         where d.plan in ('pro','business')
           and not exists (select 1 from public.receipts r
                            where r.driver_id = d.id and r.created_at >= now() - interval '30 days')
         order by ultimo_recibo nulls first limit 30""",
    "emails_por_dia": r"""
        select substring(key from '(\d{4}-\d{2}-\d{2})$') as dia,
               sum(count) as tentativas, sum(least(count, %(teto_email)s)) as enviados,
               count(*) as motoristas, count(*) filter (where count > %(teto_email)s) as no_teto
          from public.rate_limits where key like 'email:%%' group by 1 order by 1 desc""",
    "resets_por_dia": r"""
        select substring(key from '(\d{4}-\d{2}-\d{2})$') as dia,
               coalesce(sum(count) filter (where key like 'reset:e:%%'), 0) as pedidos,
               count(*) filter (where key like 'reset:e:%%') as emails_distintos,
               count(*) filter (where key like 'reset:ip:%%' and count >= %(teto_reset_ip)s) as ips_no_teto
          from public.rate_limits where key like 'reset:%%' group by 1 order by 1 desc""",
    "tentativas_admin": """
        select key, count, created_at from public.rate_limits
         where key like 'admin_login:%%' order by count desc limit 20""",
    "retencao": """
        select (select min(created_at) from public.receipts where is_guest) as guest_mais_antigo,
               (select count(*) from public.receipts where is_guest and created_at < %(corte_guest)s) as guest_vencidos,
               (select min(created_at) from public.rate_limits) as contador_mais_antigo,
               (select count(*) from public.rate_limits where created_at < %(corte_contadores)s) as contadores_vencidos""",
    "tabelas": """
        select c.relname as tabela, greatest(c.reltuples, 0)::bigint as linhas,
               pg_total_relation_size(c.oid) as bytes
          from pg_class c join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relkind = 'r' order by bytes desc""",
    "indices": """
        select indexname from pg_indexes where schemaname = 'public'
           and indexname in ('receipts_created_idx', 'drivers_created_idx')""",
    # Cobranca, pelos webhooks (migracao 0011). Teste = sandbox das lojas e
    # modo teste da Stripe: nao e assinatura, nao entra nas contagens.
    "assinaturas_por_mes": """
        with e as (
            select *, environment in ('SANDBOX', 'TEST') as teste
              from public.billing_events where received_at >= %(ini_12m)s)
        select public.br_period(received_at) as mes,
               count(*) filter (where not teste and type in ('INITIAL_PURCHASE', 'checkout.session.completed')) as novas,
               count(*) filter (where not teste and type = 'INITIAL_PURCHASE') as loja,
               count(*) filter (where not teste and type = 'checkout.session.completed') as site,
               count(*) filter (where not teste and type in ('INITIAL_PURCHASE', 'checkout.session.completed')
                                  and cycle = 'anual') as anuais,
               count(*) filter (where not teste and type in ('CANCELLATION', 'customer.subscription.deleted')) as cancelamentos,
               count(*) filter (where not teste and outcome like 'free%%') as rebaixados,
               count(*) filter (where teste) as testes
          from e group by 1 order by 1""",
    "ultimos_eventos": """
        select e.received_at, e.source, e.type, e.store, e.environment, e.cycle, e.price, e.currency,
               e.outcome, e.app_user_id, e.driver_id, d.full_name
          from public.billing_events e left join public.drivers d on d.id = e.driver_id
         order by e.received_at desc, e.seq desc limit 15""",
    "ultimo_webhook": """
        select max(updated_at) as em from public.drivers where plan <> 'free'""",
    "admins": """
        select email, last_login_at, password_changed_at from public.admin_users
         order by last_login_at desc nulls last""",
}

# Uma linha (fetchone) ou varias (fetchall)?
_ADMIN_UMA_LINHA = {
    "geral", "recibos_mes", "recibos_estim", "ativacao", "funil", "ticket", "logins",
    "sessoes", "inativos_30d", "retencao", "ultimo_webhook", "corrida", "estimativas", "crm_fila",
    "caixa",
}


def _executar_secoes(conn, nomes: list[str], params: dict, dados: dict) -> None:
    import time as _time

    for nome in nomes:
        inicio = _time.perf_counter()
        try:
            with conn.transaction():  # savepoint: a falha de uma nao derruba as outras
                cur = conn.execute(_SQL_ADMIN[nome], params)
                dados[nome] = dict(cur.fetchone() or {}) if nome in _ADMIN_UMA_LINHA \
                    else [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            app.logger.warning("admin: secao %s indisponivel: %s", nome, exc)
            dados[nome] = None
        dados["_tempos"][nome] = round((_time.perf_counter() - inicio) * 1000)


def _modo_da_chave_stripe(chave: str) -> str:
    """Rótulo da chave secreta da Stripe na saúde do painel, só pelo prefixo.

    `sk_live_` é a chave padrão e `rk_live_` a chave restrita: as duas cobram
    de verdade. O site usa uma restrita, e até 2026-11-24 o painel a rotulava
    "modo teste" (só conhecia `sk_live_`), o que virou um alarme falso de
    "ninguém está sendo cobrado". `sk_test_`/`rk_test_` são o sandbox. Outro
    prefixo é chave errada: já aconteceu de colar a `pk_` no lugar.
    """
    if not chave:
        return ""
    if chave.startswith("sk_live_"):
        return "modo live"
    if chave.startswith("rk_live_"):
        return "modo live (chave restrita)"
    if chave.startswith(("sk_test_", "rk_test_")):
        return "modo teste: ninguém é cobrado"
    return "prefixo desconhecido: confira a chave"


def _saude_do_sistema() -> dict:
    """Configuracao presente ou ausente. Nunca o valor — so booleano e prefixo."""
    def tem(nome: str) -> bool:
        return bool(os.environ.get(nome, "").strip())

    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    return {
        "integracoes": [
            ("Stripe (chave secreta)", bool(stripe_key), _modo_da_chave_stripe(stripe_key)),
            ("Stripe (webhook)", tem("STRIPE_WEBHOOK_SECRET"), ""),
            ("Stripe (preço Pro mensal)", tem("STRIPE_PRO_PRICE_ID"), ""),
            ("Stripe (preço Pro anual)", tem("STRIPE_PRO_ANUAL_PRICE_ID"), ""),
            ("Stripe (preço Pro anual por Pix, pagamento único)", tem("STRIPE_PRO_ANUAL_PIX_PRICE_ID"),
             "Pix ligado também em Configurações → Formas de pagamento"),
            ("Resend (e-mail)", tem("RESEND_API_KEY"), os.environ.get("EMAIL_FROM", "").strip()),
            ("RevenueCat (webhook)", tem("REVENUECAT_WEBHOOK_SECRET"), ""),
            ("Cron de limpeza", tem("CRON_SECRET"), "04:00 UTC, diário"),
            ("Cron da automação de e-mails", tem("CRON_SECRET"), "de hora em hora"),
            ("Cron do CRM (PilotPostAi)", tem("CRON_SECRET"), "a cada 15 minutos"),
            ("CRM PilotPostAi (CRM_URL + CRM_SECRET)", crm_configurado(), os.environ.get("CRM_URL", "").strip()[:70]),
            ("Resposta dos e-mails automáticos (EMAIL_REPLY_TO)", tem("EMAIL_REPLY_TO"),
             os.environ.get("EMAIL_REPLY_TO", "").strip()),
            ("Avisos para a equipe (cadastro e assinatura)", bool(os.environ.get("EMAIL_AVISOS", EMAIL_AVISOS_PADRAO).strip()),
             os.environ.get("EMAIL_AVISOS", EMAIL_AVISOS_PADRAO).strip()),
            ("APP_BASE_URL", tem("APP_BASE_URL"), os.environ.get("APP_BASE_URL", "").strip()),
            ("Supabase (URL)", tem("SUPABASE_URL"), ""),
            ("Supabase (chave de serviço)", tem("SUPABASE_SECRET_KEY") or tem("SUPABASE_SERVICE_ROLE_KEY"), ""),
            ("SECRET_KEY com 32+ caracteres", len(os.environ.get("SECRET_KEY", "")) >= 32, ""),
        ],
        "deploy": {
            "ambiente": os.environ.get("VERCEL_ENV", "local"),
            "regiao": os.environ.get("VERCEL_REGION", ""),
            "commit": os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:7],
            "mensagem": os.environ.get("VERCEL_GIT_COMMIT_MESSAGE", "")[:80],
        },
        "constantes": [
            ("Recibos/mês no Grátis", FREE_MONTHLY_LIMIT),
            ("E-mails/dia por motorista", EMAIL_DAILY_LIMIT),
            ("Retenção de recibos sem conta (dias)", GUEST_RETENTION_DAYS),
            ("Retenção de contadores (dias)", COUNTER_RETENTION_DAYS),
            ("Pedidos de nova senha/dia por e-mail", RESET_DAILY_LIMIT_EMAIL),
            ("Tentativas de login no painel/dia por IP", ADMIN_LOGIN_DAILY_LIMIT_IP),
        ],
        # Com nome, e nao pela posicao em `constantes`: o titulo da secao de
        # e-mail lia o indice errado e anunciava 5 dias em vez de 7.
        "retencao_contadores_dias": COUNTER_RETENTION_DAYS,
    }


def _params_dashboard_admin() -> dict:
    """Parametros das consultas de _SQL_ADMIN. A suite usa os mesmos."""
    agora = datetime.now(timezone.utc)
    return {
        "ini_hoje": _inicio_dia_br(0),
        "ini_7d": _inicio_dia_br(6),
        "ini_30d": _inicio_dia_br(29),
        # download_clicks guarda a data (dia de Brasília), não o instante.
        "dia_30d": (datetime.now(BR_TZ) - timedelta(days=29)).date().isoformat(),
        "dia_cliques": (datetime.now(BR_TZ) - timedelta(days=CLIQUES_DIAS - 1)).date().isoformat(),
        "ini_mes": _inicio_mes_br(0),
        "ini_12m": _inicio_mes_br(11),
        "ini_fat": _inicio_mes_br(FATURAMENTO_MESES_PASSADOS),
        # Só os produtos do Recibo Táxi nas lojas (ver PRODUTO_LOJA_PREFIXO).
        "produto_loja": PRODUTO_LOJA_PREFIXO.replace("_", "\\_") + "%",
        "limite": FREE_MONTHLY_LIMIT,
        "teto_email": EMAIL_DAILY_LIMIT,
        "teto_reset_ip": RESET_DAILY_LIMIT_IP,
        "teto_estimativa": ESTIMATIVA_DIARIA_LIMITE,
        "corte_guest": to_utc_iso(agora - timedelta(days=GUEST_RETENTION_DAYS + 1)),
        "corte_contadores": to_utc_iso(agora - timedelta(days=COUNTER_RETENTION_DAYS + 1)),
    }


def montar_dashboard_admin() -> dict:
    """Numeros do painel, por secao. Ver templates/admin/dashboard.html."""
    import time as _time

    store = get_store()
    params = _params_dashboard_admin()
    d: dict = {"_tempos": {}, "gerado_em": datetime.now(BR_TZ)}

    inicio = _time.perf_counter()
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute("set local statement_timeout = 4000")
            d["latencia_ms"] = None
            t0 = _time.perf_counter()
            conn.execute("select 1").fetchone()
            d["latencia_ms"] = round((_time.perf_counter() - t0) * 1000, 1)
            _executar_secoes(conn, list(_SQL_ADMIN), params, d)
    d["_tempos"]["total"] = round((_time.perf_counter() - inicio) * 1000)

    # Series com os dias/meses vazios preenchidos e a barra ja calculada.
    d["novos_por_dia"] = _com_maximo(_serie_dias(d.get("novos_por_dia") or [], 30, ("n",)), "n")
    d["novos_por_mes"] = _com_maximo(_serie_meses(d.get("novos_por_mes") or [], 12, ("n",)), "n")
    d["uso_por_dia"] = _com_maximo(_serie_dias(d.get("uso_por_dia") or [], 30, ("logado", "guest", "n", "valor")), "n")
    d["uso_por_mes"] = _com_maximo(
        _serie_meses(d.get("uso_por_mes") or [], 12, ("logado", "guest", "n", "valor", "motoristas_ativos")), "n")
    por_hora = {int(l["hora"]): int(l["n"]) for l in (d.get("por_hora") or [])}
    d["por_hora"] = _com_maximo([{"hora": h, "n": por_hora.get(h, 0)} for h in range(24)], "n")
    for nome in ("pagamentos", "cidades_recibos", "cidades_cadastro"):
        d[nome] = _com_maximo(d.get(nome) or [], "n")
    if d.get("cliques_canal") is not None:
        d["cliques_canal"] = _com_maximo(d["cliques_canal"], "n")
    if d.get("cadastros_canal") is not None:
        d["cadastros_canal"] = _com_maximo(d["cadastros_canal"], "n")
    d["cliques_grafico"] = montar_cliques_por_dia(d.get("cliques_dia"))
    # A Stripe fica fora da transação do banco: é uma ida à API, com o cache
    # de 60 s do painel em cima.
    t0 = _time.perf_counter()
    d["faturamento"] = montar_faturamento(d)
    d["_tempos"]["faturamento"] = round((_time.perf_counter() - t0) * 1000)
    if d.get("ultimos_eventos") is not None:
        d["ultimos_eventos"] = [dict(e, origem=nome_da_loja(e.get("store"), e.get("source")))
                                for e in d["ultimos_eventos"]]

    # MRR como faixa: o banco nao diz quem e mensal e quem e anual, nem o preco
    # do Business legado. Piso = todo Pro anual (59,90/12), na Stripe e na loja;
    # teto = todo Pro mensal. Loja: bruto; a Apple fica com 15%.
    g = d.get("geral") or {}
    pro_stripe = int(g.get("pro_stripe") or 0)
    pro_loja = int(g.get("pro_loja") or 0)
    d["mrr"] = {
        # Preços de 2026-11-10 para todo mundo: assinatura feita antes continua
        # no preço antigo até migrar, então a faixa pode ficar baixa por um tempo.
        "piso": round(pro_stripe * 59.90 / 12 + pro_loja * 59.90 / 12 * 0.85, 2),
        "teto": round((pro_stripe + pro_loja) * 9.90, 2),
        "business_sem_preco": int(g.get("business") or 0),
    }
    a = d.get("ativacao") or {}
    d["ativacao_pct"] = round(100 * int(a.get("ativados") or 0) / max(int(a.get("total") or 0), 1))
    d["ativacao_30d_pct"] = round(100 * int(a.get("coorte_30d_ativados") or 0) / max(int(a.get("coorte_30d") or 0), 1))
    d["saude"] = _saude_do_sistema()
    d["indices_ok"] = {r["indexname"] for r in (d.get("indices") or [])} >= {"receipts_created_idx", "drivers_created_idx"}
    return d


# ── Lista e ficha de motoristas ──────────────────────────────────────────────

_ADMIN_CURSOR_SALT = "admin-cursor"


def _cursor_dumps(created_at, driver_id: str) -> str:
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=_ADMIN_CURSOR_SALT).dumps(
        [to_utc_iso(created_at), str(driver_id)])


def _cursor_loads(valor: str) -> tuple[str | None, str | None]:
    """Cursor invalido ou adulterado vira primeira pagina, nao 500."""
    if not valor:
        return None, None
    try:
        ts, did = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=_ADMIN_CURSOR_SALT).loads(valor, max_age=86400)
        return str(ts), str(did)
    except Exception:
        return None, None


def _escapar_like(valor: str) -> str:
    return valor.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Botoes dos cards de Engajamento do dashboard: cada um abre a lista com um
# destes filtros. A condicao e a mesma da consulta que gera o numero do card
# (_SQL_ADMIN: logins, sessoes, inativos_30d). Mudou la, muda aqui — senao o
# card diz 11 e a lista traz 9.
_FILTROS_ENGAJAMENTO = {
    "login_24h": ("Entraram nas últimas 24 h", "u.last_sign_in_at >= now() - interval '1 day'"),
    "login_7d": ("Entraram nos últimos 7 dias", "u.last_sign_in_at >= now() - interval '7 days'"),
    "login_30d": ("Entraram nos últimos 30 dias", "u.last_sign_in_at >= now() - interval '30 days'"),
    "sessao_viva": ("Sessão viva nos últimos 7 dias", """exists (
        select 1 from auth.sessions ss
         where ss.user_id = d.id
           and coalesce(ss.refreshed_at, ss.updated_at, ss.created_at) >= now() - interval '7 days'
           and (ss.not_after is null or ss.not_after > now()))"""),
    "inativos_30d": ("Inativos há 30 dias", """d.created_at < now() - interval '30 days'
        and coalesce(u.last_sign_in_at, 'epoch') < now() - interval '30 days'
        and not exists (select 1 from public.receipts r
                         where r.driver_id = d.id and r.created_at >= now() - interval '30 days')"""),
    "nunca_logou": ("Nunca entraram", "u.last_sign_in_at is null"),
}


@app.get("/admin/motoristas")
@admin_required
def admin_motoristas():
    q = request.args.get("q", "").strip()[:80]
    plano = request.args.get("plano", "") if request.args.get("plano") in ("free", "pro", "business") else ""
    status = request.args.get("status", "").strip()[:40]
    origem = request.args.get("origem", "") if request.args.get("origem") in ("stripe", "loja", "prepago") else ""
    so_teto = request.args.get("so_teto") == "1"
    sem_recibo = request.args.get("sem_recibo") == "1"
    filtro = request.args.get("filtro", "") if request.args.get("filtro") in _FILTROS_ENGAJAMENTO else ""
    # Vem do dicionario acima, nunca do pedido: pode entrar direto no SQL.
    condicao_filtro = _FILTROS_ENGAJAMENTO[filtro][1] if filtro else "true"
    cur_ts, cur_id = _cursor_loads(request.args.get("cursor", ""))
    tamanho = max(1, min(int(ADMIN_PAGE_SIZE), 100))

    digitos = re.sub(r"\D", "", q)
    params = {
        "cur_ts": cur_ts, "cur_id": cur_id,
        "plan": plano or None, "status": status or None, "origem": origem or None,
        "q": f"%{_escapar_like(q)}%" if q else None,
        # CPF e WhatsApp sao gravados como o motorista digitou, com ou sem
        # pontuacao: a comparacao e so de digitos, dos dois lados. "Contem" e
        # nao "comeca com", para achar o telefone digitado sem o DDD.
        "qdig": f"%{digitos}%" if len(digitos) >= 4 else None,
        "so_teto": so_teto, "sem_recibo": sem_recibo,
        "limite": FREE_MONTHLY_LIMIT, "n": tamanho + 1,
    }
    sql = f"""
        select d.id, d.full_name, d.email, d.city, d.plate, d.plan, d.subscription_status,
               case when d.plan = 'free' then '-'
                    when d.stripe_subscription_id is not null then 'Stripe'
                    when d.subscription_status in ('prepago','indicacao','cortesia') then 'Pré-pago'
                    else 'Loja' end as origem,
               d.created_at, u.last_sign_in_at, s.n as recibos, s.ultimo as ultimo_recibo, q.used as uso_mes
          from public.drivers d
          left join auth.users u on u.id = d.id
          left join lateral (select count(*) as n, max(created_at) as ultimo
                               from public.receipts r where r.driver_id = d.id) s on true
          left join public.receipt_quotas q on q.driver_id = d.id and q.period = public.br_period()
         where (%(cur_ts)s::timestamptz is null
                or (d.created_at, d.id) < (%(cur_ts)s::timestamptz, %(cur_id)s::uuid))
           and (%(plan)s::text is null or d.plan = %(plan)s)
           and (%(status)s::text is null or d.subscription_status = %(status)s)
           and (%(origem)s::text is null
                or (%(origem)s = 'stripe' and d.stripe_subscription_id is not null)
                or (%(origem)s = 'loja' and d.plan <> 'free' and d.stripe_subscription_id is null
                    and coalesce(d.subscription_status, '') not in ('prepago','indicacao','cortesia'))
                or (%(origem)s = 'prepago' and d.plan <> 'free' and d.stripe_subscription_id is null
                    and d.subscription_status in ('prepago','indicacao','cortesia')))
           and (%(q)s::text is null or d.email ilike %(q)s or d.full_name ilike %(q)s
                or d.plate ilike %(q)s
                or (%(qdig)s::text is not null
                    and (regexp_replace(d.whatsapp, '\\D', '', 'g') like %(qdig)s
                         or regexp_replace(d.cpf, '\\D', '', 'g') like %(qdig)s)))
           and (not %(so_teto)s or coalesce(q.used, 0) >= %(limite)s)
           and (not %(sem_recibo)s or s.n = 0)
           and ({condicao_filtro})
         order by d.created_at desc, d.id desc
         limit %(n)s"""
    with get_store().pool.connection() as conn:
        linhas = [dict(r) for r in conn.execute(sql, params).fetchall()]

    proximo = ""
    if len(linhas) > tamanho:
        linhas = linhas[:tamanho]
        proximo = _cursor_dumps(linhas[-1]["created_at"], linhas[-1]["id"])
    app.logger.info("admin %s listou motoristas (busca com %d caracteres)", g.admin["email"], len(q or ""))
    return render_template(
        "admin/motoristas.html", motoristas=linhas, proximo=proximo,
        filtros={"q": q, "plano": plano, "status": status, "origem": origem,
                 "so_teto": so_teto, "sem_recibo": sem_recibo, "filtro": filtro},
        filtros_engajamento={nome: rotulo for nome, (rotulo, _) in _FILTROS_ENGAJAMENTO.items()},
    )


@app.get("/admin/motoristas/<uuid:driver_id>")
@admin_required
def admin_motorista(driver_id):
    store = get_store()
    did = str(driver_id)
    with store.pool.connection() as conn:
        cols = ", ".join("d." + c.strip() for c in PostgresStore._DRIVER_COLS.split(",") if c.strip())
        perfil = conn.execute(
            f"""select {cols}, u.last_sign_in_at, u.email_confirmed_at,
                       u.banned_until, u.created_at as auth_created_at
                  from public.drivers d left join auth.users u on u.id = d.id
                 where d.id = %(id)s""", {"id": did}).fetchone()
        if not perfil:
            abort(404)
        totais = conn.execute(
            """select count(*) as n,
                      min(created_at) as primeiro, max(created_at) as ultimo,
                      count(*) filter (where created_at >= %(ini_30d)s) as n_30d
                 from public.receipts where driver_id = %(id)s""",
            {"id": did, "ini_30d": _inicio_dia_br(29)}).fetchone()
        # Uma soma por moeda: somar colones com reais daria um número sem sentido.
        por_moeda = conn.execute(
            """select currency, sum(amount) as valor from public.receipts
                where driver_id = %s group by currency order by currency <> 'BRL', currency""",
            (did,)).fetchall()
        cotas = conn.execute(
            "select period, used from public.receipt_quotas where driver_id = %s order by period desc limit 6",
            (did,)).fetchall()
        try:
            with conn.transaction():
                sessoes = conn.execute(
                    "select count(*) as n from auth.sessions where user_id = %s and (not_after is null or not_after > now())",
                    (did,)).fetchone()["n"]
        except Exception:
            sessoes = None
        emails_hoje = conn.execute(
            "select count from public.rate_limits where key = %s", (f"email:{did}:{today_br()}",)).fetchone()
        try:
            with conn.transaction():
                automacao = conn.execute(
                    """select d.marketing_optout_at,
                              count(s.id) filter (where s.status = 'sent') as recebidos,
                              max(s.created_at) filter (where s.status = 'sent') as ultimo
                         from public.drivers d
                         left join public.email_sends s on s.driver_id = d.id
                        where d.id = %s
                        group by d.id""", (did,)).fetchone()
        except Exception:
            automacao = None  # migração 0007 ainda não aplicada
        try:
            with conn.transaction():
                eventos = [dict(e, origem=nome_da_loja(e["store"], e["source"])) for e in conn.execute(
                    """select received_at, source, type, store, environment, cycle, price, currency,
                              outcome, app_user_id
                         from public.billing_events where driver_id = %s
                        order by received_at desc, seq desc limit 30""", (did,)).fetchall()]
        except Exception:
            eventos = None  # migração 0011 ainda não aplicada
        try:
            with conn.transaction():
                indicacoes = dict(conn.execute(
                    """select count(*) as n, count(activated_at) as ativas
                         from public.referrals where referrer_id = %s""", (did,)).fetchone())
                indicacoes["por"] = conn.execute(
                    "select id, full_name from public.drivers where id = %s",
                    (perfil["referred_by"],)).fetchone() if perfil.get("referred_by") else None
        except Exception:
            indicacoes = None  # migração 0012 ainda não aplicada
        try:
            with conn.transaction():
                canal_cadastro = conn.execute(
                    "select signup_channel from public.drivers where id = %s", (did,)).fetchone()["signup_channel"]
        except Exception:
            canal_cadastro = None  # migração 0018 ainda não aplicada
    recibos = store.list_receipts_by_driver(did, limit=20)
    app.logger.info("admin %s abriu a ficha de %s", g.admin["email"], did)
    # Os links saem do motorista como o store devolve (datas em ISO, id em
    # texto): a digital do link de senha é calculada sobre esse formato.
    motorista = store.get_user_by_id(did) or {}
    return render_template(
        "admin/motorista.html", m=dict(perfil), totais=dict(totais), cotas=[dict(c) for c in cotas],
        valor_emitido=" · ".join(formatar_valor(l["valor"], l["currency"]) for l in por_moeda)
        or formatar_valor(0),
        sessoes=sessoes, emails_hoje=(emails_hoje or {}).get("count", 0), recibos=recibos,
        automacao=dict(automacao) if automacao else None,
        eventos=eventos, indicacoes=indicacoes,
        limite=FREE_MONTHLY_LIMIT,
        assina_loja=assina_pela_loja(motorista), status_prepago=STATUS_PREPAGO,
        interna=conta_interna(did),
        canal_cadastro=canal_cadastro,
        venda=_venda_na_ficha(motorista, request.args) if motorista else None,
    )


@app.post("/admin/motoristas/<uuid:driver_id>/excluir")
@admin_required
def admin_excluir_motorista(driver_id):
    """Exclui o cadastro de um motorista pelo painel.

    Segue as mesmas travas da exclusão que o próprio motorista faz, menos a
    senha dele. No lugar da senha entram a sessão de admin, o CSRF e o
    "EXCLUIR" digitado.
    """
    did = str(driver_id)
    motorista = get_store().get_user_by_id(did)
    if not motorista:
        abort(404)
    voltar = redirect(url_for("admin_motorista", driver_id=did))

    if request.form.get("confirmacao", "").strip().upper() != "EXCLUIR":
        flash("Digite EXCLUIR para confirmar. Nada foi apagado.", "danger")
        return voltar

    # Apagar sem parar a cobrança deixaria uma assinatura órfã, cobrando
    # alguém que já não tem conta para cancelar. Mesma regra do site e do app.
    assinatura = motorista.get("stripe_subscription_id")
    if assinatura:
        stripe = get_stripe()
        try:
            if not stripe:
                raise RuntimeError("Stripe não configurada")
            stripe.Subscription.cancel(assinatura)
        except Exception as exc:
            app.logger.error("admin: não cancelou a assinatura %s: %s", assinatura, exc)
            flash("Não consegui cancelar a assinatura na Stripe, então o cadastro "
                  "não foi excluído. Tente de novo em alguns minutos.", "danger")
            return voltar
    elif assina_pela_loja(motorista):
        # A Apple e a Google não deixam o servidor cancelar por fora. Pro
        # pré-pago (indicação, Pix, cortesia) não cobra nada: pode apagar.
        flash("Este motorista assina pela App Store ou pelo Google Play. Ele precisa "
              "cancelar na loja antes; apagar agora não para a cobrança de lá.", "danger")
        return voltar

    try:
        # O cascade do Auth leva perfil, recibos e cotas junto.
        auth_excluir_usuario(did)
        sincronizar_crm_agora(did)
    except Exception as exc:
        app.logger.error("admin: falha ao excluir %s no Auth: %s", did, exc)
        flash("O Supabase recusou a exclusão. Nada foi apagado; tente de novo.", "danger")
        return voltar

    _ADMIN_CACHE.pop("dashboard", None)
    app.logger.warning("admin %s excluiu o motorista %s (%s)", g.admin["email"], did,
                       filtro_mascarar_email(motorista.get("email")))
    flash(f"Cadastro de {motorista.get('full_name') or motorista.get('email')} excluído, com todos os recibos.", "success")
    return redirect(url_for("admin_motoristas"))


# Dias de Pro que o painel pode dar de uma vez. Lista fechada: o campo vem do
# formulário, e um número livre viraria "Pro para sempre" com um zero a mais.
PRO_CORTESIA_DIAS = (30, 90, 180, 365)


@app.post("/admin/motoristas/<uuid:driver_id>/pro-cortesia")
@admin_required
def admin_pro_cortesia(driver_id):
    """Libera Pro sem cobrança pelo painel: cooperativa parceira, criador em teste.

    Usa a mesma mecânica do Pro pré-pago (drivers.pro_until, rebaixado pelo
    cron quando a data passa). Quem já assina pela Stripe ou pela loja continua
    assinante e os dias ficam guardados para quando a assinatura acabar.
    """
    did = str(driver_id)
    motorista = get_store().get_user_by_id(did)
    if not motorista:
        abort(404)
    voltar = redirect(url_for("admin_motorista", driver_id=did))
    try:
        dias = int(request.form.get("dias", ""))
    except ValueError:
        dias = 0
    if dias not in PRO_CORTESIA_DIAS:
        flash("Escolha um dos prazos da lista. Nada foi alterado.", "danger")
        return voltar
    fim = conceder_pro_prepago(motorista, dias, "cortesia")
    _ADMIN_CACHE.pop("dashboard", None)
    app.logger.warning("admin %s deu %s dias de Pro de cortesia a %s (%s); vale até %s",
                       g.admin["email"], dias, did, motorista.get("email"), fim.date().isoformat())
    flash(f"{dias} dias de Pro liberados para {motorista.get('full_name') or motorista.get('email')}, "
          f"até {fim.astimezone(BR_TZ).strftime('%d/%m/%Y')}.", "success")
    return voltar


@app.post("/admin/motoristas/<uuid:driver_id>/interna")
@admin_required
def admin_conta_interna(driver_id):
    """Marca (ou desmarca) a conta como interna: dono, equipe, teste com cartão
    real. Ela segue funcionando igual; só sai do faturamento da visão geral."""
    did = str(driver_id)
    motorista = get_store().get_user_by_id(did)
    if not motorista:
        abort(404)
    interna = request.form.get("interna") == "1"
    try:
        auth_marcar_conta_interna(did, interna)
    except Exception as exc:
        app.logger.error("admin: não gravei a marca de conta interna de %s: %s", did, exc)
        flash("O Supabase não aceitou a mudança agora. Tente de novo em alguns instantes.", "danger")
        return redirect(url_for("admin_motorista", driver_id=did))
    _ADMIN_CACHE.pop("dashboard", None)
    app.logger.warning("admin %s %s %s como conta interna", g.admin["email"],
                       "marcou" if interna else "desmarcou", did)
    flash("Conta marcada como interna: fica fora do faturamento da visão geral." if interna
          else "Conta voltou a contar no faturamento da visão geral.", "success")
    return redirect(url_for("admin_motorista", driver_id=did))


@app.post("/admin/motoristas/<uuid:driver_id>/sem-ofertas")
@admin_required
def admin_sem_ofertas(driver_id):
    """O motorista pediu para não ser mais procurado com ofertas (ao vendedor,
    ao suporte ou respondendo "SAIR"). A Política promete marcar na conta e
    parar as ofertas por e-mail, WhatsApp e telefone: é a mesma marca do
    descadastro pelo link do e-mail, que já tira a conta da régua."""
    did = str(driver_id)
    with get_store().pool.connection() as conn:
        linha = conn.execute(
            """update public.drivers set marketing_optout_at = coalesce(marketing_optout_at, now())
                where id = %s returning id""", (did,)).fetchone()
    if not linha:
        abort(404)
    _ADMIN_CACHE.pop("dashboard", None)
    sincronizar_crm_agora(did)
    app.logger.warning("admin %s marcou %s como sem ofertas", g.admin["email"], did)
    flash("Pronto: este motorista não recebe mais ofertas por e-mail, WhatsApp nem telefone. "
          "Tire o nome dele da lista do vendedor.", "success")
    return redirect(url_for("admin_motorista", driver_id=did))


@app.post("/admin/crm/enfileirar-todos")
@admin_required
def admin_crm_enfileirar_todos():
    """Carga inicial (e reenvio geral) do CRM: todo motorista entra na fila como
    'created'. O PilotPostAi é idempotente pela chave do motorista, então
    repetir só atualiza. O cron manda em lotes de CRM_POR_RODADA a cada 15 min."""
    try:
        with get_store().pool.connection() as conn:
            n = conn.execute("insert into public.crm_sync (driver_id, event) select id, 'created' from public.drivers").rowcount
    except psycopg.errors.UndefinedTable:
        flash("A fila do CRM ainda não existe no banco: rode a migração 0021 antes.", "danger")
        return redirect(url_for("admin_dashboard"))
    _ADMIN_CACHE.pop("dashboard", None)
    app.logger.warning("admin %s enfileirou %s motorista(s) para o CRM", g.admin["email"], n)
    flash(f"{n} motorista(s) na fila do CRM. O envio sai em lotes de {CRM_POR_RODADA} a cada 15 minutos.", "success")
    return redirect(url_for("admin_dashboard"))


# ── Painel: vender o Pro (link de pagamento, cadastro e formulário) ──────────

def ja_assina(motorista: dict) -> str:
    """"ja_assina_stripe", "ja_assina_loja" ou "" (pode comprar). Pro pré-pago
    (Pix, cortesia, indicação) pode comprar, como no botão do site."""
    if motorista.get("plan") in PAID_PLANS and motorista.get("subscription_status") not in STATUS_PREPAGO:
        return "ja_assina_stripe" if motorista.get("stripe_subscription_id") else "ja_assina_loja"
    return ""


def link_whatsapp(fone: str, texto: str) -> str:
    """wa.me com a mensagem pronta. Sem telefone, o WhatsApp pede o contato."""
    return f"https://wa.me/{fone}?text={quote(texto)}" if fone else f"https://wa.me/?text={quote(texto)}"


def mensagem_link_de_pagamento(motorista: dict, plano: str, url: str, vence: datetime) -> str:
    return "\n".join([
        f"Olá, {_primeiro_nome(motorista.get('full_name', ''))}! Aqui é do {APP_NAME}.",
        f"Este é o link para ativar o seu Pro ({PLANOS_DO_LINK[plano]}):",
        url,
        f"O link é só seu e vale até {vence.astimezone(BR_TZ).strftime('%d/%m/%Y')}. "
        "Depois de pagar, é só abrir o app: o Pro aparece sozinho.",
    ])


def mensagem_criar_senha(motorista: dict, url: str, plano: str = "") -> str:
    acao = "Complete o seu cadastro e crie sua senha neste link" if campos_que_faltam(motorista) else "Crie sua senha neste link"
    passo = (f"{acao}; logo depois você ativa o Pro ({PLANOS_DO_LINK[plano]}):"
             if plano in PLANOS_DO_LINK else f"{acao}:")
    return "\n".join([
        f"Olá, {_primeiro_nome(motorista.get('full_name', ''))}! Aqui é do {APP_NAME}. Sua conta está pronta.",
        passo,
        url,
        f"Depois é só baixar o app e entrar com o e-mail {motorista.get('email', '')} e a sua senha: "
        f"{absolute_url('baixar_app', c='whatsapp')}",
    ])


def mensagem_formulario(plano: str, url: str) -> str:
    passo = (f"Faça o seu cadastro neste link; no final você já ativa o Pro ({PLANOS_DO_LINK[plano]}):"
             if plano in PLANOS_DO_LINK else "Faça o seu cadastro neste link:")
    return "\n".join([f"Olá! Aqui é do {APP_NAME}.", passo, url])


def _venda_na_ficha(motorista: dict, args) -> dict:
    """Os cartões "Vender o Pro" e "Primeiro acesso" da ficha.

    Gerar link não grava nada (o token é assinado, não guardado), por isso é
    GET: ?link=<plano> gera o de pagamento, ?senha_plano=<plano> põe o plano
    no de criar senha.
    """
    disponiveis = planos_do_link_disponiveis()
    fone = phone_e164(motorista.get("whatsapp", ""), motorista.get("country") or PAIS_PADRAO)
    venda = {"planos": disponiveis, "todos": PLANOS_DO_LINK, "bloqueio": ja_assina(motorista),
             "stripe": bool(get_stripe()), "fone": fone, "link": None, "senha": None, "erro": ""}
    plano = args.get("link", "")
    if plano:
        if plano in disponiveis:
            url, vence = gerar_link_de_pagamento(motorista["_id"], plano)
            texto = mensagem_link_de_pagamento(motorista, plano, url, vence)
            venda["link"] = {"plano": plano, "url": url, "vence": vence, "texto": texto,
                             "whatsapp": link_whatsapp(fone, texto)}
            # O link é uma credencial de pagamento: no log vai só que ele existiu.
            app.logger.warning("admin %s gerou link de pagamento (%s) para %s (%s)", g.admin["email"], plano,
                               motorista["_id"], filtro_mascarar_email(motorista.get("email")))
        else:
            venda["erro"] = "Este plano não está disponível agora. Escolha outro da lista."
    if senha_pendente(motorista):
        # Plano só para quem ainda está no Grátis: quem já pagou pelo link de
        # pagamento não pode ser mandado a pagar de novo depois da senha.
        plano_senha = plano_do_primeiro_acesso(motorista, args.get("senha_plano", ""))
        url = gerar_link_criar_senha(motorista, plano_senha)
        texto = mensagem_criar_senha(motorista, url, plano_senha)
        venda["senha"] = {"plano": plano_senha, "url": url, "texto": texto, "whatsapp": link_whatsapp(fone, texto),
                          "ja_e_pro": motorista.get("plan") in PAID_PLANS,
                          "prova": prova_de_posse(motorista)[0], "faltam": rotulos_que_faltam(motorista)}
    return venda


@app.post("/admin/motoristas/<uuid:driver_id>/criar-senha")
@admin_required
def admin_reenviar_criar_senha(driver_id):
    """Manda por e-mail o link de criar senha de uma conta feita pelo painel.

    É um botão, e não automático: o e-mail foi ditado por telefone, e o link
    entra na conta. O admin confere o endereço com o motorista antes.
    """
    did = str(driver_id)
    motorista = get_store().get_user_by_id(did)
    if not motorista:
        abort(404)
    plano = plano_do_primeiro_acesso(motorista, request.form.get("plano_link", ""))
    voltar = redirect(url_for("admin_motorista", driver_id=did, senha_plano=plano or None) + "#primeiro-acesso")
    if not senha_pendente(motorista):
        flash("Esta conta já tem senha (ou não foi criada pelo painel). Se ele esqueceu, "
              "o caminho é o \"Esqueci minha senha\" no app ou no site.", "warning")
        return voltar
    if enviar_link_criar_senha(motorista, gerar_link_criar_senha(motorista, plano), plano):
        app.logger.warning("admin %s mandou por e-mail o link de criar senha a %s (%s)", g.admin["email"], did,
                           filtro_mascarar_email(motorista.get("email")))
        flash(f"Link para criar a senha enviado para {motorista.get('email')}.", "success")
    else:
        flash("O e-mail não saiu. Mande o link pelo WhatsApp.", "danger")
    return voltar


@app.route("/admin/motoristas/novo", methods=["GET", "POST"])
@admin_required
def admin_motorista_novo():
    """Cadastro feito pelo painel, a pedido do motorista (venda por telefone).

    A conta nasce com uma senha aleatória que ninguém conhece e a marca do
    painel no app_metadata. O motorista cria a própria senha pelo link de 7
    dias, que aparece na ficha para mandar pelo WhatsApp (o número que o
    vendedor acabou de usar). O e-mail não sai sozinho: foi ditado por telefone,
    e um endereço errado entregaria a conta ao dono daquela caixa. O botão
    "Enviar por e-mail" fica na ficha, para depois de conferir o endereço.
    Senha padrão ("123") não: o site exige 8 caracteres, e quem soubesse o
    e-mail entraria na conta e veria o CPF dele e os recibos dos passageiros.

    Só o e-mail é obrigatório (desde 2026-11-22, para um motorista que não
    conseguia se cadastrar pelo app): o que faltar, o link de criar senha pede
    no site, junto com a senha, e só depois manda ele para o app. O que vier
    preenchido aqui é conferido como antes (CPF pelo dígito verificador,
    WhatsApp com DDD). Sem CPF, o link não tem o que perguntar: ver criar_senha.
    """
    disponiveis = planos_do_link_disponiveis()
    formulario = None
    if "formulario" in request.args:
        # Link do formulário público, para o motorista preencher sozinho.
        plano_form = request.args.get("formulario", "")
        plano_form = plano_form if plano_form in disponiveis else ""
        url = absolute_url("cadastro", plano=plano_form) if plano_form else absolute_url("cadastro")
        texto = mensagem_formulario(plano_form, url)
        formulario = {"plano": plano_form, "url": url, "texto": texto, "whatsapp": link_whatsapp("", texto)}

    def tela(status: int = 200):
        return render_template("admin/motorista_novo.html", form=request.form, planos=disponiveis,
                               todos=PLANOS_DO_LINK, formulario=formulario), status

    if request.method == "GET":
        return tela()

    erro_tamanho = field_limit_error(SIGNUP_FIELD_LIMITS)
    if erro_tamanho:
        flash(erro_tamanho, "danger")
        return tela(400)
    full_name = request.form.get("nome_completo", "").strip()
    email = normalize_email(request.form.get("email", ""))
    whatsapp = request.form.get("whatsapp", "").strip()
    cpf = request.form.get("cpf", "").strip()
    city = request.form.get("cidade", "").strip()
    plate = request.form.get("placa", "").strip().upper()
    vehicle_model = request.form.get("modelo_veiculo", "").strip()
    taxi_prefix = request.form.get("prefixo_taxi", "").strip()
    license_number = request.form.get("numero_alvara", "").strip()
    plano = request.form.get("plano_link", "")

    erro = None
    if not email:
        erro = "Preencha o e-mail: é o login dele."
    elif not email_valido(email):
        erro = "Confira o e-mail: ele não parece válido."
    elif request.form.get("pediu") != "1":
        erro = "Confirme que o motorista pediu o cadastro e passou estes dados."
    elif plano and plano not in disponiveis:
        erro = "Este plano não está disponível agora. Escolha outro da lista."
    if erro:
        flash(erro, "danger")
        return tela(400)
    try:
        pais, moeda = pais_e_moeda(request.form.get("pais"), request.form.get("moeda"))
        if whatsapp and not phone_e164(whatsapp, pais):
            # É para onde vai o link (e, fora do Brasil, o link de senha pede os 4 últimos números dele).
            raise ValueError("Confira o WhatsApp: com DDD (fora do Brasil, com + e o código do país).")
        if cpf and pais == PAIS_PADRAO and not cpf_valido(cpf):
            raise ValueError("Confira o CPF: os dígitos não conferem.")
        coop_name, coop_cnpj = cooperativa_informada(
            request.form.get("cooperativa", ""), request.form.get("cnpj_cooperativa", ""), pais)
    except ValueError as exc:  # CooperativaInvalida também é ValueError
        flash(str(exc), "danger")
        return tela(400)

    dados = {"full_name": full_name, "cpf": cpf, "whatsapp": whatsapp, "city": city, "plate": plate,
             "vehicle_model": vehicle_model, "taxi_prefix": taxi_prefix, "license_number": license_number,
             "country": pais, "currency": moeda}
    try:
        # Senha que ninguém vê nem guarda; os sufixos cobrem uma política de
        # senha mais exigente no Supabase.
        user_id = auth_criar_usuario(email, secrets.token_urlsafe(32) + "Aa1!", dados,
                                     app_metadata={MARCA_CRIADO_PELO_PAINEL: True})
    except ValueError:
        flash("Já existe uma conta com este e-mail. Procure o motorista na lista e gere o link de "
              "pagamento na ficha dele.", "danger")
        return tela(409)
    except RuntimeError as exc:
        flash(str(exc), "danger")
        return tela(503)

    guardar_cooperativa(user_id, coop_name, coop_cnpj)
    avisar_novo_cadastro(user_id, {**dados, "email": email}, "painel")
    sincronizar_crm_agora(user_id)
    _ADMIN_CACHE.pop("dashboard", None)
    app.logger.warning("admin %s cadastrou o motorista %s (%s) pelo painel", g.admin["email"], user_id,
                       filtro_mascarar_email(email))
    flash(f"Conta de {full_name or email} criada. Agora mande para ele, pelo WhatsApp, o link para criar a senha "
          "(logo abaixo). Antes de mandar por e-mail, confira o endereço com ele, letra por letra.", "success")
    return redirect(url_for("admin_motorista", driver_id=user_id, senha_plano=plano or None) + "#primeiro-acesso")


# ── Painel: automação de e-mails ─────────────────────────────────────────────

EMAIL_MOTIVOS = {
    "desligado": "O motor está desligado — nada saiu.",
    "fora_do_horario": f"Fora do horário de envio ({EMAIL_AUTO_HORA_INICIO}h às {EMAIL_AUTO_HORA_FIM + 1}h, Brasília).",
    "teto_diario": "O teto diário de e-mails foi atingido.",
    "sem_resend": "RESEND_API_KEY não está configurada.",
    "provedor_indisponivel": "O Resend recusou (excesso, fora do ar ou chave/domínio mal configurados); a próxima hora tenta de novo.",
}


def _modelo_ou_404(conn, modelo_id) -> dict:
    row = conn.execute("select * from public.email_templates where id = %s", (str(modelo_id),)).fetchone()
    if not row:
        abort(404)
    return dict(row)


def _para_formulario(m: dict) -> dict:
    """Linha do banco no formato dos campos do formulário."""
    f = dict(m)
    quando = m.get("scheduled_at")
    f["scheduled_local"] = quando.astimezone(BR_TZ).strftime("%Y-%m-%dT%H:%M") if isinstance(quando, datetime) else (quando or "")
    return f


def _ler_formulario_modelo() -> tuple[dict, list[str]]:
    f = request.form
    erros: list[str] = []

    def inteiro(campo: str, minimo: int, maximo: int, rotulo: str) -> int:
        try:
            valor = int(f.get(campo, "").strip())
        except ValueError:
            erros.append(f"{rotulo}: informe um número.")
            return minimo
        if not minimo <= valor <= maximo:
            erros.append(f"{rotulo}: use um número de {minimo} a {maximo}.")
        return valor

    dados = {
        "name": f.get("name", "").strip(),
        "subject": " ".join(f.get("subject", "").split()),
        "body": f.get("body", "").replace("\r\n", "\n").strip(),
        "button_text": f.get("button_text", "").strip(),
        "button_url": f.get("button_url", "").strip(),
        "trigger": f.get("trigger", ""),
        "trigger_value": inteiro("trigger_value", 0, 365, "Número do gatilho"),
        "send_hour": inteiro("send_hour", 0, 23, "Hora"),
        "position": inteiro("position", 0, 999, "Ordem"),
        "active": f.get("active") == "1",
        "scheduled_at": None,
        "scheduled_local": f.get("scheduled_at", "").strip(),
    }
    for campo, rotulo, maximo in (("name", "Nome interno", 80), ("subject", "Assunto", 150), ("body", "Texto", 5000)):
        if not dados[campo]:
            erros.append(f"{rotulo}: obrigatório.")
        elif len(dados[campo]) > maximo:
            erros.append(f"{rotulo}: no máximo {maximo} caracteres.")
    if len(dados["button_text"]) > 60 or len(dados["button_url"]) > 300:
        erros.append("Botão: texto até 60 caracteres e link até 300.")
    if bool(dados["button_text"]) != bool(dados["button_url"]):
        erros.append("Botão: preencha o texto e o link, ou deixe os dois vazios.")
    if dados["trigger"] not in EMAIL_GATILHOS:
        erros.append("Escolha quando o e-mail sai.")
    desconhecidas = variaveis_desconhecidas(dados["subject"], dados["body"], dados["button_text"], dados["button_url"])
    if desconhecidas:
        erros.append("Variáveis que não existem: " + ", ".join("{{" + v + "}}" for v in desconhecidas) + ".")
    elif dados["button_url"]:
        link = _preencher(dados["button_url"], valores_do_email(MOTORISTA_EXEMPLO))
        if not re.match(r"^https?://[^\s]+$", link):
            erros.append("Link do botão: use um endereço começando com https:// ou uma variável como {{link_planos}}.")
    if dados["trigger"] == "scheduled":
        if dados["scheduled_local"]:
            try:
                dados["scheduled_at"] = datetime.strptime(dados["scheduled_local"], "%Y-%m-%dT%H:%M").replace(tzinfo=BR_TZ)
            except ValueError:
                erros.append("Data da campanha inválida.")
        elif dados["active"]:
            erros.append("Campanha ativa precisa de data e hora.")
    return dados, erros


_COLUNAS_MODELO = ("name", "subject", "body", "button_text", "button_url", "trigger",
                   "trigger_value", "send_hour", "scheduled_at", "position", "active")


def painel_emails() -> dict:
    store = get_store()
    agora = datetime.now(timezone.utc)
    params = {"ini_hoje": _inicio_dia_br(0), "ini_7d": _inicio_dia_br(6), "ini_30d": _inicio_dia_br(29)}
    with store.pool.connection() as conn:
        cfg = config_email(conn)
        modelos = [dict(r) for r in conn.execute(
            """select t.*,
                      count(s.id) filter (where s.status = 'sent') as enviados,
                      count(s.id) filter (where s.status = 'sent' and s.created_at >= %(ini_7d)s) as enviados_7d,
                      count(s.id) filter (where s.status = 'failed') as falhas,
                      count(distinct s.driver_id) filter (where s.status = 'sent' and d.plan <> 'free') as viraram_pro
                 from public.email_templates t
                 left join public.email_sends s on s.template_id = t.id
                 left join public.drivers d on d.id = s.driver_id
                group by t.id
                order by t.position, t.created_at""", params).fetchall()]
        totais = dict(conn.execute(
            """select count(*) filter (where status <> 'failed' and created_at >= %(ini_hoje)s) as hoje,
                      count(*) filter (where status = 'sent' and created_at >= %(ini_7d)s) as d7,
                      count(*) filter (where status = 'sent' and created_at >= %(ini_30d)s) as d30,
                      count(*) filter (where status = 'failed') as falhas,
                      count(*) filter (where status = 'sending' and created_at < now() - interval '1 hour') as presos,
                      count(distinct driver_id) filter (where status = 'sent') as alcancados
                 from public.email_sends""", params).fetchone())
        totais.update(conn.execute(
            f"""select count(*) filter (where plan = 'free' and marketing_optout_at is null
                                          and {_sql_email_aceito('email')}) as publico,
                       count(*) filter (where marketing_optout_at is not null) as descadastrados,
                       count(*) filter (where plan <> 'free' and exists (
                           select 1 from public.email_sends s
                            where s.driver_id = drivers.id and s.status = 'sent')) as convertidos
                  from public.drivers""").fetchone())
        # Quem cada modelo ativo pegaria se o motor rodasse agora (sem contar
        # teto diário e horário). Um motorista pode aparecer em dois modelos;
        # o intervalo mínimo faz ele receber só um por vez.
        ativos = [m["id"] for m in modelos if m["active"]]
        for m in modelos:
            m["na_vez"] = (len(candidatos_do_modelo(conn, m, agora, cfg["min_gap_hours"], 1000, passada=ativos))
                           if m["active"] else None)
            m["quando"] = descrever_gatilho(m)
        ultimos = [dict(r) for r in conn.execute(
            """select s.created_at, s.status, s.error, t.name as modelo, d.id as driver_id, d.full_name
                 from public.email_sends s
                 join public.email_templates t on t.id = s.template_id
                 join public.drivers d on d.id = s.driver_id
                order by s.created_at desc limit 30""").fetchall()]
    return {"cfg": cfg, "modelos": modelos, "totais": totais, "ultimos": ultimos,
            "reply_to": endereco_de_resposta(), "remetente": remetente_automacao(),
            "resend_ok": bool(os.environ.get("RESEND_API_KEY", "").strip()),
            "agora_br": agora.astimezone(BR_TZ)}


@app.get("/admin/emails")
@admin_required
def admin_emails():
    try:
        dados = painel_emails()
    except Exception as exc:
        if _migracao_email_pendente(exc):
            return render_template("admin/emails.html", pendente=True)
        raise
    return render_template("admin/emails.html", d=dados, pendente=False,
                           hora_inicio=EMAIL_AUTO_HORA_INICIO, hora_fim=EMAIL_AUTO_HORA_FIM + 1,
                           janela=EMAIL_AUTO_JANELA_DIAS)


@app.post("/admin/emails/motor")
@admin_required
def admin_emails_motor():
    ligar = request.form.get("ligar") == "1"
    with get_store().pool.connection() as conn:
        conn.execute("update public.email_settings set enabled = %s, updated_at = now() where id", (ligar,))
    app.logger.info("admin %s %s a automação de e-mails", g.admin["email"], "ligou" if ligar else "desligou")
    flash("Motor ligado: os modelos ativos saem a partir da próxima hora cheia." if ligar
          else "Motor desligado: nenhum e-mail automático sai até você ligar de novo.", "success")
    return redirect(url_for("admin_emails"))


@app.post("/admin/emails/config")
@admin_required
def admin_emails_config():
    try:
        teto = int(request.form.get("daily_cap", ""))
        intervalo = int(request.form.get("min_gap_hours", ""))
    except ValueError:
        flash("Use números inteiros no teto e no intervalo.", "danger")
        return redirect(url_for("admin_emails"))
    if not (0 <= teto <= 10000 and 0 <= intervalo <= 720):
        flash("Teto de 0 a 10.000 por dia e intervalo de 0 a 720 horas.", "danger")
        return redirect(url_for("admin_emails"))
    with get_store().pool.connection() as conn:
        conn.execute("update public.email_settings set daily_cap = %s, min_gap_hours = %s, updated_at = now() where id",
                     (teto, intervalo))
    app.logger.info("admin %s mudou a automação: teto %s/dia, intervalo %sh", g.admin["email"], teto, intervalo)
    flash("Configuração salva.", "success")
    return redirect(url_for("admin_emails"))


@app.post("/admin/emails/rodar")
@admin_required
def admin_emails_rodar():
    r = rodar_automacao_email()
    texto = f"{r['enviados']} e-mail(s) enviado(s)" + (f", {r['falhas']} falha(s)" if r["falhas"] else "") + "."
    if r["motivo"]:
        texto += " " + EMAIL_MOTIVOS.get(r["motivo"], r["motivo"])
    app.logger.info("admin %s rodou a automação de e-mails: %s", g.admin["email"], r)
    flash(texto, "success" if r["enviados"] else "info")
    return redirect(url_for("admin_emails"))


def _render_modelo(m: dict, novo: bool, status: int = 200):
    previa = None
    if not novo and not variaveis_desconhecidas(m.get("subject", ""), m.get("body", "")):
        previa = montar_email_automacao(m, valores_do_email(MOTORISTA_EXEMPLO), absolute_url("email_descadastro", token="exemplo"))
    return render_template("admin/email_modelo.html", m=m, novo=novo, previa=previa,
                           gatilhos=EMAIL_GATILHOS, variaveis=EMAIL_VARIAVEIS,
                           exemplo=valores_do_email(MOTORISTA_EXEMPLO)), status


@app.route("/admin/emails/novo", methods=["GET", "POST"])
@admin_required
def admin_email_novo():
    if request.method == "POST":
        dados, erros = _ler_formulario_modelo()
        if erros:
            for e in erros:
                flash(e, "danger")
            return _render_modelo(dados, novo=True, status=400)
        with get_store().pool.connection() as conn:
            novo = conn.execute(
                f"""insert into public.email_templates ({", ".join(_COLUNAS_MODELO)})
                    values ({", ".join("%(" + c + ")s" for c in _COLUNAS_MODELO)}) returning id""",
                dados).fetchone()
        app.logger.info("admin %s criou o modelo de e-mail %s", g.admin["email"], novo["id"])
        flash("Modelo criado.", "success")
        return redirect(url_for("admin_email_modelo", modelo_id=novo["id"]))

    with get_store().pool.connection() as conn:
        proxima = conn.execute("select coalesce(max(position), 0) + 1 as p from public.email_templates").fetchone()["p"]
    vazio = {"name": "", "subject": "", "body": "Olá, {{nome}}.\n\n", "button_text": "",
             "button_url": "", "trigger": "signup", "trigger_value": 1, "send_hour": 10, "position": proxima,
             "active": False, "scheduled_local": ""}
    return _render_modelo(vazio, novo=True)


@app.route("/admin/emails/<uuid:modelo_id>", methods=["GET", "POST"])
@admin_required
def admin_email_modelo(modelo_id):
    with get_store().pool.connection() as conn:
        atual = _modelo_ou_404(conn, modelo_id)
    if request.method == "POST":
        dados, erros = _ler_formulario_modelo()
        if erros:
            for e in erros:
                flash(e, "danger")
            return _render_modelo(dict(dados, id=atual["id"]), novo=False, status=400)
        with get_store().pool.connection() as conn:
            conn.execute(
                f"""update public.email_templates
                       set {", ".join(c + " = %(" + c + ")s" for c in _COLUNAS_MODELO)}, updated_at = now()
                     where id = %(id)s""",
                dict(dados, id=atual["id"]))
        app.logger.info("admin %s editou o modelo de e-mail %s", g.admin["email"], atual["id"])
        flash("Modelo salvo.", "success")
        return redirect(url_for("admin_email_modelo", modelo_id=atual["id"]))
    return _render_modelo(_para_formulario(atual), novo=False)


@app.post("/admin/emails/<uuid:modelo_id>/ativo")
@admin_required
def admin_email_ativo(modelo_id):
    with get_store().pool.connection() as conn:
        m = _modelo_ou_404(conn, modelo_id)
        if not m["active"] and m["trigger"] == "scheduled" and not m["scheduled_at"]:
            flash("Marque a data da campanha antes de ativar.", "danger")
            return redirect(url_for("admin_email_modelo", modelo_id=m["id"]))
        conn.execute("update public.email_templates set active = not active, updated_at = now() where id = %s", (m["id"],))
    app.logger.info("admin %s %s o modelo de e-mail %s", g.admin["email"], "desativou" if m["active"] else "ativou", m["id"])
    flash(f"“{m['name']}” {'desativado' if m['active'] else 'ativado'}.", "success")
    if request.form.get("voltar") == "modelo":
        return redirect(url_for("admin_email_modelo", modelo_id=m["id"]))
    return redirect(url_for("admin_emails"))


@app.post("/admin/emails/<uuid:modelo_id>/duplicar")
@admin_required
def admin_email_duplicar(modelo_id):
    with get_store().pool.connection() as conn:
        m = _modelo_ou_404(conn, modelo_id)
        copia = dict(m, name=f"Cópia de {m['name']}"[:80], active=False,
                     position=conn.execute("select coalesce(max(position), 0) + 1 as p from public.email_templates").fetchone()["p"])
        novo = conn.execute(
            f"""insert into public.email_templates ({", ".join(_COLUNAS_MODELO)})
                values ({", ".join("%(" + c + ")s" for c in _COLUNAS_MODELO)}) returning id""",
            copia).fetchone()
    flash("Cópia criada, desativada. Ajuste e ative quando quiser.", "success")
    return redirect(url_for("admin_email_modelo", modelo_id=novo["id"]))


@app.post("/admin/emails/<uuid:modelo_id>/excluir")
@admin_required
def admin_email_excluir(modelo_id):
    with get_store().pool.connection() as conn:
        m = _modelo_ou_404(conn, modelo_id)
        envios = conn.execute("select count(*) as n from public.email_sends where template_id = %s", (m["id"],)).fetchone()["n"]
        if envios:
            # O histórico é o que impede reenviar o mesmo e-mail e o que mede
            # quem virou Pro. Apagar o modelo levaria os dois.
            flash(f"“{m['name']}” já foi enviado {envios} vez(es) e guarda esse histórico. Desative em vez de excluir.", "danger")
            return redirect(url_for("admin_email_modelo", modelo_id=m["id"]))
        conn.execute("delete from public.email_templates where id = %s", (m["id"],))
    app.logger.info("admin %s excluiu o modelo de e-mail %s", g.admin["email"], m["id"])
    flash(f"“{m['name']}” excluído.", "success")
    return redirect(url_for("admin_emails"))


@app.post("/admin/emails/<uuid:modelo_id>/teste")
@admin_required
def admin_email_teste(modelo_id):
    with get_store().pool.connection() as conn:
        m = _modelo_ou_404(conn, modelo_id)
    if get_store().bump_counter(f"email_teste:{g.admin['_id']}:{today_br()}") > EMAIL_AUTO_TESTES_DIA:
        flash(f"Limite de {EMAIL_AUTO_TESTES_DIA} e-mails de teste por dia atingido.", "danger")
        return redirect(url_for("admin_email_modelo", modelo_id=m["id"]))
    msg = montar_email_automacao(m, valores_do_email(MOTORISTA_EXEMPLO), absolute_url("email_descadastro", token="exemplo"))
    msg["assunto"] = f"[Teste] {msg['assunto']}"
    ok, _, erro, _ = enviar_email_automacao(g.admin["email"], msg)
    if ok:
        flash(f"Teste enviado para {g.admin['email']}, com os dados de um motorista de exemplo.", "success")
    else:
        flash(f"O teste não saiu: {erro or 'erro desconhecido'}.", "danger")
    return redirect(url_for("admin_email_modelo", modelo_id=m["id"]))


@app.get("/admin/emails/<uuid:modelo_id>/previa")
@admin_required
def admin_email_previa(modelo_id):
    """O HTML do e-mail, para o iframe da página do modelo. Sem script nenhum."""
    with get_store().pool.connection() as conn:
        m = _modelo_ou_404(conn, modelo_id)
    msg = montar_email_automacao(m, valores_do_email(MOTORISTA_EXEMPLO), absolute_url("email_descadastro", token="exemplo"))
    resposta = app.response_class(msg["html"], mimetype="text/html")
    # Só esta resposta pode ir num iframe, e só dentro do próprio site.
    resposta.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; frame-ancestors 'self'")
    resposta.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resposta


# ── Filtros Jinja do painel ──────────────────────────────────────────────────

@app.template_filter("brl")
def filtro_brl(valor) -> str:
    try:
        n = float(valor or 0)
    except (TypeError, ValueError):
        return "-"
    inteiro, _, dec = f"{n:,.2f}".partition(".")
    return "R$ " + inteiro.replace(",", ".") + "," + dec


@app.template_filter("brl_eixo")
def filtro_brl_eixo(valor) -> str:
    """Marca do eixo em reais: redonda, sem centavos quando não precisa."""
    n = float(valor or 0)
    if n >= 1000:
        return f"R$ {n / 1000:g} mil".replace(".", ",")
    return f"R$ {n:,.0f}".replace(",", ".") if n == int(n) else filtro_brl(n)


@app.template_filter("valor")
def filtro_valor(valor, moeda=None) -> str:
    """Valor na moeda do recibo. O `brl` fica para as somas, que são só em reais."""
    return formatar_valor(valor, moeda)


def mascarar_documento(valor, pais: str = PAIS_PADRAO) -> str:
    """CPF com o meio escondido (123.***.***-09): o que fica na página pública
    do recibo e nas mensagens, porque link vazado não pode entregar o CPF
    inteiro do passageiro. CNPJ é dado público e sai inteiro; documento de
    outro país sai como veio (não dá para saber o que é)."""
    texto = str(valor or "")
    if pais != PAIS_PADRAO:
        return texto
    digitos = re.sub(r"\D", "", texto)
    if len(digitos) == 11:
        return f"{digitos[:3]}.***.***-{digitos[9:]}"
    if len(digitos) == 14:
        return format_document_br(digitos)
    return texto


@app.template_filter("mascarar_cpf")
def filtro_mascarar_cpf(valor) -> str:
    digitos = re.sub(r"\D", "", str(valor or ""))
    if len(digitos) == 11:
        return f"{digitos[:3]}.***.***-{digitos[9:]}"
    if len(digitos) == 14:  # CNPJ e dado publico
        return format_document_br(digitos)
    return "***" if digitos else ""


@app.template_filter("mascarar_email")
def filtro_mascarar_email(valor) -> str:
    usuario, _, dominio = str(valor or "").partition("@")
    return f"{usuario[:1]}***@{dominio}" if dominio else ""


@app.template_filter("mascarar_fone")
def filtro_mascarar_fone(valor) -> str:
    digitos = re.sub(r"\D", "", str(valor or ""))
    return f"*****-{digitos[-4:]}" if len(digitos) >= 4 else ""


@app.template_filter("data_br")
def filtro_data_br(valor, com_hora: bool = True) -> str:
    if not valor:
        return "-"
    if isinstance(valor, str):
        try:
            valor = datetime.fromisoformat(valor.replace("Z", "+00:00"))
        except ValueError:
            return valor
    if isinstance(valor, datetime):
        if valor.tzinfo is None:
            valor = valor.replace(tzinfo=timezone.utc)
        valor = valor.astimezone(BR_TZ)
        return valor.strftime("%d/%m/%Y %H:%M") if com_hora else valor.strftime("%d/%m/%Y")
    return valor.strftime("%d/%m/%Y")


@app.cli.command("criar-admin")
def cli_criar_admin():
    """Cria um administrador do painel, ou redefine a senha se o e-mail ja existir.

    Uso: flask --app app criar-admin  (pede e-mail e senha; a senha nao ecoa)
    Nao ha cadastro de admin pelo site de proposito.
    """
    import getpass

    email = normalize_email(input("E-mail do administrador: "))
    if not email_valido(email):
        raise SystemExit("E-mail inválido.")
    senha = getpass.getpass("Senha: ")
    if not senha:
        raise SystemExit("Senha vazia.")
    admin = get_store().upsert_admin(email, generate_password_hash(senha))
    print(f"Administrador pronto: {admin['email']} (id {admin['_id']}).")


if __name__ == "__main__":
    app.run(debug=True)
