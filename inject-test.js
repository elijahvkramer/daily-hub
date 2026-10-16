window.CNBC_ALIAS = { "BTC-USD":"BTC.CM=", "ETH-USD":"ETH.CM=", "^GSPC":".SPX", "^IXIC":".IXIC", "^DJI":".DJI",
  "GC=F":"@GC.1", "BZ=F":"@LCO.1", "^IRX":"US3M", "2YY=F":"US2Y", "^TNX":"US10Y", "^TYX":"US30Y" };
window.cnbcSym = s => CNBC_ALIAS[s] || s;
window.isCryptoSym = s => /-USD$/.test(String(s || ""));
window.cnbcNum = v => { if(v == null) return null; const n = parseFloat(String(v).replace(/[,%$]/g, "")); return isFinite(n) ? n : null; };
window.isMutualFund = s => /^[A-Z]{4}X$/.test(String(s || ""));   // US fund tickers: five letters ending in X
window.isWeekendDay = d => { const w = new Date(d + "T12:00:00Z").getUTCDay(); return w === 0 || w === 6; };
window.INS_BAD_LS = "dh-anthropic-key-bad";
window.insKeyFp = k => k ? k.length + ":" + k.slice(-6) : "";
window.nyEpoch = function nyEpoch(y, mo, d, h, mi, s){
  const guess = Date.UTC(y, mo - 1, d, h || 0, mi || 0, s || 0);
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {timeZone:"America/New_York", hourCycle:"h23",
    year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit"})
    .formatToParts(new Date(guess)).map(x => [x.type, x.value]));
  const asNY = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second);
  return Math.round((guess + (guess - asNY)) / 1000);
};
window.cnbcTime = function cnbcTime(raw){
  const t = String(raw || "").trim();
  let m = t.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if(m) return nyEpoch(+m[1], +m[2], +m[3], 16, 0, 0);
  const ms = Date.parse(t.replace(/\.\d+/, "").replace(/([+-]\d{2})(\d{2})$/, "$1:$2"));
  return isNaN(ms) ? null : Math.round(ms / 1000);
};
window.cnbcBarTime = function cnbcBarTime(s){
  const m = String(s || "").match(/^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})/);
  return m ? nyEpoch(+m[1], +m[2], +m[3], +m[4], +m[5], +m[6]) : null;
};
window.cnbcQuotes = async function cnbcQuotes(syms, ms){
  syms = [...new Set(syms || [])].filter(Boolean);
  if(!syms.length) return {};
  const back = {}; syms.forEach(s => { back[cnbcSym(s).toUpperCase()] = s; });
  const url = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols=" +
    Object.keys(back).map(encodeURIComponent).join("%7C") +
    "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json&events=1";
  const r = await pFetchTimeout(url, ms || 7000, {cache:"no-store"});
  if(!r.ok) throw new Error("cnbc quote " + r.status);
  const j = await r.json();
  let rows = j && j.FormattedQuoteResult && j.FormattedQuoteResult.FormattedQuote || [];
  if(!Array.isArray(rows)) rows = [rows];
  const out = {}, now = Math.round(Date.now()/1000);
  rows.forEach(q => {
    const sym = back[String(q.symbol || "").toUpperCase()];
    const price = cnbcNum(q.last); if(!sym || price == null) return;
    let prev = cnbcNum(q.previous_day_closing);
    if(prev == null){ const ch = cnbcNum(q.change); prev = ch != null ? price - ch : price; }
    out[sym] = { price, prev, time: cnbcTime(q.last_time) || now };
  });
  return out;
};
window.cnbcBars = async function cnbcBars(sym, rng, ms){
  const r = await pFetchTimeout(`https://ts-api.cnbc.com/harmony/app/charts/${rng}.json?symbol=${encodeURIComponent(cnbcSym(sym))}`, ms || 9000, {cache:"no-store"});
  if(!r.ok) throw new Error("cnbc chart " + r.status);
  const j = await r.json();
  const bars = (j && j.barData && j.barData.priceBars) || [];
  const out = [];
  bars.forEach(b => { const t = cnbcBarTime(b.tradeTime), c = cnbcNum(b.close); if(t != null && c != null) out.push([t, c]); });
  return out.sort((a,b) => a[0] - b[0]);
};
window.cnbcIntraday = async function cnbcIntraday(sym, bucketMin){
  const bars = await cnbcBars(sym, "1D", 8000);
  if(bars.length < 5) return null;
  const step = (bucketMin || 5) * 60, buckets = new Map();
  bars.forEach(([t,c]) => buckets.set(Math.floor(t/step)*step, c));
  const last = bars[bars.length-1][0];
  const d = new Date(last*1000).toLocaleDateString("en-CA", {timeZone:"America/New_York"}).split("-").map(Number);
  const crypto = isCryptoSym(sym);
  const start = crypto ? nyEpoch(d[0], d[1], d[2], 0, 0, 0) : nyEpoch(d[0], d[1], d[2], 9, 30, 0);
  const end = crypto ? start + 86400 : nyEpoch(d[0], d[1], d[2], 16, 0, 0);
  const keep = [...buckets.keys()].filter(t => t >= start && t <= end).sort((a,b) => a-b);
  if(keep.length < 3) return null;
  return { t: keep, c: keep.map(t => buckets.get(t)), start, end };
};
window.cnbcDaily = async function cnbcDaily(sym){
  const bars = await cnbcBars(sym, "6M", 12000);
  const byDay = new Map();
  bars.forEach(([t,c]) => byDay.set(new Date(t*1000).toLocaleDateString("en-CA", {timeZone:"America/New_York"}), c));
  const days = [...byDay.keys()].sort().filter(d => isCryptoSym(sym) || !isWeekendDay(d));
  return days.length > 20 ? { t: days.map(dayEpoch), c: days.map(d => byDay.get(d)) } : null;
};
window.pCleanHist = function pCleanHist(sym, h){
  if(!h || !h.t || !h.c) return h;
  const crypto = isCryptoSym(sym);
  const byDay = new Map();
  h.t.forEach((ts,i) => { if(h.c[i] != null) byDay.set(new Date(ts*1000).toISOString().slice(0,10), [ts, h.c[i]]); });
  let days = [...byDay.keys()].sort();
  if(!crypto){
    /* where does it become daily? the first run of 5 consecutive gaps of <= 4 days */
    let dailyFrom = days.length;
    for(let i = 0; i + 5 < days.length; i++){
      let ok = true;
      for(let k = i; k < i + 5; k++) if((Date.parse(days[k+1]) - Date.parse(days[k]))/864e5 > 4){ ok = false; break; }
      if(ok){ dailyFrom = i; break; }
    }
    const from = days[dailyFrom] || "9999";
    days = days.filter(d => d < from || !isWeekendDay(d));
  }
  return { t: days.map(d => byDay.get(d)[0]), c: days.map(d => byDay.get(d)[1]) };
};
window.pMergeHist = function pMergeHist(a, b){
  if(!a || !a.t || !a.t.length) return b || a;
  if(!b || !b.t || !b.t.length) return a;
  const m = new Map();
  a.t.forEach((ts,i) => m.set(new Date(ts*1000).toISOString().slice(0,10), [ts, a.c[i]]));
  b.t.forEach((ts,i) => m.set(new Date(ts*1000).toISOString().slice(0,10), [ts, b.c[i]]));
  const days = [...m.keys()].sort();
  return { t: days.map(d => m.get(d)[0]), c: days.map(d => m.get(d)[1]) };
};
window.cnbcNews = async function cnbcNews(q, count, ms){
  const url = "https://api.queryly.com/cnbc/json.aspx?queryly_key=31a35d40a9a64ab3&query=" + encodeURIComponent(q) +
    "&endindex=0&batchsize=" + (count || 10) + "&callback=&showfaceted=false&timezoneoffset=300&sort=date";
  const r = await pFetchTimeout(url, ms || 6000, {cache:"no-store"});
  if(!r.ok) throw new Error("cnbc search " + r.status);
  const j = await r.json();
  return ((j && j.results) || []).map(x => ({
    title: String(x["cn:title"] || "").trim(), url: x.url || x["cn:liveURL"] || "", outlet: "CNBC",
    section: x.section || "", summary: String(x.summary || x.description || "").trim(),
    t: x.datePublished ? new Date(String(x.datePublished).replace(/([+-]\d{2})(\d{2})$/, "$1:$2")) : null,
    img: x["cn:promoImage"] || ""
  })).filter(x => x.title && x.url && x.t && !isNaN(x.t));
};
window.pSparkAll = async function pSparkAll(syms, range, interval){
  if(!syms.length) return {};
  /* Today's prices + session series: straight from CNBC (one quote request + one chart request per symbol, in
     parallel), no proxy. The old proxy race below only runs for whatever CNBC didn't return, or for a
     multi-day range. */
  if(!range || range === "1d"){
    const bucket = /^(\d+)m$/.test(interval || "") ? +interval.slice(0, -1) : 5;
    const out = {};
    try{
      const [qs, series] = await Promise.all([
        cnbcQuotes(syms).catch(() => ({})),
        Promise.all(syms.map(s => cnbcIntraday(s, bucket).catch(() => null)))
      ]);
      syms.forEach((s, i) => { if(qs[s]) out[s] = {...qs[s], series: series[i] || null}; });
    }catch(e){}
    const rest = syms.filter(s => !out[s]);
    if(!rest.length) return out;
    syms = rest;
    const viaProxy = await withDeadline(pSparkAllProxy(syms, range, interval), 6000, {});
    return Object.assign(viaProxy, out);
  }
  return pSparkAllProxy(syms, range, interval);
};
window.pSparkAllProxy = async function pSparkAllProxy(syms, range, interval){
  if(!syms.length) return {};
  // see the cache-buster note in pQuote() above -- same fix, same reason.
  const target = `https://query1.finance.yahoo.com/v7/finance/spark?symbols=${encodeURIComponent(syms.join(","))}&range=${range || "1d"}&interval=${interval || "5m"}&_ts=${Date.now()}`;
  const urls = NR_PROXY_URLS(target);   // same three-proxy race the newsroom uses -- allorigins goes down for stretches and corsproxy.io now wants a paid key, so two was really one
  // Race both proxies instead of trying them one after another -- see pQuote() above.
  const attempts = urls.map(u => (async () => {
    const r = await pFetchTimeout(u, (range && range !== "1d") ? 16000 : 8000, {cache:"no-store"}); if(!r.ok) throw new Error("bad status");
    const j = await r.json(); const results = j?.spark?.result;
    const out = {};
    if(Array.isArray(results) && results.length){
      for(const x of results){
        const resp = x.response?.[0]; const m = resp?.meta;
        if(x.symbol && m && typeof m.regularMarketPrice === "number"){
          const reg = m.currentTradingPeriod?.regular;
          out[x.symbol] = {
            price: m.regularMarketPrice,
            prev: (m.chartPreviousClose ?? m.previousClose ?? m.regularMarketPrice),
            time: m.regularMarketTime,
            series: (resp.timestamp && resp.indicators?.quote?.[0]?.close)
              ? {t:resp.timestamp, c:resp.indicators.quote[0].close, start:reg?.start, end:reg?.end}
              : null
          };
        }
      }
    }
    if(!Object.keys(out).length) throw new Error("no data");
    return out;
  })());
  try{ return await Promise.any(attempts); }catch(e){ return {}; }
};
window.pApplyQuotes = function pApplyQuotes(map, src, at){
  let n = 0;
  for(const [sym, q] of Object.entries(map || {})){
    if(!q || typeof q.price !== "number") continue;
    const cur = P.live[sym];
    /* A live quote is authoritative over a snapshot/cache one even if its stamp is OLDER: before Oct 2026 the
       server stamped CNBC quotes with the run time, so a fund still on yesterday's NAV carried a too-new stamp
       that would otherwise block the correctly-dated live quote. */
    if(cur && cur.time && q.time && q.time < cur.time && !(src === "live" && cur.src !== "live")) continue;
    P.live[sym] = {price:q.price, prev:(q.prev ?? q.price), time:q.time, src};
    if(q.series && q.series.t && q.series.c){ P.intraday = P.intraday || {}; P.intraday[sym] = q.series; }
    n++;
  }
  if(n){
    P.liveAt = at || Date.now(); P.liveSrc = src; P.fetched = true; P.intraReady = true;
    pPickIntraMeta(); pSaveQuoteCache();
  }
  return n;
};
window.pFetchLive = async function pFetchLive(){
  if(P._fetchPromise) return P._fetchPromise;   // dedupe concurrent callers (Today snapshot fires two at once)
  pLoadQuoteCache();
  P._fetchPromise = (async () => {
    P.fetching = true;
    pRepaintLive(false);
    const syms = [...new Set(HOLDINGS.positions.filter(p => p.type !== "cash").map(p => p.fetchSymbol || p.ticker))];
    // 1. server snapshot -- lands in ~200ms and paints immediately, whatever the proxies do
    const server = pFetchServerQuotes().then(sv => {
      if(sv && pApplyQuotes(sv.quotes, "server", sv.at)) pRepaintLive(true);
    });
    // 2. live quotes + today's session bars, straight from CNBC (see pSparkAll); proxies only as a last resort
    const spark = await pSparkAll(syms);
    const n = pApplyQuotes(spark, "live", Date.now());
    const missing = syms.filter(x => !spark[x]);
    // only chase stragglers one by one if the proxies are answering at all -- when both are
    // down this used to add a second full 8-second timeout for every single symbol
    if(missing.length && n) await withDeadline(Promise.all(missing.map(async x => { const q = await pQuote(x); if(q) pApplyQuotes({[x]:q}, "live", Date.now()); })), 6000, null);
    await server;
  })();
  try{ await P._fetchPromise; }
  catch(e){ console.warn("Daily Hub: live price refresh failed", e); }
  finally{
    // Whatever happened, nothing stays stuck on "fetching…" -- the previous version left
    // P.fetching true forever if anything in here threw.
    P.fetching = false; P._fetchPromise = null; P.intraReady = true;
    pRepaintLive(true);
  }
};
window.pHistCacheLoad = function pHistCacheLoad(){
  try{
    const c = JSON.parse(localStorage.getItem(HCACHE_KEY) || "null");
    if(c && c.day === etToday() && c.data && Object.keys(c.data).length){
      const out = {}; for(const [k,h] of Object.entries(c.data)) out[k] = pCleanHist(k, h);
      return out;
    }
  }catch(e){}
  return null;
};
window.pHistCacheSave = function pHistCacheSave(data){
  try{
    const slim = {};
    for(const [k,h] of Object.entries(data)) slim[k] = {t:h.t, c:h.c.map(v => v == null ? null : Math.round(v*10000)/10000)};
    localStorage.setItem(HCACHE_KEY, JSON.stringify({day:etToday(), data:slim}));
  }catch(e){ try{ localStorage.removeItem(HCACHE_KEY); }catch(_){} }
};
window.pPool = async function pPool(items, n, fn){
  const q = items.slice(), workers = [];
  for(let i = 0; i < Math.min(n, q.length); i++) workers.push((async () => { while(q.length){ const x = q.shift(); try{ await fn(x); }catch(e){} } })());
  await Promise.all(workers);
};
window.pHistOne = async function pHistOne(sym){
  const bh = await withDeadline(boardHistory(), 6000, null);
  const srv = bh && bh.series && bh.series[sym] && bh.series[sym].t && bh.series[sym].t.length > 30 ? pCleanHist(sym, bh.series[sym]) : null;
  const lastT = srv ? srv.t[srv.t.length-1] : 0;
  const stale = !srv || srv.t.length < 250 || (Date.now()/1000 - lastT) > 5*86400;
  let h = srv;
  if(stale){ const cn = await cnbcDaily(sym).catch(() => null); h = pMergeHist(srv, cn); }
  if(!h || !h.t || h.t.length < 30){ const px = await withDeadline(fetchHistSym(sym), 8000, null); if(px) h = pMergeHist(h, px); }
  return h && h.t && h.t.length ? pCleanHist(sym, h) : null;
};
window.loadHistory = async function loadHistory(){
  if(P.histReady) return;
  if(P.histLoading) return P._histP;
  P.histLoading = true;
  P._histP = (async () => {
    const syms = [...new Set(HOLDINGS.positions.filter(p => p.type !== "cash").map(p => p.fetchSymbol || p.ticker))];
    const cached = pHistCacheLoad();
    if(cached && syms.every(x => cached[x])) P.histData = cached;
    else {
      const data = {};
      await pPool(syms, 4, async s => {
        const h = await pHistOne(s);
        if(h) data[s] = h; else if(cached && cached[s]) data[s] = cached[s];
      });
      P.histData = data;
      if(Object.keys(data).length) pHistCacheSave(data);
    }
    P.histStamp = Date.now();
    P.histLoading = false; P.histReady = true;
    if(S.tab === "portfolio"){ renderHistory(); pRepaintTiles(); drawAlloc({risk:true}); }
  })();
  return P._histP;
};
window.pHistFor = async function pHistFor(sym){
  if(P.histData && P.histData[sym]) return P.histData[sym];
  if(P.stockHist[sym]) return P.stockHist[sym];
  if(!P._oneHist) P._oneHist = {};
  if(!P._oneHist[sym]) P._oneHist[sym] = pHistOne(sym).then(h => { if(h) P.stockHist[sym] = h; return h; }).finally(() => { delete P._oneHist[sym]; });
  return P._oneHist[sym];
};
window.pPeriodEnds = function pPeriodEnds(bars){
  const out = [];
  for(let i = 0; i + 1 < bars.length; i++){
    let d = new Date(bars[i+1][0]*1000 - 864e5);
    let ds = d.toLocaleDateString("en-CA", {timeZone:"America/New_York"});
    while(isWeekendDay(ds)){ d = new Date(d.getTime() - 864e5); ds = d.toLocaleDateString("en-CA", {timeZone:"America/New_York"}); }
    out.push([dayEpoch(ds), bars[i][1]]);
  }
  return out;
};
window.cnbcLongHist = async function cnbcLongHist(sym, daily){
  const [all, wk] = await Promise.all([cnbcBars(sym, "ALL", 15000).catch(() => []), cnbcBars(sym, "10Y", 15000).catch(() => [])]);
  let pts = daily && daily.t && daily.t.length ? daily.t.map((t,i) => [t, daily.c[i]]) : [];
  for(const layer of [pPeriodEnds(wk), pPeriodEnds(all)]){
    const floor = pts.length ? pts[0][0] : Infinity;
    pts = layer.filter(([t]) => t < floor - 2*86400).concat(pts);
  }
  return pts.length > 30 ? { t: pts.map(p => p[0]), c: pts.map(p => p[1]) } : null;
};
window.pStitch = function pStitch(long, daily){
  if(!daily || !daily.t || !daily.t.length) return long;
  if(!long || !long.t || !long.t.length) return daily;
  const first = daily.t[0] - 2*86400, t = [], c = [];
  long.t.forEach((ts,i) => { if(ts < first && long.c[i] != null){ t.push(ts); c.push(long.c[i]); } });
  return { t: t.concat(daily.t), c: c.concat(daily.c) };
};
window.pHistAllFor = async function pHistAllFor(sym){
  const daily = await pHistFor(sym);
  const all = await withDeadline(boardHistoryAll(), 15000, null);
  const srv = all && all.series && all.series[sym] && all.series[sym].t && all.series[sym].t.length > 30 ? pCleanHist(sym, all.series[sym]) : null;
  if(srv && (!daily || srv.t[0] < daily.t[0] - 400*86400)) return pStitch(srv, daily);
  const cn = await cnbcLongHist(sym, daily).catch(() => null);
  return cn || pStitch(srv, daily) || daily;
};
window.loadHistoryAll = async function loadHistoryAll(){
  if(P.histAllReady || P.histAllLoading) return;
  P.histAllLoading = true; P.histAllData = P.histAllData || {};
  if(!P.histReady) await loadHistory();
  const syms = [...new Set(HOLDINGS.positions.filter(p => p.type !== "cash").map(p => p.fetchSymbol || p.ticker))];
  let done = 0;
  await pPool(syms, 4, async s => {
    if(!P.histAllData[s]){ const h = await pHistAllFor(s); if(h && h.t && h.t.length) P.histAllData[s] = h; }
    done++;
    const el = $("#histAllProg"); if(el) el.textContent = `Loading full price history… ${done} of ${syms.length}`;
  });
  P.histAllLoading = false;
  if(Object.keys(P.histAllData).length){ P.histAllStamp = Date.now(); P.histAllReady = true; }
  if(S.tab === "portfolio" && P.histRange === "ALL") renderHistory();
};
window.fetchHistSym = async function fetchHistSym(sym, range){
  const target = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(sym)}?interval=1d&range=${range || "5y"}`;
  const urls = NR_PROXY_URLS(target);   // same three-proxy race the newsroom uses -- allorigins goes down for stretches and corsproxy.io now wants a paid key, so two was really one
  for(const u of urls){
    try{
      const r = await pFetchTimeout(u, 8000, {cache:"no-store"}); if(!r.ok) continue;   // was a bare fetch: a hung proxy held it open forever
      const j = await r.json(); const res = j?.chart?.result?.[0];
      if(res?.timestamp && res?.indicators?.quote?.[0]?.close) return {t:res.timestamp, c:res.indicators.quote[0].close};
    }catch(e){}
  }
  return null;
};
window.openStockModal = async function openStockModal(ticker){
  const pos = pViewMerged().find(p => p.ticker === ticker);
  if(!pos || pos.type === "cash") return;   // no price history to show for a cash sweep
  closeStockModal();
  const sym = pos.fetchSymbol || pos.ticker;
  SM = { ticker, sym, range: "1Y" };

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.id = "stockModalOverlay";
  overlay.innerHTML = `<div class="modal-card">
    <button class="modal-close" id="smClose">✕</button>
    <div class="sm-head">
      <div class="mk" style="background:${pcolor(pos.ticker)}">${esc(pos.ticker.slice(0,4))}</div>
      <div>
        <div class="sm-tk">${esc(pos.ticker)}<span class="sm-nm">${esc(pos.name)}</span></div>
        <div class="sm-price">${usd(pos.price)} <span class="pchgline ${pcls(pos.dayDollar)}">${usdS(pos.dayDollar)} (${pctS(pos.dayPct)}) today</span></div>
      </div>
    </div>
    <div class="ptoolbar" style="margin:14px 0 8px">
      <div class="seg" id="smSeg">${SM_RANGES.map(r => `<button data-r="${r}" class="${SM.range===r?"on":""}">${r}</button>`).join("")}</div>
      <span class="pstatus" id="smHead"></span>
    </div>
    <div id="smBody" style="position:relative"><div class="spinner" style="margin:46px auto"></div></div>
    ${pos.hasCost ? `<div class="sm-costrow">
        <span>Your avg cost: <b>${usd(pos.avgCost)}</b> · ${pos.shares.toLocaleString("en-US",{maximumFractionDigits:8})} shares</span>
        <span class="${pcls(pos.gainPct)}">${pctS(pos.gainPct)} (${usdS(pos.gainDollar)}) since purchase</span>
      </div>` : `<div class="sm-costrow"><span style="color:var(--muted)">No cost basis on record for ${esc(pos.ticker)}.</span></div>`}
    <div class="chart-cap">Yahoo Finance daily closes (unofficial) · dashed line marks your average cost per share · educational, not investment advice.</div>
  </div>`;
  document.body.appendChild(overlay);

  overlay.addEventListener("click", e => { if(e.target === overlay) closeStockModal(); });
  $("#smClose").addEventListener("click", closeStockModal);
  SM._escHandler = e => { if(e.key === "Escape") closeStockModal(); };
  document.addEventListener("keydown", SM._escHandler);
  overlay.querySelectorAll("#smSeg button").forEach(b => b.addEventListener("click", () => {
    SM.range = b.dataset.r;
    overlay.querySelectorAll("#smSeg button").forEach(x => x.classList.toggle("on", x === b));
    renderStockChart(pos);
  }));

  /* This holding's own history only (server file -> CNBC -> capped proxy), never "every holding first".
     Before Oct 2026 a click with no cached history waited up to 15s on dead proxies, then said "unavailable". */
  if(!P.stockHist[sym]){
    SM.loading = true;
    const h = await withDeadline(pHistFor(sym), 14000, null);
    if(h) P.stockHist[sym] = h;
    if(SM) SM.loading = false;
  }
  if(SM && SM.ticker === ticker) renderStockChart(pos);
  /* Today's session bars for this one symbol if the page doesn't have them yet (funds have none -- NAV only) */
  if(!(P.intraday && P.intraday[sym] && intraIsToday()) && !isMutualFund(sym)){
    const ser = await cnbcIntraday(sym).catch(() => null);
    if(ser){ P.intraday = P.intraday || {}; P.intraday[sym] = ser; if(!P.intraMeta && !isCryptoSym(sym)) P.intraMeta = {start:ser.start, end:ser.end}; }
    if(SM && SM.ticker === ticker && SM.range === "Today") renderStockChart(pos);
  }
};
window.renderStockChart = function renderStockChart(pos){
  const body = $("#smBody"); if(!body || !SM) return;
  const sym = SM.sym, range = SM.range;
  const head = $("#smHead");

  if(range === "Today"){
    const raw = P.intraday && P.intraday[sym];
    const ser = raw ? { pts: raw.t.map((ts,i) => ({ts, v:raw.c[i]})).filter(p => p.v != null),
                        start: raw.start ?? P.intraMeta?.start, end: raw.end ?? P.intraMeta?.end } : null;
    if(!ser || ser.pts.length < 2 || ser.start == null){
      const why = isMutualFund(sym) ? `${esc(pos.ticker)} is a mutual fund — it prices once a day after the close, so there is no intraday chart.` : `Today's intraday data isn't available for ${esc(pos.ticker)} right now.`;
      body.innerHTML = `<div class="empty">${why}</div>`; if(head) head.textContent = ""; return; }
    const first = ser.pts[0].v, last = ser.pts[ser.pts.length-1].v, delta = last - first, dPct = first ? delta/first*100 : 0;
    if(head) head.innerHTML = `<span class="hist-delta ${pcls(delta)}">${usdS(delta)} (${pctS(dPct)})</span>&nbsp; Today's session`;
    drawSmChart(body, ser.pts.map(p => ({x:p.ts, v:p.v})), {
      timeAxis:true, start:ser.start, end:ser.end,
      refLine: pos.hasCost ? pos.avgCost : null, refLabel: pos.hasCost ? `Your cost ${usd(pos.avgCost)}` : "",
      fmtX0: () => "Open", fmtX1: () => "Close",
      fmtTip: p => `${esc(new Date(p.x*1000).toLocaleTimeString("en-US",{timeZone:"America/New_York",hour:"numeric",minute:"2-digit"}))} ET · <span class="tv">${usd(p.v)}</span>`
    });
    return;
  }

  let hist = P.stockHist[sym] || (P.histData && P.histData[sym]);
  if(range === "ALL"){
    /* the full history (Yahoo range=max) is fetched the first time ALL is chosen for a holding */
    P.stockAll = P.stockAll || {}; P.stockAllLoading = P.stockAllLoading || {};
    const have = (P.histAllData && P.histAllData[sym]) || P.stockAll[sym];
    if(have) hist = have;
    else if(P.stockAll[sym] === false){ body.innerHTML = `<div class="empty">Full price history isn't available for ${esc(pos.ticker)} right now \u2014 pick another range.</div>`; if(head) head.textContent = ""; return; }
    else {
      body.innerHTML = `<div class="spinner" style="margin:46px auto"></div>`; if(head) head.textContent = "";
      if(!P.stockAllLoading[sym]){
        P.stockAllLoading[sym] = true;
        withDeadline(pHistAllFor(sym), 20000, null).then(h => {
          P.stockAllLoading[sym] = false;
          P.stockAll[sym] = (h && h.t && h.t.length) ? h : false;
          if(SM && SM.sym === sym && SM.range === "ALL") renderStockChart(pos);
        });
      }
      return;
    }
  }
  if(!hist || !hist.t || !hist.t.length){
    body.innerHTML = SM.loading ? `<div class="spinner" style="margin:46px auto"></div>` : `<div class="empty">Price history is unavailable right now — try Refresh.</div>`;
    if(head) head.textContent = ""; return; }
  /* The trading date of a daily bar is its UTC date (bars are stamped between 00:00 and 13:30 New York time).
     Before Oct 2026 this used the DEVICE's zone: in Central time a bar stamped 00:00 ET is 11pm the day before,
     so every point in this window sat one day early (and today's live point was appended a day late). */
  const dstr = ts => new Date(ts*1000).toISOString().slice(0,10);
  let pts = hist.t.map((ts,i) => ({d: dstr(ts), v: hist.c[i]})).filter(p => p.v != null);
  let cutoff;
  if(range === "ALL") cutoff = "0000-00-00";
  else if(range === "YTD") cutoff = etToday().slice(0,4) + "-01-01";
  else { const days = HIST_RANGES[range] || HIST_RANGES["1M"]; cutoff = new Date(Date.now() - days*864e5).toLocaleDateString("en-CA", {timeZone:"America/New_York"}); }
  let view = pts.filter(p => p.d >= cutoff);
  if(view.length < 2) view = pts.slice(-2);
  if(view.length < 2){ body.innerHTML = `<div class="empty">Not enough price history for that range yet.</div>`; if(head) head.textContent = ""; return; }
  // splice in today's live price if it's newer than the last daily close on record
  const today = etToday();
  if(pos.price != null && view[view.length-1].d < today) view.push({d: today, v: pos.price});

  const first = view[0].v, last = view[view.length-1].v, delta = last - first, dPct = first ? delta/first*100 : 0;
  if(head) head.innerHTML = `<span class="hist-delta ${pcls(delta)}">${usdS(delta)} (${pctS(dPct)})</span>&nbsp; ${esc(fullDate(view[0].d))} → ${esc(fullDate(view[view.length-1].d))}`;
  drawSmChart(body, view.map(p => ({x:dayEpoch(p.d), d:p.d, v:p.v})), {
    dateAxis:true,
    refLine: pos.hasCost ? pos.avgCost : null, refLabel: pos.hasCost ? `Your cost ${usd(pos.avgCost)}` : "",
    fmtTip: p => `${esc(longDate(p.d))} · <span class="tv">${usd(p.v)}</span>`
  });
};
window.renderPortfolio = function renderPortfolio(){
  pCompute();
  const t = pTotals(pView());
  const acctLabel = P.acct === "all" ? "all accounts" : (HOLDINGS.accounts.find(a=>a.id===P.acct)||{}).label;

  /* hero */
  $("#heroTitle").textContent = usd(t.value);
  $("#heroSub").textContent = `${usdS(t.day)} today (${pctS(t.dayPct)}) · ${usdS(t.gain)} all-time (${pctS(t.gainPct)} on cost) · ${acctLabel}`;
  $("#heroDateline").textContent = `Live prices via Yahoo Finance (unofficial) · Fidelity snapshot ${HOLDINGS.meta.snapshotLabel} · educational, not investment advice`;

  const panel = $("#panel-portfolio");
  let h = "";

  h += `<button class="pbtn game-back" id="pBackToday" style="margin-bottom:14px">← Back to Today</button>`;

  const statusTxt = pStatusText();
  const ledCls = pLedClass();

  /* ---------------------------------------------------------------
     Bento grid. Everything below lives in one 2-column mosaic instead
     of the old full-width stack: control bar across the top, then
     Positions beside Allocation+Calendar, AI Insight beside the value
     history, and the full Holdings table across the bottom.
     --------------------------------------------------------------- */
  h += `<div class="pbento">`;

  /* control bar: account toggle + perf toggle + refresh + live status */
  h += `<div class="pb-bar">
    <div class="acct-toggle">` + [{id:"all",label:"All accounts"}, ...HOLDINGS.accounts].map(a => {
      const list = a.id === "all" ? P.pos : P.pos.filter(p => p.account === a.id);
      const lt = pTotals(list);
      return `<button class="acct-card${P.acct===a.id?" on":""}" data-acct="${a.id}">${esc(a.label)} <b>${usd(lt.value,0)}</b></button>`;
    }).join("") + `</div>
    <div class="pb-bar-r">
      <button class="pbtn" id="pRefresh">⟳ Refresh</button>
      <span class="pstatus"><span class="led ${ledCls}"></span>${esc(statusTxt)}</span>
    </div>
  </div>`;

  /* positions -- one card per holding */
  h += `<section class="pcard pb-pos">
    <header class="pc-head">
      <div><h4>Positions</h4><p>Today's move, size &amp; 30-day trend</p></div>
      <span class="pc-note" id="pPosNote">Best → worst today</span>
    </header>
    <div class="pc-body pb-pos-body"><div class="ptiles" id="pTiles"></div></div>
    <p class="pc-cap">One card per priced holding, best to worst by today's move — the cash sweep is left out (it never moves) and shown in the header instead. The big number is today; underneath it is market value, this position's share of the current view and its all-time gain. The line is the last 30 trading days, the bar across the bottom is position size. Click a card for the full detail view.</p>
  </section>`;

  /* side stack: portfolio value history over the calendar */
  const MON3 = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];
  const calToday = new Date(); calToday.setHours(0,0,0,0);
  const calUpcoming = PORTFOLIO_EVENTS.map(e => ({...e, d:new Date(e.date+"T00:00:00")}))
    .filter(e => e.d >= calToday).sort((a,b) => a.d-b.d).slice(0,4);

  h += `<div class="pb-side">
    <section class="pcard pb-hist">
      <header class="pc-head"><div><h4>Portfolio Value</h4><p>History</p></div></header>
      <div class="seg pb-ranges" id="histSeg">${["Today","1W","1M","3M","6M","YTD","1Y","5Y","ALL"].map(r => `<button data-r="${r}" class="${(P.histRange||"Today")===r?"on":""}">${r}</button>`).join("")}</div>
      <div class="pb-histhead" id="histHead"></div>
      <div id="histBody" style="position:relative"></div>
      <p class="pc-cap">Computed in your browser from today's share quantities held constant across the window (Yahoo daily closes) — trades or contributions made during the period aren't reflected. Positions without daily price data are held at their snapshot price.</p>
    </section>
    <section class="pcard pb-cal">
      <header class="pc-head"><div><h4>Calendar</h4><p>Earnings &amp; events</p></div></header>
      <div class="pb-cal-list">`
        + (calUpcoming.length ? calUpcoming.map(e => `<div class="pb-cal-item${e.account==="crypto"?" crypto":""}">
            <div class="cal-date"><span>${e.d.getDate()}</span><b>${MON3[e.d.getMonth()]}</b></div>
            <div class="cal-label"><b>${esc(e.ticker)}</b><span>${esc(e.label)}</span></div></div>`).join("")
          : `<p class="pb-empty">No upcoming events on record.</p>`)
      + `</div>
    </section>
  </div>`;

  /* allocation + risk -- full width (Eli, Sep 2026: "make it cooler, add a risk score"); drawAlloc() fills #allocBody */
  h += `<section class="pcard pb-alloc">
    <header class="pc-head">
      <div><h4>Allocation &amp; Risk</h4><p>What you own, and how hard it can swing</p></div>
      <div class="seg" id="allocSeg">${[["sector","Sector"],["holding","Holding"],["class","Type"]].map(([k,l]) => `<button data-v="${k}" class="${(P.allocView||"sector")===k?"on":""}">${l}</button>`).join("")}</div>
    </header>
    <div class="alloc-grid" id="allocBody"><div class="spinner" style="margin:40px auto"></div></div>
    <p class="pc-cap">Click any slice or row to filter the Holdings table; tap any factor card to see the working behind it. Sectors are read from each ticker (ETFs by what they hold — e.g. BAI is an AI &amp; tech fund, so it counts as Technology). The score weighs how much of the account sits in growth assets rather than bonds and cash, how hard the mix actually swings, how concentrated it is and how much of it is speculative. Volatility and drawdown are measured from the last year of daily closes once they load, and estimated from what each holding is until then. An educational estimate — not a forecast and not investment advice.</p>
  </section>`;

  /* AI insight -- renderInsightCard() fills this card in; nothing is pre-generated */
  h += `<section class="pcard pb-ai" id="hnews"></section>`;

  /* holdings table */
  h += `<section class="pcard pb-hold">
    <header class="pc-head">
      <div><h4>Holdings</h4><p>All positions across accounts</p></div>
      <div class="pb-hold-tools">
        <div class="seg" id="pQuickSeg"><button data-q="">All</button><button data-q="gain">Gainers</button><button data-q="loss">Losers</button></div>
        <span class="ftag${P.filter?" show":""}" id="pFtag">Filtered: ${esc(P.filter||"")} ✕</span>
        <div class="psearch"><span class="si">⌕</span><input id="pSearch" placeholder="Search ticker or company…" value="${esc(P.search)}"></div>
      </div>
    </header>
    <div class="ptable-wrap"><table class="ptable"><thead><tr id="pHead"></tr></thead><tbody id="pBody"></tbody></table></div>
  </section>`;

  h += `</div>`;
  h += `<p class="sources">Holdings driven by exact share quantities from the Fidelity export (${HOLDINGS.meta.snapshotLabel}). Values = shares × price. Educational use only — not investment advice.</p>`;

  /* Keep the AI Insight card that is on screen across this rebuild. Every price refresh (on open, then every
     5 minutes in market hours, and on account switches) re-runs renderPortfolio(); before Oct 2026 that rebuilt
     the card and re-fired the read from scratch, so a take that was mid-stream was killed and restarted --
     and if refreshes kept landing it never finished at all. */
  const keepAI = document.getElementById("hnews");
  panel.innerHTML = h;
  if(keepAI && keepAI.dataset.built === "1"){ const fresh = document.getElementById("hnews"); if(fresh) fresh.replaceWith(keepAI); }

  /* wire toolbar */
  panel.querySelectorAll("#histSeg button").forEach(b => b.addEventListener("click", () => {
    P.histRange = b.dataset.r;
    panel.querySelectorAll("#histSeg button").forEach(x => x.classList.toggle("on", x === b));
    renderHistory();
  }));
  panel.querySelectorAll("#allocSeg button").forEach(b => b.addEventListener("click", () => {
    P.allocView = b.dataset.v; P.filter = null;
    panel.querySelectorAll("#allocSeg button").forEach(x => x.classList.toggle("on", x === b));
    pSyncFilterTag(); drawAlloc(); pTable();
  }));
  const backBtn = $("#pBackToday");
  if(backBtn) backBtn.addEventListener("click", () => animatedTabSwitch(async () => {
    S.tab = "calendar"; localStorage.setItem("dh-tab","calendar");
    markTab("calendar");
    document.querySelectorAll(".panel").forEach(pn => pn.classList.toggle("active", pn.id==="panel-calendar"));
    fillDates(); await show(); window.scrollTo({top:0,behavior:"smooth"});
  }));
  $("#pRefresh").addEventListener("click", () => pFetchLive());
  panel.querySelectorAll(".acct-card").forEach(c => c.addEventListener("click", () => { P.acct = c.dataset.acct; P.filter = null; renderPortfolio(); }));
  $("#pFtag").addEventListener("click", () => { P.filter = null; renderPortfolio(); });
  panel.querySelectorAll("#pQuickSeg button").forEach(b => {
    b.classList.toggle("on", (b.dataset.q || null) === P.quick);
    b.addEventListener("click", () => {
      P.quick = b.dataset.q || null;
      panel.querySelectorAll("#pQuickSeg button").forEach(x => x.classList.toggle("on", x === b));
      pTable();
    });
  });
  const si = $("#pSearch");
  si.addEventListener("input", e => { P.search = e.target.value; pTable(); });

  drawAlloc({risk:true});
  drawPTiles(t);
  pHead(); pTable();
  renderHistory();
  renderInsightCard();

  pAutoFetch();
};
window.renderInsightCard = function renderInsightCard(){
  const el = $("#hnews"); if(!el) return;
  const list = pViewMerged().filter(p => p.type !== "cash");
  const chips = list.map(p => `<button class="ins-chip${P.insTicker===p.ticker?" on":""}" data-ins="${esc(p.ticker)}">
      <span class="mk" style="background:${pcolor(p.ticker)}">${esc(String(p.ticker).slice(0,3))}${logoImg(p.ticker)}</span>${esc(p.ticker)}</button>`).join("");
  const wireChips = () => el.querySelectorAll("[data-ins]").forEach(b => b.addEventListener("click", () => {
    if(P.insTicker === b.dataset.ins) insClear(); else insGenerate(b.dataset.ins);   // click the lit chip again = hide the read
  }));
  if(el.dataset.built === "1" && el.querySelector("#insPick")){
    /* a repaint: refresh the chips only; the read underneath stays exactly as it is */
    el.querySelector("#insPick").innerHTML = chips || `<span class="ins-empty">No holdings in this view.</span>`;
    wireChips();
    if(P.insTicker && !list.some(p => p.ticker === P.insTicker)) insClear();
    return;
  }
  el.dataset.built = "1";
  el.innerHTML = `<div class="ai-head">
      <span class="ai-head-l"><svg width="15" height="15" viewBox="0 0 24 24" fill="var(--gold)"><path d="M12 2l1.8 5.4L19 9l-5.2 1.6L12 16l-1.8-5.4L5 9l5.2-1.6L12 2z"/></svg>AI Insight</span>
      <span class="ai-head-r">Written live when you click</span>
    </div>
    <div class="ins-key-warn" id="insKeyWarn" hidden></div>
    <div class="ins-pick" id="insPick">${chips || `<span class="ins-empty">No holdings in this view.</span>`}</div>
    <div id="insBody">${P.insCleared ? "" : `<p class="ins-empty">Pick any holding above and get a fresh take on what's going on with it — the latest news, in plain English, written on the spot — with the price picture underneath.</p>`}</div>`;
  wireChips();
  insKeyWarn();
  if(P.insTicker && list.some(p => p.ticker === P.insTicker)) insGenerate(P.insTicker);
};
window.insFetchNews = async function insFetchNews(p){
  const name = String(p.name || "").replace(/\b(inc|corp|corporation|company|co|plc|ltd|holdings|class [a-c]|the|fund|retail shares|etf|trust)\b\.?/gi, "").replace(/\s+/g, " ").trim();
  const nameKey = insNorm(name).split(" ").filter(w => w.length > 2).slice(0, 2).join(" ");
  const tick = new RegExp("\\b" + String(p.ticker).replace(/[^A-Za-z0-9]/g, "") + "\\b", "i");
  const mentions = it => tick.test(it.title + " " + it.summary) || (nameKey && insNorm(it.title + " " + it.summary).includes(nameKey));
  const [byTicker, byName] = await Promise.all([
    cnbcNews(p.ticker, 15).catch(() => []),
    name.length > 3 ? cnbcNews(name, 15).catch(() => []) : Promise.resolve([])
  ]);
  const own = insDedupe(byTicker.concat(byName).filter(mentions));
  let context = [];
  if(own.length < 4){
    const qs = SECTOR_PROXY[sectorOf(p)] || [];
    for(const q of qs){
      context = context.concat(await cnbcNews(q, 8).catch(() => []));
      if(context.length >= 6) break;
    }
    context = insDedupe(context).filter(c => !own.some(o => insNorm(o.title) === insNorm(c.title)));
  }
  return { own: own.slice(0, 8), context: context.slice(0, 4) };
};
window.insDedupe = function insDedupe(items){
  const seen = new Set(), out = [];
  for(const it of items){
    const k = insNorm(it.title).slice(0, 60);
    if(!k || seen.has(k)) continue;
    seen.add(k); out.push(it);
  }
  return out.sort((a,b) => (b.t ? b.t.getTime() : 0) - (a.t ? a.t.getTime() : 0));
};
window.insSeries = function insSeries(sym){
  const h = (P.histData && P.histData[sym]) || (P.stockHist && P.stockHist[sym]);
  const c = h && h.c ? h.c.filter(v => v != null) : [];
  if(c.length < 30) return null;
  const last = c[c.length-1];
  const ma = n => c.length >= n ? c.slice(-n).reduce((a,b) => a+b, 0)/n : null;
  const ret = n => c.length > n ? (last/c[c.length-1-n] - 1)*100 : null;
  const yr = c.slice(-252);
  const hi = Math.max(...yr), lo = Math.min(...yr);
  const r = [];
  for(let i=1; i<yr.length; i++) if(yr[i-1] > 0) r.push(yr[i]/yr[i-1] - 1);
  const mean = r.length ? r.reduce((a,b) => a+b, 0)/r.length : 0;
  const sd = r.length ? Math.sqrt(r.reduce((a,b) => a + (b-mean)*(b-mean), 0)/r.length) : 0;
  return { last, ma50: ma(50), ma200: ma(200), r21: ret(21), r63: ret(63), r252: ret(252),
           hi, lo, rangePos: hi > lo ? (last-lo)/(hi-lo)*100 : 50, dayVol: sd*100, days: c.length, closes: c };
};
window.insApiKey = function insApiKey(){ try{ return localStorage.getItem(INS_KEY_LS) || ""; }catch(e){ return ""; } };
window.insKeyBad = function insKeyBad(){ try{ const k = insApiKey(); return !!k && localStorage.getItem(INS_BAD_LS) === insKeyFp(k); }catch(e){ return false; } };
window.insKeyUsable = function insKeyUsable(){ return !!insApiKey() && !insKeyBad(); };
window.insKeyWarn = function insKeyWarn(){
  const w = $("#insKeyWarn"); if(!w) return;
  const k = insApiKey();
  if(k && insKeyBad()){
    w.hidden = false;
    w.innerHTML = `<b>Claude isn't connected.</b> Anthropic rejected the API key saved in this browser (it was revoked, rotated or mistyped), so you're getting the quick read instead of Claude's live take. <button type="button" class="ins-key-btn warn" id="insKeyFix">Replace Claude key</button>`;
  } else if(!k){
    w.hidden = false;
    w.innerHTML = `<b>Claude is off in this browser.</b> Add an Anthropic API key to get the live, searched take; until then you get the quick read. <button type="button" class="ins-key-btn" id="insKeyFix">Turn on Claude</button>`;
  } else { w.hidden = true; w.innerHTML = ""; }
  const b = $("#insKeyFix"); if(b) b.addEventListener("click", insKeyPrompt);
};
window.insTakeLocal = async function insTakeLocal(p, st, spy, token, tw, setSub, setSrc){
  const up = p.dayPct >= 0, mv = Math.abs(p.dayPct || 0);
  const typical = st && st.dayVol ? mv / st.dayVol : null;
  const size = typical == null ? "" : typical < .6 ? "a quiet session" : typical < 1.4 ? "an ordinary-sized move" : typical < 2.5 ? "a big move for it" : "an outlier move";
  let head = `**${p.ticker} is ${up ? "up" : "down"} ${mv.toFixed(2)}% today${size ? " — " + size : ""}.**\n`;
  if(st && spy != null && st.r63 != null){ const gap = st.r63 - spy; head += `- Over three months it has ${gap >= 0 ? "beaten" : "lagged"} the S&P 500 by ${Math.abs(gap).toFixed(1)} points.\n`; }
  if(st) head += `- It sits ${st.rangePos.toFixed(0)}% of the way up its 52-week range, ${st.ma200 != null && st.last >= st.ma200 ? "above" : "below"} its 200-day average.\n`;
  tw.push(head);
  setSub("checking the latest headlines…");
  let news = { own:[], context:[] };
  try{ news = await withDeadline(insFetchNews(p), 8000, news); }catch(e){}
  if(token !== P.insToken) return true;
  const fresh = news.own.filter(x => x.t && Date.now() - x.t.getTime() < 4*864e5);
  let more = "";
  if(fresh.length){
    const tags = {}; fresh.forEach(x => { const t = insNewsTag(x.title).label; tags[t] = (tags[t] || 0) + 1; });
    const themes = Object.entries(tags).sort((a,b) => b[1]-a[1]).map(([k]) => k.toLowerCase()).filter(k => k !== "coverage").slice(0, 2);
    more += `- In the news over the last few days: mostly ${themes.length ? themes.join(" and ") : "general coverage"}, across ${fresh.length} stor${fresh.length === 1 ? "y" : "ies"}. Latest: “${fresh[0].title}”.\n`;
  } else {
    more += `- Nothing company-specific broke in the last few days, so this looks like it's trading with ${sectorOf(p) === "Other" ? "the market" : "its " + sectorOf(p).toLowerCase() + " theme"}.\n`;
    if(news.context[0]) more += `- Backdrop for the theme: “${news.context[0].title}”.\n`;
  }
  tw.push(more); tw.finish();
  setSrc(news.own.concat(news.context).slice(0, 4).map(x => [x.url, (x.outlet ? x.outlet + ": " : "") + x.title]));
  return true;
};
window.insTake = async function insTake(p, st, spy, token){
  const body = $("#insTakeB"), subEl = $("#insTakeS"), srcEl = $("#insTakeSrc"); if(!body) return;
  const setSub = t => { if(subEl && document.contains(subEl)) subEl.textContent = t; };
  const setSrc = list => { if(!srcEl || !document.contains(srcEl)) return;
    srcEl.innerHTML = list.length ? `<span>Sources</span>` + list.map(([u, t]) => { let host = ""; try{ host = new URL(u).hostname.replace(/^www\./, ""); }catch(e){}
      return `<a href="${esc(u)}" target="_blank" rel="noopener" title="${esc(t)}">${esc(host || t)}</a>`; }).join("") : ""; };
  const keyBtn = `<button class="ins-key-btn${insKeyBad() ? " warn" : ""}" id="insKeyBtn" type="button">${insKeyBad() ? "Replace Claude key" : insApiKey() ? "Claude key ✓" : "Turn on Claude"}</button>`;
  const cacheKey = "dh-take:" + p.ticker;
  let cached = null; try{ cached = JSON.parse(sessionStorage.getItem(cacheKey) || "null"); }catch(e){}
  const tw = insTypewriter(body);
  const capture = tw.push; let all = "";
  tw.push = t => { all += t; capture(t); };
  const wire = () => { const b = $("#insKeyBtn"); if(b) b.addEventListener("click", insKeyPrompt); };
  if(cached && !P.insForce && Date.now() - cached.at < 20*60e3 && cached.ai === insKeyUsable()){
    body.innerHTML = ""; tw.push(cached.text); tw.finish(); setSrc(cached.src || []);
    setSub(`${cached.ai ? "Claude" : "Quick read"} · ${new Date(cached.at).toLocaleTimeString("en-US",{hour:"numeric",minute:"2-digit"})}`);
    srcEl.insertAdjacentHTML("beforeend", keyBtn); wire(); return;
  }
  P.insForce = false;
  let srcList = [];
  const setSrc2 = l => { srcList = l; setSrc(l); };
  let ai = insKeyUsable(), ok = false;
  try{
    body.innerHTML = `<span class="ins-think"><i></i><i></i><i></i></span>`;
    let first = true; const push0 = tw.push; tw.push = t => { if(first){ body.innerHTML = ""; first = false; } push0(t); };
    ok = ai ? await insTakeClaude(p, st, spy, token, tw, setSub, setSrc2) : await insTakeLocal(p, st, spy, token, tw, setSub, setSrc2);
  }catch(e){
    if(token !== P.insToken) return;
    if(ai){
      const bad = e.status === 401 || e.status === 403;
      if(bad){ try{ localStorage.setItem(INS_BAD_LS, insKeyFp(insApiKey())); }catch(_){} insKeyWarn(); }
      const why = bad ? "Claude key rejected — quick read below" :
        e.status === 429 ? "Claude is rate-limited right now — quick read below" :
        e.status === 529 || e.status >= 500 ? "Claude is overloaded right now — quick read below" :
        "Claude error: " + String(e.message || e).slice(0, 80) + " — quick read below";
      console.warn("Daily Hub AI Insight:", e.status || "", e.message || e);
      ai = false; body.innerHTML = "";
      try{ ok = await insTakeLocal(p, st, spy, token, tw, () => {}, setSrc2); }catch(e2){}
      if(token === P.insToken){ setSub(why); P._insWhy = why; }
    }
  }
  if(token !== P.insToken) return;
  if(ok){
    setSub(ai ? "Claude · just now" : (P._insWhy || (insKeyBad() ? "Quick read · Claude key rejected" : "Quick read · add a Claude key for the full take")));
    P._insWhy = "";
    try{ sessionStorage.setItem(cacheKey, JSON.stringify({ at:Date.now(), text:all, src:srcList, ai })); }catch(e){}
  } else if(!all){ body.innerHTML = `<p class="ins-empty">Couldn't pull anything just now. Hit Redo in a minute.</p>`; }
  if(srcEl) srcEl.insertAdjacentHTML("beforeend", keyBtn); wire();
};
window.insKeyPrompt = function insKeyPrompt(){
  const cur = insApiKey();
  const v = prompt(cur && insKeyBad() ? "Anthropic rejected the saved key (revoked, rotated or mistyped).\nPaste a new key from console.anthropic.com → API keys:" :
    cur ? "Your Anthropic API key is saved in this browser only.\nPaste a new one to replace it, or clear the box to remove it:" :
    "Paste an Anthropic API key (console.anthropic.com → API keys).\nIt's stored only in this browser, never on GitHub, and used just for these stock takes.", cur ? "••••" + cur.slice(-4) : "");
  if(v === null) return;
  try{
    if(!v.trim()){ localStorage.removeItem(INS_KEY_LS); localStorage.removeItem(INS_BAD_LS); }
    else if(!v.startsWith("••••")){ localStorage.setItem(INS_KEY_LS, v.trim()); localStorage.removeItem(INS_BAD_LS); }
  }catch(e){}
  insKeyWarn();
  if(P.insTicker){ P.insForce = true; insGenerate(P.insTicker); }
};
window.insGenerate = async function insGenerate(ticker){
  P.insTicker = ticker; P.insCleared = false;
  const body = $("#insBody"); if(!body) return;
  document.querySelectorAll("#insPick .ins-chip").forEach(c => c.classList.toggle("on", c.dataset.ins === ticker));
  body.innerHTML = `<div class="spinner" style="margin:26px auto"></div>`;
  const token = P.insToken = (P.insToken || 0) + 1;

  /* Only this holding's history (and SPY's, for the versus-the-market read) -- not every holding's. Before Oct
     2026 this awaited loadHistory(), which on a cold day sat on 45s+ of dead-proxy timeouts before a word. */
  if(!P.histReady) loadHistory();
  const p0 = pViewMerged().find(x => x.ticker === ticker);
  await withDeadline(Promise.all([pHistFor((p0 && p0.fetchSymbol) || ticker), pHistFor("SPY")]), 7000, null);
  if(token !== P.insToken) return;

  const t = pTotals(pView());
  const p = pViewMerged().find(x => x.ticker === ticker);
  if(!p){ body.innerHTML = `<p class="ins-empty">That holding is not in the current account view.</p>`; return; }
  p.weight = t.value > 0 ? p.marketValue/t.value*100 : null;

  let st = insSeries(p.fetchSymbol || p.ticker);
  if(!st){
    try{
      const bh = await withDeadline(boardHistory(), 6000, null), sym = p.fetchSymbol || p.ticker;
      if(bh && bh.series && bh.series[sym] && bh.series[sym].c && bh.series[sym].c.length > 30){
        P.histData = P.histData || {}; P.histData[sym] = {t:bh.series[sym].t, c:bh.series[sym].c}; st = insSeries(sym);
      }
    }catch(e){}
    if(token !== P.insToken) return;
  }
  const spyS = insSeries("SPY");
  const built = insBuild(p, st, spyS ? spyS.r63 : null);
  const up = p.dayPct >= 0;

  /* Paint everything that needs no network first, then drop the headlines in when they land —
     the price picture should never sit behind a news request. */
  body.innerHTML = `<div class="ins-head">
      <span class="mk" style="background:${pcolor(p.ticker)}">${esc(String(p.ticker).slice(0,3))}${logoImg(p.ticker)}</span>
      <span class="ins-head-t"><b>${esc(p.ticker)}${p.name ? " · " + esc(p.name) : ""}</b><span>${esc(sectorOf(p))} · ${usd(p.marketValue,0)} held</span></span>
      <span class="ins-head-px"><b class="${up?"pos":"neg"}">${usd(p.price)}</b><span class="${up?"pos":"neg"}">${esc(pctS(p.dayPct))} · ${esc(usdS(p.dayDollar))}</span></span>
      <button class="ins-regen" id="insRegen">⟳ Redo</button>
    </div>
    <div class="ins-hero">${insSpark(st, up)}<div class="ins-hero-cap"><span>Last 90 trading days</span><b>${esc(built.verdict)}</b></div></div>
    <div class="ins-take" id="insTake">
      <div class="ins-take-h"><span class="ins-take-ic"><svg viewBox="0 0 24 24"><path d="M12 2l1.8 5.4L19 9l-5.2 1.6L12 16l-1.8-5.4L5 9l5.2-1.6L12 2z"/></svg></span>
        <b>What's going on with ${esc(p.ticker)}</b><span class="ins-take-s" id="insTakeS">searching the latest news…</span></div>
      <div class="ins-take-b" id="insTakeB"><span class="ins-think"><i></i><i></i><i></i></span></div>
      <div class="ins-take-src" id="insTakeSrc"></div>
    </div>
    ${insVisuals(p, st, spyS ? spyS.r63 : null)}
    <details class="ins-more"><summary>Full written read — every number behind the picture</summary>
      <div class="ins-more-b" id="insMoreB">
        ${built.rows.map(r => `<div class="ins-row"><i style="background:${r.c}"></i><div>${r.txt}</div></div>`).join("")}
      </div></details>
    <p class="ins-foot">The take is searched and written live when you click; the price read is written in your browser at ${new Date().toLocaleTimeString("en-US",{hour:"numeric",minute:"2-digit"})} from the live quote, five years of daily closes and your own cost basis. News next to a move is not proof it caused it. Educational only — not investment advice.</p>`;

  const rb = $("#insRegen");
  if(rb) rb.addEventListener("click", () => { P.insForce = true; insGenerate(ticker); });

  /* the take: Claude searches and writes it live, streamed word by word */
  insTake(p, st, spyS ? spyS.r63 : null, token);

  /* anything the published briefs already said, folded into the written read */
  let notes = [];
  try{ notes = await insBriefNotes(ticker); }catch(e){}
  if(token !== P.insToken || !notes.length) return;
  const mb = $("#insMoreB");
  if(mb) notes.forEach(n => mb.insertAdjacentHTML("beforeend",
    `<div class="ins-row"><i style="background:var(--gold)"></i><div><b>From your ${esc(shortDate(n.date))} brief.</b> ${esc(n.lead)} ${esc(n.body)}</div></div>`));
};
{const st=document.createElement('style'); st.textContent=".ins-key-btn.warn{color:#fff;background:var(--down);border:1px solid var(--down)}\n  .ins-key-warn{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin:2px 0 12px;padding:10px 12px;border-radius:12px;font-size:12.5px;line-height:1.45;\n    background:color-mix(in srgb,var(--down) 9%,transparent);border:1px solid color-mix(in srgb,var(--down) 30%,transparent)}\n  .ins-key-warn[hidden]{display:none}\n  .ins-key-warn .ins-key-btn{margin-left:0}"; document.head.appendChild(st);}
"injected";