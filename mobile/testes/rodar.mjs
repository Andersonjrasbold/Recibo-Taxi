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
fs.writeFileSync(pagina, html.replace(ancora, sonda + ancora));

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

function encerrar(codigo) {
  try { chrome.kill('SIGKILL'); } catch { /* ja saiu */ }
  servidor.close();
  fs.rmSync(tmp, { recursive: true, force: true });
  process.exit(codigo);
}
setTimeout(() => { console.log('VIGIA: o teste passou de 4 minutos'); encerrar(2); }, 240000).unref();

let alvo;
for (let i = 0; i < 80 && !alvo; i++) {
  await espera(250);
  try {
    const porta = fs.readFileSync(path.join(perfil, 'DevToolsActivePort'), 'utf8').split('\n')[0];
    const lista = await (await fetch(`http://127.0.0.1:${porta}/json/list`)).json();
    alvo = lista.find((t) => t.type === 'page');
  } catch { /* o Chrome ainda subindo */ }
}
if (!alvo) { console.log('O Chrome nao abriu a porta de depuracao.'); encerrar(2); }

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
    ['passo=6', '6-encerrada'], ['passo=7', '7-recibo-preenchido'], ['c=negado&passo=1', '8-sem-permissao'],
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
  encerrar(0);
}

await abrir('c=asserts');
let resultado;
for (let i = 0; i < 120 && !resultado; i++) {
  await espera(500);
  resultado = await avaliar("document.getElementById('resultado-teste')?.textContent");
}
if (!resultado) { console.log('A sonda nao terminou: sem resultado.'); encerrar(1); }
console.log(resultado);
const falhas = resultado.split('\n').filter((l) => !l.startsWith('OK'));
console.log(`\n${falhas.length ? `FALHAS: ${falhas.length}` : 'Tudo certo.'}`);
encerrar(falhas.length ? 1 : 0);
