/* Daily Hub service worker — offline reading.
   Everything under data/ is AES-GCM ciphertext, so caching it locally leaks nothing:
   without the passphrase the cache is noise. The app shell is cached so the Hub opens
   instantly and still works with no signal, showing the last editions it saw. */
const VERSION = "dh-2026-10-15s1";
const SHELL_CACHE = "shell-" + VERSION;
const DATA_CACHE  = "data-v2";        // survives shell upgrades
const IMG_CACHE   = "img-v1";
const LIB_CACHE   = "lib-v1";        // Firebase SDK: versioned URLs never change, so the app syncs offline-launched too

const SHELL = [
  "./", "./index.html", "./manifest.webmanifest",
  "./icon-192.png", "./icon-512.png", "./apple-touch-icon.png", "./favicon.svg",
  "./sync-core.js?v=1", "./app-shell.js?v=1"
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
    const keep = new Set([SHELL_CACHE, DATA_CACHE, IMG_CACHE, LIB_CACHE]);
    const all = await caches.keys();
    const upgrading = all.some(k => k.startsWith("shell-") && k !== SHELL_CACHE);
    for(const k of all) if(!keep.has(k)) await caches.delete(k);
    await self.clients.claim();
    precacheLatest();               // pull down the newest editions for offline use
    if(upgrading){ const m = await caches.open(SHELL_CACHE); await m.put("__upgraded", new Response("1")); }   // a page's hello then checks for old tabs
  })());
});

/* Pages from before Oct 10 2026 write whole lists to Firestore and would undo the new sync's
   protections (and cannot notice an update on their own). New pages answer this ping from
   app-shell.js; any open page that does not answer is reloaded once onto the current version.
   Triggered by the first current page's hello after an upgrade, inside that message event's
   waitUntil — so the worker stays alive for it and it never delays activation or page loads. */
async function reloadOldPages(){
  try{
    const m = await caches.open(SHELL_CACHE);
    if(!(await m.match("__upgraded"))) return;
    await m.delete("__upgraded");
    const wins = await self.clients.matchAll({ type: "window" });
    if(!wins.length) return;
    const answered = new Set();
    const onMsg = ev => { if(ev.data && ev.data.type === "dh-pong" && ev.source) answered.add(ev.source.id); };
    self.addEventListener("message", onMsg);
    wins.forEach(c => { try{ c.postMessage({ type: "dh-ping" }); }catch(e){} });
    await new Promise(r => setTimeout(r, 8000));
    self.removeEventListener("message", onMsg);
    for(const c of wins){
      if(answered.has(c.id) || !c.url.startsWith(self.registration.scope)) continue;
      try{ await c.navigate(c.url); }catch(e){}
    }
  }catch(e){}
}

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
  /* Firebase SDK modules — cache-first: each URL is a fixed version, so a cold offline launch can still
     load sync and show the last data instead of nothing */
  if(url.hostname === "www.gstatic.com" && url.pathname.startsWith("/firebasejs/")){
    e.respondWith((async () => {
      const c = await caches.open(LIB_CACHE);
      const hit = await c.match(req.url);
      if(hit) return hit;
      const res = await fetch(req);
      if(res.ok) c.put(req.url, res.clone());
      return res;
    })());
    return;
  }
  if(url.origin !== self.location.origin) return;    // weather, quotes, Wikipedia API: live only

  /* the page itself — always try the network so a new deploy lands immediately */
  if(isHTML(req, url.pathname)){
    e.respondWith((async () => {
      try{
        // no-store: skip the browser's 10-minute HTTP cache so a new deploy reaches the phone on the very next open
        const res = await fetch(new Request(req.url, { cache: "no-store", credentials: "same-origin" }));
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

  /* our own scripts (sync-core.js, app-shell.js): network first, so the page and its scripts are never
     from two different deploys; the cached copy is the offline fallback */
  if(url.pathname.endsWith(".js")){
    e.respondWith((async () => {
      const c = await caches.open(SHELL_CACHE);
      const hit = await c.match(req);
      const net = fetch(new Request(req.url, { cache: "no-store", credentials: "same-origin" }))
        .then(res => { if(res.ok){ c.put(req, res.clone()); return res; } return hit || res; });
      if(!hit) return net.catch(() => Response.error());
      // on a crawling connection don't hold the launch hostage: after 4 s use the cached copy
      return Promise.race([net, new Promise(r => setTimeout(() => r(hit), 4000))]).catch(() => hit);
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
  if(e.data && e.data.type === "dh-hello") e.waitUntil(reloadOldPages());
  // pre-sync pages send "precache" on every resume: on a phone with only the old page open, that is
  // the moment to reload it (current pages answer the ping and are left alone)
  if(e.data === "precache" && e.waitUntil) e.waitUntil(reloadOldPages());
  if(e.data === "precache") precacheLatest();
  if(e.data === "skipWaiting") self.skipWaiting();
});
