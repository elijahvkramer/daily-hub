// End-to-end test of the REAL inline sync + tracker modules from index.html, run in Node against a
// fake Firestore (transactions, snapshots, offline). Two "devices" share one fake server.
// Run: node scripts/test_sync_page.js
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm"), assert = require("assert");
const ROOT = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const core = fs.readFileSync(path.join(ROOT, "sync-core.js"), "utf8");
const mods = [...html.matchAll(/<script type="module">([\s\S]*?)<\/script>/g)].map(m => m[1]);
const fbMod = mods.find(m => m.includes("window.__todosApi = {"));
const rxMod = mods.find(m => m.includes("Treatment dose tracker"));
assert.ok(fbMod && rxMod, "found both modules");

/* ---------------- fake Firestore ---------------- */
const SERVER = { docs: {}, ver: {}, retries: 0, listeners: [] };
const clone = v => v === undefined ? undefined : JSON.parse(JSON.stringify(v));
function serverWrite(id, data){ SERVER.docs[id] = data; SERVER.ver[id] = (SERVER.ver[id] || 0) + 1; SERVER.listeners.forEach(l => l.id === id && l.dev.online && l.fire()); }
function makeFirebase(dev){
  const snapOf = id => ({ exists: () => SERVER.docs[id] !== undefined, data: () => clone(SERVER.docs[id]), metadata: { fromCache: !dev.online } });
  const F = {
    initializeFirestore: () => ({}), getFirestore: () => ({}),
    persistentLocalCache: () => ({}), persistentMultipleTabManager: () => ({}),
    doc: (db, c, d) => ({ id: c + "/" + d }),
    onSnapshot(ref, next){
      const l = { id: ref.id, dev, fire: () => setImmediate(() => next(snapOf(ref.id))) };
      SERVER.listeners.push(l); l.fire(); return () => {};
    },
    async runTransaction(db, fn){
      if(!dev.online){ const e = new Error("offline"); e.code = "unavailable"; throw e; }
      if(dev.denied){ const e = new Error("denied"); e.code = "permission-denied"; throw e; }
      // optimistic concurrency like Firestore: if a doc read in the transaction changed before commit, rerun
      for(let attempt = 0; attempt < 5; attempt++){
        const writes = [], seen = {};
        const tr = {
          get: async ref => { seen[ref.id] = SERVER.ver[ref.id] || 0; await new Promise(r => dev.txDelay ? setTimeout(r, dev.txDelay) : setImmediate(r)); return snapOf(ref.id); },
          update: (ref, d) => writes.push(() => serverWrite(ref.id, { ...SERVER.docs[ref.id], ...clone(d) })),
          set: (ref, d) => writes.push(() => serverWrite(ref.id, clone(d)))
        };
        await fn(tr);
        if(Object.keys(seen).some(id => (SERVER.ver[id] || 0) !== seen[id])){ SERVER.retries++; continue; }
        dev.commits++;
        writes.forEach(w => w());
        return;
      }
      const e = new Error("contention"); e.code = "aborted"; throw e;
    }
  };
  const AU = {
    getAuth: () => ({ currentUser: null }),
    onAuthStateChanged: (auth, cb) => { dev.authCb = cb; setImmediate(() => cb(null)); },
    signInAnonymously: async auth => { auth.currentUser = { uid: "anon-" + dev.name }; setImmediate(() => dev.authCb(auth.currentUser)); return auth.currentUser; }
  };
  const A = { initializeApp: () => ({}) };
  return { A, AU, F };
}

/* ---------------- a device = its own window/localStorage running the real modules ---------------- */
class Store { constructor(){ this.m = new Map(); } get length(){ return this.m.size; } key(i){ return [...this.m.keys()][i]; }
  getItem(k){ return this.m.has(k) ? this.m.get(k) : null; } setItem(k, v){ this.m.set(k, String(v)); } removeItem(k){ this.m.delete(k); } }
// Web Locks: one manager per browser (tabs sharing a store share it)
const LOCKS = new Map();
function lockManager(){
  const chains = new Map();
  return { request(name, fn){
    const prev = chains.get(name) || Promise.resolve();
    const run = prev.then(() => fn());
    chains.set(name, run.catch(() => {}));
    return run;
  } };
}
function device(name, seedLocal, sharedStore){
  const dev = { name, online: true, commits: 0, logs: [], errors: [], txDelay: 0 };
  const store = sharedStore || new Store(); Object.entries(seedLocal || {}).forEach(([k, v]) => store.setItem(k, v));
  if(!LOCKS.has(store)) LOCKS.set(store, lockManager());
  const listeners = {};
  const win = {
    localStorage: store, navigator: { onLine: true, locks: LOCKS.get(store) }, setTimeout, clearTimeout, setInterval: () => 0, setImmediate,
    console: { log: () => {}, warn: (...a) => dev.logs.push(a.join(" ")), error: (...a) => dev.errors.push(a.map(String).join(" ")) },
    addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); },
    document: { getElementById: () => null, addEventListener: () => {}, hidden: false, visibilityState: "visible", title: "Daily Hub", documentElement: { style: {} } },
    Date, JSON, Math, Promise, Object, Array, Set, Map, String, Number, Error, isFinite, Intl
  };
  win.window = win; win.globalThis = win;
  Object.defineProperty(win.navigator, "onLine", { get: () => dev.online });
  dev.win = win; dev.store = store;
  dev.goOnline = () => { dev.online = true; (listeners.online || []).forEach(f => f()); };
  const ctx = vm.createContext(win);
  vm.runInContext(core, ctx);
  const fb = makeFirebase(dev);
  ctx.__fakeImports = [fb.A, fb.AU, fb.F];
  // the real module text, with its three CDN imports pointed at the fake
  const src = fbMod.replace(/await Promise\.all\(\[[\s\S]*?\]\);/, "await Promise.resolve(globalThis.__fakeImports);");
  assert.notStrictEqual(src, fbMod, "import shim applied");
  vm.runInContext("(async () => {" + src + "\n})().catch(e => console.error('MODULE', e && e.stack));", ctx);
  vm.runInContext("(async () => {" + rxMod + "\n})().catch(e => console.error('RXMODULE', e && e.stack));", ctx);
  return dev;
}
const tick = (ms = 30) => new Promise(r => setTimeout(r, ms));
async function settle(){ for(let i = 0; i < 8; i++) await tick(15); }

(async () => {
  SERVER.docs["todos/main"] = { items: [{ id: "a", text: "Alpha", kind: "urgent", list: "personal", done: false }], history: [],
    backupPreMerge: { keep: true }, protocol: { entries: { "2026-10-10": { om: [100] } } } };
  SERVER.docs["friends/main"] = { people: [{ id: "p1", name: "Hayden", freqDays: 21, lastContact: null, history: [] }] };

  // the phone carries a local-only chip, a local hide, and an unsynced dose from the old code
  const phone = device("phone", {
    "dh-now-ov": JSON.stringify({ added: [{ id: "loc:x", label: "Taxes", date: "2027-04-15", kind: "deadline" }], hidden: ["cal:2026-11-01:drill"] }),
    "dh_rx_v1": JSON.stringify({ entries: { "2026-10-10": { om: [100, 555] } } }),
    "dh_rx_dirty_v1": JSON.stringify({ "2026-10-10|om": 1 }),
    "dh_rx_migrated_v1": "1"
  });
  const mac = device("mac");
  await settle();
  const P = phone.win, M = mac.win;
  assert.ok(P.__todosApi.isReady() && M.__todosApi.isReady(), "both ready");

  // the old unsynced dose was handed off to the cloud
  assert.deepStrictEqual(SERVER.docs["todos/main"].protocol.entries["2026-10-10"].om, [100, 555], "legacy dose handed off");
  console.log("ok legacy dose hand-off");

  // simultaneous edits on both devices: both survive
  P.__todosApi.toggle("a");
  M.__todosApi.add("From mac", "chill", "work");
  await settle();
  const items = SERVER.docs["todos/main"].items;
  assert.strictEqual(items.find(t => t.id === "a").done, true);
  assert.ok(items.some(t => t.text === "From mac"));
  assert.deepStrictEqual(M.__todosApi.getState().items.map(t => t.text), P.__todosApi.getState().items.map(t => t.text), "both devices show the same list");
  assert.deepStrictEqual(SERVER.docs["todos/main"].backupPreMerge, { keep: true }, "backup kept");
  assert.ok(SERVER.docs["todos/main"].protocol, "protocol kept");
  console.log("ok concurrent to-do edits + other fields untouched");

  // chips: phone's local chip/hide imported once the strip renders; then add on mac shows on phone
  P.__nowApi.importLocal([{ id: "loc:x", label: "Taxes", date: "2027-04-15", kind: "deadline" }], ["cal:2026-11-01:drill"]);
  M.__nowApi.add("Wedding", "2027-05-01", "countdown");
  await settle();
  assert.deepStrictEqual(P.__nowApi.getState().chips.map(c => c.label).sort(), ["Taxes", "Wedding"]);
  assert.ok(M.__nowApi.getState().hidden.includes("cal:2026-11-01:drill"));
  const wid = M.__nowApi.getState().chips.find(c => c.label === "Wedding").id;
  P.__nowApi.remove(wid); await settle();
  assert.ok(!M.__nowApi.getState().chips.some(c => c.id === wid), "chip removal synced");
  console.log("ok countdown chips sync both ways");

  // offline phone: edits show immediately, survive, flush on reconnect; mac's edits meanwhile kept
  phone.online = false;
  P.__todosApi.add("Offline item", "urgent", "personal");
  P.__protocolApi.add("2026-10-10", "mt", 777);
  P.__friendsApi.logContact("p1", "", "2026-10-09T15:00:00.000Z");
  assert.ok(P.__todosApi.getState().items.some(t => t.text === "Offline item"), "pending add visible offline");
  M.__protocolApi.add("2026-10-10", "mt", 700);
  M.__todosApi.setNote("a", "note from mac");
  await settle();
  assert.ok(!SERVER.docs["todos/main"].items.some(t => t.text === "Offline item"), "not on server while offline");
  assert.ok(P.__syncStatus().pending >= 3, "pending counted");
  phone.goOnline(); await settle();
  const t = SERVER.docs["todos/main"];
  assert.ok(t.items.some(x => x.text === "Offline item"));
  assert.strictEqual(t.items.find(x => x.id === "a").note, "note from mac");
  assert.deepStrictEqual(t.protocol.entries["2026-10-10"].mt, [700, 777]);
  assert.strictEqual(SERVER.docs["friends/main"].people[0].lastContact, "2026-10-09T15:00:00.000Z");
  assert.strictEqual(P.__syncStatus().pending, 0, "outbox drained");
  console.log("ok offline queue reconciles without loss");

  // offline + app killed: a fresh page on the same storage still has the op and flushes it
  phone.online = false;
  P.__todosApi.add("Survives restart", "urgent", "personal");
  const saved = new Map(phone.store.m);
  const phone2 = device("phone2", Object.fromEntries(saved));
  phone2.online = true; await settle();
  assert.ok(SERVER.docs["todos/main"].items.some(x => x.text === "Survives restart"), "op survived app kill");
  console.log("ok outbox survives app restart");

  // restore (undo delete) on two devices -> one item
  const fm = M.__todosApi.getState().items.find(x => x.text === "From mac");
  M.__todosApi.remove(fm.id); await settle();
  P.__todosApi.restore(fm.id); M.__todosApi.restore(fm.id); await settle();
  assert.strictEqual(SERVER.docs["todos/main"].items.filter(x => x.text === "From mac").length, 1);
  console.log("ok delete + double restore -> one item");

  // a writer elsewhere drops `now` and `protocol` from the doc: self-heal puts them back
  const before = clone(SERVER.docs["todos/main"]);
  serverWrite("todos/main", { items: before.items, history: before.history });
  await settle();
  assert.ok(SERVER.docs["todos/main"].protocol && SERVER.docs["todos/main"].now, "fields restored");
  assert.deepStrictEqual(SERVER.docs["todos/main"].protocol.entries["2026-10-10"].mt, [700, 777]);
  console.log("ok self-heal of dropped fields");

  // permission denied: nothing lost, error logged once, ops kept
  mac.denied = true;
  M.__todosApi.add("Denied", "urgent", "personal"); await settle();
  assert.ok(M.__todosApi.getState().items.some(x => x.text === "Denied"), "still shown locally");
  assert.ok(M.__syncStatus().pending > 0);
  mac.denied = false; M.__todosApi.setText("a", "Alpha!"); await tick(2200); await settle();
  assert.ok(SERVER.docs["todos/main"].items.some(x => x.text === "Denied"), "flushed after recovery");
  console.log("ok permission error keeps edits and recovers");

  // reorder while another device adds
  const ids = P.__todosApi.getState().items.map(x => x.id).reverse();
  M.__todosApi.add("Mid-drag", "urgent", "personal");
  P.__todosApi.reorder(ids); await settle();
  assert.ok(SERVER.docs["todos/main"].items.some(x => x.text === "Mid-drag"), "mid-drag add kept");
  console.log("ok reorder keeps concurrent add");

  // two tabs share one outbox: a slow tab must never re-commit Take after the fast tab committed Undo
  const shared = new Store();
  const tabA = device("tabA", { dh_rx_handoff_v1: "1" }, shared), tabB = device("tabB", { dh_rx_handoff_v1: "1" }, shared);
  await settle();
  tabB.txDelay = 150;
  tabA.win.__protocolApi.add("2026-10-12", "om", 5);
  tabB.goOnline();
  await tick(60);
  tabA.win.__protocolApi.remove("2026-10-12", "om", 5);
  await tick(500); await settle();
  assert.deepStrictEqual(SERVER.docs["todos/main"].protocol.entries["2026-10-12"].om, [], "Undo survives a slow second tab");
  console.log("ok two tabs: no out-of-order re-commit");

  // offline device with queued edits + an old page drops protocol/now: reconnect restores, then applies
  const q = device("q", { dh_rx_handoff_v1: "1" });
  await settle();
  const chipsBefore = clone(SERVER.docs["todos/main"].now.chips).length;
  q.online = false;
  q.win.__protocolApi.add("2026-10-12", "pb", 4242);
  q.win.__nowApi.add("Offline chip", "2027-03-01", "countdown");
  const keep = SERVER.docs["todos/main"];
  serverWrite("todos/main", { items: keep.items, history: keep.history });
  q.goOnline(); await tick(300); await settle();
  const after = SERVER.docs["todos/main"];
  assert.deepStrictEqual(after.protocol.entries["2026-10-10"].mt, [700, 777], "tracker history kept");
  assert.ok(after.protocol.entries["2026-10-12"].pb.includes(4242));
  assert.strictEqual(after.now.chips.length, chipsBefore + 1, "old chips kept + new one");
  console.log("ok reconnect after a field-dropping writer: restored, then queued edits applied");

  // legacy hand-off with several unsynced doses across days (reviewer repro)
  const h = device("h", { dh_rx_v1: JSON.stringify({ entries: { "2026-10-08": { om: [50] }, "2026-10-09": { om: [1, 60], mt: [70] } } }) });
  await settle();
  const e = SERVER.docs["todos/main"].protocol.entries;
  assert.ok(e["2026-10-08"].om.includes(50) && e["2026-10-09"].om.includes(60) && e["2026-10-09"].mt.includes(70), "every legacy dose handed off");
  console.log("ok legacy hand-off keeps every dose");

  for(const d of [phone, mac, phone2]){
    const bad = d.errors.filter(e => !/sync write failed/.test(e));
    assert.deepStrictEqual(bad, [], d.name + " unexpected console errors: " + bad.join("\n"));
  }
  console.log("\nall page-level sync tests passed (" + SERVER.retries + " transaction retries exercised)");
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
