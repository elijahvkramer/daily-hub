# Daily Hub as an iPhone app — design (Oct 10, 2026)

## Decision: PWA (Home Screen web app), not a native wrapper

| Goal | PWA | Capacitor wrapper |
|---|---|---|
| Always runs the latest GitHub deploy | Yes — the page *is* the site; the service worker fetches the page network-first with `cache: "no-store"` | Only if the wrapper loads the remote URL, which App Store review discourages; otherwise every change needs a rebuild |
| Same data as the computer | Same Firestore project and code | Same |
| Full screen, icon, splash, safe areas | `display: standalone`, apple-touch-icon, startup images, `viewport-fit=cover` | Yes |
| Offline | Service worker + Firestore IndexedDB cache + a durable outbox | Yes |
| Cost / upkeep | Free, zero steps after install | Xcode, $99/yr developer account, re-signing |
| Push (optional) | Supported for Home Screen web apps since iOS 16.4 | Yes |

Nothing in the goals needs a native API, so a wrapper only adds cost and a second copy of the site that can drift. **PWA.**

Evidence relied on:
- Home Screen apps do not share storage with Safari — by design (WebKit bug 181849, Apple engineer comment).
- Home Screen apps have their own days-of-use counter, so the 7-day script-storage cap does not delete their data (WebKit blog, "Full Third-Party Cookie Blocking and More").
- Firestore transactions fail while offline; persistence is enabled with `persistentLocalCache` + `persistentMultipleTabManager` (Firebase docs).

## What the first launch of the installed app looks like

Storage is separate from Safari, so the installed app starts empty: passphrase screen, a new anonymous Firebase user, no Face ID record. Enter the passphrase once; the app then offers Face ID (passkey + PRF, existing code). After that, launches unlock with Face ID. All synced data arrives from Firestore; the security rules only require `request.auth != null`, which a new anonymous user satisfies (verified live with a fresh anonymous user: `todos/main` and `friends/main` readable; `now/main` and `protocol/main` denied).

## Sync model (per document)

All synced data lives in two documents the rules allow:

| Doc | Fields | Features |
|---|---|---|
| `todos/main` | `items`, `history`, `backupPreMerge` | To-dos (add, edit, sub-items, due, note, reorder, delete → recently deleted, restore) |
| | `protocol.entries[day][med] = [ms…]` | Treatment tracker doses |
| | `now = {chips, hidden, seeded}` | Countdown chips (moved here from `now/main`, which the rules reject) |
| `friends/main` | `people` | Friends check-in |

**Old model:** every edit wrote the whole list from this device's copy (last write wins). Two devices editing near-simultaneously, or an offline device coming back, silently overwrote the other's changes. Offline edits lived only in memory.

**New model (`sync-core.js` + the Firebase module in `index.html`):**
1. Every edit is an *operation* (`todo.patch {id, set}`, `dose.add {day, med, ts}`, `now.remove {ids}`, …), saved immediately to `localStorage` (one key per op, so tabs never clobber each other's queue).
2. The screen shows `server snapshot + pending ops`, so edits appear instantly, online or off.
3. A flusher replays pending ops inside a Firestore **transaction**: read the latest doc, apply the ops to *that*, write back only the top-level fields that changed. Concurrent edits from another device are preserved, and fields other code owns (`protocol`, `now`, `backupPreMerge`) are never touched by a to-do edit.
4. Ops are idempotent (add-if-absent, remove-if-present, set-field, set-done-to-value, deterministic ids for restores), so a commit whose acknowledgement was lost replays harmlessly.
5. Ops leave the queue only after the commit succeeds. Offline, they wait (surviving app kills) and flush on `online`, on app resume, and every 20 s while pending.

Conflict semantics: different items/fields → both kept. Same field of the same item edited on two devices → the later commit wins for that field only. Delete vs. edit → delete wins (the item sits in Recently deleted). Doses are a set of unique millisecond timestamps, so two devices logging at once both count; Undo removes that exact timestamp.

Firestore's IndexedDB cache is enabled so a cold offline launch still shows the last data. The Firebase SDK files are cached by the service worker (cache-first; versioned URLs never change), so offline launches can still load it.

## Updates

- The page is fetched network-first with `no-store` (existing), so any cold launch runs the latest deploy.
- iOS often *resumes* a suspended app instead of relaunching it, so the page checks for a new deploy on every resume: `HEAD index.html`; if the ETag moved, fetch it and compare a SHA-256 of the bytes with what is running (data-only deploys change the ETag but not the bytes).
- New version found on resume with nothing being typed and no dialog open → reload silently (the unlocked session survives a reload — it is in `sessionStorage`; pending edits survive — they are in the outbox). Otherwise a small "New version — tap to update" pill, so an update never interrupts typing.
- Tabs still running code from before this change cannot be told to update, and their whole-document writes would undo the new protections. When the new service worker activates it pings every open page; pages that do not answer (old code) are reloaded once.

## Failure modes and handling

| Failure | Handling |
|---|---|
| Offline edit, app killed | Op persisted in `localStorage`; flushed on next online launch |
| Two devices edit at once | Transaction re-reads and merges; Firestore retries on contention |
| Ack lost after commit | Op replays idempotently |
| A whole-document writer elsewhere drops `now` | Field is re-created from the next op; chips also kept in `localStorage` fallback |
| Rules reject a write | Ops kept, error logged once, tracker footer shows "not synced" |
| Firebase SDK unreachable | Edits still queue locally and show on screen; they flush when it loads |

## Security

No new secrets. The Firebase web config is a public identifier, not a credential; access is governed by the rules. No GitHub token is in page JavaScript. Rules and billing unchanged.
