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
        abrirAssinatura(
          `Você usou os ${limite ?? ''} recibos deste mês do plano Grátis. ` +
          'O Pro libera recibos ilimitados.');
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
const telas = ['tela-login', 'tela-cadastro', 'tela-emitir', 'tela-recibo',
               'tela-assinatura', 'tela-historico', 'tela-excluir'];
function mostrar(qual) {
  telas.forEach((t) => { $(t).hidden = t !== qual; });
  window.scrollTo(0, 0);
}

// O wa.me exige o numero com codigo do pais. Sem isso o WhatsApp abre sem
// destinatario e o motorista so consegue enviar se ja tiver o passageiro
// salvo na agenda — o problema que este botao existe para resolver.
//
// A classificacao e por tamanho, nao por prefixo: o DDD 55 existe (Santa
// Maria/RS), entao "5598765432", de 10 digitos, e DDD + fixo e vira
// "555598765432", nao um numero ja internacional.
function foneE164(valor, ddi = '55') {
  let d = String(valor || '').replace(/\D/g, '');
  if (d.startsWith('00')) d = d.slice(2);
  d = d.replace(/^0+/, '');
  if (d.length === 10 || d.length === 11) return ddi + d;
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
function documentoBR(valor) {
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
  return String(documento || '').replace(/\D/g, '').length === 14;
}

function atualizarCampoRazaoSocial() {
  const mostrar = ehCNPJ($('documento').value);
  $('campo-razao-social').hidden = !mostrar;
  if (!mostrar) $('razao-social').value = '';
}

$('documento').addEventListener('input', atualizarCampoRazaoSocial);

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
    <div class="recibo-valor">R$ ${d.valor_exibido}</div>
    <dl class="recibo-dados">
      <dt>Passageiro</dt><dd>${d.passageiro}</dd>
      ${d.documento_passageiro ? `<dt>CPF/CNPJ</dt><dd>${d.documento_passageiro}</dd>` : ''}
      ${d.razao_social ? `<dt>Razão social</dt><dd>${d.razao_social}</dd>` : ''}
      <dt>Data</dt><dd>${formatarBR(d.data)}${d.hora ? ' às ' + d.hora : ''}</dd>
      <dt>Origem</dt><dd>${d.origem}</dd>
      <dt>Destino</dt><dd>${d.destino}</dd>
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
  $('recibo').innerHTML = montarRecibo(recibo);
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
    if (atual) mostrarRecibo(atual);
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

function textoParaCompartilhar(recibo) {
  const d = recibo.dados;
  const linhas = [
    `✅ Recibo #${recibo.rid}`,
    `👤 Passageiro: ${d.passageiro}`,
    ...(d.documento_passageiro ? [`🧾 CPF/CNPJ: ${d.documento_passageiro}`] : []),
    ...(d.razao_social ? [`🏢 Razão social: ${d.razao_social}`] : []),
    `📅 Data: ${formatarBR(d.data)}`,
    `📍 Origem: ${d.origem}`,
    `🏁 Destino: ${d.destino}`,
    `💰 Valor: R$ ${d.valor_exibido}`,
    `💳 Pagamento: ${d.forma_pagamento}`,
  ];
  linhas.push('', '🔗 Recibo completo, para ver, imprimir ou salvar em PDF:',
              linkDoRecibo(recibo));
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

// Tocar na marca volta para o formulário — o caminho de volta que faltava
// depois de olhar o histórico ou a tela de assinatura.
$('btn-inicio').addEventListener('click', async () => {
  fecharMenu();
  if (!token() || !$('tela-emitir').hidden) return;
  // Sem renovar data e hora: o motorista pode ter posto a data de ontem e ido
  // olhar o historico. Voltar pela marca nao e comecar um recibo novo.
  await irParaEmitir({ comHoraDeAgora: false });
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
    await iniciar();
  } catch {
    erro.textContent = 'Sem conexão. Tente de novo quando tiver sinal.';
    erro.hidden = false;
  }
});

$('btn-ir-cadastro').addEventListener('click', () => mostrar('tela-cadastro'));
$('btn-ir-login').addEventListener('click', () => mostrar('tela-login'));

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
  };

  const mensagens = {
    email_em_uso: 'Já existe uma conta com este e-mail.',
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
    vibrar('HEAVY');
    await iniciar();
  } catch {
    // Cadastro é a única coisa que exige rede: sem conta não há o que emitir.
    erro.textContent = 'Sem conexão. O cadastro precisa de internet — depois o app funciona offline.';
    erro.hidden = false;
  }
});

$('form-recibo').addEventListener('submit', async (ev) => {
  ev.preventDefault();
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
      origem: $('origem').value.trim(),
      destino: $('destino').value.trim(),
      valor,
      valor_exibido: valor.replace('.', ','),
      data_exibida: formatarBR($('data').value),
      observacoes: '',
      forma_pagamento: $('forma').value,
      whatsapp_passageiro: $('whats').value.trim(),
      email_passageiro: emailLimpo($('email-passageiro').value),
      documento_passageiro: documentoBR($('documento').value),
      razao_social: ehCNPJ($('documento').value)
        ? $('razao-social').value.trim().replace(/\s+/g, ' ') : '',
    },
  };

  await salvarRecibo(recibo);        // primeiro guarda, depois tenta enviar
  vibrar('HEAVY');
  $('form-recibo').reset();
  atualizarCampoRazaoSocial();   // o reset limpa o valor, nao o que eu escondi
  revisarWhats(); revisarEmail();
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
        data: pdfDoRecibo(recibo),
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
// A Apple exige (Guideline 3.1.1) que a compra passe pelo IAP. Não há, e não
// pode haver, link para assinar no site dentro do app iOS.

async function iniciarCompras() {
  const { Purchases } = P();
  const chave = window.RC_CHAVE_PUBLICA;
  if (!Purchases || !chave) return false;
  try {
    await Purchases.configure({ apiKey: chave });
    // Amarra a compra ao motorista: sem isto o webhook não sabe quem assinou.
    const perfil = Guardado.get('motorista_id');
    if (perfil) await Purchases.logIn({ appUserID: perfil });
    return true;
  } catch (e) {
    return false;
  }
}

function abrirAssinatura(motivo) {
  $('motivo-pro').textContent = motivo || '';
  $('erro-pro').hidden = true;
  mostrar('tela-assinatura');
}

// A tela de assinatura tambem abre sozinha no 402. Este botao existe para o
// caso em que ninguem bateu no limite ainda: sem ele a analise da Apple nao
// tem como chegar na compra, e assinante nenhum tem como restaurar.
$('btn-pro').addEventListener('click', () => abrirAssinatura(''));

$('btn-fechar-pro').addEventListener('click', () => mostrar('tela-emitir'));

// "Nao foi possivel concluir a compra" era tudo que aparecia, para qualquer
// causa: produto que a loja nao devolveu, contrato pendente, aparelho sem conta
// de teste, rede caida. Sem o motivo na tela, o motorista repete o toque e eu
// fico adivinhando. Agora o app diz o que deu, e guarda o codigo tecnico junto
// — feio, mas e o que permite consertar em vez de chutar.
const MOTIVOS_DE_COMPRA = {
  PRODUCT_NOT_AVAILABLE_FOR_PURCHASE:
    'A App Store não está oferecendo a assinatura ainda. Isso costuma ser o '
    + 'contrato de apps pagos que ainda não entrou em vigor.',
  CONFIGURATION_ERROR:
    'A assinatura não chegou da App Store. Confira se o produto está pronto e '
    + 'se o contrato de apps pagos está ativo.',
  OFFLINE_CONNECTION_ERROR: 'Sem conexão. Tente com sinal melhor.',
  NETWORK_ERROR: 'A loja não respondeu. Tente de novo em instantes.',
  STORE_PROBLEM_ERROR: 'A App Store está com problema agora. Tente mais tarde.',
  PURCHASE_NOT_ALLOWED_ERROR:
    'Este aparelho não permite compras. Veja Tempo de Uso e restrições.',
  PAYMENT_PENDING_ERROR: 'A compra ficou pendente de aprovação. Aguarde a confirmação.',
  RECEIPT_ALREADY_IN_USE_ERROR: 'Esta compra já está em outra conta.',
  INVALID_APP_USER_ID: 'Sessão inconsistente. Saia e entre de novo.',
};

function mostrarErroDeCompra(erro, e) {
  const codigo = e?.code || e?.errorCode || '';
  const base = MOTIVOS_DE_COMPRA[codigo]
    || (String(e?.message || '').includes('sem oferta')
        ? 'A App Store não devolveu nenhuma assinatura para vender. '
          + 'O produto precisa estar pronto e o contrato de apps pagos ativo.'
        : 'Não foi possível concluir a compra.');
  erro.textContent = `${base}${codigo ? ` (${codigo})` : ''}`;
  erro.hidden = false;
}

$('btn-assinar').addEventListener('click', async () => {
  const erro = $('erro-pro');
  erro.hidden = true;
  const { Purchases } = P();
  if (!Purchases || !window.RC_CHAVE_PUBLICA) {
    erro.textContent = 'Assinatura ainda não disponível nesta versão.';
    erro.hidden = false;
    return;
  }
  try {
    const ofertas = await Purchases.getOfferings();
    const pacotes = ofertas?.current?.availablePackages || [];
    // Pelo identificador, nao pela posicao: se alguem acrescentar um pacote
    // no painel do RevenueCat, o [0] viraria sorteio e o motorista poderia
    // acabar comprando outra coisa.
    const pacote = pacotes.find((p) => p.identifier === '$rc_monthly') || pacotes[0];
    if (!pacote) throw new Error('sem oferta configurada');
    const r = await Purchases.purchasePackage({ aPackage: pacote });
    const virou = !!r?.customerInfo?.entitlements?.active?.pro;
    if (virou) {
      vibrar('HEAVY');
      await atualizarSessao();
      sincronizar().then(atualizarAvisoFila);   // o que a cota segurava sobe agora
      mostrar('tela-emitir');
    }
  } catch (e) {
    // Cancelar não é erro: o motorista fechou a folha de pagamento.
    if (e?.code === 'PURCHASE_CANCELLED' || /cancel/i.test(e?.message || '')) return;
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
      mostrar('tela-emitir');
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
    atualizarCabecalho();
    await reabrirRecusadosPorCota(s);
    return s;
  } catch { return null; }
}

async function irParaEmitir({ comHoraDeAgora = true } = {}) {
  if (comHoraDeAgora) preencherDataEHora();
  await atualizarSugestoesDeLocal();   // aprende o local que acabou de ser usado
  await atualizarAvisoFila();
  mostrar('tela-emitir');
}
// Sem passar o ouvinte direto: ele receberia o evento do clique como opcoes.
$('btn-novo').addEventListener('click', () => irParaEmitir());
$('btn-voltar').addEventListener('click', () => mostrar('tela-emitir'));

// As duas lojas exigem que quem cria conta dentro do app consiga apagar dentro
// do app: Apple na 5.1.1(v), Google na politica de exclusao de dados. Mandar
// para o site nao cumpre.
$('btn-excluir-conta').addEventListener('click', () => {
  $('form-excluir').reset();
  $('erro-excluir').hidden = true;
  mostrar('tela-excluir');
});
$('btn-fechar-excluir').addEventListener('click', () => mostrar('tela-emitir'));

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

async function mostrarHistorico() {
  const lista = await listarRecibos();
  $('lista-historico').innerHTML = lista.length
    ? lista.map((r) => `
        <div class="item" role="button" data-rid="${r.rid}">
          <div>
            <strong>${r.dados.passageiro}</strong>
            <small>${formatarBR(r.dados.data)} · ${r.dados.origem} → ${r.dados.destino}</small>
          </div>
          <div class="item-direita">
            <span class="item-valor">R$ ${r.dados.valor_exibido}</span>
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

window.addEventListener('online', async () => { await sincronizar(); atualizarAvisoFila(); });
window.addEventListener('offline', atualizarAvisoFila);

// No aparelho o evento do plugin é mais confiável que o do navegador.
if (P().Network) {
  P().Network.addListener('networkStatusChange', async (st) => {
    if (st.connected) await sincronizar();
    atualizarAvisoFila();
  });
}

// ── Início ─────────────────────────────────────────────────────────────────
function agoraLocal() {
  // toISOString devolve UTC; para a hora do relógio do motorista, monta local.
  const d = new Date();
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
  if (!token()) { mostrar('tela-login'); return; }
  mostrar('tela-emitir');
  await atualizarSessao();
  await iniciarCompras();
  await atualizarSugestoesDeLocal();
  await atualizarAvisoFila();
  sincronizar().then(atualizarAvisoFila);
}

iniciar();
