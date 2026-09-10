/* Daily Hub service worker — offline reading.
   Everything under data/ is AES-GCM ciphertext, so caching it locally leaks nothing:
   without the passphrase the cache is noise. The app shell is cached so the Hub opens
   instantly and still works with no signal, showing the last editions it saw. */
const VERSION = "dh-2026-09-10b";
const SHELL_CACHE = "shell-" + VERSION;
const DATA_CACHE  = "data-v2";        // survives shell upgrades
const IMG_CACHE   = "img-v1";

const SHELL = [
  "./", "./index.html", "./manifest.webmanifest",
  "./icon-192.png", "./icon-512.png", "./apple-touch-icon.png", "./favicon.svg"
];

self.addEventListener("install", e => {
  e.waitUntil((async () => {
    const c = await caches.open(SHELL_CACHE);
    await Promise.allSettled(SHELL.map(u => c.add(new Request(u, {cache:"reload"}))));
    self.skipWaiting();
  })());
});

self.addEventListener("activate", e => {
  e.waitUntil((async () => {
    const keep = new Set([SHELL_CACHE, DATA_CACHE, IMG_CACHE]);
    for(const k of await caches.keys()) if(!keep.has(k)) await caches.delete(k);
    await self.clients.claim();
    precacheLatest();               // pull down the newest editions for offline use
  })());
});

/* grab the most recent few of each kind so a cold offline launch still has content */
async function precacheLatest(){
  try{
    const r = await fetch("data/manifest.json?sw=" + Date.now());
    if(!r.ok) return;
    const c = await caches.open(DATA_CACHE);
    await c.put(key("data/manifest.json"), r.clone());   // clone BEFORE reading the body
    const m = await r.json();
    const urls = ["data/holdings.json.enc", "data/words.json.enc", "data/quotes.json.enc"];
    ["news","market","calendar"].forEach(k =>
      (m[k]||[]).slice(0,5).forEach(d => urls.push(`data/${k}/${d}.json.enc`)));
    await Promise.allSettled(urls.map(async u => {
      try{
        if(/\/\d{4}-\d{2}-\d{2}\.json\.enc$/.test(u) && await c.match(key(u))) return;   // dated editions: keep what we have
        const res = await fetch(u + "?sw=" + Date.now());
        if(res.ok) await c.put(key(u), res.clone());
      }catch(e){}
    }));
  }catch(e){}
}

/* cache keys ignore the app's ?t= cache-buster so one edition is stored once */
const key = u => new Request(new URL(u, self.registration.scope).pathname);

const isData = p => p.includes("/data/");
const isHTML = (req, p) => req.mode === "navigate" || p.endsWith("/") || p.endsWith("index.html");

self.addEventListener("fetch", e => {
  const req = e.request;
  if(req.method !== "GET") return;
  const url = new URL(req.url);

  /* Wikimedia story photos — cache-first, they never change */
  if(url.hostname.endsWith("wikimedia.org")){
    e.respondWith((async () => {
      const c = await caches.open(IMG_CACHE);
      const hit = await c.match(req);
      if(hit) return hit;
      try{ const res = await fetch(req); if(res.ok) c.put(req, res.clone()); return res; }
      catch(err){ return hit || Response.error(); }
    })());
    return;
  }
  if(url.origin !== self.location.origin) return;    // weather, quotes, Wikipedia API: live only

  /* the page itself — always try the network so a new deploy lands immediately */
  if(isHTML(req, url.pathname)){
    e.respondWith((async () => {
      try{
        const res = await fetch(req);
        const c = await caches.open(SHELL_CACHE);
        c.put(key("index.html"), res.clone());
        return res;
      }catch(err){
        const c = await caches.open(SHELL_CACHE);
        return (await c.match(key("index.html"))) || (await c.match("./")) || Response.error();
      }
    })());
    return;
  }

  /* data files — network first, cache as the offline fallback. (These used to be cache-first
     on the theory that "editions never change", but today's calendar day is rewritten every
     refresh, and holdings/words/quotes are rewritten in place, so a cache-first hit could be
     hours stale. The page itself only comes here when raw.githubusercontent.com failed.) */
  if(isData(url.pathname)){
    e.respondWith((async () => {
      const c = await caches.open(DATA_CACHE);
      const k = key(url.pathname);
      try{
        const res = await fetch(req);
        if(res.ok) c.put(k, res.clone());
        return res;
      }catch(err){
        const hit = await c.match(k);
        if(hit) return hit;
        return new Response(JSON.stringify({offline:true}), {status:503, headers:{"Content-Type":"application/json"}});
      }
    })());
    return;
  }

  /* everything else same-origin: cache, revalidate in the background */
  e.respondWith((async () => {
    const c = await caches.open(SHELL_CACHE);
    const hit = await c.match(req);
    const net = fetch(req).then(res => { if(res.ok) c.put(req, res.clone()); return res; }).catch(() => null);
    return hit || (await net) || Response.error();
  })());
});

self.addEventListener("message", e => {
  if(e.data === "precache") precacheLatest();
  if(e.data === "skipWaiting") self.skipWaiting();
});
