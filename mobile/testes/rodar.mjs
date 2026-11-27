// Testa o fluxo da corrida no Chrome sem janela, sem simulador.
//
//   node mobile/testes/rodar.mjs                  confere o fluxo; sai com 1 se algo falhar
//   node mobile/testes/rodar.mjs fotos <pasta>    grava uma foto de cada passo
//
// Por que nao o simulador do iPhone: nesta maquina, de 8 GB, ele cai no meio
// ("server died") quando a memoria aperta. E por que nao o --virtual-time-budget
// do Chrome: com IndexedDB o tempo virtual nao anda e o Chrome nunca sai. Aqui
// o Chrome e controlado pelo protocolo de depuracao, em tempo real, com a tela
// emulada em 390 x 844, escala 2, como um iPhone — sem moldura de iframe.
//
// O que NAO e testado aqui: os plugins nativos de verdade (GPS, endereco a
// partir da posicao, abrir o Waze). A sonda os simula; o teste deles e no
// aparelho, pelo TestFlight e pelo teste fechado do Google Play.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// fileURLToPath, e nao .pathname: o caminho do projeto tem espaco ("Recibo Taxi").
const AQUI = path.dirname(fileURLToPath(import.meta.url));
const WWW = path.join(AQUI, '..', 'www');
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const [modo = 'asserts', pastaFotos = path.join(process.cwd(), 'fotos-corrida')] = process.argv.slice(2);
const espera = (ms) => new Promise((ok) => setTimeout(ok, ms));

// 1. Copia do www/ com a sonda antes do config.js. O www/ de verdade nao e tocado.
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'recibo-teste-'));
const web = path.join(tmp, 'web');
fs.cpSync(WWW, web, { recursive: true });
const pagina = path.join(web, 'index.html');
const ancora = '<script src="config.js"></script>';
const html = fs.readFileSync(pagina, 'utf8');
if (!html.includes(ancora)) throw new Error('nao achei o config.js no index.html');
const sonda = fs.readFileSync(path.join(AQUI, 'sonda-corrida.html'), 'utf8');
// Com funcao, e nao com texto: no texto de troca, "$'" quer dizer "o resto da
// pagina" — um 'R$' num teste colava os scripts do app duas vezes.
fs.writeFileSync(pagina, html.replace(ancora, () => sonda + ancora));

// 2. Servidor em localhost: em file:// o IndexedDB e o localStorage nao valem.
const TIPOS = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.json': 'application/json', '.svg': 'image/svg+xml',
};
const servidor = http.createServer((req, res) => {
  const arquivo = path.join(web, decodeURIComponent(new URL(req.url, 'http://x').pathname));
  if (!arquivo.startsWith(web) || !fs.existsSync(arquivo) || fs.statSync(arquivo).isDirectory()) {
    res.writeHead(404); res.end(); return;
  }
  res.writeHead(200, { 'Content-Type': TIPOS[path.extname(arquivo)] || 'application/octet-stream' });
  fs.createReadStream(arquivo).pipe(res);
});
await new Promise((ok) => servidor.listen(0, '127.0.0.1', ok));
const BASE = `http://127.0.0.1:${servidor.address().port}/index.html`;

// 3. Chrome sem janela e sem chaveiro do macOS (perfil novo pediria acesso a
//    ele e ficaria esperando). A porta de depuracao e escolhida pelo Chrome.
const perfil = path.join(tmp, 'perfil');
const chrome = spawn(CHROME, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--use-mock-keychain', '--password-store=basic', '--disable-extensions',
  `--user-data-dir=${perfil}`, '--remote-debugging-port=0', 'about:blank',
], { stdio: 'ignore' });

// Espera o Chrome sair de verdade antes de apagar o perfil: matar e apagar
// na mesma hora dava ENOTEMPTY, com o Chrome ainda gravando na pasta.
async function encerrar(codigo) {
  const saiu = new Promise((ok) => { chrome.once('exit', ok); setTimeout(ok, 3000); });
  try { chrome.kill('SIGKILL'); } catch { /* ja saiu */ }
  await saiu;
  servidor.close();
  try { fs.rmSync(tmp, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 }); } catch { /* temporario */ }
  process.exit(codigo);
}
// A parte de rede tem esperas de verdade (fila lenta de 9 s no Salvar do perfil;
// sessao lenta e prazo de 5 s na compra pela vitrine), e a do caixa tambem (o
// "apagar" armado por 4 s, a confirmacao do inicio por 5 s, o envio em 2 s).
setTimeout(() => { console.log('VIGIA: o teste passou de 9 minutos'); encerrar(2); }, 540000).unref();

let alvo;
for (let i = 0; i < 80 && !alvo; i++) {
  await espera(250);
  try {
    const porta = fs.readFileSync(path.join(perfil, 'DevToolsActivePort'), 'utf8').split('\n')[0];
    const lista = await (await fetch(`http://127.0.0.1:${porta}/json/list`)).json();
    alvo = lista.find((t) => t.type === 'page');
  } catch { /* o Chrome ainda subindo */ }
}
if (!alvo) { console.log('O Chrome nao abriu a porta de depuracao.'); await encerrar(2); }

const ws = new WebSocket(alvo.webSocketDebuggerUrl);
await new Promise((ok, falha) => { ws.onopen = ok; ws.onerror = falha; });
let seq = 0;
const esperando = new Map();
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && esperando.has(d.id)) { esperando.get(d.id)(d); esperando.delete(d.id); }
};
const cdp = (method, params = {}) => new Promise((ok) => {
  const n = ++seq;
  esperando.set(n, ok);
  ws.send(JSON.stringify({ id: n, method, params }));
});
const avaliar = async (expr) =>
  (await cdp('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }))
    .result?.result?.value;

await cdp('Page.enable');
await cdp('Runtime.enable');
await cdp('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });

// O mesmo endereco com outro #hash nao recarrega: passa pelo about:blank antes.
async function abrir(hash) {
  await cdp('Page.navigate', { url: 'about:blank' });
  await espera(300);
  await cdp('Page.navigate', { url: `${BASE}#${hash}` });
}

if (modo === 'fotos') {
  fs.mkdirSync(pastaFotos, { recursive: true });
  const casos = [
    ['passo=0', '0-inicio'], ['passo=1', '1-corrida-nova'], ['passo=2', '2-tarifa'],
    ['passo=3', '3-estimativa'], ['passo=4', '4-em-andamento'], ['passo=5', '5-inicio-com-corrida'],
    ['passo=6', '6-encerrada'], ['passo=7', '7-recibo-preenchido'], ['passo=8', '8-recibo-com-qr'],
    ['c=negado&passo=1', '9-sem-permissao'],
    // Tela de assinatura com os precos de 2026-11 (captura para a revisao da
    // assinatura na App Store Connect; a loja nao responde no Chrome, entao a
    // sonda poe os pacotes na mao).
    ['c=pro', '10-assinatura'],
    // Vitrine dos planos (plano Gratis), em tela cheia: ao abrir o app, depois de
    // gerar o recibo, e o recibo depois do "Agora não".
    ['c=vitrine', '11-vitrine-abertura'], ['c=vitrine-recibo', '12-vitrine-recibo'],
    ['c=vitrine-recibo-depois', '13-recibo-depois-da-vitrine'],
    // Caixa (1.8). Os valores abrem cobertos (o olho): as telas do Caixa vem
    // com o olho aberto, para revisar o desenho com os numeros: o dia vazio, o
    // dia com lancamentos e meta (com o "+ Lançamento"), o mes anterior no
    // Gratis (travado) e no Pro, o dia travado, a receita de um recibo (com
    // "Mais detalhes" aberto), o mes corrente e o campo da meta aberto. E
    // mais: o "Novo lançamento" trocado para Despesa (combustivel com
    // litros), o fim da corrida com o valor, o inicio com o cartao em
    // primeiro, coberto, com a meta (como abre), o inicio sem lancamento (com
    // "Definir meta do dia"), o inicio com o olho aberto e a confirmacao
    // "Corrida lançada", o dia e o mes cobertos, e o "Novo lançamento" como
    // abre (Receita e "Corrida").
    ['c=caixa&passo=0', '14-caixa-vazio'], ['c=caixa&passo=1', '15-caixa-dia'],
    ['c=caixa&passo=2', '16-lancamento-despesa'], ['c=caixa&passo=3', '17-fim-da-corrida'],
    ['c=caixa&passo=4', '18-mes-gratis-bloqueado'], ['c=caixa&passo=5', '19-mes-pro'],
    ['c=caixa&passo=6', '20-inicio-com-caixa'], ['c=caixa&passo=7', '21-dia-gratis-bloqueado'],
    ['c=caixa&passo=8', '22-lancamento-do-recibo'], ['c=caixa&passo=9', '23-mes-atual'],
    ['c=caixa&passo=10', '24-caixa-meta'], ['c=caixa&passo=11', '25-inicio-sem-lancamento'],
    ['c=caixa&passo=12', '26-inicio-olho-aberto'], ['c=caixa&passo=13', '27-caixa-coberto'],
    ['c=caixa&passo=14', '28-mes-coberto'], ['c=caixa&passo=15', '29-lancamento-novo'],
  ];
  for (const [hash, nome] of casos) {
    await abrir(hash);
    await espera(4500);
    const altura = await avaliar('Math.max(document.documentElement.scrollHeight, 844)');
    const foto = await cdp('Page.captureScreenshot', {
      format: 'png', captureBeyondViewport: true,
      clip: { x: 0, y: 0, width: 390, height: Math.min(altura, 2400), scale: 1 },
    });
    fs.writeFileSync(path.join(pastaFotos, `${nome}.png`), Buffer.from(foto.result.data, 'base64'));
    console.log('foto', nome);
  }
  await encerrar(0);
}

// Tres modos, nesta ordem: o fluxo (sem sessao, nada sobe), o envio com o
// servidor simulado e as telas do caixa (1.8). O IndexedDB e o mesmo nos tres;
// o modo rede limpa os recibos antes de comecar, e o do caixa, o banco inteiro.
// Depois, o menu do perfil em telas baixas, cada uma no seu tamanho: o iPhone
// SE/8 (375 x 647 uteis) e um Android pequeno, com o e-mail em duas linhas.
const partes = [];
for (const [hash, nome, [largura, altura] = [390, 844]] of [['c=asserts', 'fluxo'], ['c=rede', 'rede simulada'], ['c=caixa', 'caixa'],
  ['c=menu&sem_rolar=1', 'menu no iPhone SE', [375, 647]], ['c=menu&email=longo', 'menu em tela pequena', [360, 568]]]) {
  await cdp('Emulation.setDeviceMetricsOverride', { width: largura, height: altura, deviceScaleFactor: 2, mobile: true });
  await abrir(hash);
  let parte;
  for (let i = 0; i < 480 && !parte; i++) {   // ate 240 s por parte
    await espera(500);
    parte = await avaliar("document.getElementById('resultado-teste')?.textContent");
  }
  if (!parte) { console.log(`A sonda nao terminou (${nome}): sem resultado.`); await encerrar(1); }
  partes.push(parte);
}
const resultado = partes.join('\n');
console.log(resultado);
const falhas = resultado.split('\n').filter((l) => !l.startsWith('OK'));
console.log(`\n${falhas.length ? `FALHAS: ${falhas.length}` : 'Tudo certo.'}`);
await encerrar(falhas.length ? 1 : 0);
