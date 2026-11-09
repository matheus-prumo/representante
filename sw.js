// App do Representante - service worker (v1.3, GitHub Pages)
// Faz o app abrir sem internet e recebe prints compartilhados do WhatsApp.
const VERSAO = '1.3';
const SHELL = 'rep-shell-' + VERSAO;
const CDN = 'rep-cdn-1';
const BASE = new URL('./', self.location).pathname; // ex.: /representante/
const SHELL_FILES = ['./', './index.html', './manifest.json', './icons/icon-192.png', './icons/icon-512.png', './icons/icon-maskable-512.png'];

self.addEventListener('install', e => {
  e.waitUntil((async () => {
    const c = await caches.open(SHELL);
    await c.addAll(SHELL_FILES);
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith('rep-shell-') && k !== SHELL) await caches.delete(k);
    await self.clients.claim();
  })());
});

function abrirIDB() {
  return new Promise((res, rej) => {
    const r = indexedDB.open('representante', 1);
    r.onupgradeneeded = () => { const d = r.result; if (!d.objectStoreNames.contains('itens')) d.createObjectStore('itens', { keyPath: 'id' }); if (!d.objectStoreNames.contains('meta')) d.createObjectStore('meta'); };
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
}

async function receberCompartilhado(req) {
  try {
    const fd = await req.formData();
    const files = fd.getAll('prints').filter(f => f && f.size);
    const db = await abrirIDB();
    await new Promise((res, rej) => { const t = db.transaction('meta', 'readwrite'); t.objectStore('meta').put(files, 'compartilhados'); t.oncomplete = res; t.onerror = () => rej(t.error); });
  } catch (err) { }
  return Response.redirect(BASE + '?compartilhado=1', 303);
}

async function redePrimeiro(req) {
  const c = await caches.open(SHELL);
  const chave = req.mode === 'navigate' ? BASE : req;
  try {
    const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 3000);
    const r = await fetch(req, { signal: ctl.signal }); clearTimeout(t);
    if (r.ok) c.put(chave, r.clone());
    return r;
  } catch (err) {
    const hit = await c.match(chave, { ignoreSearch: true });
    if (hit) return hit;
    throw err;
  }
}

async function cachePrimeiro(req) {
  const v = await caches.open(CDN);
  const hit = await v.match(req);
  if (hit) return hit;
  const r = await fetch(req);
  if (r.ok || r.type === 'opaque') v.put(req, r.clone());
  return r;
}

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method === 'POST' && url.origin === location.origin && url.pathname === BASE + 'compartilhar') { e.respondWith(receberCompartilhado(e.request)); return; }
  if (e.request.method !== 'GET') return;
  // leitor de prints (Tesseract.js) vem do jsDelivr: guarda para funcionar sem internet
  if (url.hostname === 'cdn.jsdelivr.net') { e.respondWith(cachePrimeiro(e.request)); return; }
  if (url.origin !== location.origin) return; // ponte do Termux e fontes: direto
  e.respondWith(redePrimeiro(e.request));
});
