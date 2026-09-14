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

async function api(caminho, opcoes = {}) {
  const resp = await fetch(API + caminho, {
    ...opcoes,
    headers: {
      'Content-Type': 'application/json',
      ...(token() ? { Authorization: `Bearer ${token()}` } : {}),
      ...(opcoes.headers || {}),
    },
  });
  return resp;
}

async function sincronizar() {
  if (!(await temRede()) || !token()) {
    return { enviados: 0, restantes: (await pendentes()).length };
  }

  let enviados = 0;
  for (const recibo of await pendentes()) {
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
        break; // sessão expirou; o próximo login reenvia
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
               'tela-assinatura', 'tela-historico'];
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

function montarRecibo(recibo) {
  const d = recibo.dados, m = recibo.motorista || {};
  return `
    <div class="recibo-topo">
      <span class="recibo-rotulo">Recibo</span>
      <span class="recibo-id">#${recibo.rid}</span>
    </div>
    <div class="recibo-valor">R$ ${d.valor_exibido}</div>
    <dl class="recibo-dados">
      <dt>Passageiro</dt><dd>${d.passageiro}</dd>
      <dt>Data</dt><dd>${formatarBR(d.data)}${d.hora ? ' às ' + d.hora : ''}</dd>
      <dt>Origem</dt><dd>${d.origem}</dd>
      <dt>Destino</dt><dd>${d.destino}</dd>
      <dt>Pagamento</dt><dd>${d.forma_pagamento}</dd>
      <dt>Motorista</dt><dd>${m.full_name || ''}</dd>
      <dt>Placa</dt><dd>${m.plate || ''}</dd>
      ${m.whatsapp ? `<dt>WhatsApp</dt><dd>${m.whatsapp}</dd>` : ''}
    </dl>
    ${chamadaDoMotorista(m) ? `<p class="recibo-chamada">${chamadaDoMotorista(m)}</p>` : ''}
    <p class="recibo-estado">
      ${recibo.pendente ? '⏳ Aguardando sinal para sincronizar' : '✓ Sincronizado'}
    </p>`;
}

// Desenha o recibo e prepara o link do WhatsApp a partir do MESMO objeto.
// Eram duas coisas separadas e elas saiam de sincronia: o corpo era redesenhado
// depois do envio, o link nao, e o passageiro recebia mensagem sem o endereco.
function mostrarRecibo(recibo) {
  $('recibo').innerHTML = montarRecibo(recibo);
  const fone = foneE164(recibo.dados.whatsapp_passageiro);
  $('btn-whats').hidden = !fone;
  $('btn-compartilhar').className = fone ? 'secundario' : 'primario';
  if (fone) {
    const msg = encodeURIComponent(textoParaCompartilhar(recibo));
    $('btn-whats').href = `https://wa.me/${fone}?text=${msg}`;
  }
}

function textoParaCompartilhar(recibo) {
  const d = recibo.dados;
  const linhas = [
    `✅ Recibo #${recibo.rid}`,
    `👤 Passageiro: ${d.passageiro}`,
    `📅 Data: ${formatarBR(d.data)}`,
    `📍 Origem: ${d.origem}`,
    `🏁 Destino: ${d.destino}`,
    `💰 Valor: R$ ${d.valor_exibido}`,
    `💳 Pagamento: ${d.forma_pagamento}`,
  ];
  if (recibo.url) {
    linhas.push('', '🔗 Recibo completo, para ver, imprimir ou salvar em PDF:',
                recibo.url);
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
    Guardado.set('access_token', (await resp.json()).access_token);
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
    },
  };

  await salvarRecibo(recibo);        // primeiro guarda, depois tenta enviar
  vibrar('HEAVY');
  $('form-recibo').reset();
  preencherDataEHora();     // o próximo recibo já nasce com a hora certa

  $('btn-compartilhar').dataset.rid = rid;
  mostrarRecibo(recibo);
  mostrar('tela-recibo');

  sincronizar().then(async () => {
    const atual = (await listarRecibos()).find((r) => r.rid === rid);
    // Redesenha inteiro, link incluso: a URL publica so existe depois que o
    // servidor responde. Atualizar so o corpo deixava o WhatsApp saindo sem
    // o endereco do recibo.
    if (atual) mostrarRecibo(atual);
  });
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
      mostrar('tela-emitir');
    }
  } catch (e) {
    // Cancelar não é erro: o motorista fechou a folha de pagamento.
    if (e?.code === 'PURCHASE_CANCELLED' || /cancel/i.test(e?.message || '')) return;
    erro.textContent = 'Não foi possível concluir a compra. Tente de novo.';
    erro.hidden = false;
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
      mostrar('tela-emitir');
    } else {
      erro.textContent = 'Nenhuma assinatura ativa encontrada nesta conta.';
      erro.hidden = false;
    }
  } catch {
    erro.textContent = 'Não conseguimos verificar agora. Tente com sinal melhor.';
    erro.hidden = false;
  }
});

async function atualizarSessao() {
  try {
    const r = await api('/api/sessao');
    if (!r.ok) return null;
    const s = await r.json();
    Guardado.set('motorista', s.motorista);
    Guardado.set('motorista_id', s.id);
    Guardado.set('plano', s.plano);
    Guardado.set('limite', s.limite_mensal);
    return s;
  } catch { return null; }
}

$('btn-novo').addEventListener('click', async () => {
  preencherDataEHora();
  await atualizarSugestoesDeLocal();   // aprende o local que acabou de ser usado
  await atualizarAvisoFila();
  mostrar('tela-emitir');
});
$('btn-voltar').addEventListener('click', () => mostrar('tela-emitir'));
$('btn-sair').addEventListener('click', () => {
  Guardado.del('access_token'); Guardado.del('motorista'); mostrar('tela-login');
});

$('btn-historico').addEventListener('click', async () => {
  const lista = await listarRecibos();
  $('lista-historico').innerHTML = lista.length
    ? lista.map((r) => `
        <div class="item">
          <div>
            <strong>${r.dados.passageiro}</strong>
            <small>${formatarBR(r.dados.data)} · ${r.dados.origem} → ${r.dados.destino}</small>
          </div>
          <div class="item-direita">
            <span class="item-valor">R$ ${r.dados.valor_exibido}</span>
            <small>${r.pendente ? '⏳ na fila' : r.recusado ? '⚠ ' + r.recusado : '✓ enviado'}</small>
          </div>
        </div>`).join('')
    : '<p class="ajuda">Nenhum recibo ainda.</p>';
  mostrar('tela-historico');
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
  if (!token()) { mostrar('tela-login'); return; }
  mostrar('tela-emitir');
  await atualizarSessao();
  await iniciarCompras();
  await atualizarSugestoesDeLocal();
  await atualizarAvisoFila();
  sincronizar().then(atualizarAvisoFila);
}

iniciar();
