// Tests for sync-core.js: merge, undo, offline queue, concurrent edits across two devices.
// Run: node scripts/test_sync.js
"use strict";
const path = require("path");
const assert = require("assert");
require(path.join(__dirname, "..", "sync-core.js"));
const S = globalThis.DHSync;

class Mem { constructor(){ this.m = new Map(); } get length(){ return this.m.size; } key(i){ return [...this.m.keys()][i]; }
  getItem(k){ return this.m.has(k) ? this.m.get(k) : null; } setItem(k, v){ this.m.set(k, String(v)); } removeItem(k){ this.m.delete(k); } }

// a fake Firestore doc store with transactional semantics like the real flusher in index.html
class Server {
  constructor(docs){ this.docs = JSON.parse(JSON.stringify(docs)); }
  // exactly what the page's flush() does inside runTransaction
  commit(docName, ops){
    const before = this.docs[docName];
    const after = S.project(docName, before, ops);
    const changed = S.changedFields(before || {}, after);
    this.docs[docName] = { ...(before || {}), ...JSON.parse(JSON.stringify(changed)) };
    return changed;
  }
}
class Device {
  constructor(server){ this.server = server; this.ob = S.makeOutbox(new Mem()); this.online = true; }
  do(docName, op){ this.ob.add(docName, op); }
  view(docName){ return S.project(docName, this.server.docs[docName], this.ob.list(docName)); }
  flush(docName){
    if(!this.online) return false;
    const ops = this.ob.list(docName); if(!ops.length) return true;
    this.server.commit(docName, ops);
    this.ob.remove(ops.map(o => o.oid));
    return true;
  }
}
const base = () => ({
  todos: { items: [ { id: "a", text: "Alpha", kind: "urgent", list: "personal", done: false },
                    { id: "b", text: "Bravo", kind: "chill",  list: "work",     done: false },
                    { id: "c", text: "Charlie", kind: "urgent", list: "personal", done: false } ],
           history: [], backupPreMerge: { keep: 1 },
           protocol: { entries: { "2026-10-10": { om: [100], mt: [200] } } } },
  friends: { people: [ { id: "p1", name: "Hayden", freqDays: 21, lastContact: "2026-10-01T00:00:00.000Z", history: [] } ] }
});
let n = 0; const test = (name, fn) => { fn(); n++; console.log("ok", name); };

test("concurrent edits to different items both survive (old code lost one)", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  phone.do("todos", { t: "todo.patch", itemId: "a", set: { done: true } });
  mac.do("todos", { t: "todo.patch", itemId: "b", set: { text: "Bravo 2" } });
  phone.flush("todos"); mac.flush("todos");
  const it = srv.docs.todos.items;
  assert.strictEqual(it.find(t => t.id === "a").done, true);
  assert.strictEqual(it.find(t => t.id === "b").text, "Bravo 2");
});

test("add on phone while computer deletes another: nothing lost", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  phone.do("todos", { t: "todo.add", item: { id: "n1", text: "New", kind: "urgent", list: "personal", done: false } });
  mac.do("todos", { t: "todo.remove", itemId: "c", at: new Date().toISOString() });
  mac.flush("todos"); phone.flush("todos");
  const ids = srv.docs.todos.items.map(t => t.id);
  assert.deepStrictEqual(ids, ["a", "b", "n1"]);
  assert.strictEqual(srv.docs.todos.history[0].id, "c");
});

test("to-do edits never touch protocol / now / backupPreMerge", () => {
  const srv = new Server(base()); const d = new Device(srv);
  d.do("todos", { t: "todo.patch", itemId: "a", set: { note: "x" } });
  const changed = srv.commit("todos", d.ob.list("todos"));
  assert.deepStrictEqual(Object.keys(changed), ["items"]);
  assert.deepStrictEqual(srv.docs.todos.protocol.entries["2026-10-10"].om, [100]);
  assert.deepStrictEqual(srv.docs.todos.backupPreMerge, { keep: 1 });
});

test("offline queue: ops survive, show on screen, flush later, other device's edits kept", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  phone.online = false;
  phone.do("todos", { t: "todo.add", item: { id: "off1", text: "Offline", kind: "chill", list: "personal", done: false } });
  phone.do("todos", { t: "dose.add", day: "2026-10-10", k: "om", ts: 300 });
  assert.strictEqual(phone.flush("todos"), false);
  assert.ok(phone.view("todos").items.some(t => t.id === "off1"), "pending add visible");
  mac.do("todos", { t: "dose.add", day: "2026-10-10", k: "om", ts: 250 });
  mac.do("todos", { t: "todo.patch", itemId: "a", set: { text: "Alpha (mac)" } });
  mac.flush("todos");
  phone.online = true; phone.flush("todos");
  assert.deepStrictEqual(srv.docs.todos.protocol.entries["2026-10-10"].om, [100, 250, 300]);
  assert.strictEqual(srv.docs.todos.items.find(t => t.id === "a").text, "Alpha (mac)");
  assert.ok(srv.docs.todos.items.some(t => t.id === "off1"));
  assert.strictEqual(phone.ob.count(), 0);
});

test("replay after lost ack is idempotent (no duplicates)", () => {
  const srv = new Server(base()); const d = new Device(srv);
  d.do("todos", { t: "todo.add", item: { id: "z", text: "Z", kind: "urgent", list: "personal", done: false } });
  d.do("todos", { t: "dose.add", day: "2026-10-11", k: "pb", ts: 5 });
  d.do("todos", { t: "todo.remove", itemId: "b", at: new Date().toISOString() });
  d.do("friends", { t: "friend.log", personId: "p1", date: "2026-10-10T20:00:00.000Z", method: "text" });
  const ops = d.ob.list("todos"), fops = d.ob.list("friends");
  srv.commit("todos", ops); srv.commit("todos", ops);           // ack lost -> replayed
  srv.commit("friends", fops); srv.commit("friends", fops);
  assert.strictEqual(srv.docs.todos.items.filter(t => t.id === "z").length, 1);
  assert.deepStrictEqual(srv.docs.todos.protocol.entries["2026-10-11"].pb, [5]);
  assert.strictEqual(srv.docs.todos.history.filter(h => h.id === "b").length, 1);
  assert.strictEqual(srv.docs.friends.people[0].history.length, 1);
});

test("dose undo removes exactly that dose; two devices logging at once both count", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  phone.do("todos", { t: "dose.add", day: "2026-10-10", k: "mt", ts: 1001 });
  mac.do("todos", { t: "dose.add", day: "2026-10-10", k: "mt", ts: 1002 });
  phone.flush("todos"); mac.flush("todos");
  assert.deepStrictEqual(srv.docs.todos.protocol.entries["2026-10-10"].mt, [200, 1001, 1002]);
  phone.do("todos", { t: "dose.remove", day: "2026-10-10", k: "mt", ts: 1001 });
  phone.flush("todos");
  assert.deepStrictEqual(srv.docs.todos.protocol.entries["2026-10-10"].mt, [200, 1002]);
});

test("delete then restore (undo) on two devices yields one item", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  phone.do("todos", { t: "todo.remove", itemId: "a", at: new Date().toISOString() }); phone.flush("todos");
  const restoreOp = { t: "todo.restore", historyId: "a", item: { id: "r-a", text: "Alpha", kind: "urgent", list: "personal", done: false } };
  phone.do("todos", restoreOp); mac.do("todos", restoreOp);
  phone.flush("todos"); mac.flush("todos");
  assert.strictEqual(srv.docs.todos.items.filter(t => t.text === "Alpha").length, 1);
  assert.strictEqual(srv.docs.todos.history.length, 0);
});

test("reorder keeps an item another device added mid-drag", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  mac.do("todos", { t: "todo.add", item: { id: "m", text: "Mac add", kind: "urgent", list: "personal", done: false } }); mac.flush("todos");
  phone.do("todos", { t: "todo.reorder", ids: ["c", "a", "b"] }); phone.flush("todos");
  assert.deepStrictEqual(srv.docs.todos.items.map(t => t.id), ["c", "a", "b", "m"]);
});

test("reorder concurrent with delete drops nothing else", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  mac.do("todos", { t: "todo.remove", itemId: "b", at: new Date().toISOString() }); mac.flush("todos");
  phone.do("todos", { t: "todo.reorder", ids: ["c", "b", "a"] }); phone.flush("todos");
  assert.deepStrictEqual(srv.docs.todos.items.map(t => t.id), ["c", "a"]);
});

test("same field edited on both devices: later commit wins, only that field", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  phone.do("todos", { t: "todo.patch", itemId: "a", set: { text: "P" } });
  mac.do("todos", { t: "todo.patch", itemId: "a", set: { text: "M", due: "2026-10-12" } });
  phone.flush("todos"); mac.flush("todos");
  const a = srv.docs.todos.items.find(t => t.id === "a");
  assert.strictEqual(a.text, "M"); assert.strictEqual(a.due, "2026-10-12");
});

test("edit vs delete: delete wins, item lands in history", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  mac.do("todos", { t: "todo.remove", itemId: "a", at: new Date().toISOString() }); mac.flush("todos");
  phone.do("todos", { t: "todo.patch", itemId: "a", set: { done: true } }); phone.flush("todos");
  assert.ok(!srv.docs.todos.items.some(t => t.id === "a"));
  assert.ok(srv.docs.todos.history.some(h => h.id === "a"));
});

test("sub-items: concurrent adds on two devices both kept", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  phone.do("todos", { t: "todo.sub.add", itemId: "a", sub: { id: "s1", text: "one", done: false } });
  mac.do("todos", { t: "todo.sub.add", itemId: "a", sub: { id: "s2", text: "two", done: false } });
  phone.flush("todos"); mac.flush("todos");
  mac.do("todos", { t: "todo.sub.patch", itemId: "a", subId: "s1", set: { done: true } });
  phone.do("todos", { t: "todo.sub.remove", itemId: "a", subId: "s2" });
  mac.flush("todos"); phone.flush("todos");
  assert.deepStrictEqual(srv.docs.todos.items.find(t => t.id === "a").subitems, [{ id: "s1", text: "one", done: true }]);
});

test("sub-items: checking one off removes it from the list, can be added back, replays safely", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  phone.do("todos", { t: "todo.sub.add", itemId: "a", sub: { id: "s1", text: "one", done: false } });
  phone.do("todos", { t: "todo.sub.add", itemId: "a", sub: { id: "s2", text: "two", done: false } });
  phone.flush("todos"); mac.flush("todos");
  const at = new Date().toISOString();
  const done = { t: "todo.sub.complete", itemId: "a", subId: "s1", at };
  phone.do("todos", done); phone.do("todos", done);               // replayed op
  phone.flush("todos");
  let a = srv.docs.todos.items.find(t => t.id === "a");
  assert.deepStrictEqual(a.subitems.map(s => s.id), ["s2"]);
  assert.deepStrictEqual(a.subdone, [{ id: "s1", text: "one", doneAt: at }]);
  const back = { t: "todo.sub.restore", itemId: "a", subId: "s1" };
  mac.do("todos", back); mac.do("todos", back);                   // replayed op
  mac.flush("todos");
  a = srv.docs.todos.items.find(t => t.id === "a");
  assert.deepStrictEqual(a.subitems, [{ id: "s2", text: "two", done: false }, { id: "s1", text: "one", done: false }]);
  assert.ok(!a.subdone || a.subdone.length === 0);
});

test("sub-items: completed ones expire after 48h like recently-completed to-dos", () => {
  const now = Date.now();
  const doc = { items: [ { id: "a", text: "A", kind: "urgent", subdone: [
    { id: "old", text: "old", doneAt: new Date(now - 49 * 3600e3).toISOString() },
    { id: "new", text: "new", doneAt: new Date(now - 3600e3).toISOString() } ] } ], history: [] };
  const out = S.normalizeTodos(doc, now);
  assert.deepStrictEqual(out.items[0].subdone.map(s => s.id), ["new"]);
});

test("countdown chips: add/remove/edit sync; import of local chips dedupes by label+date", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  phone.do("todos", { t: "now.seed", chips: [{ id: "file:Wedding", label: "Wedding", date: "2027-05-01", kind: "countdown" }] });
  mac.do("todos", { t: "now.seed", chips: [{ id: "file:Wedding", label: "Wedding", date: "2027-05-01", kind: "countdown" }] });
  phone.flush("todos"); mac.flush("todos");
  assert.strictEqual(srv.docs.todos.now.chips.length, 1);
  mac.do("todos", { t: "now.import", added: [{ id: "loc:1", label: "wedding", date: "2027-05-01" }, { id: "loc:2", label: "Taxes", date: "2027-04-15", kind: "deadline" }], hidden: ["cal:2026-11-01:Drill"] });
  mac.flush("todos");
  assert.deepStrictEqual(srv.docs.todos.now.chips.map(c => c.id), ["file:Wedding", "loc:2"]);
  assert.ok(srv.docs.todos.now.hidden.includes("cal:2026-11-01:Drill"));
  // edit on phone = retire old ids + add new
  phone.do("todos", { t: "now.remove", ids: ["loc:2"] });
  phone.do("todos", { t: "now.add", chip: { id: "x9", label: "Taxes (ext)", date: "2027-10-15", kind: "deadline" } });
  phone.flush("todos");
  assert.deepStrictEqual(srv.docs.todos.now.chips.map(c => c.label), ["Wedding", "Taxes (ext)"]);
  // a to-do edit leaves chips alone
  mac.do("todos", { t: "todo.patch", itemId: "a", set: { done: true } }); mac.flush("todos");
  assert.strictEqual(srv.docs.todos.now.chips.length, 2);
  // seeding again never resurrects a removed chip
  phone.do("todos", { t: "now.remove", ids: ["file:Wedding"] }); phone.flush("todos");
  phone.do("todos", { t: "now.seed", chips: [{ id: "file:Wedding", label: "Wedding", date: "2027-05-01" }] }); phone.flush("todos");
  assert.ok(!srv.docs.todos.now.chips.some(c => c.id === "file:Wedding"));
});

test("field.restore only fills a missing field", () => {
  const srv = new Server(base());
  srv.commit("todos", [{ t: "field.restore", field: "protocol", value: { entries: {} } }]);
  assert.deepStrictEqual(srv.docs.todos.protocol.entries["2026-10-10"].om, [100], "existing not replaced");
  delete srv.docs.todos.now;
  srv.commit("todos", [{ t: "field.restore", field: "now", value: { chips: [{ id: "k", label: "K", date: "2027-01-01", kind: "countdown" }], hidden: [], seeded: true } }]);
  assert.strictEqual(srv.docs.todos.now.chips[0].id, "k");
});

test("friends: concurrent rename + log + add all survive; restore at index", () => {
  const srv = new Server(base()); const phone = new Device(srv), mac = new Device(srv);
  phone.do("friends", { t: "friend.patch", personId: "p1", set: { name: "Hayden M" } });
  mac.do("friends", { t: "friend.log", personId: "p1", date: "2026-10-09T12:00:00.000Z", method: "" });
  mac.do("friends", { t: "friend.add", person: { id: "p2", name: "Jack", freqDays: 21, lastContact: null, history: [] } });
  phone.flush("friends"); mac.flush("friends");
  const p1 = srv.docs.friends.people[0];
  assert.strictEqual(p1.name, "Hayden M"); assert.strictEqual(p1.lastContact, "2026-10-09T12:00:00.000Z");
  assert.strictEqual(srv.docs.friends.people.length, 2);
  // back-dated log never moves lastContact backward
  phone.do("friends", { t: "friend.log", personId: "p1", date: "2026-09-01T12:00:00.000Z", method: "" }); phone.flush("friends");
  assert.strictEqual(srv.docs.friends.people[0].lastContact, "2026-10-09T12:00:00.000Z");
  // remove + undo-restore
  const p2 = srv.docs.friends.people[1];
  phone.do("friends", { t: "friend.remove", personId: "p2" }); phone.flush("friends");
  phone.do("friends", { t: "friend.restore", person: p2, index: 0 }); phone.flush("friends");
  assert.deepStrictEqual(srv.docs.friends.people.map(p => p.id), ["p2", "p1"]);
});

test("friends seed only when the doc has no list", () => {
  const srv = new Server({ todos: {}, friends: undefined });
  const a = new Device(srv), b = new Device(srv);
  a.do("friends", { t: "friend.seed", people: [{ id: "s1", name: "A" }] });
  b.do("friends", { t: "friend.seed", people: [{ id: "s9", name: "A" }] });
  b.do("friends", { t: "friend.add", person: { id: "n", name: "New" } });
  a.flush("friends"); b.flush("friends");
  assert.deepStrictEqual(srv.docs.friends.people.map(p => p.id), ["s1", "n"]);
});

test("normalize: 48h purge, legacy fold, unparsable kept", () => {
  const now = Date.parse("2026-10-10T12:00:00Z");
  const d = S.normalizeTodos({ items: [{ id: "a" }], urgent: [{ id: "u" }], history: [
    { id: "h1", doneAt: "2026-10-10T00:00:00Z" }, { id: "h2", doneAt: "2026-10-01T00:00:00Z" }, { id: "h3", doneAt: "bad" }] }, now);
  assert.deepStrictEqual(d.items.map(t => t.id + ":" + t.kind), ["a:urgent", "u:urgent"]);
  assert.deepStrictEqual(d.history.map(h => h.id), ["h1", "h3"]);
  assert.ok(d.backupPreMerge);
});

test("history cap and outbox ordering across tabs sharing one store", () => {
  const store = new Mem(); const t1 = S.makeOutbox(store), t2 = S.makeOutbox(store);
  t1.add("todos", { t: "todo.patch", itemId: "a", set: { text: "1" } });
  t2.add("todos", { t: "todo.patch", itemId: "a", set: { text: "2" } });
  const ops = t1.list("todos"); assert.strictEqual(ops.length, 2);
  t2.remove([ops[0].oid]); assert.strictEqual(t1.list("todos").length, 1);
  const d = S.project("todos", { items: [], history: [] }, Array.from({ length: 20 }, (_, i) => [
    { t: "todo.add", item: { id: "i" + i, text: "t" } }, { t: "todo.remove", itemId: "i" + i, at: new Date().toISOString() }]).flat());
  assert.strictEqual(d.history.length, S.HISTORY_CAP);
});

test("unknown or malformed ops are skipped, not fatal", () => {
  const d = S.project("todos", base().todos, [{ t: "future.op" }, { t: "todo.patch" }, { t: "dose.add", day: "x", k: "om", ts: 1 }]);
  assert.strictEqual(d.items.length, 3);
});

test("field.restore wins over pending edits queued before it (reviewer repro)", () => {
  const srv = new Server(base());
  const lostProtocol = JSON.parse(JSON.stringify(srv.docs.todos.protocol));
  const now0 = { chips: [{ id: "c1", label: "Old chip", date: "2027-01-01", kind: "countdown" }], hidden: ["cal:x"], seeded: true };
  srv.docs.todos.now = now0;
  const d = new Device(srv);
  d.do("todos", { t: "dose.add", day: "2026-10-10", k: "om", ts: 999 });
  d.do("todos", { t: "now.add", chip: { id: "c2", label: "New chip", date: "2027-02-01" } });
  srv.docs.todos = { items: srv.docs.todos.items, history: [] };            // old-code writer drops fields
  d.do("todos", { t: "field.restore", field: "protocol", value: lostProtocol });
  d.do("todos", { t: "field.restore", field: "now", value: now0 });
  d.flush("todos");
  assert.deepStrictEqual(srv.docs.todos.protocol.entries["2026-10-10"].om, [100, 999]);
  assert.deepStrictEqual(srv.docs.todos.now.chips.map(c => c.id), ["c1", "c2"]);
  assert.deepStrictEqual(srv.docs.todos.now.hidden, ["cal:x"]);
});

console.log(`\n${n} sync tests passed`);
