// Testa as contas do caixa (mobile/www/caixa.js) no Node, sem navegador.
//
//   node mobile/testes/caixa.mjs      confere tudo; sai com 1 se algo falhar
//
// O caixa.js e um script de pagina (window.Caixa), nao um modulo: roda aqui num
// contexto do vm com um window falso, como o navegador o carregaria. As funcoes
// do app.js que transformam o valor digitado em decimal (valorLimpo,
// separadoresDoValor e decimalDoValor) vem do proprio app.js, recortadas do
// arquivo: e o mesmo texto que roda no aparelho, e nao uma copia que envelhece.
//
// O fuso e o de Sao Paulo, fixado antes de qualquer data: e nele que o
// toISOString() (UTC) troca o dia a partir das 21h.
process.env.TZ = 'America/Sao_Paulo';

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const AQUI = path.dirname(fileURLToPath(import.meta.url));
const WWW = path.join(AQUI, '..', 'www');

const janela = {};
vm.runInNewContext(fs.readFileSync(path.join(WWW, 'caixa.js'), 'utf8'), { window: janela }, { filename: 'caixa.js' });
const C = janela.Caixa;

// Funcao de topo do app.js: de "function nome(" ate o "}" da coluna 0.
const appJs = fs.readFileSync(path.join(WWW, 'app.js'), 'utf8');
const recortar = (nome) => {
  const achada = new RegExp(`^function ${nome}\\([^)]*\\) \\{[\\s\\S]*?^\\}`, 'm').exec(appJs);
  if (!achada) throw new Error(`nao achei a funcao ${nome} no app.js`);
  return achada[0];
};
const doApp = {};
vm.runInNewContext(`${['valorLimpo', 'separadoresDoValor', 'decimalDoValor'].map(recortar).join('\n')}
doApp.decimalDoValor = decimalDoValor;`, { doApp }, { filename: 'app.js (recorte)' });
const { decimalDoValor } = doApp;

const linhas = [];
const ok = (nome, cond, extra = '') => linhas.push(`${cond ? 'OK   ' : 'FALHA'} ${nome}${cond ? '' : ' :: ' + extra}`);
// Os objetos do caixa.js nascem no contexto do vm: compara pelo JSON.
const igual = (nome, obtido, esperado) => {
  const a = JSON.stringify(obtido), b = JSON.stringify(esperado);
  ok(nome, a === b, `${a} (esperado ${b})`);
};

try {
  // ── Categorias ──────────────────────────────────────────────────────────
  igual('categorias de receita, na ordem da tela', C.CATEGORIAS.receita.map((c) => c.id),
    ['corrida', 'aplicativo', 'gorjeta', 'outra_receita']);
  igual('categorias de despesa, na ordem da tela', C.CATEGORIAS.despesa.map((c) => c.id),
    ['combustivel', 'alimentacao', 'aluguel_veiculo', 'pedagio', 'estacionamento', 'lavagem', 'manutencao',
      'multa', 'prestacao', 'seguro', 'ipva', 'cooperativa', 'celular', 'outra_despesa']);
  ok('todo id de categoria passa na regra do servidor',
    [...C.CATEGORIAS.receita, ...C.CATEGORIAS.despesa].every((c) => /^[a-z][a-z0-9_]{0,29}$/.test(c.id)));
  igual('rotulos legiveis, com acento', ['aluguel_veiculo', 'combustivel', 'ipva', 'outra_receita', 'outra_despesa'].map(C.rotuloDaCategoria),
    ['Aluguel do veículo', 'Combustível', 'IPVA e licenciamento', 'Outra', 'Outra']);
  igual('categoria desconhecida (de versao futura) vira "Outra", nunca o id cru',
    [C.rotuloDaCategoria('pet_shop'), C.rotuloDaCategoria(undefined)], ['Outra', 'Outra']);
  ok('as listas nao se mexem por fora', Object.isFrozen(C.CATEGORIAS) && Object.isFrozen(C.CATEGORIAS.despesa)
    && Object.isFrozen(C.CATEGORIAS.despesa[0]));

  // ── Moeda ───────────────────────────────────────────────────────────────
  igual('casas: 0 no peso chileno e no guarani, 2 nas outras (e na desconhecida)',
    ['BRL', 'CLP', 'PYG', 'CRC', 'clp', undefined, 'XYZ'].map(C.casasDaMoeda), [2, 0, 0, 2, 0, 2, 2]);
  igual('teto na menor unidade, igual ao do servidor',
    ['BRL', 'USD', 'CLP', 'PYG', 'CRC', 'ARS', 'XYZ'].map(C.tetoEmCentavos),
    [9999999, 9999999, 99999999, 99999999, 9999999999, 9999999999, 9999999]);
  igual('simbolo da moeda (a desconhecida vira o codigo)', ['BRL', 'CRC', 'PYG', 'XYZ'].map(C.simboloDaMoeda),
    ['R$', '₡', '₲', 'XYZ']);

  // ── Valor: texto -> centavos ────────────────────────────────────────────
  igual('centavosDe: inteiro, com casas e com casas a menos',
    [C.centavosDe('1500.50', 2), C.centavosDe('45', 2), C.centavosDe('45.5', 2), C.centavosDe(' 12.30 ', 2)],
    [150050, 4500, 4550, 1230]);
  igual('centavosDe: por texto, sem o erro do ponto flutuante (0,29 e 1,005)',
    [C.centavosDe('0.29', 2), C.centavosDe('1.005', 2), C.centavosDe('4.35', 2)], [29, 101, 435]);
  igual('centavosDe: casas demais arredondam meio para cima',
    [C.centavosDe('45.004', 2), C.centavosDe('45.0049', 2), C.centavosDe('45.005', 2), C.centavosDe('45.999', 2)],
    [4500, 4500, 4501, 4600]);
  igual('centavosDe: moeda sem centavos (0 casas)',
    [C.centavosDe('15000', 0), C.centavosDe('100.4', 0), C.centavosDe('100.5', 0)], [15000, 100, 101]);
  igual('centavosDe: o que nao e numero sem sinal devolve null',
    ['', 'abc', '-5', '.5', '45.', '1.2.3', '12abc34', null, undefined].map((v) => C.centavosDe(v, 2)),
    [null, null, null, null, null, null, null, null, null]);
  igual('centavosDe: casas invalidas e numero grande demais devolvem null',
    [C.centavosDe('12', -1), C.centavosDe('12', 1.5), C.centavosDe('99999999999999999999', 2)], [null, null, null]);

  // Com a limpeza e os separadores do app (os mesmos que aceitaram o recibo).
  const doDigitado = (texto, casas = 2) => C.centavosDe(decimalDoValor(texto), casas);
  igual('separadores: o que o app manda para o centavosDe',
    ['1.500', '45,50', 'R$ 1.500,50', '1 500,00', '1,234.56', '15 000'].map(decimalDoValor),
    ['1500', '45.50', '1500.50', '1500.00', '1234.56', '15000']);
  igual('valor digitado como o recibo aceita vira centavos ("R$ 45", "R$45,00", "1 500,00", "15 000")',
    [doDigitado('R$ 45'), doDigitado('R$45,00'), doDigitado('1 500,00'), doDigitado('15 000'), doDigitado('61,40'), doDigitado('1.234,56')],
    [4500, 4500, 150000, 1500000, 6140, 123456]);
  igual('valor em guarani (0 casas) com ponto de milhar', doDigitado('15.000', 0), 15000);

  // ── Formatacao ──────────────────────────────────────────────────────────
  igual('formatar: milhar com ponto e virgula decimal',
    [C.formatar(123450, 2, 'R$'), C.formatar(123456789, 2, 'R$'), C.formatar(5, 2, 'R$'), C.formatar(0, 2, 'R$')],
    ['R$ 1.234,50', 'R$ 1.234.567,89', 'R$ 0,05', 'R$ 0,00']);
  igual('formatar: 0 casas e sem simbolo',
    [C.formatar(1500000, 0, '₲'), C.formatar(999, 0, '$'), C.formatar(100000, 2, '')], ['₲ 1.500.000', '$ 999', '1.000,00']);
  const negativo = C.formatar(-8760, 2, 'R$');
  ok('formatar: negativo com o menos de verdade (U+2212) antes do simbolo',
    negativo === '−R$ 87,60' && negativo.charCodeAt(0) === 0x2212, negativo);
  igual('formatar na moeda do lancamento', [C.formatarNaMoeda(1500000, 'CRC'), C.formatarNaMoeda(15000, 'CLP'), C.formatarNaMoeda(-100, 'BRL')],
    ['₡ 15.000,00', '$ 15.000', '−R$ 1,00']);

  // ── Km, litros e hora ───────────────────────────────────────────────────
  igual('km: como o servidor aceita, arredondado a uma casa como la',
    ['12,4', '12.45', '12', 14.2, '12 km', '0,05', '9999,94'].map(C.kmDe), ['12.4', '12.5', '12.0', '14.2', '12.0', '0.1', '9999.9']);
  igual('km: vazio e "" e invalido e null (0,04 viraria 0,0; 9999,99 viraria 10000,0)',
    ['', null, 'abc', 'uns 12', '0,04', '9999,99', '-3'].map(C.kmDe), ['', '', null, null, null, null, null]);
  igual('litros: ate 999,99 com 2 casas', ['30,5', '30.5', 30.5, '999,99', '0,01'].map(C.litrosDe), ['30.5', '30.5', '30.5', '999.99', '0.01']);
  igual('litros: vazio e "" e invalido e null', ['', null, '0', '1000', '30,555', 'abc', '-2'].map(C.litrosDe),
    ['', '', null, null, null, null, null]);
  igual('hora: so HH:MM valida', ['18:42', '00:00', '23:59', '24:00', '8:30', '', 'abc'].map(C.horaValida),
    ['18:42', '00:00', '23:59', '', '', '', '']);

  // ── Datas: o dia do motorista, nunca o UTC ──────────────────────────────
  const noite = new Date(2026, 10, 26, 23, 30);
  ok('diaLocal as 23h30 continua no dia (o toISOString ja diz amanha)',
    C.diaLocal(noite) === '2026-11-26' && noite.toISOString().slice(0, 10) === '2026-11-27', `${C.diaLocal(noite)} / ${noite.toISOString()}`);
  igual('diaLocal logo depois da meia-noite e o dia novo', C.diaLocal(new Date(2026, 10, 27, 0, 10)), '2026-11-27');
  igual('horaLocal', C.horaLocal(noite), '23:30');
  igual('somarDias: virada de mes, de ano e fevereiro',
    [C.somarDias('2026-11-30', 1), C.somarDias('2026-12-31', 1), C.somarDias('2027-01-01', -1), C.somarDias('2028-02-28', 1),
      C.somarDias('2026-02-28', 1), C.somarDias('2026-03-01', -1), C.somarDias('2026-11-26', 0), C.somarDias('lixo', 1)],
    ['2026-12-01', '2027-01-01', '2026-12-31', '2028-02-29', '2026-03-01', '2026-02-28', '2026-11-26', '']);
  igual('mes: de uma data, anterior e seguinte, com virada de ano',
    [C.mesDe('2026-11-26'), C.mesAnterior('2026-11'), C.mesAnterior('2026-01'), C.mesSeguinte('2026-12'), C.mesSeguinte('2026-09')],
    ['2026-11', '2026-10', '2025-12', '2027-01', '2026-10']);
  igual('nome do mes em portugues', [C.nomeDoMes('2026-11'), C.nomeDoMes('2026-03')], ['novembro de 2026', 'março de 2026']);
  igual('rotulo do dia: Hoje, Ontem, dia da semana e, de outro ano, com o ano',
    [C.rotuloDoDia('2026-11-26', '2026-11-26'), C.rotuloDoDia('2026-11-25', '2026-11-26'), C.rotuloDoDia('2026-11-24', '2026-11-26'),
      C.rotuloDoDia('2026-11-01', '2026-11-02'), C.rotuloDoDia('2025-12-31', '2026-01-01'), C.rotuloDoDia('2025-12-30', '2026-11-26')],
    ['Hoje', 'Ontem', 'ter, 24/11', 'Ontem', 'Ontem', 'ter, 30/12/2025']);
  igual('data curta (para "Hoje, qui 26/11")', C.dataCurta('2026-11-26', '2026-11-26'), 'qui, 26/11');
  igual('data que existe no calendario', ['2026-02-29', '2028-02-29', '2026-13-01', '2026-11-31', '2026-11-30', '26/11/2026'].map(C.dataReal),
    [false, true, false, false, true, false]);
  // No Chile o horario de verao comeca a meia-noite: o dia 06/09/2026 nao tem 0h.
  process.env.TZ = 'America/Santiago';
  igual('somarDias atravessa o horario de verao sem pular nem repetir dia (Chile)',
    [C.somarDias('2026-09-05', 1), C.somarDias('2026-09-06', 1), C.somarDias('2026-09-07', -2), C.somarDias('2026-04-04', 1)],
    ['2026-09-06', '2026-09-07', '2026-09-05', '2026-04-05']);
  process.env.TZ = 'America/Sao_Paulo';
  igual('mesLiberado: Gratis so o mes corrente (e os seguintes); Pro ve tudo',
    [C.mesLiberado('2026-10', '2026-11', false), C.mesLiberado('2026-11', '2026-11', false), C.mesLiberado('2026-12', '2026-11', false),
      C.mesLiberado('2025-12', '2026-11', false), C.mesLiberado('2026-10', '2026-11', true), C.mesLiberado('2025-01', '2026-11', true)],
    [false, true, true, false, true, true]);

  // ── Recibo -> receita ───────────────────────────────────────────────────
  const agora = '2026-11-27T12:00:00.000Z';
  const recibo = {
    rid: '0A1B2C3D4E5F60', dono: 'id-1', criado_em: '2026-11-26T21:42:10.000Z', pendente: true, motorista: { full_name: 'X' },
    dados: { rid: '0A1B2C3D4E5F60', valor: 'R$ 58,00', valor_exibido: '58,00', moeda: 'BRL', data: '2026-11-26', hora: '18:42',
      forma_pagamento: 'Pix', km: '12,4', origem: 'Centro', destino: 'Aeroporto', passageiro: 'Maria', documento_passageiro: '123' },
  };
  const doRecibo = C.lancamentoDoRecibo(recibo, { decimalDe: decimalDoValor, moedaPadrao: 'BRL', agora });
  igual('recibo vira receita r-<rid>, de corrida, pendente, com a rota so local', doRecibo, {
    id: 'r-0A1B2C3D4E5F60', dono: 'id-1', tipo: 'receita', categoria: 'corrida', centavos: 5800, moeda: 'BRL',
    data: '2026-11-26', hora: '18:42', forma: 'Pix', origem: 'recibo', rid: '0A1B2C3D4E5F60', rota: 'Centro → Aeroporto',
    obs: '', km: '12.4', litros: null, versao: 1, apagado: false, criado_em: '2026-11-26T21:42:10.000Z',
    atualizado_em: agora, pendente: true, recusado: '',
  });
  ok('a receita do recibo e valida para o servidor', C.erroDoLancamento(doRecibo, { hoje: '2026-11-27' }) === '',
    C.erroDoLancamento(doRecibo, { hoje: '2026-11-27' }));
  const semMoeda = { ...recibo, dados: { ...recibo.dados, moeda: undefined, valor: '15 000' } };
  const emColon = C.lancamentoDoRecibo(semMoeda, { decimalDe: decimalDoValor, moedaPadrao: 'CRC', agora });
  igual('recibo antigo sem moeda vale na moeda de agora (colones: 2 casas)', emColon && [emColon.moeda, emColon.centavos], ['CRC', 1500000]);
  const emGuarani = C.lancamentoDoRecibo({ ...semMoeda, dados: { ...semMoeda.dados, valor: '15.000' } },
    { decimalDe: decimalDoValor, moedaPadrao: 'PYG', agora });
  igual('recibo sem moeda, moeda de agora sem centavos (guarani)', emGuarani && [emGuarani.moeda, emGuarani.centavos], ['PYG', 15000]);
  igual('recibo com valor que nao da numero (ou zero) fica fora do caixa',
    ['abc', '', '0', '0,00', '12abc34'].map((valor) => C.lancamentoDoRecibo({ ...recibo, dados: { ...recibo.dados, valor } },
      { decimalDe: decimalDoValor, agora })), [null, null, null, null, null]);
  // Antes do build 23 o app nao conferia o valor: "150000" (querendo dizer
  // 1.500,00) foi recusado pelo servidor e ficou no aparelho. Nao vira receita.
  const comValor = (valor, moeda) => C.lancamentoDoRecibo({ ...recibo, dados: { ...recibo.dados, valor, moeda } },
    { decimalDe: decimalDoValor, agora });
  igual('recibo antigo acima do teto da moeda fica fora do caixa; no teto, entra',
    [comValor('150000', 'BRL'), comValor('100.000,00', 'BRL'), comValor('100000,00', 'BRL'), (comValor('99999,99', 'BRL') || {}).centavos,
      comValor('100000000', 'CLP'), (comValor('99999999', 'CLP') || {}).centavos],
    [null, null, null, 9999999, null, 99999999]);
  igual('sem recibo, sem rid ou sem o conversor do app: null',
    [C.lancamentoDoRecibo(null, { decimalDe: decimalDoValor }), C.lancamentoDoRecibo({ ...recibo, rid: '' }, { decimalDe: decimalDoValor }),
      C.lancamentoDoRecibo(recibo, {})], [null, null, null]);
  const dataTorta = C.lancamentoDoRecibo({ ...recibo, criado_em: '2026-11-03T01:30:00.000Z', dados: { ...recibo.dados, data: '2026-02-31' } },
    { decimalDe: decimalDoValor, agora });
  igual('recibo com data que nao existe cai no dia (local) em que nasceu', dataTorta && dataTorta.data, '2026-11-02');
  const kmTorto = C.lancamentoDoRecibo({ ...recibo, dados: { ...recibo.dados, km: 'uns 12', hora: '25:99', forma_pagamento: undefined } },
    { decimalDe: decimalDoValor, agora });
  igual('km, hora e forma que nao servem nao derrubam a receita', kmTorto && [kmTorto.km, kmTorto.hora, kmTorto.forma, kmTorto.centavos],
    [null, '', '', 5800]);
  igual('dono pode vir de quem chama', C.lancamentoDoRecibo(recibo, { decimalDe: decimalDoValor, dono: 'id-2' }).dono, 'id-2');

  // ── Corrida sem recibo e lancamento a mao ───────────────────────────────
  const corrida = { inicio: new Date(2026, 10, 26, 22, 15).toISOString(), fim: new Date(2026, 10, 26, 22, 50).toISOString(),
    origem: 'Rua Augusta, 100', destino: 'Shopping', estimativa: { km: 14.2 } };
  const daCorrida = C.camposDaCorrida(corrida, { centavos: 5800, forma: 'Dinheiro' });
  igual('corrida: data e hora da SAIDA no relogio local (22h15 nao vira o dia seguinte)', daCorrida, {
    tipo: 'receita', categoria: 'corrida', centavos: 5800, origem: 'corrida', forma: 'Dinheiro',
    data: '2026-11-26', hora: '22:15', km: '14.2', rota: 'Rua Augusta, 100 → Shopping',
  });
  igual('corrida sem saida ou sem estimativa', [C.camposDaCorrida({}, { centavos: 1 }), C.camposDaCorrida({ ...corrida, estimativa: null }, { centavos: 1 }).km],
    [null, null]);
  const manual = C.novoLancamento({ tipo: 'despesa', categoria: 'combustivel', centavos: 8600, data: '2026-11-26', hora: '15:10',
    forma: ' Pix ', obs: '  posto da esquina ', litros: '30,5' }, { id: 'l-0123456789abcdef', dono: 'id-1', moeda: 'BRL', agora });
  igual('lancamento a mao: manual, sem recibo, campos limpos, versao 1 e pendente', manual, {
    id: 'l-0123456789abcdef', dono: 'id-1', tipo: 'despesa', categoria: 'combustivel', centavos: 8600, moeda: 'BRL',
    data: '2026-11-26', hora: '15:10', forma: 'Pix', origem: 'manual', rid: null, rota: '', obs: 'posto da esquina',
    km: null, litros: '30.5', versao: 1, apagado: false, criado_em: agora, atualizado_em: agora, pendente: true, recusado: '',
  });
  igual('sem data, o lancamento e de hoje (local)', C.novoLancamento({ tipo: 'receita', categoria: 'gorjeta', centavos: 1 },
    { id: 'l-1', dono: 'x', agora: '2026-11-27T02:30:00.000Z' }).data, '2026-11-26');

  // ── O que o servidor recusaria ──────────────────────────────────────────
  const valido = { ...manual };
  const erro = (mudanca, hoje = '2026-11-26') => C.erroDoLancamento({ ...valido, ...mudanca }, { hoje });
  igual('lancamento valido passa', erro({}), '');
  igual('tipo e categoria fora da regra',
    [erro({ tipo: 'ganho' }), erro({ tipo: undefined }), erro({ categoria: 'Combustível' }), erro({ categoria: '1abc' }), erro({ categoria: 'a'.repeat(31) })],
    ['tipo_invalido', 'tipo_invalido', 'categoria_invalida', 'categoria_invalida', 'categoria_invalida']);
  igual('valor: inteiro de verdade, de 1 ate o teto da moeda',
    [erro({ centavos: 0 }), erro({ centavos: -1 }), erro({ centavos: 1.5 }), erro({ centavos: '4500' }), erro({ centavos: true }),
      erro({ centavos: 10000000 }), erro({ centavos: 9999999 }), erro({ moeda: 'CLP', centavos: 99999999 }), erro({ moeda: 'CLP', centavos: 100000000 })],
    ['valor_invalido', 'valor_invalido', 'valor_invalido', 'valor_invalido', 'valor_invalido', 'valor_invalido', '', '', 'valor_invalido']);
  igual('data: real, de 2000 ate hoje (amanha ja nao: o Caixa abre no dia de hoje)',
    [erro({ data: '2026-02-31' }), erro({ data: '1999-12-31' }), erro({ data: '2000-01-01' }), erro({ data: '2026-11-26' }), erro({ data: '2026-11-27' }),
      erro({ data: '2026-11-28' }), erro({ data: '' })],
    ['data_invalida', 'data_invalida', '', '', 'data_invalida', 'data_invalida', 'data_invalida']);
  igual('forma ate 40 e observacao ate 120 (sem contar espaco nas pontas)',
    [erro({ forma: 'x'.repeat(41) }), erro({ forma: 'x'.repeat(40) }), erro({ obs: 'x'.repeat(121) }), erro({ obs: ` ${'x'.repeat(120)} ` })],
    ['campo_longo', '', 'campo_longo', '']);
  igual('km e litros invalidos', [erro({ km: 'abc' }), erro({ litros: '1000' }), erro({ litros: null, km: null })], ['km_invalido', 'litros_invalido', '']);
  igual('litros e km invalidos digitados ficam para a conferencia apontar',
    C.erroDoLancamento(C.novoLancamento({ ...valido, litros: 'muito' }, { id: 'l-2', dono: 'x', agora }), { hoje: '2026-11-26' }), 'litros_invalido');

  // ── Editar e apagar ─────────────────────────────────────────────────────
  const recusado = { ...manual, pendente: false, recusado: 'valor_invalido' };
  const depois = '2026-11-27T13:00:00.000Z';
  const editadoL = C.editado(recusado, { centavos: 9000, obs: ' troco ', id: 'outro', dono: 'outro', origem: 'recibo', moeda: 'USD' }, { agora: depois });
  igual('editar sobe a versao, volta a fila, limpa o recusado e nao troca id, dono, origem nem moeda',
    [editadoL.versao, editadoL.pendente, editadoL.recusado, editadoL.atualizado_em, editadoL.centavos, editadoL.obs,
      editadoL.id, editadoL.dono, editadoL.origem, editadoL.moeda, editadoL.criado_em],
    [2, true, '', depois, 9000, 'troco', 'l-0123456789abcdef', 'id-1', 'manual', 'BRL', agora]);
  ok('editar nao mexe no original', recusado.versao === 1 && recusado.centavos === 8600);
  // Recibo e corrida sao sempre receita: a troca de tipo nao pega, o resto sim.
  const daCorridaL = C.novoLancamento(C.camposDaCorrida(corrida, { centavos: 5800 }), { id: 'l-corrida', dono: 'id-1', agora });
  const gorjetaL = C.novoLancamento({ tipo: 'receita', categoria: 'gorjeta', centavos: 1000, data: '2026-11-26' }, { id: 'l-gorjeta', dono: 'id-1', agora });
  const reciboVirado = C.editado(doRecibo, { tipo: 'despesa', centavos: 4000 }, { agora: depois });
  igual('o tipo do que veio de recibo ou de corrida fica receita ao editar; o da manual muda',
    [reciboVirado.tipo, reciboVirado.centavos, reciboVirado.versao, C.editado(daCorridaL, { tipo: 'despesa' }).tipo, C.editado(gorjetaL, { tipo: 'despesa' }).tipo],
    ['receita', 4000, 2, 'receita', 'despesa']);
  igual('tipo fixo: recibo e corrida sim; manual e nada, nao',
    [C.tipoFixo(doRecibo), C.tipoFixo(daCorridaL), C.tipoFixo(gorjetaL), C.tipoFixo(null)], [true, true, false, false]);
  const tumulo = C.apagado(editadoL, { agora: depois });
  igual('apagar vira tumulo: apagado, versao + 1, pendente', [tumulo.apagado, tumulo.versao, tumulo.pendente, tumulo.centavos], [true, 3, true, 9000]);

  // ── O que vai ao servidor ───────────────────────────────────────────────
  const enviado = C.paraEnvio({ ...doRecibo, pendente: true, recusado: 'x' });
  igual('envio: so os campos do contrato, sem rota, dono, pendente nem datas do aparelho', Object.keys(enviado),
    ['id', 'tipo', 'categoria', 'centavos', 'moeda', 'data', 'hora', 'forma', 'origem', 'rid', 'obs', 'km', 'litros', 'versao', 'apagado']);
  ok('envio: o trajeto (origem e destino) nunca vai', !JSON.stringify(enviado).includes('Aeroporto') && !JSON.stringify(enviado).includes('Centro'));
  igual('envio do tumulo e do manual', [C.paraEnvio(tumulo).apagado, C.paraEnvio(tumulo).rid, C.paraEnvio(manual).apagado, C.paraEnvio(manual).litros],
    [true, null, false, '30.5']);

  // ── Resumos ─────────────────────────────────────────────────────────────
  const L = (id, tipo, categoria, centavos, data, hora = '', extra = {}) => ({
    id, dono: 'id-1', tipo, categoria, centavos, moeda: 'BRL', data, hora, forma: 'Pix', origem: 'manual', rid: null,
    rota: '', obs: '', km: null, litros: null, versao: 1, apagado: false, criado_em: `${data}T12:00:00.000Z`,
    atualizado_em: `${data}T12:00:00.000Z`, pendente: false, recusado: '', ...extra,
  });
  const lista = [
    L('c58', 'receita', 'corrida', 5800, '2026-11-26', '18:42'),
    L('c45', 'receita', 'corrida', 4500, '2026-11-26', '15:10', { criado_em: '2026-11-26T18:10:00.000Z' }),
    L('comb', 'despesa', 'combustivel', 8600, '2026-11-26', '15:10', { criado_em: '2026-11-26T18:20:00.000Z' }),
    L('gorj', 'receita', 'gorjeta', 500, '2026-11-26', ''),
    L('apag', 'despesa', 'alimentacao', 2000, '2026-11-26', '12:00', { apagado: true }),
    L('crc', 'receita', 'corrida', 1500000, '2026-11-26', '09:00', { moeda: 'CRC' }),
    L('alug', 'despesa', 'aluguel_veiculo', 30000, '2026-11-25', '08:00'),
    L('app', 'receita', 'aplicativo', 12000, '2026-11-24', '20:00'),
    L('out', 'receita', 'corrida', 7000, '2026-10-31', '23:00'),
    L('dez', 'receita', 'corrida', 1000, '2026-12-01', '00:30'),
  ];
  igual('resumo do dia: ignora o apagado e conta a outra moeda a parte', C.resumoDoDia(lista, '2026-11-26', 'BRL'),
    { receitas: 10800, despesas: 8600, liquido: 2200, nReceitas: 3, nDespesas: 1, nCorridas: 2, outrasMoedas: 1 });
  igual('resumo do dia sem nada', C.resumoDoDia(lista, '2026-11-20', 'BRL'),
    { receitas: 0, despesas: 0, liquido: 0, nReceitas: 0, nDespesas: 0, nCorridas: 0, outrasMoedas: 0 });
  const mes = C.resumoDoMes(lista, '2026-11', 'BRL');
  igual('resumo do mes: totais so do mes e da moeda (31/10 e 01/12 ficam fora)',
    [mes.receitas, mes.despesas, mes.liquido, mes.nReceitas, mes.nDespesas, mes.nCorridas, mes.outrasMoedas], [22800, 38600, -15800, 4, 2, 2, 1]);
  igual('dias trabalhados: so os dias com receita (o do aluguel nao conta)', mes.diasTrabalhados, 2);
  igual('media por dia: liquido dividido pelos dias trabalhados', mes.mediaPorDia, -7900);
  igual('por dia: do mais recente para o mais antigo, com o liquido de cada um',
    mes.porDia.map((d) => [d.data, d.receitas, d.despesas, d.liquido]),
    [['2026-11-26', 10800, 8600, 2200], ['2026-11-25', 0, 30000, -30000], ['2026-11-24', 12000, 0, 12000]]);
  igual('por categoria: do maior para o menor, com a parte do total',
    [mes.porCategoria.receita.map((c) => [c.categoria, c.rotulo, c.centavos, c.n]), mes.porCategoria.despesa.map((c) => [c.categoria, c.centavos])],
    [[['aplicativo', 'Aplicativo', 12000, 1], ['corrida', 'Corrida', 10300, 2], ['gorjeta', 'Gorjeta', 500, 1]],
      [['aluguel_veiculo', 30000], ['combustivel', 8600]]]);
  ok('por categoria: as partes somam 1', Math.abs(mes.porCategoria.receita.reduce((t, c) => t + c.parte, 0) - 1) < 1e-9);
  igual('por categoria: empate de valor segue a ordem da tela',
    C.resumoDoMes([L('a', 'despesa', 'seguro', 100, '2026-11-02'), L('b', 'despesa', 'combustivel', 100, '2026-11-02')], '2026-11', 'BRL')
      .porCategoria.despesa.map((c) => c.categoria), ['combustivel', 'seguro']);
  const arredonda = (receitas) => C.resumoDoMes(receitas.map((v, i) => (v > 0
    ? L(`r${i}`, 'receita', 'corrida', v, `2026-11-0${i + 1}`) : L(`d${i}`, 'despesa', 'multa', -v, `2026-11-0${i + 1}`))),
  '2026-11', 'BRL').mediaPorDia;
  igual('media arredonda meio para longe do zero', [arredonda([1250, 1251]), arredonda([1000, 1, 1]), arredonda([100, 100, -2701])],
    [1251, 334, -1251]);
  igual('mes vazio', [C.resumoDoMes(lista, '2026-09', 'BRL').diasTrabalhados, C.resumoDoMes(lista, '2026-09', 'BRL').mediaPorDia,
    C.resumoDoMes(lista, '2026-09', 'BRL').porDia.length], [0, 0, 0]);
  igual('virada de mes: o 01/12 entra em dezembro', C.resumoDoMes(lista, '2026-12', 'BRL').receitas, 1000);
  igual('resumo na outra moeda conta so ela (os 6 em reais ficam a parte)',
    [C.resumoDoMes(lista, '2026-11', 'CRC').receitas, C.resumoDoMes(lista, '2026-11', 'CRC').outrasMoedas], [1500000, 6]);
  igual('lancamentos do dia: do mais recente ao mais antigo; mesma hora, o lancado por ultimo primeiro; sem hora no fim; sem apagado',
    C.doDia(lista, '2026-11-26').map((l) => l.id), ['c58', 'comb', 'c45', 'crc', 'gorj']);
  igual('lista estranha nao quebra', [C.doDia(null, '2026-11-26').length, C.resumoDoDia(undefined, '2026-11-26', 'BRL').liquido], [0, 0]);

  // ── Meta do dia ─────────────────────────────────────────────────────────
  const meta = C.progressoDaMeta(31240, 40000);
  igual('meta: quanto falta e quanto da barra', [meta.falta, meta.passou, meta.batida, Math.round(meta.fracao * 1000)], [8760, 0, false, 781]);
  igual('meta batida: barra cheia e quanto passou', C.progressoDaMeta(45000, 40000), { fracao: 1, falta: 0, passou: 5000, batida: true });
  igual('meta exata conta como batida', C.progressoDaMeta(40000, 40000), { fracao: 1, falta: 0, passou: 0, batida: true });
  igual('liquido negativo: barra vazia', C.progressoDaMeta(-500, 40000), { fracao: 0, falta: 40500, passou: 0, batida: false });
  igual('sem meta', [C.progressoDaMeta(100, 0), C.progressoDaMeta(100, null)],
    [{ fracao: 0, falta: 0, passou: 0, batida: false }, { fracao: 0, falta: 0, passou: 0, batida: false }]);
} catch (e) {
  linhas.push(`FALHA excecao: ${(e && e.stack) || e}`);
}

console.log(linhas.join('\n'));
const falhas = linhas.filter((l) => !l.startsWith('OK'));
console.log(`\n${falhas.length ? `FALHAS: ${falhas.length}` : `Tudo certo (${linhas.length} verificações).`}`);
process.exit(falhas.length ? 1 : 0);
