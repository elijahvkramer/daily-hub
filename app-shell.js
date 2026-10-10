/* Daily Hub installed-app layer, part 2 (Oct 10 2026): update delivery + Add to Home Screen card.

   Updates. A cold launch always runs the newest deploy (sw.js fetches the page with no-store). But
   iOS usually RESUMES a Home Screen app from memory instead of relaunching it, so on every resume
   this checks GitHub for a new index.html: HEAD first (cheap); if the ETag moved, fetch the page and
   compare a SHA-256 of its bytes with the copy that is running (data-only deploys move the ETag but
   not the bytes). A real change found on resume, with nothing being typed and no dialog open, reloads
   at once — the unlocked session lives in sessionStorage and pending edits in the sync outbox, so a
   reload loses neither. Otherwise a small pill offers it, so an update never interrupts typing.

   Old pages. The service worker pings open pages when a new version activates; a page answers here.
   Pages from before this change cannot answer and are reloaded once (their whole-list writes would
   undo the new sync protections). */
(function(){
  "use strict";
  var INDEX = new URL("index.html", location.href).href;
  var KEY_PATH = new URL("index.html", location.href).pathname;   // sw.js caches the page under this key

  /* ---------- answer the service worker's "are you new code?" ping ---------- */
  if("serviceWorker" in navigator){
    navigator.serviceWorker.addEventListener("message", function(e){
      if(e.data && e.data.type === "dh-ping" && e.source) e.source.postMessage({ type: "dh-pong" });
    });
    try{ navigator.serviceWorker.startMessages(); }catch(e){}
  }

  /* ---------- update check ---------- */
  var base = null, offered = false, hiddenAt = 0, checking = false;
  function hex(buf){ return Array.prototype.map.call(new Uint8Array(buf), function(b){ return b.toString(16).padStart(2, "0"); }).join(""); }
  function sha(buf){ return crypto.subtle.digest("SHA-256", buf).then(hex); }

  // what is running = the bytes the service worker served for this page (it stores them as it serves)
  function baseline(){
    var fromCache = ("caches" in window) ? caches.keys().then(function(keys){
      var shell = keys.filter(function(k){ return k.indexOf("shell-") === 0; });
      return shell.reduce(function(p, k){
        return p.then(function(hit){ return hit || caches.open(k).then(function(c){ return c.match(KEY_PATH); }); });
      }, Promise.resolve(null));
    }).catch(function(){ return null; }) : Promise.resolve(null);
    return fromCache.then(function(res){
      return res || fetch(INDEX, { cache: "no-store" });       // no service worker yet: ask the network
    }).then(function(res){
      var etag = res.headers.get("etag");
      return res.arrayBuffer().then(sha).then(function(h){ base = { etag: etag, hash: h }; });
    }).catch(function(){});
  }

  function newVersionAvailable(){
    if(!base || !navigator.onLine || checking) return Promise.resolve(false);
    checking = true;
    return fetch(INDEX, { method: "HEAD", cache: "no-store" }).then(function(h){
      var etag = h.headers.get("etag");
      if(!h.ok || (etag && etag === base.etag)) return false;
      // through the service worker, which also refreshes the offline copy
      return fetch(INDEX, { cache: "no-store" }).then(function(r){
        if(!r.ok) return false;
        var e2 = r.headers.get("etag") || etag;
        return r.arrayBuffer().then(sha).then(function(h2){
          if(h2 === base.hash){ base.etag = e2; return false; }   // a data-only deploy: same page
          return true;
        });
      });
    }).catch(function(){ return false; }).then(function(v){ checking = false; return v; });
  }

  // nothing would be lost by reloading right now
  function safeToReload(){
    var a = document.activeElement;
    if(a && a !== document.body && (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))) return false;
    var open = Array.prototype.some.call(document.querySelectorAll(".modal-overlay, [aria-modal='true']"), function(el){
      return !el.closest("#rxRemind") && el.getClientRects().length > 0;   // the dose reminder comes straight back after a reload
    });
    return !open;
  }

  function offer(){
    if(offered) return; offered = true;
    var b = document.createElement("button");
    b.type = "button"; b.id = "dhUpdate";
    b.textContent = "New version ready · tap to update";
    b.style.cssText = "position:fixed;left:50%;transform:translateX(-50%);bottom:calc(16px + env(safe-area-inset-bottom));" +
      "z-index:2147482500;border:0;border-radius:999px;padding:11px 18px;font:800 13px/1.2 var(--dh-font,-apple-system,sans-serif);" +
      "background:var(--ink,#16181D);color:var(--card,#fff);box-shadow:0 12px 30px -8px rgba(0,0,0,.45);cursor:pointer";
    b.addEventListener("click", function(){ location.reload(); });
    document.body.appendChild(b);
  }

  function check(onResume){
    newVersionAvailable().then(function(yes){
      if(!yes) return;
      if(onResume && safeToReload()) location.reload();
      else offer();
    });
  }

  document.addEventListener("visibilitychange", function(){
    if(document.visibilityState === "hidden"){ hiddenAt = Date.now(); return; }
    check(hiddenAt && Date.now() - hiddenAt > 1500);              // back from the app switcher / Home Screen
  });
  window.addEventListener("pageshow", function(e){ if(e.persisted) check(true); });   // restored from the back/forward cache
  setInterval(function(){ if(document.visibilityState === "visible") check(false); }, 10 * 60 * 1000);
  window.addEventListener("load", function(){ setTimeout(baseline, 1500); });

  /* ---------- Add to Home Screen card: iPhone Safari only, never inside the installed app ---------- */
  var DISMISS = "dh-install-dismissed";
  function installCard(){
    if(navigator.standalone !== false) return;                    // only iOS Safari defines it, and false = not installed
    try{ if(localStorage.getItem(DISMISS)) return; }catch(e){}
    var share = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="vertical-align:-3px"><path d="M12 3v12"/><path d="M8 7l4-4 4 4"/><path d="M6 11H5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-8a1 1 0 0 0-1-1h-1"/></svg>';
    var plus = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true" style="vertical-align:-3px"><rect x="4" y="4" width="16" height="16" rx="4"/><path d="M12 8.5v7M8.5 12h7"/></svg>';
    var c = document.createElement("div");
    c.id = "dhInstall"; c.setAttribute("role", "dialog"); c.setAttribute("aria-label", "Install Daily Hub");
    c.style.cssText = "position:fixed;left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom));z-index:2147482400;" +
      "max-width:440px;margin:0 auto;border-radius:20px;padding:16px 16px 14px;background:var(--card,#fff);color:var(--ink,#16181D);" +
      "box-shadow:0 18px 50px -12px rgba(0,0,0,.45);border:1px solid var(--hairline,rgba(0,0,0,.08));font:500 14px/1.4 var(--dh-font,-apple-system,sans-serif)";
    c.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:8px">' +
      '<b style="font-size:16px;font-weight:800">Get the Daily Hub app</b>' +
      '<button type="button" id="dhInstallX" aria-label="Dismiss" style="border:0;background:transparent;color:inherit;font-size:20px;line-height:1;padding:4px 6px;cursor:pointer;opacity:.6">×</button></div>' +
      '<div style="display:flex;gap:10px;align-items:baseline;margin:6px 0"><b style="min-width:16px">1</b><span>Tap ' + share + ' <b>Share</b> in Safari’s toolbar.</span></div>' +
      '<div style="display:flex;gap:10px;align-items:baseline;margin:6px 0"><b style="min-width:16px">2</b><span>Choose ' + plus + ' <b>Add to Home Screen</b>, then <b>Add</b>.</span></div>' +
      '<div style="font-size:12px;opacity:.65;margin-top:8px">It opens full screen and updates itself. You’ll enter your passphrase once inside the app.</div>';
    document.body.appendChild(c);
    c.querySelector("#dhInstallX").addEventListener("click", function(){
      try{ localStorage.setItem(DISMISS, "1"); }catch(e){}
      c.remove();
    });
  }
  if(document.readyState === "loading") document.addEventListener("DOMContentLoaded", function(){ setTimeout(installCard, 1200); });
  else setTimeout(installCard, 1200);
})();
