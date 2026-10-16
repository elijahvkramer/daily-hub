// Tests for the Portfolio's proxy-free data layer and AI Insight wiring, run against the REAL functions in
// index.html (extracted, not copied). Run: node scripts/test_portfolio_data.js
//
// Regression guards for the Oct 16 2026 portfolio audit:
//   * every free CORS proxy was dead at once, and live prices, intraday, daily history, ALL and AI Insight
//     headlines all depended on them -> CNBC direct is now first (mocked here with its real response shapes);
//   * the stock window labelled daily bars in the DEVICE's time zone (Central) -> every point a day early;
//   * server history carried a phantom Sunday bar every week -> pCleanHist must drop them;
//   * a fund quote stamped date-only must land on that day's close in New York, not the evening before;
//   * AI Insight blocked on every holding's history and was restarted by every price repaint.
"use strict";
process.env.TZ = "America/Chicago";           // the bug only shows up west of New York -- test where Eli is
const fs = require("fs"), path = require("path"), vm = require("vm"), assert = require("assert");
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");

function grabFunction(name){
  const re = new RegExp("(async )?function " + name + "\\(");
  const m = html.match(re); assert.ok(m, "found function " + name);
  const i = m.index;
  let depth = 0, started = false;
  for(let k = html.indexOf("{", i); k < html.length; k++){
    const ch = html[k];
    if(ch === "{"){ depth++; started = true; } else if(ch === "}"){ depth--; if(started && depth === 0) return html.slice(i, k + 1); }
  }
  throw new Error("unbalanced " + name);
}
function grabLine(prefix){
  const m = html.match(new RegExp("^" + prefix.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ".*$", "m"));
  assert.ok(m, "found line " + prefix); return m[0];
}
function grabBlock(startPrefix){        // a multi-line `const X = {...};`
  const i = html.indexOf(startPrefix); assert.ok(i >= 0, "found " + startPrefix);
  return html.slice(i, html.indexOf("};", i) + 2);
}

const src = [
  grabLine("const dayEpoch"), grabFunction("pFetchTimeout"), grabBlock("const CNBC_ALIAS ="),
  grabLine("const cnbcSym"), grabLine("const isCryptoSym"), grabLine("const cnbcNum"), grabLine("const isMutualFund"),
  grabLine("const isWeekendDay"), grabFunction("nyEpoch"), grabFunction("cnbcTime"), grabFunction("cnbcBarTime"),
  grabFunction("cnbcQuotes"), grabFunction("cnbcBars"), grabFunction("cnbcIntraday"), grabFunction("cnbcDaily"),
  grabFunction("pCleanHist"), grabFunction("pMergeHist"), grabFunction("pStitch"), grabFunction("pPeriodEnds"),
  grabFunction("cnbcNews"),
  "const nrSleep = ms => new Promise(r => setTimeout(r, ms));", grabLine("const withDeadline"),
  grabFunction("pSparkAll"),
  "async function pSparkAllProxy(){ PROXY_CALLS.n++; return {}; }",
  "this.api = { dayEpoch, nyEpoch, cnbcTime, cnbcBarTime, cnbcQuotes, cnbcIntraday, cnbcDaily, pCleanHist, pMergeHist, pStitch, pPeriodEnds, cnbcNews, pSparkAll, isMutualFund, cnbcSym };"
].join("\n");

/* ---- a fake CNBC that answers with the real response shapes (captured live Oct 16 2026) ---- */
const PROXY_CALLS = { n: 0 };
const ROUTES = [];
function fakeFetch(url){
  const hit = ROUTES.find(r => r.test(url));
  if(!hit) return Promise.reject(new TypeError("Failed to fetch " + url));
  const body = hit.body(url);
  return Promise.resolve({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) });
}
const ctx = { console, Intl, Date, Math, JSON, Promise, setTimeout, clearTimeout, AbortController, encodeURIComponent,
              Map, Set, Object, Array, String, Number, RegExp, isFinite, isNaN, parseFloat, PROXY_CALLS, fetch: fakeFetch };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const A = ctx.api;

const results = []; let failed = 0;
async function test(name, fn){
  try{ await fn(); results.push("ok  - " + name); }
  catch(e){ failed++; results.push("FAIL- " + name + "\n      " + (e && e.stack || e)); }
}
const nyDate = ts => new Date(ts*1000).toLocaleDateString("en-CA", {timeZone:"America/New_York"});
const nyClock = ts => new Date(ts*1000).toLocaleTimeString("en-US", {timeZone:"America/New_York", hour12:false, hour:"2-digit", minute:"2-digit"});

(async () => {
  await test("New York wall clock -> epoch is right on both sides of DST", () => {
    assert.strictEqual(new Date(A.nyEpoch(2026,10,16,9,30,0)*1000).toISOString(), "2026-10-16T13:30:00.000Z");   // EDT
    assert.strictEqual(new Date(A.nyEpoch(2026,12,16,9,30,0)*1000).toISOString(), "2026-12-16T14:30:00.000Z");   // EST
  });

  await test("a fund quote stamped date-only is that day's 4pm close in New York (not the evening before)", () => {
    const t = A.cnbcTime("2026-10-15");
    assert.strictEqual(nyDate(t), "2026-10-15");
    assert.strictEqual(nyClock(t), "16:00");
    assert.strictEqual(new Date(A.cnbcTime("2026-10-16T16:00:00.783-0400")*1000).toISOString(), "2026-10-16T20:00:00.000Z");
  });

  await test("CNBC chart bar times are New York time", () => {
    assert.strictEqual(nyClock(A.cnbcBarTime("20261016093500")), "09:35");
    assert.strictEqual(nyDate(A.cnbcBarTime("20261016000000")), "2026-10-16");
  });

  await test("pCleanHist drops the phantom Sunday bars, keeps an older weekly stretch, never touches crypto", () => {
    const day = d => A.dayEpoch(d);
    // weekly (Sundays) long ago, then daily with a phantom Sunday carrying another week's close
    const t = [day("2022-01-02"), day("2022-01-09"), day("2022-01-16"),
               day("2026-10-05"), day("2026-10-06"), day("2026-10-07"), day("2026-10-08"), day("2026-10-09"),
               day("2026-10-11"),                                                       // Sunday phantom
               day("2026-10-12"), day("2026-10-13"), day("2026-10-14"), day("2026-10-15")];
    const c = t.map((_, i) => 100 + i);
    const out = A.pCleanHist("BPTRX", {t, c});
    const dates = out.t.map(x => new Date(x*1000).toISOString().slice(0,10));
    assert.ok(!dates.includes("2026-10-11"), "phantom Sunday inside the daily stretch is gone");
    assert.ok(dates.includes("2022-01-14") && !dates.includes("2022-01-09"), "older weekly points are kept, re-dated from their Sunday label to the Friday they closed");
    assert.ok(!dates.some(d => { const w = new Date(d + "T12:00:00Z").getUTCDay(); return w === 0 || w === 6; }), "no weekend date anywhere");
    assert.strictEqual(out.t.length, t.length - 1);
    assert.strictEqual(out.c[dates.indexOf("2022-01-14")], 101, "the value travels with its re-dated bar");
    const btc = A.pCleanHist("BTC-USD", {t, c});
    assert.strictEqual(btc.t.length, t.length, "crypto trades weekends");
  });

  await test("pMergeHist: newer source wins per date; pStitch never interleaves resolutions", () => {
    const d = s => A.dayEpoch(s);
    const a = {t:[d("2026-10-12"), d("2026-10-13")], c:[1, 2]}, b = {t:[d("2026-10-13"), d("2026-10-14")], c:[20, 30]};
    assert.deepStrictEqual(Array.from(A.pMergeHist(a, b).c), [1, 20, 30]);
    const long = {t:[d("2020-01-03"), d("2026-10-09"), d("2026-10-13")], c:[5, 6, 7]};
    const st = A.pStitch(long, b);
    assert.deepStrictEqual(Array.from(st.c), [5, 6, 20, 30], "long series contributes only BEFORE the daily one starts (its Oct 13 point is dropped)");
  });

  await test("pPeriodEnds re-dates Sunday-stamped weekly bars to the Friday they closed", () => {
    const sun = s => A.nyEpoch(...s.split("-").map(Number), 0, 0, 0);
    const out = A.pPeriodEnds([[sun("2026-09-27"), 1], [sun("2026-10-04"), 2], [sun("2026-10-11"), 3]]);
    assert.strictEqual(JSON.stringify(out.map(([t]) => new Date(t*1000).toISOString().slice(0,10))), JSON.stringify(["2026-10-02", "2026-10-09"]));
    assert.strictEqual(JSON.stringify(out.map(([, c]) => c)), "[1,2]");
  });

  /* ---- live data through the fake CNBC ---- */
  ROUTES.push({ test: u => u.includes("restQuote"), body: () => ({ FormattedQuoteResult: { FormattedQuote: [
    { symbol:"AAPL", last:"343.46", previous_day_closing:"342.00", last_time:"2026-10-16T16:00:00.000-0400" },
    { symbol:"BPTRX", last:"279.84", previous_day_closing:"277.52", last_time:"2026-10-15" },
    { symbol:"BTC.CM=", last:"81,558.57", previous_day_closing:"82,354.74", last_time:"2026-10-16T16:08:36.000-0400" } ] } }) });
  const minute = (h, m) => `20261016${String(h).padStart(2,"0")}${String(m).padStart(2,"0")}00`;
  const session = []; for(let h = 4; h < 20; h++) for(let m = 0; m < 60; m++) session.push({ tradeTime: minute(h, m), close: String(340 + h + m/100) });
  ROUTES.push({ test: u => u.includes("/charts/1D.json") && !u.includes("BPTRX"), body: () => ({ barData: { priceBars: session } }) });
  ROUTES.push({ test: u => u.includes("/charts/1D.json") && u.includes("BPTRX"), body: () => ({ barData: { priceBars: [] } }) });
  const dailyBars = []; for(let i = 0; i < 1100; i++){ const d = new Date(Date.UTC(2023,9,16) + i*864e5); const ds = d.toISOString().slice(0,10).replace(/-/g,"");
    dailyBars.push({ tradeTime: ds + "000000", close: String(100 + i) }); }
  ROUTES.push({ test: u => u.includes("/charts/6M.json"), body: () => ({ barData: { priceBars: dailyBars } }) });

  await test("pSparkAll answers from CNBC without touching a proxy", async () => {
    PROXY_CALLS.n = 0;
    const out = await A.pSparkAll(["AAPL", "BPTRX", "BTC-USD"]);
    assert.strictEqual(PROXY_CALLS.n, 0, "no proxy call when CNBC answered every symbol");
    assert.strictEqual(out.AAPL.price, 343.46);
    assert.strictEqual(out["BTC-USD"].price, 81558.57, "crypto is mapped to BTC.CM=");
    assert.strictEqual(nyDate(out.BPTRX.time), "2026-10-15", "fund keeps its NAV date -> no fake day change");
    const ser = out.AAPL.series;
    assert.ok(ser && ser.t.length > 60, "a full session of 5-minute buckets");
    assert.strictEqual(nyClock(ser.start), "09:30"); assert.strictEqual(nyClock(ser.end), "16:00");
    assert.ok(ser.t.every(t => t >= ser.start && t <= ser.end), "regular session only");
    assert.ok(nyClock(ser.t[ser.t.length-1]) >= "15:55", "runs to the close, not frozen at 10am");
    assert.strictEqual(out.BPTRX.series, null, "a fund has no intraday series");
  });

  await test("pSparkAll falls back to the proxies only for what CNBC missed", async () => {
    PROXY_CALLS.n = 0;
    await A.pSparkAll(["AAPL", "ZZZZ"]);
    assert.strictEqual(PROXY_CALLS.n, 1);
  });

  await test("cnbcDaily: ~3 years of weekday closes, stamped on their New York trading date", async () => {
    const h = await A.cnbcDaily("AAPL");
    const dates = Array.from(h.t, t => new Date(t*1000).toISOString().slice(0,10));
    assert.ok(dates.length > 700);
    assert.ok(dates.every(d => { const w = new Date(d + "T12:00:00Z").getUTCDay(); return w !== 0 && w !== 6; }), "no weekend bars");
    assert.strictEqual(dates[0], "2023-10-16");
  });

  ROUTES.push({ test: u => u.includes("queryly"), body: () => ({ results: [
    { "cn:title":"Nvidia shares slip as chip stocks cool", url:"https://www.cnbc.com/a", datePublished:"2026-10-16T18:24:27+0000", summary:"NVDA fell.", section:"Markets" },
    { "cn:title":"No date story", url:"https://www.cnbc.com/b" } ] }) });
  await test("cnbcNews parses CNBC search into dated headlines", async () => {
    const n = await A.cnbcNews("NVDA");
    assert.strictEqual(n.length, 1, "undated results are dropped");
    assert.strictEqual(n[0].t.toISOString(), "2026-10-16T18:24:27.000Z");
    assert.strictEqual(n[0].outlet, "CNBC");
  });

  /* ---- structural guards on the wiring (cheap, and they catch the exact regressions) ---- */
  await test("stock window dates daily bars by trading date, not the device's zone", () => {
    const fn = grabFunction("renderStockChart");
    assert.ok(/const dstr = ts => new Date\(ts\*1000\)\.toISOString\(\)\.slice\(0,10\)/.test(fn));
    assert.ok(!/toLocaleDateString\("en-CA"\)/.test(fn), "no bare local-zone date in the stock window");
  });
  await test("AI Insight never waits on every holding's history, and repaints don't restart it", () => {
    assert.ok(!/await loadHistory\(\)/.test(grabFunction("insGenerate")), "insGenerate must not await loadHistory()");
    assert.ok(/keepAI/.test(grabFunction("renderPortfolio")), "renderPortfolio keeps the AI card across rebuilds");
    assert.ok(/dataset\.built === "1"/.test(grabFunction("renderInsightCard")));
  });
  await test("no portfolio fetch path can hang: history and the stock window are deadline-capped", () => {
    assert.ok(/withDeadline\(pHistFor/.test(grabFunction("openStockModal")));
    assert.ok(/pFetchTimeout\(u, 8000/.test(grabFunction("fetchHistSym")), "the proxy fallback has its own timeout");
    assert.ok(!/fetchHistSym\(s, "max"\)/.test(html), "ALL no longer walks the proxies one symbol at a time");
  });

  await test("AI Insight asks and answers 'why did it move today' -- no full written read, no extra cards", () => {
    const qsrc = [grabLine("const isMutualFund"), grabFunction("insLastNav"), grabFunction("insQuestion"), "this.q = insQuestion;"].join("\n");
    const c2 = { Math }; vm.createContext(c2); vm.runInContext(qsrc, c2);
    assert.strictEqual(c2.q({ticker:"AAPL", dayPct:0.4321}, null), "Why is AAPL up 0.43% today?");
    assert.strictEqual(c2.q({ticker:"INTC", dayPct:-5.79}, null), "Why is INTC down 5.79% today?");
    assert.strictEqual(c2.q({ticker:"BPTRX", dayPct:0}, {closes:[100, 101]}), "Why did BPTRX rise 1.00% on its last price?");
    const gen = grabFunction("insGenerate");
    assert.ok(/insQuestion\(p, st\)/.test(gen), "the card's heading is the question");
    assert.ok(!/ins-more|Full written read|insVisuals|ins-hero/.test(gen), "removed sections stay removed");
    assert.ok(/why it moved today/.test(grabFunction("insTakeClaude")), "Claude is asked the why-today question");
  });

  console.log(results.join("\n"));
  console.log(failed ? `\n${failed} FAILED` : `\n${results.length} tests passed`);
  process.exit(failed ? 1 : 0);
})();
