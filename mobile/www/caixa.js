/* Recibo Táxi — caixa do motorista (app 1.8): receitas e despesas.
 *
 * So as contas: sem tela, sem banco e sem rede. O app.js guarda, envia e
 * desenha; este arquivo diz quanto e de quem. Fica separado para o teste
 * (mobile/testes/caixa.mjs) rodar no Node, sem navegador.
 *
 * Dinheiro e sempre inteiro na menor unidade da moeda ("centavos"). Em ponto
 * flutuante, 0,1 + 0,2 da 0,30000000000000004, e o resumo do mes soma
 * centenas de lancamentos: o centavo que sobra aparece na tela.
 */
(function (raiz) {
  'use strict';

  // Na ordem em que aparecem na tela. O id vai ao servidor e a lista e aberta
  // (a fase 2 cria outras); o rotulo e so do app.
  const CATEGORIAS = Object.freeze({
    receita: Object.freeze([
      { id: 'corrida', rotulo: 'Corrida' },
      { id: 'aplicativo', rotulo: 'Aplicativo' },
      { id: 'gorjeta', rotulo: 'Gorjeta' },
      { id: 'outra_receita', rotulo: 'Outra' },
    ].map(Object.freeze)),
    despesa: Object.freeze([
      { id: 'combustivel', rotulo: 'Combustível' },
      { id: 'alimentacao', rotulo: 'Alimentação' },
      { id: 'aluguel_veiculo', rotulo: 'Aluguel do veículo' },
      { id: 'pedagio', rotulo: 'Pedágio' },
      { id: 'estacionamento', rotulo: 'Estacionamento' },
      { id: 'lavagem', rotulo: 'Lavagem' },
      { id: 'manutencao', rotulo: 'Manutenção' },
      { id: 'multa', rotulo: 'Multa' },
      { id: 'prestacao', rotulo: 'Prestação do carro' },
      { id: 'seguro', rotulo: 'Seguro' },
      { id: 'ipva', rotulo: 'IPVA e licenciamento' },
      { id: 'cooperativa', rotulo: 'Cooperativa ou ponto' },
      { id: 'celular', rotulo: 'Celular e internet' },
      { id: 'outra_despesa', rotulo: 'Outra' },
    ].map(Object.freeze)),
  });

  const TIPOS = Object.freeze(['receita', 'despesa']);
  const ORIGENS = Object.freeze(['recibo', 'corrida', 'manual']);

  // Categoria que este app nao conhece (criada por uma versao mais nova e
  // trazida pelo servidor, na fase 2) aparece como "Outra", nunca como o id cru.
  function rotuloDaCategoria(id) {
    for (const tipo of TIPOS) {
      const achada = CATEGORIAS[tipo].find((c) => c.id === id);
      if (achada) return achada.rotulo;
    }
    return 'Outra';
  }

  const ordemDaCategoria = (id) => {
    for (const tipo of TIPOS) {
      const i = CATEGORIAS[tipo].findIndex((c) => c.id === id);
      if (i >= 0) return i;
    }
    return Infinity;
  };

  // ── Moeda ──────────────────────────────────────────────────────────────────
  // Mesma tabela do servidor (MOEDAS, em app.py). Moeda fora dela nao quebra:
  // vira o proprio codigo como simbolo, com centavos e o teto do real, como
  // dados_da_moeda la.
  const SIMBOLOS = {
    BRL: 'R$', USD: 'US$', EUR: '€', ARS: '$', BOB: 'Bs', CLP: '$', COP: '$',
    CRC: '₡', DOP: 'RD$', GTQ: 'Q', HNL: 'L', MXN: '$', NIO: 'C$', PEN: 'S/',
    PYG: '₲', UYU: '$', VES: 'Bs.',
  };
  // So o peso chileno e o guarani nao tem centavos.
  const SEM_CENTAVOS = ['CLP', 'PYG'];
  // Moedas em que uma corrida passa de cem mil (normalize_money, no servidor;
  // tetoDoValor, no app): o teto delas e 99.999.999,99, e o das outras, 99.999,99.
  const MOEDAS_GRANDES = ['ARS', 'CLP', 'COP', 'CRC', 'PYG', 'VES'];

  const codigoDe = (moeda) => String(moeda || 'BRL').trim().toUpperCase() || 'BRL';
  const casasDaMoeda = (moeda) => (SEM_CENTAVOS.includes(codigoDe(moeda)) ? 0 : 2);
  const simboloDaMoeda = (moeda) => SIMBOLOS[codigoDe(moeda)] || codigoDe(moeda);

  // O teto ja na menor unidade e em inteiro: 99.999,99 reais sao 9.999.999
  // centavos; no peso chileno (0 casas) os ",99" nao existem e o teto e 99.999.999.
  function tetoEmCentavos(moeda) {
    const grande = MOEDAS_GRANDES.includes(codigoDe(moeda));
    if (casasDaMoeda(moeda) === 0) return grande ? 99999999 : 99999;
    return grande ? 9999999999 : 9999999;
  }

  // "1500.50" -> 150050, por aritmetica de texto. Recebe o texto ja com ponto
  // decimal (a saida de separadoresDoValor do app). Multiplicar o Number por
  // 100 erra: 0.29 * 100 da 28.999999999999996. Mais casas do que a moeda tem:
  // arredonda meio para cima (45,005 -> 45,01), como o arredondamento de escola.
  // O que nao for numero sem sinal devolve null.
  function centavosDe(decimal, casas) {
    if (!Number.isInteger(casas) || casas < 0) return null;
    const m = /^(\d+)(?:\.(\d+))?$/.exec(String(decimal == null ? '' : decimal).trim());
    if (!m) return null;
    const fracao = m[2] || '';
    let n = Number(m[1] + (fracao + '0'.repeat(casas)).slice(0, casas));
    if (fracao.length > casas && fracao[casas] >= '5') n += 1;
    return Number.isSafeInteger(n) ? n : null;
  }

  // 123450 -> "R$ 1.234,50": ponto no milhar e virgula decimal, como
  // formatar_numero no servidor. Negativo leva o sinal de menos de verdade
  // (U+2212) antes do simbolo: o hifen some ao lado do "R$" na fonte do app.
  function formatar(centavos, casas = 2, simbolo = '') {
    const n = Math.round(Number(centavos) || 0);
    const c = Number.isInteger(casas) && casas >= 0 ? casas : 2;
    const digitos = String(Math.abs(n)).padStart(c + 1, '0');
    const inteiro = digitos.slice(0, digitos.length - c).replace(/\B(?=(\d{3})+(?!\d))/g, '.');
    const numero = c ? `${inteiro},${digitos.slice(-c)}` : inteiro;
    return `${n < 0 ? '−' : ''}${simbolo ? `${simbolo} ` : ''}${numero}`;
  }

  const formatarNaMoeda = (centavos, moeda) => formatar(centavos, casasDaMoeda(moeda), simboloDaMoeda(moeda));

  // ── Campos ─────────────────────────────────────────────────────────────────
  // Os tres devolvem '' para vazio, null para invalido e o texto normalizado
  // (com ponto) quando serve — o mesmo jeito do kmNormalizado do app.

  // Km como o servidor aceita (normalize_km): "12,4", "12.4", 12.4 ou "12 km",
  // ja arredondado a uma casa como la (12,45 -> 12,5). Confere o arredondado,
  // como o kmNormalizado do app: "0,04" viraria 0,0 km e "9999,99", 10.000,0.
  function kmDe(valor) {
    const bruto = String(valor == null ? '' : valor).trim().toLowerCase().replace(/\s|km/g, '');
    if (!bruto) return '';
    if (!/^\d{1,4}([.,]\d{1,2})?$/.test(bruto)) return null;
    const decimos = centavosDe(bruto.replace(',', '.'), 1);
    if (!(decimos >= 1 && decimos <= 99999)) return null;
    return `${(decimos - (decimos % 10)) / 10}.${decimos % 10}`;
  }

  // Litros de combustivel: "30,5", "30.5" ou 30.5; maior que zero, ate
  // 999,99, com no maximo 2 casas (como no servidor).
  function litrosDe(valor) {
    const bruto = String(valor == null ? '' : valor).trim().replace(/\s/g, '');
    if (!bruto) return '';
    if (!/^\d{1,3}([.,]\d{1,2})?$/.test(bruto)) return null;
    const texto = bruto.replace(',', '.');
    return Number(texto) > 0 ? texto : null;
  }

  // "HH:MM" ou vazio, como hora_valida no servidor.
  const horaValida = (valor) => {
    const h = String(valor == null ? '' : valor).trim();
    return /^([01]\d|2[0-3]):[0-5]\d$/.test(h) ? h : '';
  };

  // A rota e so do aparelho (ver paraEnvio): ajuda o motorista a reconhecer a
  // corrida na lista do dia.
  function rotaDe(origem, destino) {
    const de = String(origem || '').trim(), para = String(destino || '').trim();
    return de && para ? `${de} → ${para}` : de || para;
  }

  // ── Datas ──────────────────────────────────────────────────────────────────
  // O dia do motorista, no relogio dele. O toISOString() e UTC: no Brasil, a
  // partir das 21h ele ja diz amanha, e a corrida das 22h caia no dia seguinte.
  const doisDigitos = (n) => String(n).padStart(2, '0');

  function diaLocal(d = new Date()) {
    return `${d.getFullYear()}-${doisDigitos(d.getMonth() + 1)}-${doisDigitos(d.getDate())}`;
  }

  const horaLocal = (d = new Date()) => `${doisDigitos(d.getHours())}:${doisDigitos(d.getMinutes())}`;

  function partesDaData(data) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(data || ''));
    return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
  }

  // Data que existe no calendario: "2026-02-31" tem o formato, mas nao existe.
  function dataReal(data) {
    const p = partesDaData(data);
    if (!p || p[1] < 1 || p[1] > 12 || p[2] < 1) return false;
    return p[2] <= new Date(p[0], p[1], 0, 12).getDate();
  }

  // Ao meio-dia, e nao a meia-noite: onde o horario de verao comeca a 0h
  // (Chile, Paraguai), a meia-noite daquele dia nao existe e somar 24 h a
  // partir dela pula ou repete um dia.
  function somarDias(data, n) {
    const p = partesDaData(data);
    if (!p) return '';
    return diaLocal(new Date(p[0], p[1] - 1, p[2] + Number(n || 0), 12));
  }

  const mesDe = (data) => String(data || '').slice(0, 7);

  function mesAnterior(mes) {
    const [a, m] = String(mes).split('-').map(Number);
    return m === 1 ? `${a - 1}-12` : `${a}-${doisDigitos(m - 1)}`;
  }

  function mesSeguinte(mes) {
    const [a, m] = String(mes).split('-').map(Number);
    return m === 12 ? `${a + 1}-01` : `${a}-${doisDigitos(m + 1)}`;
  }

  const MESES = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho',
    'agosto', 'setembro', 'outubro', 'novembro', 'dezembro'];
  const DIAS_DA_SEMANA = ['dom', 'seg', 'ter', 'qua', 'qui', 'sex', 'sáb'];

  // "2026-11" -> "novembro de 2026". Escrito aqui, e nao pelo Intl: o nome tem
  // de sair igual no aparelho, em qualquer idioma do sistema, e no teste.
  function nomeDoMes(mes) {
    const [a, m] = String(mes).split('-').map(Number);
    return MESES[m - 1] ? `${MESES[m - 1]} de ${a}` : String(mes || '');
  }

  // "qua, 26/11"; de outro ano, com o ano ("ter, 31/12/2025"): o Pro ve os
  // meses anteriores, e dezembro passado nao pode parecer deste ano.
  function dataCurta(data, hoje = diaLocal()) {
    const p = partesDaData(data);
    if (!p) return '';
    const semana = DIAS_DA_SEMANA[new Date(p[0], p[1] - 1, p[2], 12).getDay()];
    const ano = (partesDaData(hoje) || [])[0] === p[0] ? '' : `/${p[0]}`;
    return `${semana}, ${doisDigitos(p[2])}/${doisDigitos(p[1])}${ano}`;
  }

  function rotuloDoDia(data, hoje = diaLocal()) {
    if (data === hoje) return 'Hoje';
    if (data === somarDias(hoje, -1)) return 'Ontem';
    return dataCurta(data, hoje);
  }

  // Gratis: so o mes corrente (e os seguintes, de lancamento com data
  // adiantada). O dado dos meses anteriores continua guardado, no aparelho e
  // no servidor: a trava e de exibicao, e ao assinar o historico aparece inteiro.
  const mesLiberado = (mes, mesAtual, ehPro) => !!ehPro || String(mes) >= String(mesAtual);

  // ── Lancamentos ────────────────────────────────────────────────────────────
  //
  // { id, dono, tipo, categoria, centavos, moeda, data, hora, forma, origem,
  //   rid, rota, obs, km, litros, versao, apagado, criado_em, atualizado_em,
  //   pendente, recusado }
  //
  // A versao sobe a cada mudanca: e por ela que o servidor sabe qual copia e a
  // mais nova (e o envio, se o motorista mexeu no lancamento enquanto ele subia).

  // Km e litros invalidos ficam como vieram, para o erroDoLancamento apontar;
  // vazios viram null, e os validos, o texto com ponto.
  const normalizado = (valor, regra) => {
    const n = regra(valor);
    return n === null ? valor : n || null;
  };

  function novoLancamento(campos = {}, { id, dono = '', moeda = 'BRL', agora } = {}) {
    const quando = agora || new Date().toISOString();
    const c = campos || {};
    return {
      id,
      dono: String(dono || ''),
      tipo: c.tipo,
      categoria: c.categoria,
      centavos: c.centavos,
      moeda: codigoDe(c.moeda || moeda),
      data: c.data || diaLocal(new Date(quando)),
      hora: horaValida(c.hora),
      forma: String(c.forma == null ? '' : c.forma).trim(),
      origem: c.origem || 'manual',
      rid: c.rid || null,
      rota: String(c.rota || ''),
      obs: String(c.obs == null ? '' : c.obs).trim(),
      km: normalizado(c.km, kmDe),
      litros: normalizado(c.litros, litrosDe),
      versao: 1,
      apagado: false,
      criado_em: quando,
      atualizado_em: quando,
      pendente: true,
      recusado: '',
    };
  }

  // O que o motorista muda pela tela. Id, dono, moeda, origem, recibo e rota
  // ficam: o lancamento de um recibo continua sendo dele.
  const EDITAVEIS = ['tipo', 'categoria', 'centavos', 'data', 'hora', 'forma', 'obs', 'km', 'litros'];

  // O que veio de recibo ou de corrida e sempre receita: e o dinheiro que o
  // passageiro pagou, e o recibo dele continua na mao do passageiro. Virar
  // despesa descontava esse valor do liquido (o dia caia o dobro do recibo) e
  // o recibo nunca voltava como receita, porque o r-<rid> ja existia.
  const tipoFixo = (l) => !!l && (l.origem === 'recibo' || l.origem === 'corrida');

  function editado(atual, mudancas = {}, { agora } = {}) {
    const novo = { ...atual };
    for (const campo of EDITAVEIS) {
      if (campo === 'tipo' && tipoFixo(atual)) continue;
      if (mudancas && Object.prototype.hasOwnProperty.call(mudancas, campo)) novo[campo] = mudancas[campo];
    }
    novo.hora = horaValida(novo.hora);
    novo.forma = String(novo.forma == null ? '' : novo.forma).trim();
    novo.obs = String(novo.obs == null ? '' : novo.obs).trim();
    novo.km = normalizado(novo.km, kmDe);
    novo.litros = normalizado(novo.litros, litrosDe);
    return marcadoParaEnviar(novo, atual, agora);
  }

  // Apagar nao tira do banco: marca. E o tumulo que impede o recibo de voltar
  // ao caixa (completar com os recibos antigos ve que o r-<rid> ja existe), e o
  // que avisa o servidor que o lancamento saiu.
  const apagado = (atual, { agora } = {}) => marcadoParaEnviar({ ...atual, apagado: true }, atual, agora);

  function marcadoParaEnviar(novo, atual, agora) {
    return {
      ...novo,
      versao: (Number.isInteger(atual.versao) ? atual.versao : 0) + 1,
      pendente: true,
      recusado: '',
      atualizado_em: agora || new Date().toISOString(),
    };
  }

  // Do recibo guardado no aparelho (o objeto do app.js) para a receita dele.
  // O valor do recibo e o TEXTO que o motorista digitou ("R$ 45", "1 500,00",
  // "15 000"): quem o transforma em decimal e o app (decimalDoValor), com a
  // mesma limpeza que aceitou o recibo — aqui nao se repete a regra. Recibo de
  // antes da moeda (sem dados.moeda) vale na moeda de agora. Valor que nao da
  // numero: null, e o recibo fica fora do caixa.
  function lancamentoDoRecibo(recibo, { decimalDe, moedaPadrao = 'BRL', dono, agora } = {}) {
    if (!recibo || !recibo.rid || typeof decimalDe !== 'function') return null;
    const d = recibo.dados || {};
    const moeda = codigoDe(d.moeda || moedaPadrao);
    let decimal;
    try { decimal = decimalDe(String(d.valor == null ? '' : d.valor)); } catch { return null; }
    const centavos = centavosDe(decimal, casasDaMoeda(moeda));
    // Acima do teto da moeda: recibo de antes do build 23 (o app nao conferia
    // o valor), que o servidor recusou ("150000" querendo dizer 1.500,00).
    // Entraria no caixa com um valor que nunca foi cobrado, somado no dia e no
    // mes, com a copia recusada e, num mes anterior do Gratis, sem como apagar.
    // O recibo de hoje nem nasce assim (motivoValorInvalido).
    if (!centavos || centavos > tetoEmCentavos(moeda)) return null;
    const quando = agora || new Date().toISOString();
    // Data que nao existe (recibo de versao antiga, sem a conferencia de hoje)
    // cai no dia em que o recibo nasceu, para nao sumir de todos os dias.
    const nasceu = recibo.criado_em ? new Date(recibo.criado_em) : null;
    const data = dataReal(d.data) ? d.data : diaLocal(nasceu && !isNaN(nasceu) ? nasceu : new Date(quando));
    const l = novoLancamento({
      tipo: 'receita', categoria: 'corrida', centavos, moeda, data, hora: d.hora,
      forma: String(d.forma_pagamento || '').slice(0, 40), origem: 'recibo', rid: recibo.rid,
      rota: rotaDe(d.origem, d.destino),
      // Km que nao serve nao derruba a copia do valor no servidor.
      km: kmDe(d.km) || null,
    }, { id: `r-${recibo.rid}`, dono: dono != null ? dono : recibo.dono, moeda, agora: quando });
    // Na ordem do dia, o recibo fica na hora em que nasceu, e nao na de agora.
    if (recibo.criado_em) l.criado_em = recibo.criado_em;
    return l;
  }

  // "Só lançar no caixa", no fim da corrida: a receita leva a data e a hora da
  // SAIDA (como o recibo da corrida), o km da estimativa e a rota (so local).
  function camposDaCorrida(c, { centavos, forma = '' } = {}) {
    const saida = c && c.inicio ? new Date(c.inicio) : null;
    if (!saida || isNaN(saida)) return null;
    const km = c.estimativa && typeof c.estimativa.km === 'number' ? kmDe(c.estimativa.km) || null : null;
    return {
      tipo: 'receita', categoria: 'corrida', centavos, origem: 'corrida', forma,
      data: diaLocal(saida), hora: horaLocal(saida), km, rota: rotaDe(c.origem, c.destino),
    };
  }

  // O que o servidor recusaria (POST /api/lancamentos), conferido antes de
  // guardar: recusado la, o lancamento ficaria sem copia ate alguem editar.
  // So os campos que o motorista preenche; id, origem, recibo e versao sao do
  // app. A data vai ate hoje no relogio do aparelho: nada do que ele lanca
  // acontece amanha (o "Só lançar" usa a saida da corrida), e a data adiante,
  // um toque errado no calendario, tirava o lancamento do dia que ele olhava.
  // O servidor aceita ate hoje + 2 dias no fuso de Brasilia, folga que cobre
  // qualquer fuso e o relogio um pouco adiantado.
  // A receita de um recibo nao tem teto: leva a data do recibo, que nao tem
  // (corrida agendada e paga antes, dia errado no calendario do recibo), e o
  // servidor tambem a aceita assim. Com o teto, corrigir o valor ou a forma
  // dessa receita obrigava a trocar a data dela para hoje.
  function erroDoLancamento(l, { hoje = diaLocal() } = {}) {
    if (!l || !TIPOS.includes(l.tipo)) return 'tipo_invalido';
    if (!/^[a-z][a-z0-9_]{0,29}$/.test(String(l.categoria || ''))) return 'categoria_invalida';
    if (!Number.isInteger(l.centavos) || l.centavos < 1 || l.centavos > tetoEmCentavos(l.moeda)) return 'valor_invalido';
    const semTeto = l.origem === 'recibo';
    if (!dataReal(l.data) || l.data < '2000-01-01' || (!semTeto && l.data > hoje)) return 'data_invalida';
    if (String(l.forma || '').length > 40 || String(l.obs || '').trim().length > 120) return 'campo_longo';
    if (l.km != null && kmDe(l.km) === null) return 'km_invalido';
    if (l.litros != null && litrosDe(l.litros) === null) return 'litros_invalido';
    return '';
  }

  // O item do POST /api/lancamentos. A rota (origem e destino) e o dono NUNCA
  // vao ao servidor; o resto e o que o contrato pede, nada alem.
  // O tumulo so avisa que o id saiu: a observacao e a forma, texto livre onde
  // cabe o nome ou o telefone de alguem, nao viajam com ele (o servidor as
  // grava vazias de qualquer jeito). No aparelho, o tumulo continua inteiro.
  function paraEnvio(l) {
    const apagado = l.apagado === true;
    return {
      id: l.id,
      tipo: l.tipo,
      categoria: l.categoria,
      centavos: l.centavos,
      moeda: l.moeda,
      data: l.data,
      hora: l.hora || '',
      forma: apagado ? '' : l.forma || '',
      origem: l.origem,
      rid: l.rid || null,
      obs: apagado ? '' : l.obs || '',
      km: l.km || null,
      litros: l.litros || null,
      versao: l.versao,
      apagado,
    };
  }

  // ── Resumos ────────────────────────────────────────────────────────────────
  // Ignoram os apagados e somam so a moeda pedida. Quem trocou de moeda tem
  // lancamentos nas duas: somar colones com reais daria um numero sem sentido,
  // entao os da outra moeda so sao contados (outrasMoedas), a parte.

  function resumir(itens, moeda) {
    const r = { receitas: 0, despesas: 0, liquido: 0, nReceitas: 0, nDespesas: 0, nCorridas: 0, outrasMoedas: 0 };
    for (const l of itens) {
      if (codigoDe(l.moeda) !== moeda) { r.outrasMoedas++; continue; }
      const valor = Number.isInteger(l.centavos) && l.centavos > 0 ? l.centavos : 0;
      if (l.tipo === 'receita') {
        r.receitas += valor; r.nReceitas++;
        if (l.categoria === 'corrida') r.nCorridas++;
      } else if (l.tipo === 'despesa') {
        r.despesas += valor; r.nDespesas++;
      }
    }
    r.liquido = r.receitas - r.despesas;
    return r;
  }

  const vivos = (lista) => (Array.isArray(lista) ? lista : []).filter((l) => l && !l.apagado);

  function resumoDoDia(lista, data, moeda) {
    return resumir(vivos(lista).filter((l) => l.data === data), codigoDe(moeda));
  }

  // Meio para longe do zero: -2,5 centavos vira -3, como 2,5 vira 3.
  const arredondar = (x) => Math.sign(x) * Math.round(Math.abs(x));

  function porCategoria(itens, tipo) {
    const somas = new Map();
    for (const l of itens) {
      if (l.tipo !== tipo) continue;
      const s = somas.get(l.categoria) || { categoria: l.categoria, rotulo: rotuloDaCategoria(l.categoria), centavos: 0, n: 0 };
      s.centavos += Number.isInteger(l.centavos) && l.centavos > 0 ? l.centavos : 0;
      s.n++;
      somas.set(l.categoria, s);
    }
    const total = [...somas.values()].reduce((t, s) => t + s.centavos, 0);
    return [...somas.values()]
      .map((s) => ({ ...s, parte: total ? s.centavos / total : 0 }))
      .sort((a, b) => b.centavos - a.centavos
        || ordemDaCategoria(a.categoria) - ordemDaCategoria(b.categoria)
        || String(a.categoria).localeCompare(String(b.categoria)));
  }

  // A parte de uma categoria no total, em texto ("38%"). Os dois extremos nao
  // arredondam para o que nao e: 0,4% vira "<1%" (o "0%" parecia nada) e
  // 99,7% vira ">99%" (o "100%" dizia que a corrida era o total inteiro, com a
  // gorjeta de R$ 20 logo abaixo). Cada linha arredonda sozinha, e a soma da
  // tela pode dar 99% ou 101% (33% + 33% + 33%): cada numero esta certo, e
  // repartir a sobra (maior resto) poria 34% numa das tres partes iguais.
  function textoDaParte(fracao) {
    const f = Number(fracao) || 0;
    const p = Math.round(f * 100);
    if (f > 0 && p === 0) return '<1%';
    if (f < 1 && p >= 100) return '>99%';
    return `${p}%`;
  }

  // Dia trabalhado e dia com ao menos uma receita: o dia so de despesa (o
  // aluguel pago no domingo de folga) nao entra na media por dia.
  function resumoDoMes(lista, mes, moeda) {
    const codigo = codigoDe(moeda);
    const doMes = vivos(lista).filter((l) => mesDe(l.data) === mes);
    const r = resumir(doMes, codigo);
    const naMoeda = doMes.filter((l) => codigoDe(l.moeda) === codigo);
    const dias = new Map();
    for (const l of naMoeda) {
      if (!dias.has(l.data)) dias.set(l.data, []);
      dias.get(l.data).push(l);
    }
    r.porDia = [...dias.keys()].sort().reverse().map((data) => {
      const { outrasMoedas, ...doDiaResumido } = resumir(dias.get(data), codigo);
      return { data, ...doDiaResumido };
    });
    r.diasTrabalhados = r.porDia.filter((d) => d.nReceitas > 0).length;
    r.mediaPorDia = r.diasTrabalhados ? arredondar(r.liquido / r.diasTrabalhados) : 0;
    r.porCategoria = { receita: porCategoria(naMoeda, 'receita'), despesa: porCategoria(naMoeda, 'despesa') };
    return r;
  }

  // Os do dia, do mais recente para o mais antigo. Sem hora vai para o fim (nao
  // da para saber onde ele cabe); na mesma hora, o lancado por ultimo primeiro.
  function doDia(lista, data) {
    return vivos(lista).filter((l) => l.data === data).sort((a, b) => {
      const ha = a.hora || '', hb = b.hora || '';
      if (ha !== hb) {
        if (!ha) return 1;
        if (!hb) return -1;
        return ha < hb ? 1 : -1;
      }
      const ca = a.criado_em || '', cb = b.criado_em || '';
      if (ca !== cb) return ca < cb ? 1 : -1;
      return String(a.id) < String(b.id) ? 1 : -1;
    });
  }

  // Meta do dia (do liquido): quanto da barra encher, quanto falta e quanto passou.
  function progressoDaMeta(liquido, meta) {
    const alvo = Number.isInteger(meta) && meta > 0 ? meta : 0;
    const valor = Number.isFinite(liquido) ? liquido : 0;
    if (!alvo) return { fracao: 0, falta: 0, passou: 0, batida: false };
    return {
      fracao: Math.min(1, Math.max(0, valor / alvo)),
      falta: Math.max(0, alvo - valor),
      passou: Math.max(0, valor - alvo),
      batida: valor >= alvo,
    };
  }

  raiz.Caixa = Object.freeze({
    CATEGORIAS, TIPOS, ORIGENS, rotuloDaCategoria,
    casasDaMoeda, simboloDaMoeda, tetoEmCentavos, centavosDe, formatar, formatarNaMoeda,
    kmDe, litrosDe, horaValida, rotaDe,
    diaLocal, horaLocal, dataReal, somarDias, mesDe, mesAnterior, mesSeguinte, nomeDoMes,
    dataCurta, rotuloDoDia, mesLiberado,
    novoLancamento, editado, apagado, tipoFixo, lancamentoDoRecibo, camposDaCorrida, erroDoLancamento, paraEnvio,
    resumoDoDia, resumoDoMes, textoDaParte, doDia, progressoDaMeta,
  });
})(typeof window !== 'undefined' ? window : globalThis);
