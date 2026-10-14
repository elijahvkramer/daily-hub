/* Daily Hub sync core (Oct 2026) — pure merge logic + a durable outbox. No Firebase here.

   Every edit is an operation. The screen shows  server snapshot + pending ops, and the Firebase
   module replays pending ops inside a Firestore transaction against the LATEST server copy, so a
   change made on the phone can never overwrite a change made on the computer (the old code wrote
   whole lists from each device's own copy: last write won, the other edit vanished).

   Rules every op follows, because a commit can succeed while its acknowledgement is lost and the
   op then replays: add-if-absent, remove-if-present, set a field to a value (never "flip"),
   deterministic ids for anything created by a replayable op.

   Docs:  "todos"   -> todos/main   { items, history, backupPreMerge, protocol, now }
          "friends" -> friends/main { people }
   Tests: node scripts/test_sync.js */
(function(root){
  "use strict";
  const HISTORY_CAP = 16;                        // recently-deleted to-dos kept
  const HISTORY_MAX_AGE_MS = 48 * 60 * 60 * 1000; // ...for at most 48h (Eli, Aug 2026)
  const SUBDONE_CAP = 20;                         // checked-off checklist items kept per to-do (same 48h window)
  const FRIEND_HISTORY_CAP = 40;
  const HIDDEN_CAP = 400;                         // hidden countdown ids remembered

  const clone = v => v === undefined ? undefined : JSON.parse(JSON.stringify(v));
  const arr = v => Array.isArray(v) ? v : [];
  const kindOf = k => k === "chill" ? "chill" : "urgent";
  const listOf = l => l === "work" ? "work" : "personal";

  /* ---------- to-do doc normalisation (moved here unchanged in meaning from the page) ----------
     One-time fold of the legacy urgent/chill arrays into `items`, legacy history fold, and the
     48h purge of recently-deleted entries. Pure; `now` is injectable for tests. */
  function normalizeTodos(data, now){
    data = data ? clone(data) : {};
    now = now === undefined ? Date.now() : now;
    const legacyU = arr(data.urgent), legacyC = arr(data.chill);
    let items = Array.isArray(data.items) ? data.items.slice() : null;
    if(!items){
      items = [...legacyU.map(t => ({ ...t, kind: "urgent" })), ...legacyC.map(t => ({ ...t, kind: "chill" }))];
    }else{
      const known = new Set(items.map(t => t.id));
      items = items.concat(
        legacyU.filter(t => !known.has(t.id)).map(t => ({ ...t, kind: "urgent" })),
        legacyC.filter(t => !known.has(t.id)).map(t => ({ ...t, kind: "chill" })));
    }
    items = items.map(t => ({ ...t, kind: kindOf(t.kind) }));
    // checked-off checklist items expire after 48h, like recently-completed to-dos
    items = items.map(t => {
      if(!Array.isArray(t.subdone)) return t;
      const keep = t.subdone.filter(s => { const at = Date.parse(s.doneAt); return !Number.isFinite(at) || at >= now - HISTORY_MAX_AGE_MS; });
      if(keep.length === t.subdone.length) return t;
      const next = { ...t };
      if(keep.length) next.subdone = keep; else delete next.subdone;
      return next;
    });
    let history = Array.isArray(data.history) ? data.history : null;
    if(!history){
      history = [
        ...arr(data.historyUrgent).map(h => ({ ...h, kind: "urgent" })),
        ...arr(data.historyChill).map(h => ({ ...h, kind: "chill" }))
      ].sort((a, b) => String(b.doneAt || "").localeCompare(String(a.doneAt || ""))).slice(0, HISTORY_CAP);
    }
    const cutoff = now - HISTORY_MAX_AGE_MS;
    history = history.filter(h => { const t = Date.parse(h.doneAt); return !Number.isFinite(t) || t >= cutoff; });
    if((legacyU.length || legacyC.length) && !data.backupPreMerge){
      data.backupPreMerge = {
        urgent: legacyU, chill: legacyC,
        historyUrgent: arr(data.historyUrgent), historyChill: arr(data.historyChill),
        at: new Date(now).toISOString()
      };
    }
    data.items = items; data.history = history;
    // the legacy arrays are folded in now; drop them so they are not folded again
    delete data.urgent; delete data.chill; delete data.historyUrgent; delete data.historyChill;
    return data;
  }

  /* ---------- appliers: (data, op) -> data. data is a private clone, mutate freely ---------- */
  const mapItem = (data, id, fn) => { data.items = arr(data.items).map(t => t.id === id ? fn({ ...t }) : t); };
  const nowOf = data => {
    const n = data.now && typeof data.now === "object" ? data.now : {};
    return data.now = { chips: arr(n.chips), hidden: arr(n.hidden), seeded: !!n.seeded };
  };
  const chipKey = c => String(c.label || "").trim().toLowerCase() + "|" + (c.date || "");
  const cleanChip = c => ({ id: String(c.id), label: String(c.label || ""), date: String(c.date || ""), kind: c.kind === "deadline" ? "deadline" : "countdown" });
  const doseArr = (data, day, k, create) => {
    if(!data.protocol || typeof data.protocol !== "object"){ if(!create) return null; data.protocol = {}; }
    const p = data.protocol;
    if(!p.entries || typeof p.entries !== "object"){ if(!create) return null; p.entries = {}; }
    if(!p.entries[day] || typeof p.entries[day] !== "object"){ if(!create) return null; p.entries[day] = {}; }
    if(!Array.isArray(p.entries[day][k])){ if(!create) return null; p.entries[day][k] = []; }
    return p.entries[day][k];
  };

  const APPLY = {
    /* ----- to-dos ----- */
    "todo.add"(d, o){
      d.items = arr(d.items);
      if(o.item && o.item.id && !d.items.some(t => t.id === o.item.id)) d.items.push(clone(o.item));
    },
    "todo.remove"(d, o){
      d.items = arr(d.items);
      const it = d.items.find(t => t.id === o.itemId);
      if(!it) return;
      d.items = d.items.filter(t => t.id !== o.itemId);
      d.history = arr(d.history).filter(h => h.id !== it.id);
      d.history.unshift({ id: it.id, text: it.text, kind: kindOf(it.kind), list: listOf(it.list), doneAt: o.at || new Date().toISOString() });
      d.history = d.history.slice(0, HISTORY_CAP);
    },
    "todo.patch"(d, o){
      mapItem(d, o.itemId, t => {
        Object.keys(o.set || {}).forEach(k => { t[k] = clone(o.set[k]); });
        arr(o.unset).forEach(k => { delete t[k]; });
        return t;
      });
    },
    "todo.sub.add"(d, o){
      mapItem(d, o.itemId, t => {
        const subs = arr(t.subitems);
        t.subitems = subs.some(s => s.id === o.sub.id) ? subs : [...subs, clone(o.sub)];
        return t;
      });
    },
    "todo.sub.patch"(d, o){
      mapItem(d, o.itemId, t => { t.subitems = arr(t.subitems).map(s => s.id === o.subId ? { ...s, ...clone(o.set) } : s); return t; });
    },
    "todo.sub.remove"(d, o){
      mapItem(d, o.itemId, t => { t.subitems = arr(t.subitems).filter(s => s.id !== o.subId); return t; });
    },
    /* checking a checklist item off takes it out of the list, like a regular to-do: it moves to the
       item's own `subdone` list (kept 48h, newest first) and can be added back. Both ops are
       replay-safe: complete is a no-op once the item is gone, restore a no-op once it is back. */
    "todo.sub.complete"(d, o){
      mapItem(d, o.itemId, t => {
        const subs = arr(t.subitems);
        const s = subs.find(x => x.id === o.subId);
        if(!s) return t;
        t.subitems = subs.filter(x => x.id !== o.subId);
        t.subdone = [{ id: s.id, text: s.text, doneAt: o.at || new Date().toISOString() },
          ...arr(t.subdone).filter(x => x.id !== s.id)].slice(0, SUBDONE_CAP);
        return t;
      });
    },
    "todo.sub.restore"(d, o){
      mapItem(d, o.itemId, t => {
        const gone = arr(t.subdone).find(x => x.id === o.subId);
        if(!gone) return t;
        t.subdone = arr(t.subdone).filter(x => x.id !== o.subId);
        const subs = arr(t.subitems);
        if(!subs.some(x => x.id === gone.id)) t.subitems = [...subs, { id: gone.id, text: gone.text, done: false }];
        return t;
      });
    },
    "todo.restore"(d, o){
      d.items = arr(d.items); d.history = arr(d.history);
      const had = d.history.some(h => h.id === o.historyId);
      d.history = d.history.filter(h => h.id !== o.historyId);
      // deterministic id: restoring the same entry on two devices yields one item, not two
      if(had && !d.items.some(t => t.id === o.item.id)) d.items.push(clone(o.item));
    },
    /* the dragged items take the slots those same items occupy now, in the dragged order;
       anything another device added meanwhile keeps its place instead of being dropped */
    "todo.reorder"(d, o){
      d.items = arr(d.items);
      const want = arr(o.ids).filter(id => d.items.some(t => t.id === id));
      const wantSet = new Set(want);
      const byId = new Map(d.items.map(t => [t.id, t]));
      let i = 0;
      d.items = d.items.map(t => wantSet.has(t.id) ? byId.get(want[i++]) : t);
    },
    /* compatibility for any code that still hands over a whole list (writeTodos) */
    "todo.replace"(d, o){
      if(Array.isArray(o.items)) d.items = clone(o.items);
      if(Array.isArray(o.history)) d.history = clone(o.history);
    },

    /* ----- treatment tracker: doses are a set of unique ms timestamps ----- */
    "dose.add"(d, o){
      const a = doseArr(d, o.day, o.k, true);
      if(!a.includes(o.ts)){ a.push(o.ts); a.sort((x, y) => x - y); }
    },
    "dose.remove"(d, o){
      const a = doseArr(d, o.day, o.k, false);
      if(a){ const i = a.indexOf(o.ts); if(i >= 0) a.splice(i, 1); }
    },

    /* ----- countdown chips ----- */
    "now.add"(d, o){
      const n = nowOf(d), c = cleanChip(o.chip);
      if(!n.chips.some(x => x.id === c.id)) n.chips.push(c);
      n.hidden = n.hidden.filter(id => id !== c.id);
      n.seeded = true;
    },
    "now.remove"(d, o){
      const n = nowOf(d), ids = arr(o.ids).map(String);
      n.chips = n.chips.filter(c => !ids.includes(c.id));
      ids.forEach(id => { if(!n.hidden.includes(id)) n.hidden.push(id); });
      n.hidden = n.hidden.slice(-HIDDEN_CAP);
    },
    "now.seed"(d, o){
      const n = nowOf(d);
      if(n.seeded) return;
      arr(o.chips).forEach(c => { if(!n.chips.some(x => x.id === c.id) && !n.hidden.includes(c.id)) n.chips.push(cleanChip(c)); });
      n.seeded = true;
    },
    /* a device's old local-only chips/hides join the synced list (once); same label+date = same chip */
    "now.import"(d, o){
      const n = nowOf(d);
      arr(o.added).forEach(c => {
        if(!n.chips.some(x => x.id === c.id || chipKey(x) === chipKey(c)) && !n.hidden.includes(c.id)) n.chips.push(cleanChip(c));
      });
      arr(o.hidden).forEach(id => { if(!n.hidden.includes(id)) n.hidden.push(String(id)); });
      n.chips = n.chips.filter(c => !n.hidden.includes(c.id));
      n.hidden = n.hidden.slice(-HIDDEN_CAP);
    },

    /* ----- self-heal: a writer elsewhere replaced the doc without a field we own ----- */
    "field.restore"(d, o){
      if(d[o.field] === undefined && o.value !== undefined) d[o.field] = clone(o.value);
    },

    /* ----- friends ----- */
    "friend.seed"(d, o){ if(!Array.isArray(d.people)) d.people = clone(arr(o.people)); },
    "friend.add"(d, o){
      d.people = arr(d.people);
      if(o.person && !d.people.some(p => p.id === o.person.id)) d.people.push(clone(o.person));
    },
    "friend.remove"(d, o){ d.people = arr(d.people).filter(p => p.id !== o.personId); },
    "friend.patch"(d, o){ d.people = arr(d.people).map(p => p.id === o.personId ? { ...p, ...clone(o.set) } : p); },
    "friend.replace"(d, o){ d.people = arr(d.people).map(p => p.id === o.person.id ? clone(o.person) : p); },
    "friend.restore"(d, o){
      d.people = arr(d.people);
      if(!o.person || d.people.some(p => p.id === o.person.id)) return;
      d.people.splice(Math.max(0, Math.min(Number(o.index) || 0, d.people.length)), 0, clone(o.person));
    },
    // history stays newest-first; lastContact is the newest touchpoint and never moves backward
    "friend.log"(d, o){
      d.people = arr(d.people).map(p => {
        if(p.id !== o.personId) return p;
        const entry = { date: o.date, method: o.method || "" };
        const hist = arr(p.history).some(h => h.date === entry.date && (h.method || "") === entry.method)
          ? arr(p.history) : [entry, ...arr(p.history)];
        const sorted = hist.slice().sort((a, b) => new Date(b.date) - new Date(a.date)).slice(0, FRIEND_HISTORY_CAP);
        const prev = p.lastContact ? new Date(p.lastContact).getTime() : 0;
        return { ...p, lastContact: new Date(o.date).getTime() > prev ? o.date : p.lastContact, history: sorted };
      });
    }
  };

  /* server data + ops -> the data the screen shows (and the data a transaction writes) */
  function project(docName, server, ops, now){
    let d = docName === "todos" ? normalizeTodos(server, now) : (server ? clone(server) : {});
    // a field.restore (put back a field another writer dropped) must land BEFORE pending edits to that
    // field, or a pending dose/chip would recreate the field first and the restore would be skipped
    const list = arr(ops).filter(o => o && o.t === "field.restore").concat(arr(ops).filter(o => !o || o.t !== "field.restore"));
    list.forEach(op => {
      const f = op && APPLY[op.t];
      if(!f){ return; }                 // unknown op from a newer build: leave it for that build
      try{ f(d, op); }catch(e){ /* one malformed op must never block the rest */ }
    });
    if(docName === "todos"){ d.items = arr(d.items); d.history = arr(d.history); }
    return d;
  }

  /* top-level fields whose value differs: the only fields a transaction writes */
  function changedFields(before, after){
    const out = {};
    const keys = new Set([...Object.keys(before || {}), ...Object.keys(after || {})]);
    keys.forEach(k => {
      if(after[k] === undefined) return;          // never delete a field (other code may own it)
      if(JSON.stringify((before || {})[k]) !== JSON.stringify(after[k])) out[k] = after[k];
    });
    return out;
  }

  /* ---------- durable outbox: one localStorage key per op, so two tabs never overwrite each other ---------- */
  const PREFIX = "dh-ob1:";
  let counter = 0;
  const tabTag = Math.random().toString(36).slice(2, 6);
  function makeOutbox(store){
    const keys = () => { const out = []; for(let i = 0; i < store.length; i++){ const k = store.key(i); if(k && k.indexOf(PREFIX) === 0) out.push(k); } return out; };
    return {
      add(docName, op){
        const seq = Date.now() * 1000 + (counter++ % 1000);
        const full = { ...op, doc: docName, seq, oid: seq.toString(36) + tabTag };
        try{ store.setItem(PREFIX + full.oid, JSON.stringify(full)); }catch(e){ full.volatile = true; }
        return full;
      },
      list(docName){
        const out = [];
        keys().forEach(k => { try{ const o = JSON.parse(store.getItem(k)); if(o && o.doc === docName) out.push(o); }catch(e){} });
        return out.sort((a, b) => a.seq - b.seq);
      },
      remove(oids){ arr(oids).forEach(id => { try{ store.removeItem(PREFIX + id); }catch(e){} }); },
      count(){ return keys().length; }
    };
  }

  root.DHSync = { normalizeTodos, project, changedFields, makeOutbox, APPLY, HISTORY_CAP, PREFIX };
})(typeof window !== "undefined" ? window : globalThis);
