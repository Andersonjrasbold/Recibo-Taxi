/* Recibo Táxi — app offline-first.
 *
 * A regra que organiza tudo: emitir recibo NUNCA depende de rede. O aparelho
 * gera o rid, monta o recibo e guarda local. A sincronização é um detalhe que
 * acontece depois — e, se falhar, tenta de novo sem duplicar nada, porque o
 * servidor trata o mesmo rid como o mesmo recibo.
 */

const API = (location.protocol === 'capacitor:' || location.hostname === 'localhost')
  ? 'https://recibo-taxi.vercel.app'
  : '';

const RID_TAMANHO = 14;

// Plugins nativos do Capacitor. No navegador não existem, e tudo tem
// alternativa — o app roda igual nos dois lugares.
const P = () => (window.Capacitor && window.Capacitor.Plugins) || {};
const nativo = () => !!(window.Capacitor && window.Capacitor.isNativePlatform && window.Capacitor.isNativePlatform());
const plataforma = () => (window.Capacitor && window.Capacitor.getPlatform
  ? window.Capacitor.getPlatform() : 'web');

// Cada loja e um app diferente dentro do RevenueCat, entao cada uma tem a sua
// chave publica. Vazia significa "ainda nao configurada" — e nao erro.
const chaveDaLoja = () => (window.RC_CHAVES || {})[plataforma()] || '';

// O motorista le "App Store" no iPhone e "Google Play" no Android. Dizer a
// loja errada faz a instrucao nao bater com o que ele tem na mao.
const nomeDaLoja = () => (plataforma() === 'android' ? 'Google Play' : 'App Store');

async function temRede() {
  const net = P().Network;
  if (net) {
    try { return (await net.getStatus()).connected; } catch {}
  }
  return navigator.onLine;
}

function vibrar(estilo = 'MEDIUM') {
  try { P().Haptics?.impact({ style: estilo }); } catch {}
}

// ── Armazenamento ──────────────────────────────────────────────────────────
const Guardado = {
  get: (k) => { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  del: (k) => { try { localStorage.removeItem(k); } catch {} },
};

function abrirBanco() {
  return new Promise((ok, falha) => {
    const req = indexedDB.open('recibo-taxi', 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('recibos')) {
        const loja = db.createObjectStore('recibos', { keyPath: 'rid' });
        loja.createIndex('pendente', 'pendente');
      }
    };
    req.onsuccess = () => ok(req.result);
    req.onerror = () => falha(req.error);
  });
}

async function salvarRecibo(recibo) {
  const db = await abrirBanco();
  return new Promise((ok, falha) => {
    const tx = db.transaction('recibos', 'readwrite');
    tx.objectStore('recibos').put(recibo);
    tx.oncomplete = () => ok(recibo);
    tx.onerror = () => falha(tx.error);
  });
}

async function listarRecibos() {
  const db = await abrirBanco();
  return new Promise((ok, falha) => {
    const req = db.transaction('recibos', 'readonly').objectStore('recibos').getAll();
    req.onsuccess = () => ok(req.result.sort((a, b) => b.criado_em.localeCompare(a.criado_em)));
    req.onerror = () => falha(req.error);
  });
}

const pendentes = async () => (await listarRecibos()).filter((r) => r.pendente);

// Usada so na exclusao de conta. "Sair" NAO apaga nada de proposito: o logout
// tambem acontece sozinho quando a sessao morre, e ali apagar destruiria a
// fila de recibos que ainda nao subiram.
async function limparRecibosLocais() {
  const db = await abrirBanco();
  return new Promise((ok, falha) => {
    const tx = db.transaction('recibos', 'readwrite');
    tx.objectStore('recibos').clear();
    tx.oncomplete = () => ok();
    tx.onerror = () => falha(tx.error);
  });
}

// ── Identificador ──────────────────────────────────────────────────────────
function novoRid() {
  // 14 dígitos hex = 2^56. Gerado aqui, não no servidor: é o que permite
  // emitir sem rede e reenviar sem risco de duplicar.
  const bytes = new Uint8Array(7);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('').toUpperCase();
}

// ── Rede ───────────────────────────────────────────────────────────────────
const token = () => Guardado.get('access_token');
const tokenRenovacao = () => Guardado.get('refresh_token');

// O access token do Supabase vale uma hora. Antes disso nao havia renovacao:
// passada a hora, o 401 fazia a fila desistir sem avisar, o motorista seguia
// emitindo recibo que nunca subia, e o passageiro recebia link que nunca ia
// funcionar. So o botao "Sair", manual, destravava.
let renovando = null;      // uma renovacao por vez, mesmo com varios 401 juntos

async function renovarSessao() {
  if (!tokenRenovacao()) return false;
  if (!renovando) {
    renovando = (async () => {
      try {
        const resp = await fetch(API + '/api/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: tokenRenovacao() }),
        });
        if (!resp.ok) return false;         // sessao morreu de vez
        const t = await resp.json();
        Guardado.set('access_token', t.access_token);
        Guardado.set('refresh_token', t.refresh_token);
        return true;
      } catch {
        return false;   // sem rede nao e sessao invalida; tenta de novo depois
      } finally {
        renovando = null;
      }
    })();
  }
  return renovando;
}

async function api(caminho, opcoes = {}) {
  const enviar = () => fetch(API + caminho, {
    ...opcoes,
    headers: {
      'Content-Type': 'application/json',
      ...(token() ? { Authorization: `Bearer ${token()}` } : {}),
      ...(opcoes.headers || {}),
    },
  });
  let resp = await enviar();
  // 401 quase sempre e so token vencido. Renova por baixo e repete uma vez.
  if (resp.status === 401 && caminho !== '/api/refresh' && tokenRenovacao()) {
    if (await renovarSessao()) resp = await enviar();
  }
  return resp;
}

// A tela de assinatura aberta pela recusa por limite fecha de volta no recibo
// (ver btn-fechar-pro).
let voltarDaAssinaturaParaRecibo = false;

async function sincronizar() {
  if (!(await temRede()) || !token()) {
    return { enviados: 0, restantes: (await pendentes()).length };
  }

  let enviados = 0;
  // Do mais antigo para o mais novo: se a cota do mes acabar no meio da fila,
  // quem fica de fora e o recibo mais recente, nao o que o passageiro ja
  // espera desde antes. Aconteceu ao contrario num teste: o ultimo emitido
  // subiu e o anterior ficou recusado.
  for (const recibo of (await pendentes()).reverse()) {
    try {
      const resp = await api('/api/recibos', {
        method: 'POST',
        body: JSON.stringify(recibo.dados),
      });
      if (resp.status === 201 || resp.status === 200) {
        // 200 = o servidor já tinha este rid. Reenvio, não recibo novo.
        const corpo = await resp.json();
        await salvarRecibo({ ...recibo, pendente: false, url: corpo.url });
        enviados++;
      } else if (resp.status === 402) {
        // Cota do mês acabou: para de tentar, senão fica em laço.
        await salvarRecibo({ ...recibo, pendente: false, recusado: 'limite_mensal' });
        const limite = Guardado.get('limite');
        // Vindo da tela do recibo, "Agora não" volta para ela: e la que esta o
        // aviso de que este recibo ficou sem QR.
        if (!$('tela-recibo').hidden) voltarDaAssinaturaParaRecibo = true;
        abrirAssinatura(
          `Você usou os ${limite ?? ''} recibos deste mês do plano Grátis: o recibo novo ` +
          'ficou sem link e sem QR para o passageiro. O Pro libera recibos ilimitados.');
      } else if (resp.status === 400 || resp.status === 409) {
        await salvarRecibo({ ...recibo, pendente: false, recusado: (await resp.json()).erro });
      } else if (resp.status === 401) {
        // O api() ja tentou renovar. Chegar aqui e sessao morta de verdade —
        // seguir em silencio deixaria a fila crescendo para sempre.
        encerrarSessao();
        break;
      }
    } catch {
      break; // sem rede de verdade: tenta na próxima
    }
  }
  return { enviados, restantes: (await pendentes()).length };
}

// ── Telas ──────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const telas = ['tela-login', 'tela-cadastro', 'tela-recuperar', 'tela-inicio',
               'tela-corrida', 'tela-tarifa', 'tela-emitir', 'tela-recibo',
               'tela-assinatura', 'tela-indicar', 'tela-historico', 'tela-excluir'];

// Telas de onde o motorista sai para o menu. Fechar o historico, a assinatura,
// a exclusao ou a tarifa volta para a ultima delas, e nao sempre para o
// formulario: o formulario deixou de ser a unica tela de trabalho.
const TELAS_BASE = ['tela-inicio', 'tela-corrida', 'tela-emitir'];
let telaBase = 'tela-inicio';

function mostrar(qual) {
  telas.forEach((t) => { $(t).hidden = t !== qual; });
  if (TELAS_BASE.includes(qual)) telaBase = qual;
  window.scrollTo(0, 0);
}

// Sem refazer nada: o formulario e a corrida voltam como estavam, com o que
// o motorista ja tinha digitado. So o inicio se redesenha, porque o aviso da
// corrida aberta pode ter mudado.
function voltarParaBase() {
  if (telaBase === 'tela-inicio') { irParaInicio(); return; }
  if (telaBase === 'tela-corrida') desenharCorrida();
  mostrar(telaBase);
}

// O wa.me exige o numero com codigo do pais. Sem isso o WhatsApp abre sem
// destinatario e o motorista so consegue enviar se ja tiver o passageiro
// salvo na agenda — o problema que este botao existe para resolver.
//
// A classificacao e por tamanho, nao por prefixo: o DDD 55 existe (Santa
// Maria/RS), entao "5598765432", de 10 digitos, e DDD + fixo e vira
// "555598765432", nao um numero ja internacional.
//
// Fora do Brasil vale o codigo do pais do motorista (vem com a sessao): o
// passageiro de uma corrida na Costa Rica dita um numero de la. Com + na
// frente, o numero ja e internacional em qualquer pais. Mesma regra do servidor.
function foneE164(valor, pais = paisAtual(), ddi = ddiAtual()) {
  const bruto = String(valor || '').trim();
  let d = bruto.replace(/\D/g, '');
  if (bruto.startsWith('+')) return d.length >= 8 && d.length <= 15 ? d : '';
  if (pais !== 'BR') {
    if (d.startsWith('00')) d = d.slice(2);
    else if (!ddi) return '';
    else if (!(d.startsWith(ddi) && d.length >= ddi.length + 7)) d = ddi + d.replace(/^0+/, '');
    return d.length >= 8 && d.length <= 15 ? d : '';
  }
  if (d.startsWith('00')) d = d.slice(2);
  d = d.replace(/^0+/, '');
  if (d.length === 10 || d.length === 11) return '55' + d;
  if (d.length === 12 || d.length === 13) return d;
  return '';
}

// Por que o numero digitado nao serve para o wa.me. Devolve '' quando serve.
//
// Existe porque o botao sumia calado: um numero sem DDD, ou com um digito a
// menos, e indistinguivel de campo vazio para o foneE164, e o motorista ficava
// sem entender por que nao dava para enviar. Aconteceu na rua com "499459913".
function motivoFoneInvalido(valor) {
  const d = String(valor || '').replace(/\D/g, '');
  if (!d) return '';                       // vazio e escolha, nao erro
  if (foneE164(valor)) return '';
  if (paisAtual() !== 'BR') return 'Número incompleto. Digite com o código da cidade, ou com + e o código do país.';
  if (d.length < 10) return 'Faltam dígitos. Use DDD + número: 45 99999-9999.';
  return 'Número muito longo. Use DDD + número: 45 99999-9999.';
}

function emailInvalido(valor) {
  const e = emailLimpo(valor);
  if (!e) return '';
  const [usuario, dominio, ...sobra] = e.split('@');
  const bom = usuario && dominio && !sobra.length && /\.[^.]+$/.test(dominio)
    && !/\s/.test(e) && e.length <= 254;
  return bom ? '' : 'E-mail incompleto. Exemplo: nome@empresa.com.br';
}

function mostrarDica(idCampo, idDica, motivo) {
  const dica = $(idDica);
  dica.textContent = motivo(($(idCampo).value));
  dica.hidden = !dica.textContent;
}

const revisarWhats = () => mostrarDica('whats', 'dica-whats', motivoFoneInvalido);
const revisarEmail = () => mostrarDica('email-passageiro', 'dica-email', emailInvalido);
$('whats').addEventListener('input', revisarWhats);
$('email-passageiro').addEventListener('input', revisarEmail);

// Pontua CPF e CNPJ. Nao valida digito verificador de proposito: o campo e
// opcional, e recusar um documento ditado errado trocaria um recibo util por
// um erro na tela, com o passageiro esperando dentro do carro.
// Fora do Brasil, 11 digitos nao sao CPF: o documento fica como veio.
function documentoBR(valor) {
  if (paisAtual() !== 'BR') return String(valor || '').trim();
  const d = String(valor || '').replace(/\D/g, '');
  if (d.length === 11) return `${d.slice(0,3)}.${d.slice(3,6)}.${d.slice(6,9)}-${d.slice(9)}`;
  if (d.length === 14) return `${d.slice(0,2)}.${d.slice(2,5)}.${d.slice(5,8)}/${d.slice(8,12)}-${d.slice(12)}`;
  return String(valor || '').trim();
}

// CNPJ tem 14 digitos, CPF tem 11. O campo da razao social so existe para o
// primeiro: sem o nome da empresa, o recibo nao serve para lancar a despesa.
// Some junto quando o motorista troca para CPF, e o valor vai junto — senao um
// nome de empresa ficaria pendurado num recibo de pessoa fisica.
function ehCNPJ(documento) {
  return paisAtual() === 'BR' && String(documento || '').replace(/\D/g, '').length === 14;
}

function atualizarCampoRazaoSocial() {
  const mostrar = ehCNPJ($('documento').value);
  $('campo-razao-social').hidden = !mostrar;
  if (!mostrar) $('razao-social').value = '';
}

$('documento').addEventListener('input', atualizarCampoRazaoSocial);

// ── Pais e moeda ───────────────────────────────────────────────────────────
//
// O app esta a venda em 175 paises e ja tem motorista fora do Brasil. Pais,
// moeda, simbolo, casas decimais e codigo de discagem vem com a sessao e ficam
// guardados: o recibo sai certo sem sinal. Cada recibo leva a moeda de quando
// foi emitido, como no servidor — trocar de moeda depois nao muda recibo feito.
const moedaAtual = () => ({
  codigo: Guardado.get('moeda') || 'BRL',
  simbolo: Guardado.get('moeda_simbolo') || 'R$',
  casas: Number.isInteger(Guardado.get('moeda_casas')) ? Guardado.get('moeda_casas') : 2,
});
const paisAtual = () => Guardado.get('pais') || 'BR';
const ddiAtual = () => Guardado.get('ddi') || '55';
const simboloDe = (d) => (d && d.moeda_simbolo) || 'R$';
const rotuloDocumentoDe = (d) => (d && d.pais && d.pais !== 'BR' ? 'Documento' : 'CPF/CNPJ');

// Mesma regra do servidor (_separadores_do_valor): com ponto e virgula, o
// ultimo separa os centavos; um separador repetido e de milhar; um separador
// so, seguido de exatamente 3 digitos ("1.500", "15,000"), e de milhar.
function separadoresDoValor(bruto) {
  if (bruto.includes(',') && bruto.includes('.')) {
    const dec = bruto.lastIndexOf(',') > bruto.lastIndexOf('.') ? ',' : '.';
    const mil = dec === ',' ? '.' : ',';
    return bruto.split(mil).join('').replace(dec, '.');
  }
  for (const sep of [',', '.']) {
    if (!bruto.includes(sep)) continue;
    const partes = bruto.split(sep);
    const inteiro = partes[0].replace(/^-/, '');
    const deMilhar = partes.slice(1).every((p) => /^\d{3}$/.test(p))
      && /^\d{1,3}$/.test(inteiro) && inteiro !== '0';
    if (partes.length > 2 || deMilhar) return deMilhar ? bruto.split(sep).join('') : bruto;
    return bruto.replace(sep, '.');
  }
  return bruto;
}

// "1500" ou "1.500" → "1.500,00"; "15000" em guarani (0 casas) → "15.000".
function valorExibido(texto, casas = moedaAtual().casas) {
  const bruto = String(texto || '').replace(/\s/g, '').replace(/^[^\d,.-]+/, '').replace(/[^\d,.-]+$/, '');
  const n = Number(separadoresDoValor(bruto));
  if (!Number.isFinite(n)) return String(texto || '').replace('.', ',');
  return n.toLocaleString('pt-BR', { minimumFractionDigits: casas, maximumFractionDigits: casas });
}

function aplicarMoedaNaTela() {
  const m = moedaAtual();
  document.querySelectorAll('.simbolo-moeda').forEach((el) => { el.textContent = m.simbolo; });
  $('valor').placeholder = (45).toLocaleString('pt-BR', { minimumFractionDigits: m.casas, maximumFractionDigits: m.casas });
  $('rotulo-documento').textContent = paisAtual() === 'BR'
    ? 'CPF ou CNPJ do passageiro (opcional)' : 'Documento do passageiro (opcional)';
}

// Lista de paises e moedas para o cadastro: do servidor, com a ultima copia
// guardada para quando a rede falhar; sem nenhuma das duas fica so o Brasil.
async function carregarPaises() {
  let lista = null;
  try {
    const r = await api('/api/paises');
    if (r.ok) { lista = await r.json(); Guardado.set('paises', lista); }
  } catch { /* sem rede: usa a copia */ }
  lista = lista || Guardado.get('paises');
  if (!lista) return;
  $('c-pais').innerHTML = lista.paises
    .map((p) => `<option value="${p.codigo}" data-moeda="${p.moeda}">${p.nome}</option>`).join('');
  $('c-moeda').innerHTML = lista.moedas
    .map((m) => `<option value="${m.codigo}">${m.nome} (${m.simbolo})</option>`).join('');
  $('c-pais').value = 'BR';
  $('c-moeda').value = 'BRL';
}

$('c-pais').addEventListener('change', () => {
  const opcao = $('c-pais').selectedOptions[0];
  if (opcao && opcao.dataset.moeda) $('c-moeda').value = opcao.dataset.moeda;
  $('rotulo-c-cpf').textContent = $('c-pais').value === 'BR' ? 'CPF *' : 'Documento de identidade *';
});

// O servidor confere de novo; aqui e so tirar o obvio: espaco que o teclado
// do celular acrescenta sozinho e maiuscula do corretor.
function emailLimpo(valor) {
  return String(valor || '').trim().toLowerCase();
}

// Vira anuncio do motorista no recibo: quem recebeu tem como chamar de novo
// sem procurar o contato.
function chamadaDoMotorista(m) {
  const fone = (m && m.whatsapp) || '';
  return fone ? `Precisou de corrida? Me chame no WhatsApp: ${fone}` : '';
}

function formatarBR(iso) {
  if (!iso) return '-';
  const [a, m, d] = iso.split('-');
  return d && m && a ? `${d}/${m}/${a}` : iso;
}

// Por que o servidor recusou, em palavras. O codigo cru ('limite_mensal')
// aparecia no historico como se fosse defeito do app.
function motivoRecusa(codigo) {
  return {
    limite_mensal: 'limite de recibos do mês',
    rid_de_outro_motorista: 'código já usado',
    // Servidor anterior ao nome opcional recusando recibo sem nome.
    campos_obrigatorios: 'faltou um campo obrigatório',
    // Os demais 400 de /api/recibos. Aparecem no aviso de recibo sem QR.
    valor_invalido: 'valor inválido',
    km_invalido: 'km inválido',
    campo_longo: 'um campo passou do tamanho',
    rid_invalido: 'código inválido',
  }[codigo] || codigo;
}

// Mostra o endereco digitado, para o motorista conferir antes de mandar, e
// depois vira confirmacao. O e-mail so sai quando ele toca no botao.
// O recibo ja foi emitido: aqui nao da para corrigir, so explicar por que o
// botao nao esta ali. Emitir de novo com o numero certo e o caminho.
function estadoDoWhats(recibo) {
  const motivo = motivoFoneInvalido(recibo.dados.whatsapp_passageiro);
  return motivo ? `⚠ WhatsApp ${recibo.dados.whatsapp_passageiro}: ${motivo}` : '';
}

function estadoDoEmail(recibo) {
  const para = recibo.dados.email_passageiro;
  if (!para) return '';
  return recibo.email_enviado ? `✉ Enviado para ${para}` : `✉ E-mail: ${para}`;
}

function estadoDoRecibo(recibo) {
  if (recibo.pendente) return '⏳ Aguardando sinal para sincronizar';
  if (recibo.recusado) return `⚠ Não enviado: ${motivoRecusa(recibo.recusado)}`;
  return '✓ Sincronizado';
}

function montarRecibo(recibo) {
  const d = recibo.dados, m = recibo.motorista || {};
  return `
    <div class="recibo-topo">
      <img class="recibo-marca" src="img/marca-recibo.png" alt="Recibo Táxi">
      <div class="recibo-topo-id">
        <span class="recibo-rotulo">Recibo</span>
        <span class="recibo-id">#${recibo.rid}</span>
      </div>
    </div>
    <div class="recibo-valor">${simboloDe(d)} ${d.valor_exibido}</div>
    ${blocoQr(recibo)}
    <dl class="recibo-dados">
      ${d.passageiro ? `<dt>Passageiro</dt><dd>${d.passageiro}</dd>` : ''}
      ${d.documento_passageiro ? `<dt>${rotuloDocumentoDe(d)}</dt><dd>${d.documento_passageiro}</dd>` : ''}
      ${d.razao_social ? `<dt>Razão social</dt><dd>${d.razao_social}</dd>` : ''}
      <dt>Data</dt><dd>${formatarBR(d.data)}</dd>
      ${horarioDe(d) ? `<dt>Horário</dt><dd>${horarioDe(d)}</dd>` : ''}
      <dt>Origem</dt><dd>${d.origem}</dd>
      <dt>Destino</dt><dd>${d.destino}</dd>
      ${d.km ? `<dt>Distância</dt><dd>${kmTexto(d.km)}</dd>` : ''}
      <dt>Pagamento</dt><dd>${d.forma_pagamento}</dd>
      <dt>Motorista</dt><dd>${m.full_name || ''}</dd>
      <dt>Placa</dt><dd>${m.plate || ''}</dd>
      ${m.whatsapp ? `<dt>WhatsApp</dt><dd>${m.whatsapp}</dd>` : ''}
    </dl>
    ${chamadaDoMotorista(m) ? `<p class="recibo-chamada">${chamadaDoMotorista(m)}</p>` : ''}
    <p class="recibo-estado">${estadoDoRecibo(recibo)}</p>
    ${estadoDoEmail(recibo) ? `<p class="recibo-estado">${estadoDoEmail(recibo)}</p>` : ''}
    ${estadoDoWhats(recibo) ? `<p class="recibo-estado">${estadoDoWhats(recibo)}</p>` : ''}`;
}

// Desenha o recibo e prepara o link do WhatsApp a partir do MESMO objeto.
// Eram duas coisas separadas e elas saiam de sincronia: o corpo era redesenhado
// depois do envio, o link nao, e o passageiro recebia mensagem sem o endereco.
function mostrarRecibo(recibo) {
  const caixa = $('recibo');
  // Redesenhar o MESMO recibo (o sinal voltou) nao fecha o QR que o
  // motorista abriu para mostrar ao passageiro.
  const qrAberto = caixa.dataset.rid === recibo.rid && !!caixa.querySelector('details.recibo-qr[open]');
  caixa.innerHTML = montarRecibo(recibo);
  caixa.dataset.rid = recibo.rid;
  caixa.dataset.estado = estadoDoRecibo(recibo);
  if (qrAberto) caixa.querySelector('details.recibo-qr')?.setAttribute('open', '');
  const fone = foneE164(recibo.dados.whatsapp_passageiro);
  $('btn-whats').hidden = !fone;
  $('btn-compartilhar').className = fone ? 'secundario' : 'primario';

  const para = recibo.dados.email_passageiro;
  $('btn-email').hidden = !para;
  $('btn-email').textContent = recibo.email_enviado
    ? 'Enviar por e-mail de novo' : 'Enviar por e-mail';
  $('aviso-email').hidden = true;
  $('aviso-email').classList.remove('erro');
  if (fone) {
    const msg = encodeURIComponent(textoParaCompartilhar(recibo));
    $('btn-whats').href = `https://wa.me/${fone}?text=${msg}`;
  } else {
    // Sem isto o href do recibo ANTERIOR continuava no botao.
    $('btn-whats').removeAttribute('href');
  }
}

// Leva para a tela do recibo, venha ele de ser emitido ou do historico.
function abrirRecibo(recibo, { doHistorico = false } = {}) {
  $('btn-compartilhar').dataset.rid = recibo.rid;
  $('btn-voltar-historico').hidden = !doHistorico;
  mostrarRecibo(recibo);
  mostrar('tela-recibo');
}

// Enquanto o recibo nao subiu, o link existe mas a pagina do outro lado nao —
// enviar agora manda ao passageiro um endereco que da 404. Entao: tenta subir,
// segura o botao por no maximo cinco segundos e redesenha com o que voltou.
//
// Vale para o recibo recem-emitido e para o reaberto pelo historico. O
// historico e caminho novo e trazia o mesmo defeito de volta: recibo parado na
// fila, motorista reenvia, passageiro recebe link morto.
//
// Offline nao espera nada: nao ha o que esperar, e o WhatsApp tambem nao
// enviaria. O recibo sobe quando o sinal voltar e o mesmo link passa a valer.
async function esperarSubir(rid) {
  const subiu = sincronizar().then(async () => {
    const atual = (await listarRecibos()).find((r) => r.rid === rid);
    // So redesenha se a tela ainda mostra ESTE recibo. Com sinal fraco, o
    // envio lento do recibo anterior terminava depois de o motorista emitir
    // o seguinte e trocava a tela de volta: o passageiro novo lia o QR do
    // anterior e recebia o recibo de outra pessoa.
    if (atual && $('btn-compartilhar').dataset.rid === rid) mostrarRecibo(atual);
    return atual;
  });
  if (!(await temRede())) return;
  $('btn-whats').classList.add('aguardando');
  await Promise.race([subiu, new Promise((ok) => setTimeout(ok, 5000))]);
  $('btn-whats').classList.remove('aguardando');
}

// O rid nasce no aparelho e a URL publica e deterministica a partir dele.
// Esperar o servidor devolver o endereco criava uma corrida perdida: o
// motorista toca em enviar antes da sincronizacao terminar, e a mensagem sai
// sem link. Usa o endereco que o servidor mandou quando ja chegou; senao,
// monta o mesmo endereco aqui. A base sai do proprio API para nao divergir do
// lugar onde o recibo e gravado.
function linkDoRecibo(recibo) {
  return recibo.url || `${API || location.origin}/recibo/${recibo.rid}`;
}

// O QR leva ao recibo online. E o caminho de quem nao deu telefone nem
// e-mail: o motorista mostra a tela e o passageiro aponta a camera do
// proprio celular. O codigo e desenhado aqui, sem rede, como o resto do
// recibo. Devolve as linhas da matriz ('1' = modulo escuro), para a tela e o
// PDF desenharem o mesmo codigo; null se a biblioteca nao carregou.
function matrizQr(texto) {
  if (typeof qrcode !== 'function' || !texto) return null;
  try {
    const qr = qrcode(0, 'M');
    qr.addData(texto);
    qr.make();
    const n = qr.getModuleCount();
    return Array.from({ length: n }, (_, l) =>
      Array.from({ length: n }, (_, c) => (qr.isDark(l, c) ? '1' : '0')).join(''));
  } catch {
    return null;
  }
}

// Cada sequencia de modulos escuros de uma linha vira um retangulo so. A
// margem branca de 4 modulos faz parte do padrao: sem ela a camera nao acha
// as bordas do codigo.
function svgDoQr(linhas) {
  const n = linhas.length, borda = 4, lado = n + 2 * borda;
  let caminho = '';
  linhas.forEach((linha, l) => {
    for (let c = 0; c < n;) {
      if (linha[c] !== '1') { c++; continue; }
      let fim = c;
      while (fim < n && linha[fim] === '1') fim++;
      caminho += `M${c + borda} ${l + borda}h${fim - c}v1h-${fim - c}z`;
      c = fim;
    }
  });
  return `<svg class="qr" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${lado} ${lado}"` +
    ' role="img" aria-label="QR Code do recibo"' +
    ` shape-rendering="crispEdges"><rect width="${lado}" height="${lado}" fill="#fff"/>` +
    `<path d="${caminho}" fill="#000"/></svg>`;
}

// QR do recibo que vai para o PDF; null quando o recibo nao vai ao ar.
function qrDoRecibo(recibo) {
  return recibo.recusado ? null : matrizQr(linkDoRecibo(recibo));
}

// Fica logo abaixo do valor, para caber na tela sem rolar quando o motorista
// vira o celular para o passageiro.
function blocoQr(recibo) {
  if (recibo.recusado) {
    // Recibo recusado nao vai ao ar: o QR abriria uma pagina vazia. No lugar
    // dele, o motivo e o que fazer, para o motorista nao procurar o QR.
    const pro = recibo.recusado === 'limite_mensal'
      ? ' Assinando o Pro, ele é publicado e o QR aparece.' : '';
    return `
      <div class="recibo-qr recibo-qr-sem">
        <p class="recibo-qr-titulo">Sem QR neste recibo</p>
        <p class="recibo-qr-legenda">Ele não foi publicado (${motivoRecusa(recibo.recusado)}),
          então o link não abre. Envie o PDF ao passageiro ou deixe que ele fotografe esta tela.${pro}</p>
      </div>`;
  }
  const linhas = qrDoRecibo(recibo);
  if (!linhas) return '';
  // Na fila, o QR ja serve: a pagina do outro lado espera o recibo chegar.
  // Mas so chega com sinal e com o app aberto, e isso o motorista precisa ler.
  const legenda = recibo.pendente
    ? 'Ainda não enviado. O passageiro já pode ler o QR: o recibo aparece para ele quando este celular tiver sinal, com o app aberto.'
    : 'O recibo abre no navegador, sem instalar aplicativo.';
  const miolo = `${svgDoQr(linhas)}<p class="recibo-qr-legenda">${legenda}</p>`;
  // Sem WhatsApp nem e-mail do passageiro, o QR e o caminho do recibo: fica
  // aberto. Com contato, os botoes resolvem, e o QR recolhido nao empurra o
  // "Enviar no WhatsApp" para baixo da tela.
  const temContato = !!foneE164(recibo.dados.whatsapp_passageiro) || !!recibo.dados.email_passageiro;
  if (temContato) {
    return `
      <details class="recibo-qr">
        <summary class="recibo-qr-titulo">Mostrar QR Code ao passageiro</summary>
        ${miolo}
      </details>`;
  }
  return `
    <div class="recibo-qr">
      <p class="recibo-qr-titulo">Aponte a câmera do celular</p>
      ${miolo}
    </div>`;
}

function textoParaCompartilhar(recibo) {
  const d = recibo.dados;
  const linhas = [
    `✅ Recibo #${recibo.rid}`,
    ...(d.passageiro ? [`👤 Passageiro: ${d.passageiro}`] : []),
    ...(d.documento_passageiro ? [`🧾 ${rotuloDocumentoDe(d)}: ${d.documento_passageiro}`] : []),
    ...(d.razao_social ? [`🏢 Razão social: ${d.razao_social}`] : []),
    `📅 Data: ${formatarBR(d.data)}`,
    ...(horarioDe(d) ? [`🕐 Horário: ${horarioDe(d)}`] : []),
    `📍 Origem: ${d.origem}`,
    `🏁 Destino: ${d.destino}`,
    ...(d.km ? [`📏 Distância: ${kmTexto(d.km)}`] : []),
    `💰 Valor: ${simboloDe(d)} ${d.valor_exibido}`,
    `💳 Pagamento: ${d.forma_pagamento}`,
  ];
  // Recibo recusado nao vai ao ar: o link levaria a uma pagina de espera sem
  // fim. Mesmo criterio do QR.
  if (!recibo.recusado) {
    linhas.push('', '🔗 Recibo completo, para ver, imprimir ou salvar em PDF:',
                linkDoRecibo(recibo));
  }
  const chamada = chamadaDoMotorista(recibo.motorista);
  if (chamada) linhas.push('', `🚕 ${chamada}`);
  return linhas.join('\n');
}

async function atualizarSugestoesDeLocal() {
  /* Monta a lista de lugares a partir do que o motorista já digitou.
     Vale mais que uma API de lugares para este uso: taxista repete os mesmos
     endereços o dia todo, e isto funciona sem sinal. Ordena por frequência e,
     no empate, pelo mais recente. */
  const contagem = new Map();
  const recencia = new Map();

  for (const r of await listarRecibos()) {
    for (const local of [r.dados.origem, r.dados.destino]) {
      const limpo = (local || '').trim();
      if (limpo.length < 3) continue;
      const chave = limpo.toLocaleUpperCase('pt-BR');
      contagem.set(chave, (contagem.get(chave) || 0) + 1);
      if (!recencia.has(chave) || r.criado_em > recencia.get(chave)) {
        recencia.set(chave, r.criado_em);
      }
      if (!rotuloOriginal.has(chave)) rotuloOriginal.set(chave, limpo);
    }
  }

  const ordenados = [...contagem.entries()]
    .sort((a, b) => b[1] - a[1] || (recencia.get(b[0]) || '').localeCompare(recencia.get(a[0]) || ''))
    .slice(0, 40)
    .map(([chave]) => rotuloOriginal.get(chave) || chave);

  $('locais').innerHTML = ordenados.map((l) => `<option value="${l.replace(/"/g, '&quot;')}">`).join('');
}

const rotuloOriginal = new Map();

async function atualizarAvisoFila() {
  const n = (await pendentes()).length;
  const aviso = $('aviso-fila');
  aviso.hidden = n === 0;
  if (n) aviso.textContent = `${n} recibo(s) aguardando sinal. Serão enviados sozinhos.`;
  const rede = $('rede');
  rede.hidden = await temRede();
  rede.textContent = 'sem sinal';
}

// ── Cabeçalho e menu do perfil ─────────────────────────────────────────────
//
// Pro, histórico e sair viviam soltos: dois no cabeçalho da tela de emitir,
// um no rodapé dela. Fora dessa tela não existiam, e dentro dela disputavam
// espaço com o título. Agora moram no mesmo lugar, alcançável de qualquer
// tela, atrás das iniciais do motorista.

function iniciais(nome) {
  const partes = String(nome || '').trim().split(/\s+/).filter(Boolean);
  if (!partes.length) return '·';
  const ultima = partes.length > 1 ? partes[partes.length - 1][0] : '';
  return (partes[0][0] + ultima).toLocaleUpperCase('pt-BR');
}

const NOME_DO_PLANO = { free: 'Grátis', pro: 'Pro', business: 'Business' };

function atualizarCabecalho() {
  const dentro = !!token();
  $('btn-perfil').hidden = !dentro;
  if (!dentro) {
    // Apagar de verdade, e nao so esconder: o texto no DOM sobrevivia ao
    // "Sair" e o motorista seguinte podia ler o nome e o e-mail do anterior.
    fecharMenu();
    ['perfil-iniciais', 'menu-nome', 'menu-email', 'menu-plano']
      .forEach((id) => { $(id).textContent = ''; });
    return;
  }

  const m = Guardado.get('motorista') || {};
  $('perfil-iniciais').textContent = iniciais(m.full_name);
  $('menu-nome').textContent = m.full_name || 'Motorista';
  $('menu-email').textContent = m.email || '';

  // Quanto ainda cabe no mês: é a resposta para "por que existe um botão Pro".
  const plano = NOME_DO_PLANO[Guardado.get('plano')] || 'Grátis';
  const limite = Guardado.get('limite');
  const usados = Guardado.get('usados');
  $('menu-plano').textContent = limite
    ? `Plano ${plano} · ${usados ?? 0} de ${limite} recibos este mês`
    : `Plano ${plano} · recibos ilimitados`;
}

function fecharMenu() {
  $('menu-perfil').hidden = true;
  $('menu-fundo').hidden = true;
  $('btn-perfil').setAttribute('aria-expanded', 'false');
}

$('btn-perfil').addEventListener('click', () => {
  if (!token()) { fecharMenu(); return; }
  if (!$('menu-perfil').hidden) { fecharMenu(); return; }
  atualizarCabecalho();
  $('menu-perfil').hidden = false;
  $('menu-fundo').hidden = false;
  $('btn-perfil').setAttribute('aria-expanded', 'true');
  vibrar('LIGHT');
  // O contador de recibos envelhece a cada emissão. Com sinal, chega o número
  // de agora; sem sinal, fica o último conhecido em vez de nada.
  atualizarSessao().then(atualizarCabecalho);
});

$('menu-fundo').addEventListener('click', fecharMenu);

// Escolher qualquer item fecha o menu. O que o item FAZ continua com o
// próprio botão, que já tem o seu ouvinte.
$('menu-perfil').addEventListener('click', (ev) => {
  if (ev.target.closest('button')) fecharMenu();
});

// Tocar na marca volta para o início, venha de onde vier. O formulário não é
// apagado: quem tinha posto a data de ontem e foi olhar o histórico encontra o
// recibo como deixou, voltando por "Emitir recibo".
$('btn-inicio').addEventListener('click', () => {
  fecharMenu();
  if (!token() || !$('tela-inicio').hidden) return;
  irParaInicio();
});

// ── Ações ──────────────────────────────────────────────────────────────────
$('form-login').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const erro = $('erro-login');
  erro.hidden = true;
  try {
    const resp = await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({ email: $('email').value, senha: $('senha').value }),
    });
    if (!resp.ok) { erro.textContent = 'E-mail ou senha inválidos.'; erro.hidden = false; return; }
    const t = await resp.json();
    Guardado.set('access_token', t.access_token);
    Guardado.set('refresh_token', t.refresh_token);
    const sessao = await (await api('/api/sessao')).json();
    Guardado.set('motorista', sessao.motorista);
    Guardado.set('motorista_id', sessao.id);   // dono da corrida, ja no login
    await iniciar();
  } catch {
    erro.textContent = 'Sem conexão. Tente de novo quando tiver sinal.';
    erro.hidden = false;
  }
});

$('btn-ir-cadastro').addEventListener('click', () => { mostrar('tela-cadastro'); carregarPaises(); });
$('btn-ir-login').addEventListener('click', () => mostrar('tela-login'));

// ── Recuperar senha ────────────────────────────────────────────────────────
// O app so pede o e-mail. O link chega por e-mail e abre a pagina do site,
// porque a senha vive no Supabase Auth e a troca acontece no servidor. Sem
// esta tela o motorista que esquecia a senha nao tinha saida dentro do app.
$('btn-ir-recuperar').addEventListener('click', () => {
  // Leva o e-mail que ele ja digitou: e o caso comum de quem errou a senha.
  $('r-email').value = $('email').value.trim();
  $('erro-recuperar').hidden = true;
  $('recuperar-enviado').hidden = true;
  $('form-recuperar').hidden = false;
  mostrar('tela-recuperar');
});
$('btn-recuperar-voltar').addEventListener('click', () => mostrar('tela-login'));

$('form-recuperar').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const erro = $('erro-recuperar');
  erro.hidden = true;
  const email = $('r-email').value.trim();
  try {
    const resp = await api('/api/recuperar-senha', {
      method: 'POST',
      body: JSON.stringify({ email }),
    });
    if (resp.status === 429) {
      erro.textContent = 'Muitos pedidos para este e-mail hoje. Tente mais tarde.';
      erro.hidden = false;
      return;
    }
    if (!resp.ok) {
      erro.textContent = 'Confira o e-mail digitado.';
      erro.hidden = false;
      return;
    }
    // A resposta e a mesma exista ou nao a conta; o aviso diz isso.
    $('form-recuperar').hidden = true;
    $('recuperar-enviado').hidden = false;
    // Deixa o e-mail pronto na tela de login para quando ele voltar.
    $('email').value = email;
  } catch {
    erro.textContent = 'Sem conexão. Recuperar a senha precisa de internet.';
    erro.hidden = false;
  }
});

$('form-cadastro').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const erro = $('erro-cadastro');
  erro.hidden = true;

  const corpo = {
    nome_completo: $('c-nome').value.trim(),
    email: $('c-email').value.trim(),
    senha: $('c-senha').value,
    whatsapp: $('c-whats').value.trim(),
    cpf: $('c-cpf').value.trim(),
    cidade: $('c-cidade').value.trim(),
    placa: $('c-placa').value.trim(),
    modelo_veiculo: $('c-modelo').value.trim(),
    indicacao: $('c-indicacao').value.trim().toUpperCase(),
    pais: $('c-pais').value,
    moeda: $('c-moeda').value,
  };

  const mensagens = {
    email_em_uso: 'Já existe uma conta com este e-mail.',
    pais_ou_moeda_invalido: 'Escolha o país e a moeda na lista.',
    codigo_indicacao_invalido: 'Código de indicação não encontrado. Confira com quem te indicou ou deixe em branco.',
    senha_curta: 'A senha precisa ter pelo menos 8 caracteres.',
    campos_obrigatorios: 'Preencha todos os campos marcados com *.',
    campo_longo: 'Um dos campos ficou longo demais.',
    indisponivel: 'Serviço indisponível agora. Tente em instantes.',
  };

  try {
    const resp = await api('/api/cadastro', { method: 'POST', body: JSON.stringify(corpo) });
    if (!resp.ok) {
      const e = await resp.json().catch(() => ({}));
      erro.textContent = mensagens[e.erro] || 'Não foi possível criar a conta.';
      erro.hidden = false;
      return;
    }
    const novo = await resp.json();
    Guardado.set('access_token', novo.access_token);
    Guardado.set('refresh_token', novo.refresh_token);
    const sessao = await (await api('/api/sessao')).json();
    Guardado.set('motorista', sessao.motorista);
    Guardado.set('motorista_id', sessao.id);   // dono da corrida, ja no login
    vibrar('HEAVY');
    await iniciar();
  } catch {
    // Cadastro é a única coisa que exige rede: sem conta não há o que emitir.
    erro.textContent = 'Sem conexão. O cadastro precisa de internet — depois o app funciona offline.';
    erro.hidden = false;
  }
});

// Km rodado como o motorista digita ("12,4", "12.4", "12"); vazio e permitido.
// Um km que nao e numero para aqui, nao no servidor: o recibo ja estaria
// guardado e marcado como recusado, sem o motorista entender por que.
function kmNormalizado(valor) {
  const bruto = String(valor || '').trim().toLowerCase().replace(/\s|km/g, '');
  if (!bruto) return '';
  if (!/^\d{1,4}([.,]\d{1,2})?$/.test(bruto) || Number(bruto.replace(',', '.')) <= 0) return null;
  return Number(bruto.replace(',', '.')).toFixed(1).replace('.', ',');
}
const kmTexto = (km) => (km ? `${km} km` : '');
function horarioDe(d) {
  if (d.hora && d.hora_fim) return `${d.hora} às ${d.hora_fim}`;
  if (d.hora) return `saída ${d.hora}`;
  if (d.hora_fim) return `chegada ${d.hora_fim}`;
  return '';
}

$('form-recibo').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const erroRecibo = $('erro-recibo');
  erroRecibo.hidden = true;
  const km = kmNormalizado($('km').value);
  if (km === null) {
    erroRecibo.textContent = 'Km rodado: só o número, ex.: 12,4. Ou deixe em branco.';
    erroRecibo.hidden = false;
    $('km').focus();
    return;
  }
  const valor = $('valor').value.trim();
  const rid = novoRid();
  const recibo = {
    rid,
    criado_em: new Date().toISOString(),
    pendente: true,
    motorista: Guardado.get('motorista') || {},
    dados: {
      rid,
      passageiro: $('passageiro').value.trim(),
      data: $('data').value,
      hora: $('hora').value,
      hora_fim: $('hora-fim').value,
      km,
      origem: $('origem').value.trim(),
      destino: $('destino').value.trim(),
      valor,
      valor_exibido: valorExibido(valor),
      // Moeda e pais de quando o recibo foi emitido: o servidor grava a moeda,
      // e a tela, o PDF e o WhatsApp mostram o simbolo certo mesmo sem sinal.
      moeda: moedaAtual().codigo,
      moeda_simbolo: moedaAtual().simbolo,
      pais: paisAtual(),
      data_exibida: formatarBR($('data').value),
      observacoes: '',
      forma_pagamento: $('forma').value,
      whatsapp_passageiro: $('whats').value.trim(),
      email_passageiro: emailLimpo($('email-passageiro').value),
      documento_passageiro: documentoBR($('documento').value),
      razao_social: ehCNPJ($('documento').value)
        ? $('razao-social').value.trim().replace(/\s+/g, ' ') : '',
      // Para medir quantos recibos saem do caminho da corrida. O servidor
      // ainda ignora o campo; passa a guardar quando a coluna existir.
      ...(reciboDaCorrida ? { via_corrida: true } : {}),
    },
  };

  await salvarRecibo(recibo);        // primeiro guarda, depois tenta enviar
  vibrar('HEAVY');
  $('form-recibo').reset();
  atualizarCampoRazaoSocial();   // o reset limpa o valor, nao o que eu escondi
  revisarWhats(); revisarEmail();
  // A corrida acabou de virar recibo: some do início junto com a dica.
  if (reciboDaCorrida) { apagarCorrida(); limparCorridaDoFormulario(); }
  preencherDataEHora();     // o próximo recibo já nasce com a hora certa

  abrirRecibo(recibo);

  await esperarSubir(rid);
  await atualizarAvisoFila();
});

// O Share.share() do iOS abre a folha do sistema, que lista contatos salvos —
// o passageiro de uma corrida avulsa nunca esta la. Por isso um link direto.
//
// E um <a href> de verdade, e nao um botao com window.open, por dois motivos:
// a WKWebView bloqueia popup aberto fora do gesto do usuario, e o nosso
// handler tinha um await antes do open — quando a promessa resolvia o gesto ja
// tinha expirado e o toque nao fazia nada. Alem disso, navegacao para fora da
// origem o Capacitor entrega ao sistema, que reconhece o wa.me como universal
// link e abre o WhatsApp. O href e montado ao exibir o recibo, nao no clique.
//
// Sem target="_blank" de proposito: com ele a navegacao vira popup e cai em
// createWebViewWith, o mesmo caminho que engolia o toque. Verificado no
// simulador — navegacao simples para fora da origem faz o Capacitor entregar
// ao sistema, e o Safari abre por cima do app.
$('btn-whats').addEventListener('click', () => vibrar());

// O unico caminho do e-mail. Emitir nao manda nada: preencher o campo e uma
// coisa, mandar e outra, e cada envio tem custo.
//
// Recibo que ainda esta na fila nao existe no servidor, e o envio responderia
// 404. Entao sobe primeiro, e so depois manda.
$('btn-email').addEventListener('click', async () => {
  const rid = $('btn-compartilhar').dataset.rid;
  const aviso = $('aviso-email');
  const botao = $('btn-email');
  vibrar();

  const dizer = (texto, ehErro = false) => {
    aviso.textContent = texto;
    aviso.classList.toggle('erro', ehErro);
    aviso.hidden = false;
  };

  botao.disabled = true;
  try {
    let recibo = (await listarRecibos()).find((r) => r.rid === rid);
    if (!recibo) return;

    if (recibo.pendente) {
      dizer('Subindo o recibo…');
      await esperarSubir(rid);
      recibo = (await listarRecibos()).find((r) => r.rid === rid);
      if (!recibo || recibo.pendente) {
        dizer('O recibo ainda não subiu. Tente de novo quando tiver sinal.', true);
        return;
      }
    }

    dizer('Enviando…');
    const resp = await api(`/api/recibos/${rid}/email`, { method: 'POST' });
    if (resp.ok) {
      const atualizado = { ...recibo, email_enviado: true };
      await salvarRecibo(atualizado);
      mostrarRecibo(atualizado);          // redesenha: some o aviso e muda o rotulo
      dizer(`Enviado para ${recibo.dados.email_passageiro}.`);
      vibrar('HEAVY');
      return;
    }
    dizer({
      404: 'Este recibo ainda não chegou ao servidor.',
      400: 'Este recibo não tem e-mail do passageiro.',
      401: 'Sua sessão expirou. Entre de novo.',
      429: 'Você atingiu o limite de e-mails de hoje.',
      502: 'O provedor de e-mail recusou. Tente de novo em instantes.',
    }[resp.status] || 'Não foi possível enviar agora.', true);
  } catch {
    dizer('Sem conexão. Tente quando tiver sinal.', true);
  } finally {
    botao.disabled = false;
  }
});

$('btn-compartilhar').addEventListener('click', async () => {
  const rid = $('btn-compartilhar').dataset.rid;
  const recibo = (await listarRecibos()).find((r) => r.rid === rid);
  if (!recibo) return;
  vibrar();

  const texto = textoParaCompartilhar(recibo);
  const { Share, Filesystem } = P();

  // No aparelho, manda o PDF junto: o passageiro recebe um documento, não um
  // texto solto — e funciona mesmo sem sinal, porque o PDF é gerado local.
  if (Share && Filesystem) {
    try {
      const nomeArquivo = `recibo-${rid}.pdf`;
      const escrito = await Filesystem.writeFile({
        path: nomeArquivo,
        data: pdfDoRecibo(recibo, { link: linkDoRecibo(recibo), qr: qrDoRecibo(recibo) }),
        directory: 'CACHE',
      });
      await Share.share({
        title: `Recibo #${rid}`,
        text: texto,
        files: [escrito.uri],
        dialogTitle: 'Enviar recibo',
      });
      return;
    } catch (e) {
      // PDF falhou: cai para texto puro em vez de deixar o motorista na mão.
    }
  }

  if (Share) { await Share.share({ title: `Recibo #${rid}`, text: texto }); return; }
  if (navigator.share) { await navigator.share({ title: `Recibo #${rid}`, text: texto }); return; }

  const fone = foneE164(recibo.dados.whatsapp_passageiro);
  window.open(`https://wa.me/${fone}?text=${encodeURIComponent(texto)}`, '_blank');
});

// ── Assinatura ─────────────────────────────────────────────────────────────
//
// As duas lojas exigem que a compra passe pela loja: Apple na Guideline 3.1.1,
// Google na politica de Pagamentos. Nao ha, e nao pode haver, link para
// assinar no site dentro do app.

// Id do motorista com que o SDK da loja foi configurado neste processo.
let comprasPara = '';

async function iniciarCompras() {
  const { Purchases } = P();
  const chave = chaveDaLoja();
  // Amarra a compra ao motorista: sem isto o webhook não sabe quem assinou.
  // O id vai JA no configure. Configurar sem ele e fazer logIn depois criava
  // um cliente anonimo ($RCAnonymousID) a cada instalacao, e o RevenueCat so
  // junta o primeiro a conta: reinstalacao, troca de aparelho, revisao das
  // lojas e teste deixavam clientes sobrando (99 para 65 motoristas em
  // 2026-10). Sem o id ainda nao configura: comprar exige estar logado, e a
  // tela de assinatura chama de novo. Nunca chamar o logOut do SDK ao sair
  // da conta — ele cria outro anonimo.
  const perfil = Guardado.get('motorista_id');
  if (!Purchases || !chave || !perfil) return false;
  if (comprasPara === perfil) return true;
  try {
    await Purchases.configure({ apiKey: chave, appUserID: perfil });
    comprasPara = perfil;
    return true;
  } catch (e) {
    return false;
  }
}

// Pacotes da oferta atual, pelo identificador do RevenueCat. O preco de cada
// um vem da loja, nunca escrito aqui: a Apple e o Google nao cobram o mesmo
// valor no anual, e a tela tem que dizer exatamente o que a folha de pagamento
// vai cobrar.
const ANUAL = '$rc_annual';
const MENSAL = '$rc_monthly';
let pacotesPro = {};
let planoEscolhido = MENSAL;
let escolhidoNaTela = false;   // o toque do motorista vale mais que o padrao

const assinaPeloServidor = () => ['pro', 'business'].includes(Guardado.get('plano'));

function emReais(valor, moeda) {
  try {
    return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: moeda || 'BRL' }).format(valor);
  } catch {
    return `R$ ${Number(valor).toFixed(2).replace('.', ',')}`;
  }
}

function escolherPlano(id) {
  planoEscolhido = id;
  $('plano-anual').setAttribute('aria-checked', String(id === ANUAL));
  $('plano-mensal').setAttribute('aria-checked', String(id === MENSAL));
  $('btn-assinar').textContent = id === ANUAL ? 'Assinar o plano anual' : 'Assinar o plano mensal';
}

function mostrarPlanos() {
  const anual = pacotesPro[ANUAL]?.product;
  const mensal = pacotesPro[MENSAL]?.product;
  if (mensal?.priceString) $('preco-mensal').textContent = `${mensal.priceString}/mês`;

  // Sem o anual na resposta da loja (ainda em analise, ou sem rede na primeira
  // abertura), a tela fica so com o mensal — nunca com um preco inventado.
  $('plano-anual').hidden = !anual;
  if (anual) {
    $('preco-anual').textContent = `${anual.priceString}/ano`;
    const porMes = anual.pricePerMonthString || emReais(anual.price / 12, anual.currencyCode);
    $('nota-anual').textContent = `Cobrado uma vez por ano · equivale a ${porMes}/mês`;
    const economia = mensal?.price ? Math.round((1 - anual.price / (mensal.price * 12)) * 100) : 0;
    $('selo-anual').textContent = `economize ${economia}%`;
    $('selo-anual').hidden = economia < 5;
  }

  if (!escolhidoNaTela) escolherPlano(anual ? ANUAL : MENSAL);
  else if (!pacotesPro[planoEscolhido] && Object.keys(pacotesPro).length) escolherPlano(MENSAL);
}

async function buscarPacotes() {
  const { Purchases } = P();
  const ofertas = await Purchases.getOfferings();
  const lista = ofertas?.current?.availablePackages || [];
  pacotesPro = Object.fromEntries(lista.map((p) => [p.identifier, p]));
  mostrarPlanos();
}

// Quem ja assina ve "ativa" e o link da loja, em vez de outra compra. Vale o
// servidor OU a loja: logo depois de pagar, o webhook pode nao ter chegado
// ainda, e o servidor continua dizendo Gratis por alguns segundos.
function mostrarAssinaturaAtiva(link) {
  $('pro-ativo').hidden = false;
  $('planos-pro').hidden = true;
  $('btn-assinar').hidden = true;
  if (link) { $('link-gerenciar').href = link; $('link-gerenciar').hidden = false; }
}

async function conferirNaLoja() {
  const { Purchases } = P();
  if (!Purchases || !chaveDaLoja()) return;
  // Se o id do motorista ainda nao existia quando o app abriu, configura agora.
  if (!(await iniciarCompras())) return;
  try {
    const { customerInfo } = await Purchases.getCustomerInfo();
    if (customerInfo?.entitlements?.active?.pro || assinaPeloServidor()) {
      mostrarAssinaturaAtiva(customerInfo?.managementURL || '');
      return;
    }
  } catch { /* sem resposta da loja: segue o que o servidor disse */ }
  try { await buscarPacotes(); } catch { /* o toque em Assinar tenta de novo e mostra o motivo */ }
}

function abrirAssinatura(motivo) {
  $('motivo-pro').textContent = motivo || '';
  $('erro-pro').hidden = true;
  $('link-gerenciar').hidden = true;
  escolhidoNaTela = false;

  const jaAssina = assinaPeloServidor();
  $('pro-ativo').hidden = !jaAssina;
  mostrarCupomDeIndicacao(jaAssina);
  // Pro pre-pago (anual ou mes de indicacao) tem data de fim; assinatura, nao.
  const fimPro = Guardado.get('pro_until');
  $('pro-ativo-texto').textContent = jaAssina && fimPro
    ? `Seu Pro vale até ${new Date(fimPro).toLocaleDateString('pt-BR')}. Depois disso, é só assinar por aqui.`
    : 'Sua assinatura Pro está ativa.';
  $('planos-pro').hidden = jaAssina;
  $('btn-assinar').hidden = jaAssina;
  mostrarPlanos();

  // No Android a assinatura se cancela pelo Google Play, nao pelos Ajustes.
  const onde = plataforma() === 'android' ? 'pelo Google Play' : 'pelos Ajustes do aparelho';
  $('aviso-renovacao').textContent = 'Renova automaticamente ao fim de cada período, mensal ou '
    + `anual. Cancele quando quiser ${onde}, até 24 h antes da próxima cobrança.`;
  mostrar('tela-assinatura');
  conferirNaLoja();
}

$('plano-anual').addEventListener('click', () => { escolhidoNaTela = true; escolherPlano(ANUAL); });
$('plano-mensal').addEventListener('click', () => { escolhidoNaTela = true; escolherPlano(MENSAL); });

// A tela de assinatura tambem abre sozinha no 402. Este botao existe para o
// caso em que ninguem bateu no limite ainda: sem ele a analise da Apple nao
// tem como chegar na compra, e assinante nenhum tem como restaurar.
$('btn-pro').addEventListener('click', () => abrirAssinatura(''));

// A recusa por limite abre esta tela por cima do recibo recem-emitido. Voltar
// ao formulario escondia o aviso de que o recibo ficou sem QR.
$('btn-fechar-pro').addEventListener('click', () => {
  const paraRecibo = voltarDaAssinaturaParaRecibo;
  voltarDaAssinaturaParaRecibo = false;
  if (paraRecibo) { mostrar('tela-recibo'); return; }
  voltarParaBase();
});

// "Nao foi possivel concluir a compra" era tudo que aparecia, para qualquer
// causa: produto que a loja nao devolveu, contrato pendente, aparelho sem conta
// de teste, rede caida. Sem o motivo na tela, o motorista repete o toque e eu
// fico adivinhando. Agora o app diz o que deu, e guarda o codigo tecnico junto
// — feio, mas e o que permite consertar em vez de chutar.
const motivosDeCompra = () => ({
  PRODUCT_NOT_AVAILABLE_FOR_PURCHASE:
    `A ${nomeDaLoja()} não está oferecendo a assinatura ainda. Isso costuma ser o `
    + 'contrato de apps pagos que ainda não entrou em vigor.',
  CONFIGURATION_ERROR:
    `A assinatura não chegou da ${nomeDaLoja()}. Confira se o produto está pronto e `
    + 'se o contrato de apps pagos está ativo.',
  OFFLINE_CONNECTION_ERROR: 'Sem conexão. Tente com sinal melhor.',
  NETWORK_ERROR: 'A loja não respondeu. Tente de novo em instantes.',
  STORE_PROBLEM_ERROR: `A ${nomeDaLoja()} está com problema agora. Tente mais tarde.`,
  PURCHASE_NOT_ALLOWED_ERROR:
    'Este aparelho não permite compras. Veja as restrições de compra do aparelho.',
  PAYMENT_PENDING_ERROR: 'A compra ficou pendente de aprovação. Aguarde a confirmação.',
  RECEIPT_ALREADY_IN_USE_ERROR: 'Esta compra já está em outra conta.',
  INVALID_APP_USER_ID: 'Sessão inconsistente. Saia e entre de novo.',
});

// O plugin do RevenueCat devolve o codigo como NUMERO em texto ("23"), nao o
// nome. Descobri no aparelho: a tela mostrou "(23)" e nenhuma frase, porque a
// tabela so conhecia os nomes. Traduz o numero antes de procurar.
const NOME_DO_CODIGO_RC = {
  '1': 'PURCHASE_CANCELLED_ERROR',
  '2': 'STORE_PROBLEM_ERROR', '3': 'PURCHASE_NOT_ALLOWED_ERROR',
  '5': 'PRODUCT_NOT_AVAILABLE_FOR_PURCHASE', '7': 'RECEIPT_ALREADY_IN_USE_ERROR',
  '10': 'NETWORK_ERROR', '14': 'INVALID_APP_USER_ID', '20': 'PAYMENT_PENDING_ERROR',
  '23': 'CONFIGURATION_ERROR', '35': 'OFFLINE_CONNECTION_ERROR',
};

function mostrarErroDeCompra(erro, e) {
  const bruto = String(e?.code ?? e?.errorCode ?? '');
  const codigo = NOME_DO_CODIGO_RC[bruto] || bruto;
  const base = motivosDeCompra()[codigo]
    || (String(e?.message || '').includes('sem oferta')
        ? `A ${nomeDaLoja()} não devolveu nenhuma assinatura para vender. `
          + 'O produto precisa estar pronto e o contrato de apps pagos ativo.'
        : 'Não foi possível concluir a compra.');
  erro.textContent = `${base}${codigo ? ` (${codigo}${bruto !== codigo ? ' ' + bruto : ''})` : ''}`;
  erro.hidden = false;
}

$('btn-assinar').addEventListener('click', async () => {
  const erro = $('erro-pro');
  erro.hidden = true;
  const { Purchases } = P();
  if (!Purchases || !chaveDaLoja()) {
    erro.textContent = 'Assinatura ainda não disponível nesta versão.';
    erro.hidden = false;
    return;
  }
  // O toque confirma o plano que esta marcado na tela. Sem isto, a busca logo
  // abaixo podia trazer o anual e trocar a escolha do motorista antes da compra.
  escolhidoNaTela = true;
  try {
    // A tela pode ter aberto sem rede, ou a loja ainda nao ter respondido.
    // Buscar aqui de novo faz o erro de verdade (sem conexao, produto que a
    // loja nao devolveu) chegar na tela com o motivo, em vez de "sem oferta".
    if (!pacotesPro[planoEscolhido]) await buscarPacotes();
    // Pelo identificador, nao pela posicao: o pacote e exatamente o que esta
    // marcado na tela. Sem ele, nao compra outra coisa no lugar.
    const pacote = pacotesPro[planoEscolhido];
    if (!pacote) throw new Error('sem oferta configurada');
    // Android com o desconto de indicacao: compra a opcao da oferta
    // `indicacao70` (70% por 12 meses) em vez do plano base. So o mensal tem.
    const opcaoComDesconto = planoEscolhido === MENSAL ? opcaoGoogleComDesconto(pacote) : null;
    const r = opcaoComDesconto
      ? await Purchases.purchaseSubscriptionOption({ subscriptionOption: opcaoComDesconto })
      : await Purchases.purchasePackage({ aPackage: pacote });
    const virou = !!r?.customerInfo?.entitlements?.active?.pro;
    if (virou) {
      vibrar('HEAVY');
      await atualizarSessao();
      sincronizar().then(atualizarAvisoFila);   // o que a cota segurava sobe agora
      voltarParaBase();
    }
  } catch (e) {
    // Cancelar não é erro: o motorista fechou a folha de pagamento.
    const nome = NOME_DO_CODIGO_RC[String(e?.code ?? '')] || String(e?.code ?? '');
    if (nome === 'PURCHASE_CANCELLED_ERROR' || /cancel/i.test(e?.message || '')) return;
    mostrarErroDeCompra(erro, e);
  }
});

// Obrigatório pela Apple: quem trocou de aparelho precisa recuperar o que pagou.
$('btn-restaurar').addEventListener('click', async () => {
  const erro = $('erro-pro');
  erro.hidden = true;
  const { Purchases } = P();
  if (!Purchases) return;
  try {
    const r = await Purchases.restorePurchases();
    if (r?.customerInfo?.entitlements?.active?.pro) {
      await atualizarSessao();
      sincronizar().then(atualizarAvisoFila);   // o que a cota segurava sobe agora
      voltarParaBase();
    } else {
      erro.textContent = 'Nenhuma assinatura ativa encontrada nesta conta.';
      erro.hidden = false;
    }
  } catch (e) {
    mostrarErroDeCompra(erro, e);
  }
});

// Chamada tanto pelo botao quanto quando o servidor recusa em definitivo.
// Antes, sessao morta nao levava a lugar nenhum: o app ficava na tela de
// emitir aceitando recibo que nunca subiria.
function encerrarSessao() {
  Guardado.del('access_token'); Guardado.del('refresh_token');
  Guardado.del('motorista'); Guardado.del('motorista_id');
  Guardado.del('plano'); Guardado.del('limite'); Guardado.del('usados');
  Guardado.del('codigo_indicacao'); Guardado.del('link_indicacao'); Guardado.del('pro_until');
  Guardado.del('cupom_indicacao');
  ['moeda', 'moeda_simbolo', 'moeda_casas', 'pais', 'ddi'].forEach((k) => Guardado.del(k));
  aplicarMoedaNaTela();
  // A corrida aberta FICA. Esta funcao tambem roda sozinha quando a sessao
  // morre (401 no meio da corrida), e apagar aqui perdia a corrida do
  // motorista que so precisava entrar de novo. Quem protege o aparelho
  // dividido e corridaAtual(): corrida de outro motorista nao aparece.
  limparRascunhoDaCorrida();
  fecharMenu();
  atualizarCabecalho();
  mostrar('tela-login');
}

// Recibo recusado por cota ficava recusado para sempre. Era de proposito —
// reenviar direto seria bater no teto em laco — mas cota e coisa que muda:
// vira o mes, o motorista assina, o limite do plano sobe. Quando o servidor
// diz que ha espaco, os recusados voltam para a fila e sobem na proxima
// sincronizacao. Sem isto, subir o limite nao destravava recibo nenhum.
async function reabrirRecusadosPorCota(sessao) {
  const haEspaco = sessao.limite_mensal == null || sessao.usados_no_mes < sessao.limite_mensal;
  if (!haEspaco) return 0;
  const recusados = (await listarRecibos()).filter((r) => r.recusado === 'limite_mensal');
  for (const r of recusados) {
    const { recusado, ...resto } = r;
    await salvarRecibo({ ...resto, pendente: true });
  }
  return recusados.length;
}

// Recibo sem nome recusado por um servidor anterior ao nome opcional (build
// novo antes do deploy, ou deploy desfeito). Volta para a fila so quando o
// servidor diz que aceita: sem o sinal, seria recusado de novo.
async function reabrirRecusadosSemNome(sessao) {
  if (sessao.passageiro_opcional !== true) return 0;
  const recusados = (await listarRecibos()).filter((r) => r.recusado === 'campos_obrigatorios'
    && !r.dados.passageiro && r.dados.data && r.dados.origem && r.dados.destino);
  for (const r of recusados) {
    const { recusado, ...resto } = r;
    await salvarRecibo({ ...resto, pendente: true });
  }
  return recusados.length;
}

async function atualizarSessao() {
  try {
    const r = await api('/api/sessao');
    if (r.status === 401) { encerrarSessao(); return null; }
    if (!r.ok) return null;
    const s = await r.json();
    Guardado.set('motorista', s.motorista);
    Guardado.set('motorista_id', s.id);
    Guardado.set('plano', s.plano);
    Guardado.set('limite', s.limite_mensal);
    Guardado.set('usados', s.usados_no_mes);
    // Codigo de indicacao e fim do Pro pre-pago: guardados para a tela de
    // indicar e a de assinatura funcionarem sem sinal.
    Guardado.set('codigo_indicacao', s.codigo_indicacao || '');
    Guardado.set('link_indicacao', s.link_indicacao || '');
    Guardado.set('pro_until', s.pro_until || '');
    Guardado.set('cupom_indicacao', s.cupom_indicacao || null);
    if (s.moeda) {
      Guardado.set('moeda', s.moeda);
      Guardado.set('moeda_simbolo', s.moeda_simbolo || 'R$');
      Guardado.set('moeda_casas', Number.isInteger(s.moeda_casas) ? s.moeda_casas : 2);
      Guardado.set('pais', s.pais || 'BR');
      Guardado.set('ddi', s.ddi || '');
      aplicarMoedaNaTela();
    }
    atualizarCabecalho();
    await reabrirRecusadosPorCota(s);
    await reabrirRecusadosSemNome(s);
    return s;
  } catch { return null; }
}

// `daCorrida` e a corrida encerrada que vira recibo: origem, destino, data e
// hora de saida ja entram preenchidos.
async function irParaEmitir({ comHoraDeAgora = true, daCorrida = null } = {}) {
  if (daCorrida) {
    preencherComCorrida(daCorrida);
  } else {
    // Recibo avulso depois de um da corrida que nao foi emitido: tira o que
    // veio da corrida, senao o recibo novo sairia com a origem e o destino dela.
    if (reciboDaCorrida) limparCorridaDoFormulario();
    if (comHoraDeAgora) preencherDataEHora();
  }
  await atualizarSugestoesDeLocal();   // aprende o local que acabou de ser usado
  await atualizarAvisoFila();
  mostrar('tela-emitir');
}

// O valor fica vazio de proposito: o recibo diz o que o passageiro pagou, e na
// cidade grande quem diz isso e o taximetro. A estimativa aparece embaixo do
// campo, para conferir — nunca dentro dele.
let reciboDaCorrida = false;

function preencherComCorrida(c) {
  const saida = agoraLocal(new Date(c.inicio));
  $('data').value = saida.data;
  $('hora').value = saida.hora;
  // Chegada e km vem da propria corrida: a hora em que encerrou e a rota que
  // o Google estimou (o motorista corrige se o caminho foi outro).
  $('hora-fim').value = c.fim ? agoraLocal(new Date(c.fim)).hora : '';
  $('km').value = c.estimativa && typeof c.estimativa.km === 'number' ? kmNormalizado(String(c.estimativa.km)) || '' : '';
  $('origem').value = c.origem || '';
  $('destino').value = c.destino || '';
  $('valor').value = '';
  // O exemplo "45,00" do campo, logo acima da faixa, pareceria um valor sugerido.
  $('valor').placeholder = '0,00';
  const faixa = faixaDaCorrida(c);
  $('dica-estimativa').textContent = faixa
    ? `Estimativa desta corrida: ${textoDaFaixa(faixa)}. Digite o valor cobrado.` : '';
  $('dica-estimativa').hidden = !faixa;
  $('titulo-emitir').textContent = 'Recibo da corrida';
  reciboDaCorrida = true;
}

function limparCorridaDoFormulario() {
  ['origem', 'destino', 'valor', 'hora-fim', 'km'].forEach((id) => { $(id).value = ''; });
  $('valor').placeholder = '45,00';
  $('dica-estimativa').hidden = true;
  $('titulo-emitir').textContent = 'Novo recibo';
  reciboDaCorrida = false;
}
// Sem passar o ouvinte direto: ele receberia o evento do clique como opcoes.
$('btn-novo').addEventListener('click', () => irParaEmitir());
$('btn-voltar').addEventListener('click', voltarParaBase);

// ── Botão voltar do Android ────────────────────────────────────────────────
// No Android o voltar é do sistema, não da tela. Sem ninguém escutando, o
// Capacitor só tenta andar para trás no histórico do WebView — e este app é
// uma página só, sem histórico nenhum. O resultado é o pior possível: o
// motorista aperta voltar e não acontece NADA, como se o aparelho tivesse
// travado. No iPhone o problema não existe porque lá não há esse botão.
//
// Aqui o voltar faz o que faz em qualquer app: fecha o que está por cima,
// senão sobe um nível, e só deixa sair quando já está na tela inicial.
function voltarUmNivel() {
  if (!$('menu-perfil').hidden) { fecharMenu(); return true; }

  if (!$('tela-recibo').hidden) {
    // Veio do histórico volta para o histórico; recém-emitido volta para o
    // formulário. É o mesmo destino do botão que está na tela.
    if ($('btn-voltar-historico').hidden) irParaEmitir({ comHoraDeAgora: false });
    else mostrarHistorico();
    return true;
  }

  if (!$('tela-historico').hidden || !$('tela-assinatura').hidden
      || !$('tela-excluir').hidden || !$('tela-tarifa').hidden || !$('tela-indicar').hidden) {
    voltarParaBase();
    return true;
  }

  // O formulário e a corrida deixaram de ser a raiz: a raiz agora é o início.
  // A corrida aberta não se perde — o início mostra o aviso para continuar.
  if (!$('tela-emitir').hidden || !$('tela-corrida').hidden) {
    irParaInicio();
    return true;
  }

  if (!$('tela-cadastro').hidden) { mostrar('tela-login'); return true; }
  if (!$('tela-recuperar').hidden) { mostrar('tela-login'); return true; }

  return false;  // já está na raiz: sair do app é a resposta certa
}

if (P().App) {
  P().App.addListener('backButton', () => {
    if (!voltarUmNivel()) P().App.exitApp();
  });
}

// As duas lojas exigem que quem cria conta dentro do app consiga apagar dentro
// do app: Apple na 5.1.1(v), Google na politica de exclusao de dados. Mandar
// para o site nao cumpre.
$('btn-excluir-conta').addEventListener('click', () => {
  $('form-excluir').reset();
  $('erro-excluir').hidden = true;
  mostrar('tela-excluir');
});
$('btn-fechar-excluir').addEventListener('click', voltarParaBase);

$('form-excluir').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const erro = $('erro-excluir');
  erro.hidden = true;

  if ($('confirmacao-exclusao').value.trim().toLocaleUpperCase('pt-BR') !== 'EXCLUIR') {
    erro.textContent = 'Digite EXCLUIR, em maiúsculas, para confirmar.';
    erro.hidden = false;
    return;
  }

  // A fila fica no aparelho. Apagar a conta com recibo por subir perderia
  // aquele recibo para sempre, e o passageiro ja foi embora com o link.
  const naFila = (await pendentes()).length;
  if (naFila) {
    erro.textContent = `${naFila} recibo(s) ainda não subiram. Espere o sinal ` +
      'voltar antes de excluir, senão eles se perdem.';
    erro.hidden = false;
    return;
  }

  const botao = ev.target.querySelector('button[type="submit"]');
  botao.disabled = true;
  try {
    const resp = await api('/api/excluir-conta', {
      method: 'POST',
      body: JSON.stringify({ senha: $('senha-exclusao').value }),
    });
    if (resp.ok) {
      await limparRecibosLocais();
      vibrar('HEAVY');
      encerrarSessao();
      return;
    }
    const corpo = await resp.json().catch(() => ({}));
    erro.textContent = {
      senha_incorreta: 'Senha incorreta. A conta não foi excluída.',
      assinatura_ativa: 'Não conseguimos cancelar sua assinatura agora, então a '
        + 'conta não foi excluída — apagá-la deixaria a cobrança ativa. Tente de novo.',
      assinatura_na_loja: 'Cancele a assinatura nos Ajustes do aparelho primeiro. '
        + 'Só a loja pode parar essa cobrança; apagar a conta aqui não pararia.',
    }[corpo.erro] || 'Não foi possível excluir agora. Tente de novo.';
    erro.hidden = false;
  } catch {
    erro.textContent = 'Sem conexão. A exclusão precisa de internet.';
    erro.hidden = false;
  } finally {
    botao.disabled = false;
  }
});
$('btn-sair').addEventListener('click', encerrarSessao);

// Sem nome do passageiro, a rota vira o titulo do item: e o que distingue um
// recibo do outro.
async function mostrarHistorico() {
  const lista = await listarRecibos();
  $('lista-historico').innerHTML = lista.length
    ? lista.map((r) => `
        <div class="item" role="button" data-rid="${r.rid}">
          <div>
            <strong>${r.dados.passageiro || `${r.dados.origem} → ${r.dados.destino}`}</strong>
            <small>${formatarBR(r.dados.data)} · ${r.dados.passageiro
              ? `${r.dados.origem} → ${r.dados.destino}` : 'sem nome do passageiro'}</small>
          </div>
          <div class="item-direita">
            <span class="item-valor">${simboloDe(r.dados)} ${r.dados.valor_exibido}</span>
            <small>${r.pendente ? '⏳ na fila' : r.recusado ? '⚠ ' + motivoRecusa(r.recusado) : '✓ enviado'}</small>
          </div>
          <span class="item-seta" aria-hidden="true">›</span>
        </div>`).join('')
    : '<p class="ajuda">Nenhum recibo ainda.</p>';
  mostrar('tela-historico');
}

$('btn-historico').addEventListener('click', mostrarHistorico);
$('btn-voltar-historico').addEventListener('click', mostrarHistorico);

// Tocar num recibo do historico abre a mesma tela do recibo recem-emitido,
// com WhatsApp e PDF. E o reenvio: passageiro que perdeu a mensagem, ou
// recibo que ficou na fila e o motorista quer conferir.
$('lista-historico').addEventListener('click', async (ev) => {
  const item = ev.target.closest('.item[data-rid]');
  if (!item) return;
  const recibo = (await listarRecibos()).find((r) => r.rid === item.dataset.rid);
  if (!recibo) return;
  vibrar('LIGHT');
  abrirRecibo(recibo, { doHistorico: true });
  // So quem ainda nao subiu precisa esperar; o que ja tem pagina no ar abre
  // pronto para enviar.
  if (recibo.pendente || recibo.recusado) await esperarSubir(recibo.rid);
});

// O sinal voltou com o recibo aberto na tela: redesenha, para o estado e a
// legenda do QR dizerem que ele ja esta no ar. So quando o estado mudou de
// verdade: redesenhar apaga o aviso do e-mail que o motorista pode estar lendo.
async function redesenharReciboAberto() {
  if ($('tela-recibo').hidden) return;
  const rid = $('btn-compartilhar').dataset.rid;
  const atual = (await listarRecibos()).find((r) => r.rid === rid);
  // Confere de novo depois do await: o motorista pode ter aberto outro recibo.
  if (atual && $('btn-compartilhar').dataset.rid === rid
      && estadoDoRecibo(atual) !== $('recibo').dataset.estado) mostrarRecibo(atual);
}

window.addEventListener('online', async () => {
  await sincronizar(); atualizarAvisoFila(); redesenharReciboAberto();
});
window.addEventListener('offline', atualizarAvisoFila);

// No aparelho o evento do plugin é mais confiável que o do navegador.
if (P().Network) {
  P().Network.addListener('networkStatusChange', async (st) => {
    if (st.connected) await sincronizar();
    atualizarAvisoFila();
    redesenharReciboAberto();
  });
}

// ── Tela inicial ───────────────────────────────────────────────────────────
function primeiroNome(nome) {
  return String(nome || '').trim().split(/\s+/)[0] || '';
}

function atualizarSaudacao() {
  const nome = primeiroNome((Guardado.get('motorista') || {}).full_name);
  $('titulo-inicio').textContent = nome ? `Olá, ${nome}` : 'Olá';
}

const horaDe = (iso) => agoraLocal(new Date(iso)).hora;

function mostrarCorridaAberta() {
  const c = corridaAtual();
  $('btn-continuar-corrida').hidden = !c;
  if (!c) return;
  $('corrida-aberta-rotulo').textContent = c.fim
    ? 'Corrida encerrada · falta o recibo' : 'Corrida em andamento';
  $('corrida-aberta-destino').textContent = c.destino;
  $('corrida-aberta-desde').textContent = c.fim
    ? `Das ${horaDe(c.inicio)} às ${horaDe(c.fim)} · toque para decidir`
    : `Desde as ${horaDe(c.inicio)} · toque para continuar`;
}

function irParaInicio() {
  atualizarSaudacao();
  mostrarCorridaAberta();
  mostrar('tela-inicio');
}

$('btn-ir-emitir').addEventListener('click', () => { vibrar('LIGHT'); irParaEmitir(); });
// Uma corrida por vez: com uma aberta, "Fazer corrida" volta para ela.
$('btn-ir-corrida').addEventListener('click', () => { vibrar('LIGHT'); abrirCorrida(); });
$('btn-continuar-corrida').addEventListener('click', () => { vibrar('LIGHT'); abrirCorrida(); });

// ── Indicação ──────────────────────────────────────────────────────────────
//
// O codigo vem do servidor com a sessao e fica guardado: a tela abre sem
// sinal. A mensagem e a mesma do site, para o colega receber o mesmo texto
// venha de onde vier. Nada aqui fala de desconto no site (Apple 3.1.1).

function mensagemDeIndicacao() {
  const codigo = Guardado.get('codigo_indicacao') || '';
  const link = Guardado.get('link_indicacao') || `https://www.recibotaxi.com.br/cadastro?indicacao=${codigo}`;
  const nome = ((Guardado.get('motorista') || {}).full_name || '').split(' ')[0] || 'um colega';
  return 'Oi! Eu uso o Recibo Táxi para dar recibo de corrida pelo celular, sem internet, '
    + `em 10 segundos. Cria sua conta grátis com o meu código *${codigo}* que a gente ganha `
    + `um mês de Pro: ${link} — ${nome}`;
}

function abrirIndicacao() {
  const codigo = Guardado.get('codigo_indicacao');
  $('codigo-indicacao').textContent = codigo || '······';
  $('link-indicacao').textContent = codigo
    ? (Guardado.get('link_indicacao') || '')
    : 'Conecte à internet uma vez para o app buscar o seu código.';
  $('btn-indicar-whats').disabled = !codigo;
  $('btn-copiar-indicacao').disabled = !codigo;
  $('aviso-indicacao').hidden = true;
  fecharMenu();
  mostrar('tela-indicar');
  // Sessao de antes do codigo existir: busca agora e redesenha uma vez.
  if (!codigo) atualizarSessao().then((s) => { if (s && s.codigo_indicacao) abrirIndicacao(); });
}

// ── Desconto de indicacao nas lojas ────────────────────────────────────────
//
// Quem indicou um colega ativo ganha 70% por 12 meses no Pro mensal, dentro
// do app, pela loja. Na Apple e um codigo de oferta (INDICA70) resgatado na
// folha da App Store; no Google e uma oferta do plano base, que o app compra
// direto quando o servidor diz que o motorista tem direito.

const cupomDeIndicacao = () => {
  const c = Guardado.get('cupom_indicacao');
  return c && c.disponivel ? c : null;
};

function opcaoGoogleComDesconto(pacote) {
  const cupom = cupomDeIndicacao();
  if (!cupom || plataforma() !== 'android') return null;
  const opcoes = pacote?.product?.subscriptionOptions || [];
  return opcoes.find((o) => (o.tags || []).includes(cupom.tag_google)) || null;
}

function mostrarCupomDeIndicacao(jaAssina) {
  const cupom = cupomDeIndicacao();
  const bloco = $('cupom-pro');
  bloco.hidden = !cupom || jaAssina;
  if (bloco.hidden) return;
  const ios = plataforma() === 'ios';
  $('cupom-pro-texto').textContent = ios
    ? `Você indicou um colega: o Pro mensal sai por ${cupom.preco_apple}/mês durante ${cupom.meses} meses `
      + `(${cupom.pct}% de desconto). Resgate o código na App Store e depois toque em "Restaurar compra".`
    : `Você indicou um colega: ${cupom.pct}% de desconto por ${cupom.meses} meses no Pro mensal. `
      + 'O desconto entra sozinho ao assinar o plano mensal.';
  $('cupom-pro-codigo').textContent = ios ? cupom.codigo_apple : '';
  $('btn-resgatar-cupom').hidden = !ios;
}

$('btn-resgatar-cupom').addEventListener('click', async () => {
  const { Purchases } = P();
  const cupom = cupomDeIndicacao();
  if (!cupom) return;
  vibrar('LIGHT');
  try {
    if (Purchases?.presentCodeRedemptionSheet) await Purchases.presentCodeRedemptionSheet();
    else window.open(`https://apps.apple.com/redeem?ctx=offercodes&id=6811583712&code=${cupom.codigo_apple}`, '_blank');
  } catch (e) {
    mostrarErroDeCompra($('erro-pro'), e);
  }
});

$('btn-indicar').addEventListener('click', () => { vibrar('LIGHT'); abrirIndicacao(); });
$('btn-ir-indicar').addEventListener('click', () => { vibrar('LIGHT'); abrirIndicacao(); });
$('btn-fechar-indicar').addEventListener('click', voltarParaBase);
$('btn-indicar-whats').addEventListener('click', () => {
  vibrar('LIGHT');
  window.open(`https://wa.me/?text=${encodeURIComponent(mensagemDeIndicacao())}`, '_blank');
});
$('btn-copiar-indicacao').addEventListener('click', async () => {
  const aviso = $('aviso-indicacao');
  try {
    const { Clipboard } = P();
    if (Clipboard) await Clipboard.write({ string: mensagemDeIndicacao() });
    else await navigator.clipboard.writeText(mensagemDeIndicacao());
    aviso.textContent = 'Mensagem copiada. Cole no grupo dos taxistas ou numa conversa.';
  } catch {
    aviso.textContent = 'Não deu para copiar. Use o botão do WhatsApp.';
  }
  aviso.hidden = false;
});

// ── Corrida ────────────────────────────────────────────────────────────────
//
// O app nao mede a corrida pelo GPS no caminho: quem mede e cobra e o
// taximetro. Ele guarda de onde e para onde, a hora de saida e, se o motorista
// pediu, a rota que o Google estimou. No fim, leva tudo para o recibo.
//
// Fica no localStorage, e nao so na memoria: o motorista abre o Waze, o
// sistema pode fechar o app no meio do caminho, e a corrida nao pode sumir.
// Leva o id do motorista: em aparelho dividido, quem entra depois nao herda a
// corrida de quem saiu.
// O dono e o id do motorista; antes da primeira /api/sessao, o e-mail serve.
const donoAtual = () => Guardado.get('motorista_id') || (Guardado.get('motorista') || {}).email || '';

function corridaAtual() {
  const c = Guardado.get('corrida');
  const eu = donoAtual();
  if (c && c.motorista && eu && c.motorista !== eu) { Guardado.del('corrida'); return null; }
  return c;
}
const guardarCorrida = (c) => Guardado.set('corrida', c);

// O rascunho (campos, posicao, estimativa) vive na memoria enquanto a corrida
// nao comecou. Apagar a corrida apaga o rascunho junto: a proxima nasce vazia.
function limparRascunhoDaCorrida() {
  $('corrida-origem').value = '';
  $('corrida-destino').value = '';
  posicao = null; cidadeAtual = ''; origemDoGps = false; rotaEstimada = null;
}
const apagarCorrida = () => { Guardado.del('corrida'); limparRascunhoDaCorrida(); };

let posicao = null;        // {lat, lng} do GPS, para a estimativa
let cidadeAtual = '';      // onde o aparelho esta: ajuda o Google a achar o destino certo
let origemDoGps = false;   // a origem na tela veio do GPS e ninguem mexeu nela
let rotaEstimada = null;   // {km, minutos, minutos_sem_transito, chave}
let bandeira = Guardado.get('bandeira') === 2 ? 2 : 1;

async function abrirCorrida() {
  const c = corridaAtual();
  // Com corrida guardada, a tela vem dela. Sem, o rascunho em memoria fica
  // como estava: tocar em "Voltar" sem querer e entrar de novo zerava destino,
  // origem e uma estimativa ja consultada (e contada no teto do dia).
  if (c) {
    $('corrida-origem').value = c.origem || '';
    $('corrida-destino').value = c.destino || '';
    posicao = c.posicao || null;
    cidadeAtual = c.cidade || '';
    origemDoGps = !!c.origemDoGps;
    rotaEstimada = c.estimativa || null;
    if (c.bandeira) bandeira = c.bandeira;
  }
  ['erro-corrida', 'aviso-estimativa', 'dica-origem'].forEach((id) => { $(id).hidden = true; });
  desenharCorrida();
  mostrar('tela-corrida');
  // Corrida nova, sem rascunho: o passageiro costuma embarcar onde o motorista
  // esta. Vem antes do historico, e sem depender dele: se o banco local
  // falhar, a origem continua chegando pelo GPS.
  if (!c && !posicao && !$('corrida-origem').value.trim()) localizar();
  await atualizarSugestoesDeLocal().catch(() => {});
}

function desenharCorrida() {
  const c = corridaAtual();
  const andando = !!c && !c.fim;
  const encerrada = !!c && !!c.fim;
  $('titulo-corrida').textContent = encerrada ? 'Corrida encerrada' : c ? 'Corrida' : 'Nova corrida';
  $('corrida-dados').hidden = encerrada;
  $('corrida-fim').hidden = !encerrada;
  $('corrida-andamento').hidden = !andando;
  if (andando) $('corrida-andamento').textContent = `Em andamento desde as ${horaDe(c.inicio)}.`;
  $('btn-encerrar').hidden = !andando;
  $('btn-comecar').hidden = !!c;
  // Antes de sair, abrir o mapa e o passo principal; depois, e encerrar.
  $('btn-waze').className = `${andando ? 'secundario' : 'primario'} como-botao`;
  if (encerrada) $('fim-resumo').textContent = resumoDaCorrida(c);
  atualizarLinksDoMapa();
  desenharEstimativa();
}

function resumoDaCorrida(c) {
  const minutos = Math.max(1, Math.round((new Date(c.fim) - new Date(c.inicio)) / 60000));
  return `${c.destino} · das ${horaDe(c.inicio)} às ${horaDe(c.fim)} (${minutos} min)`;
}

// O proprio Waze procura o endereco perto de onde o motorista esta, entao vai
// so o texto digitado. Sem destino o link nao tem href: o toque explica o que
// falta, em vez de abrir o mapa vazio.
function atualizarLinksDoMapa() {
  const destino = $('corrida-destino').value.trim();
  const q = encodeURIComponent(destino);
  const links = {
    'btn-waze': `https://waze.com/ul?q=${q}&navigate=yes`,
    'btn-maps': `https://www.google.com/maps/dir/?api=1&destination=${q}&travelmode=driving&dir_action=navigate`,
  };
  for (const [id, url] of Object.entries(links)) {
    if (destino) $(id).href = url; else $(id).removeAttribute('href');
    $(id).classList.toggle('desligado', !destino);
  }
}

function erroNaCorrida(texto) {
  $('erro-corrida').textContent = texto;
  $('erro-corrida').hidden = !texto;
}

// Sincrono de proposito: o toque no Waze tira o app da tela logo em seguida,
// e a corrida tem de estar gravada antes disso.
function comecarCorrida() {
  guardarCorrida({
    motorista: donoAtual(),
    destino: $('corrida-destino').value.trim(),
    origem: $('corrida-origem').value.trim(),
    origemDoGps,
    posicao,
    cidade: cidadeAtual,
    inicio: new Date().toISOString(),
    fim: null,
    estimativa: estimativaValida() ? rotaEstimada : null,
    bandeira,
  });
  vibrar('HEAVY');
  desenharCorrida();
}

function destinoParaSair(acao) {
  if ($('corrida-destino').value.trim()) { erroNaCorrida(''); return true; }
  erroNaCorrida(`Digite o destino para ${acao}.`);
  $('corrida-destino').focus();
  return false;
}

// O <a> segue sozinho para o Waze ou o Maps depois deste ouvinte; o que ele
// faz e so registrar a saida, na primeira vez.
function aoAbrirMapa(ev) {
  if (!destinoParaSair('abrir o mapa')) { ev.preventDefault(); return; }
  if (!corridaAtual()) comecarCorrida();
}
$('btn-waze').addEventListener('click', aoAbrirMapa);
$('btn-maps').addEventListener('click', aoAbrirMapa);
$('btn-comecar').addEventListener('click', () => {
  if (destinoParaSair('começar')) comecarCorrida();
});

// Com a corrida andando, o que muda na tela vai para a corrida guardada.
function aoEditarCorrida() {
  const c = corridaAtual();
  if (c && !c.fim) {
    Object.assign(c, {
      destino: $('corrida-destino').value.trim(),
      origem: $('corrida-origem').value.trim(),
      origemDoGps, posicao, cidade: cidadeAtual,
      estimativa: estimativaValida() ? rotaEstimada : null,
    });
    guardarCorrida(c);
  }
  atualizarLinksDoMapa();
  desenharEstimativa();
}
$('corrida-destino').addEventListener('input', () => { erroNaCorrida(''); aoEditarCorrida(); });
$('corrida-origem').addEventListener('input', () => {
  origemDoGps = false;              // o motorista corrigiu: vale o que ele digitou
  $('dica-origem').hidden = true;
  aoEditarCorrida();
});

$('btn-encerrar').addEventListener('click', () => {
  const c = corridaAtual();
  if (!c) return;
  if (!c.destino) { erroNaCorrida('Digite o destino antes de encerrar.'); return; }
  c.fim = new Date().toISOString();
  guardarCorrida(c);
  vibrar('HEAVY');
  desenharCorrida();
  window.scrollTo(0, 0);
});

// Encerrou sem querer: a corrida volta a andar, com a mesma hora de saida.
$('btn-retomar').addEventListener('click', () => {
  const c = corridaAtual();
  if (c) { c.fim = null; guardarCorrida(c); }
  abrirCorrida();
});

$('btn-sem-recibo').addEventListener('click', () => {
  apagarCorrida();
  vibrar('LIGHT');
  irParaInicio();
});

$('btn-gerar-recibo').addEventListener('click', () => {
  const c = corridaAtual();
  if (!c) { irParaInicio(); return; }
  vibrar('LIGHT');
  irParaEmitir({ daCorrida: c });
});

$('btn-corrida-voltar').addEventListener('click', irParaInicio);

// ── Localização ────────────────────────────────────────────────────────────
//
// So com o app aberto, e so quando o motorista abre a corrida ou toca no alvo:
// nada em segundo plano. A posicao serve para a origem do recibo e para a
// estimativa, e nao e guardada em lugar nenhum fora do aparelho.

// Rua e numero primeiro: e o que a empresa do passageiro espera ler no recibo.
function enderecoLegivel(a) {
  const rua = [a.thoroughfare, a.subThoroughfare].filter(Boolean).join(', ');
  const bairro = a.subLocality || '';
  const texto = rua
    ? (bairro ? `${rua} - ${bairro}` : rua)
    : ((a.areasOfInterest || [])[0] || bairro || a.locality || '');
  return texto.slice(0, 200);
}

let localizando = null;

// Nunca trava a tela: sem permissao, sem sinal de GPS ou no navegador, a
// origem fica para digitar. `forcar` e o toque no alvo: troca ate o que o
// motorista digitou, e explica quando nao deu.
function localizar({ forcar = false } = {}) {
  if (!localizando) {
    localizando = buscarPosicao(forcar).finally(() => {
      localizando = null;
      $('btn-localizar').disabled = false;
    });
  }
  return localizando;
}

async function buscarPosicao(forcar) {
  const { Geolocation, NativeGeocoder } = P();
  if (!Geolocation) return;
  const avisar = (texto) => {
    if (!forcar) return;
    $('dica-origem').textContent = texto;
    $('dica-origem').hidden = false;
  };
  $('btn-localizar').disabled = true;
  try {
    let perm = await Geolocation.checkPermissions();
    if (['prompt', 'prompt-with-rationale'].includes(perm.location)) {
      perm = await Geolocation.requestPermissions({ permissions: ['location'] });
    }
    // Aproximada tambem serve: a origem sai menos precisa, e ele confere.
    if (perm.location !== 'granted' && perm.coarseLocation !== 'granted') {
      avisar('Sem permissão de localização. Libere nos Ajustes do aparelho ou digite a origem.');
      return;
    }
    const pos = await Geolocation.getCurrentPosition({
      enableHighAccuracy: true, timeout: 10000, maximumAge: 30000,
    });
    posicao = { lat: pos.coords.latitude, lng: pos.coords.longitude };
    $('dica-origem').hidden = true;
  } catch {
    avisar('Não consegui sua localização agora. Digite a origem.');
    return;
  }

  // Daqui em diante a posicao ja existe e serve para a estimativa mesmo que
  // o nome da rua nao venha: no iPhone o geocodificador precisa de internet,
  // e a falha dele era tratada como falha do GPS — a posicao ficava fora da
  // corrida guardada e a mensagem dizia que nao havia localizacao.
  let a = null;
  if (NativeGeocoder) {
    try {
      const { addresses } = await NativeGeocoder.reverseGeocode({
        latitude: posicao.lat, longitude: posicao.lng, useLocale: true, maxResults: 1,
      });
      a = (addresses || [])[0] || null;
    } catch { /* sem nome de rua; a posicao basta */ }
  }
  if (a) {
    cidadeAtual = a.locality || a.subAdministrativeArea || '';
    const texto = enderecoLegivel(a);
    const campo = $('corrida-origem');
    // Nao passa por cima do que o motorista digitou, a nao ser que ele peca.
    if (texto && (forcar || !campo.value.trim() || origemDoGps)) {
      campo.value = texto;
      origemDoGps = true;
    }
  } else {
    avisar('Peguei sua posição, mas não o nome da rua. Digite a origem, se quiser que ela saia no recibo.');
  }
  aoEditarCorrida();
}

$('btn-localizar').addEventListener('click', () => { vibrar('LIGHT'); localizar({ forcar: true }); });

// ── Estimativa ─────────────────────────────────────────────────────────────
//
// O servidor devolve so a rota (km e minutos, com e sem transito). A faixa de
// preco e calculada aqui, com a tarifa que o motorista digitou: cada cidade
// tem a sua, e nenhuma tabela nossa ficaria em dia com todas.
const tarifa = () => Guardado.get('tarifa');

// A posicao do GPS enquanto a origem na tela for a que veio dele; senao, o
// que o motorista digitou.
function origemParaEstimar() {
  const texto = $('corrida-origem').value.trim();
  if (posicao && (origemDoGps || !texto)) return { lat: posicao.lat, lng: posicao.lng };
  return texto.length >= 3 ? { origem: texto } : null;
}

// Distancia em linha reta, em km (haversine). Serve so para saber se o GPS
// ainda aponta para o mesmo lugar.
function distanciaKm(a, b) {
  const rad = (g) => (g * Math.PI) / 180;
  const dLat = rad(b.lat - a.lat), dLng = rad(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(h));
}

// A estimativa vale enquanto for da mesma rota. Trocar o destino invalida
// sempre. A origem depende de como foi consultada: pelo GPS, vale enquanto o
// aparelho estiver no mesmo lugar (ate 150 m), mesmo que o motorista digite o
// nome da rua depois — ele esta so nomeando onde esta, e o recibo precisa do
// texto; por texto, vale enquanto o texto for o mesmo. Antes, qualquer mexida
// na origem descartava uma consulta certa, e contada no teto do dia.
function estimativaValida() {
  if (!rotaEstimada || rotaEstimada.destino !== $('corrida-destino').value.trim()) return false;
  if (rotaEstimada.posicao) return !!posicao && distanciaKm(posicao, rotaEstimada.posicao) < 0.15;
  return !origemDoGps && rotaEstimada.origem === $('corrida-origem').value.trim();
}

// Embaixo, a bandeirada e o km pela rota do Google. Em cima, o tempo que o
// transito acrescenta, cobrado como hora parada — abaixo de certa velocidade
// o taximetro cobra tempo em vez de km —, e 10% de folga, porque o caminho
// feito raramente e o que o Google sugeriu.
function faixaDoValor(rota, t, b) {
  const porKm = b === 2 ? t.km2 : t.km1;
  const base = t.bandeirada + rota.km * porKm;
  const parado = (Math.max(0, rota.minutos - rota.minutos_sem_transito) / 60) * t.hora;
  return { de: Math.floor(base), ate: Math.ceil((base + parado) * 1.1) };
}

const faixaDaCorrida = (c) => (c && c.estimativa && tarifa()
  ? faixaDoValor(c.estimativa, tarifa(), c.bandeira || 1) : null);

const emReaisInteiros = (n) => `${moedaAtual().simbolo} ${n.toLocaleString('pt-BR')}`;
const textoDaFaixa = (f) => (f.de >= f.ate
  ? emReaisInteiros(f.ate) : `${emReaisInteiros(f.de)} a ${emReaisInteiros(f.ate)}`);
const kmBR = (km) => km.toLocaleString('pt-BR', { maximumFractionDigits: 1 });

function desenharEstimativa() {
  [1, 2].forEach((b) => $(`bandeira-${b}`).setAttribute('aria-checked', String(b === bandeira)));
  const t = tarifa();
  $('btn-editar-tarifa').hidden = !t;
  const mostrarFaixa = !!t && estimativaValida();
  $('resultado-estimativa').hidden = !mostrarFaixa;
  if (!mostrarFaixa) return;
  $('estimativa-faixa').textContent = textoDaFaixa(faixaDoValor(rotaEstimada, t, bandeira));
  $('estimativa-rota').textContent =
    `${kmBR(rotaEstimada.km)} km · cerca de ${rotaEstimada.minutos} min com trânsito · bandeira ${bandeira}`;
}

function escolherBandeira(b) {
  bandeira = b;
  Guardado.set('bandeira', b);
  const c = corridaAtual();
  if (c) { c.bandeira = b; guardarCorrida(c); }
  vibrar('LIGHT');
  desenharEstimativa();
}
$('bandeira-1').addEventListener('click', () => escolherBandeira(1));
$('bandeira-2').addEventListener('click', () => escolherBandeira(2));

const MOTIVOS_DA_ESTIMATIVA = {
  destino_invalido: 'Digite o destino para estimar.',
  origem_invalida: 'Digite a origem, ou toque no alvo ao lado dela para usar sua localização.',
  destino_nao_encontrado:
    'Não encontrei esse destino. Tente com rua e número, ou o nome de um lugar conhecido.',
  limite_diario: 'Você chegou ao limite de estimativas de hoje.',
  indisponivel_hoje: 'A estimativa atingiu o limite de hoje para todos os motoristas. Volta amanhã.',
  falha_na_consulta: 'O serviço de rotas não respondeu. Tente de novo em instantes.',
  indisponivel: 'A estimativa ainda não está disponível.',
  nao_autenticado: 'Sua sessão expirou. Entre de novo.',
};

function avisoDaEstimativa(texto) {
  $('aviso-estimativa').textContent = texto;
  $('aviso-estimativa').hidden = !texto;
}

let estimando = false;   // um pedido por vez: toque repetido nao gera outra consulta

async function estimar() {
  if (estimando) return;
  avisoDaEstimativa('');
  const destino = $('corrida-destino').value.trim();
  if (destino.length < 3) {
    avisoDaEstimativa(MOTIVOS_DA_ESTIMATIVA.destino_invalido);
    $('corrida-destino').focus();
    return;
  }
  // A tarifa vem antes de gastar uma consulta: sem ela nao ha faixa a mostrar.
  if (!tarifa()) { abrirTarifa({ depoisEstimar: true }); return; }

  // O botao trava ANTES de esperar o GPS: a espera leva ate 10 s, e cada toque
  // nela era mais um pedido ao Google, contado no teto e na conta.
  const botao = $('btn-estimar');
  estimando = true;
  botao.disabled = true;
  botao.textContent = 'Estimando…';
  try {
    if (!origemParaEstimar() && localizando) {
      // Com teto: uma promessa de permissao que nunca resolve nao pode
      // prender o botao para sempre.
      await Promise.race([localizando, new Promise((ok) => setTimeout(ok, 12000))]);
    }
    const origem = origemParaEstimar();
    if (!origem) { avisoDaEstimativa(MOTIVOS_DA_ESTIMATIVA.origem_invalida); return; }

    const resp = await api('/api/estimativa', {
      method: 'POST',
      body: JSON.stringify({ ...origem, destino, cidade: cidadeAtual }),
    });
    const corpo = await resp.json().catch(() => ({}));
    if (resp.status === 401) {
      // O api() ja tentou renovar: sessao morta de verdade. Sair aqui leva ao
      // login; a corrida aberta sobrevive e volta quando ele entrar.
      encerrarSessao();
      return;
    }
    if (!resp.ok || typeof corpo.km !== 'number') {
      avisoDaEstimativa(MOTIVOS_DA_ESTIMATIVA[corpo.erro] || 'Não foi possível estimar agora.');
      return;
    }
    rotaEstimada = {
      km: corpo.km,
      minutos: corpo.minutos,
      minutos_sem_transito: corpo.minutos_sem_transito,
      destino,
      // Como a origem foi consultada: e isso que decide ate quando vale.
      ...('lat' in origem ? { posicao: { lat: origem.lat, lng: origem.lng } } : { origem: origem.origem }),
    };
    aoEditarCorrida();      // guarda na corrida aberta e desenha a faixa
    vibrar('LIGHT');
  } catch {
    avisoDaEstimativa('Sem conexão. A estimativa precisa de internet; a corrida e o recibo funcionam sem.');
  } finally {
    estimando = false;
    botao.disabled = false;
    botao.textContent = 'Estimar valor';
  }
}
$('btn-estimar').addEventListener('click', estimar);

// ── Tarifa da cidade ───────────────────────────────────────────────────────
let estimarDepoisDaTarifa = false;

// Reais digitados do jeito brasileiro: "4,50", "R$ 4,50" ou "4.50".
function numeroDe(texto) {
  // Espaco so nas pontas e depois do "R$": "4 50" nao e 450, e recusado.
  let s = String(texto || '').trim().replace(/^R\$\s*/i, '');
  if (/\s/.test(s)) return null;
  if (s.includes(',')) s = s.replace(/\./g, '').replace(',', '.');
  if (!/^\d+(\.\d+)?$/.test(s)) return null;
  const n = Number(s);
  return Number.isFinite(n) && n < 10000 ? n : null;
}
const numeroBR = (n) => (n == null ? '' : n.toFixed(2).replace('.', ','));

function abrirTarifa({ depoisEstimar = false } = {}) {
  estimarDepoisDaTarifa = depoisEstimar;
  const t = tarifa() || {};
  $('t-bandeirada').value = numeroBR(t.bandeirada);
  $('t-hora').value = numeroBR(t.hora);
  $('t-km1').value = numeroBR(t.km1);
  $('t-km2').value = numeroBR(t.km2);
  $('erro-tarifa').hidden = true;
  mostrar('tela-tarifa');
}

$('btn-tarifa').addEventListener('click', () => abrirTarifa());
$('btn-editar-tarifa').addEventListener('click', () => abrirTarifa());
$('btn-fechar-tarifa').addEventListener('click', () => {
  estimarDepoisDaTarifa = false;
  voltarParaBase();
});

$('form-tarifa').addEventListener('submit', (ev) => {
  ev.preventDefault();
  const t = {
    bandeirada: numeroDe($('t-bandeirada').value),
    km1: numeroDe($('t-km1').value),
    km2: numeroDe($('t-km2').value),
    hora: numeroDe($('t-hora').value),
  };
  if (Object.values(t).some((v) => v == null) || !t.km1 || !t.km2) {
    $('erro-tarifa').textContent =
      'Preencha os quatro valores em reais, com vírgula nos centavos. O km não pode ser zero.';
    $('erro-tarifa').hidden = false;
    return;
  }
  Guardado.set('tarifa', t);
  vibrar('LIGHT');
  const estimarAgora = estimarDepoisDaTarifa;
  estimarDepoisDaTarifa = false;
  voltarParaBase();
  // Veio do botao de estimar: segue para a estimativa, sem pedir outro toque.
  if (estimarAgora && !$('tela-corrida').hidden) estimar();
});

// ── Início ─────────────────────────────────────────────────────────────────
// toISOString devolve UTC; para a hora do relógio do motorista, monta local.
function agoraLocal(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return {
    data: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`,
    hora: `${p(d.getHours())}:${p(d.getMinutes())}`,
  };
}

function preencherDataEHora() {
  const agora = agoraLocal();
  $('data').value = agora.data;
  $('hora').value = agora.hora;
}

async function iniciar() {
  preencherDataEHora();
  atualizarCabecalho();
  aplicarMoedaNaTela();   // com o que ficou guardado; a sessao atualiza se houver rede
  if (!token()) { mostrar('tela-login'); return; }
  irParaInicio();
  await atualizarSessao();
  // O nome da saudacao e o dono da corrida aberta chegam com a sessao.
  if (!$('tela-inicio').hidden) irParaInicio();
  await iniciarCompras();
  await atualizarSugestoesDeLocal();
  await atualizarAvisoFila();
  sincronizar().then(atualizarAvisoFila);
}

iniciar();
