/* Gerador de PDF mínimo, só o que um recibo precisa.
 *
 * Por que à mão em vez de jsPDF: o app tem que funcionar offline, então a
 * biblioteca teria de vir empacotada — são ~300 KB para desenhar dez linhas
 * de texto. PDF 1.4 com uma página e fonte padrão cabe em 120 linhas.
 *
 * Helvetica usa WinAnsiEncoding, que cobre o português (á, ç, ã, õ).
 */

function paraWinAnsi(texto) {
  // Fora do WinAnsi vira '?' — melhor um caractere trocado que um PDF quebrado.
  let saida = '';
  for (const ch of String(texto)) {
    const c = ch.codePointAt(0);
    saida += c <= 0xff ? ch : '?';
  }
  return saida;
}

function escaparPdf(texto) {
  return paraWinAnsi(texto).replace(/\\/g, '\\\\').replace(/\(/g, '\\(').replace(/\)/g, '\\)');
}

function bytesLatin1(str) {
  const out = new Uint8Array(str.length);
  for (let i = 0; i < str.length; i++) out[i] = str.charCodeAt(i) & 0xff;
  return out;
}

/** Monta o PDF de um recibo e devolve base64. */
function pdfDoRecibo(recibo) {
  const d = recibo.dados, m = recibo.motorista || {};
  const L = 56;              // margem esquerda
  let y = 780;               // A4 tem 842 de altura
  const linhas = [];

  const texto = (t, { fonte = 'F1', tam = 11, dx = 0, dy = 18 } = {}) => {
    y -= dy;
    linhas.push(`BT /${fonte} ${tam} Tf ${L + dx} ${y} Td (${escaparPdf(t)}) Tj ET`);
  };
  const regua = () => { y -= 10; linhas.push(`${L} ${y} m 539 ${y} l S`); };

  texto('RECIBO DE CORRIDA', { fonte: 'F2', tam: 18, dy: 10 });
  texto(`#${recibo.rid}`, { tam: 9, dy: 16 });
  regua();

  texto(`R$ ${d.valor_exibido}`, { fonte: 'F2', tam: 26, dy: 34 });
  regua();

  const campos = [
    ['Passageiro', d.passageiro],
    // Opcional: quem viaja a trabalho precisa do documento para prestar contas.
    ...(d.documento_passageiro ? [['CPF/CNPJ', d.documento_passageiro]] : []),
    ['Data', d.data_exibida || d.data],
    ['Hora', d.hora || '-'],
    ['Origem', d.origem],
    ['Destino', d.destino],
    ['Pagamento', d.forma_pagamento],
  ];
  if (d.observacoes) campos.push(['Observações', d.observacoes]);

  y -= 6;
  for (const [rotulo, valor] of campos) {
    texto(rotulo.toUpperCase(), { fonte: 'F2', tam: 8, dy: 20 });
    texto(String(valor || '-'), { tam: 12, dy: 14 });
  }

  regua();
  texto('MOTORISTA', { fonte: 'F2', tam: 8, dy: 22 });
  texto(m.full_name || '-', { tam: 12, dy: 14 });
  texto(`Placa ${m.plate || '-'}${m.city ? ' - ' + m.city : ''}`, { tam: 10, dy: 14 });
  if (m.license_number) texto(`Alvará ${m.license_number}`, { tam: 10, dy: 13 });
  if (m.whatsapp) texto(`WhatsApp ${m.whatsapp}`, { tam: 10, dy: 13 });

  // O recibo circula entre passageiros; o telefone impresso traz corrida nova.
  if (m.whatsapp) {
    regua();
    texto('Precisou de corrida? Me chame no WhatsApp:', { fonte: 'F2', tam: 10, dy: 22 });
    texto(m.whatsapp, { fonte: 'F2', tam: 14, dy: 17 });
  }

  y = 70;
  texto('Este recibo não é documento fiscal.', { tam: 8, dy: 0 });

  const fluxo = linhas.join('\n');

  const objetos = [
    '<</Type/Catalog/Pages 2 0 R>>',
    '<</Type/Pages/Kids[3 0 R]/Count 1>>',
    '<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]' +
      '/Resources<</Font<</F1 4 0 R/F2 5 0 R>>>>/Contents 6 0 R>>',
    '<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>',
    '<</Type/Font/Subtype/Type1/BaseFont/Helvetica-Bold/Encoding/WinAnsiEncoding>>',
    `<</Length ${fluxo.length}>>\nstream\n${fluxo}\nendstream`,
  ];

  let pdf = '%PDF-1.4\n';
  const offsets = [];
  objetos.forEach((corpo, i) => {
    offsets.push(pdf.length);
    pdf += `${i + 1} 0 obj\n${corpo}\nendobj\n`;
  });

  const inicioXref = pdf.length;
  pdf += `xref\n0 ${objetos.length + 1}\n0000000000 65535 f \n`;
  offsets.forEach((o) => { pdf += String(o).padStart(10, '0') + ' 00000 n \n'; });
  pdf += `trailer\n<</Size ${objetos.length + 1}/Root 1 0 R>>\nstartxref\n${inicioXref}\n%%EOF`;

  const bytes = bytesLatin1(pdf);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

if (typeof module !== 'undefined') module.exports = { pdfDoRecibo };
