// Tests for the price-chart time axis, run against the REAL functions in index.html (extracted, not copied).
// Regression guard for Eli's Oct 2026 report: "the 5Y view shows day by day through 2026/2025, but further back it
// only goes Sunday by Sunday" -- points were spaced by index instead of by date, and the end labels were bare weekdays.
// Run: node scripts/test_chart_axis.js
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm"), assert = require("assert");
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");

function grabFunction(name){
  const i = html.indexOf("function " + name + "(");
  assert.ok(i >= 0, "found function " + name);
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

const src = [
  grabLine("const esc ="), grabFunction("longDate"), grabFunction("fullDate"),
  grabLine("const dayEpoch"), grabLine("const MON3"), grabFunction("niceTimeTicks"), grabFunction("dateAxisSVG"),
  grabFunction("placeChartTip"), grabFunction("drawSmChart"),
  "const usd = (v,d) => '$' + Number(v).toFixed(d == null ? 2 : d);",
  "this.api = { niceTimeTicks, dateAxisSVG, drawSmChart, dayEpoch, fullDate, longDate };"
].join("\n");

const stubEl = () => ({ addEventListener(){}, setAttribute(){}, style:{}, getBoundingClientRect: () => ({left:0,width:760}) });
const ctx = { $: () => stubEl(), console };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const { niceTimeTicks, drawSmChart, dayEpoch, fullDate } = ctx.api;

const DAY = 86400;
const iso = e => new Date(e * 1000).toISOString().slice(0, 10);
let n = 0; const t = (name, fn) => { fn(); n++; console.log("ok  - " + name); };

t("fullDate has month, day and year and no weekday", () => {
  assert.strictEqual(fullDate("2021-10-18"), "Oct 18, 2021");
  assert.ok(!/Mon|Tue|Wed|Thu|Fri|Sat|Sun/.test(fullDate("2026-10-09")));
});

t("ticks: 3-7 per range, ascending, inside the span, unique labels, for every window", () => {
  const end = dayEpoch("2026-10-09");
  for(const [name, days] of [["1W",7],["1M",31],["3M",92],["6M",183],["YTD",282],["1Y",366],["5Y",1830],["ALL 10y",3650],["ALL 35y",12800],["ALL 100y",36500]]){
    const a = end - days * DAY, ticks = niceTimeTicks(a, end, 6);
    assert.ok(ticks.length >= 3 && ticks.length <= 7, `${name}: ${ticks.length} ticks`);
    ticks.forEach((tk, i) => { assert.ok(tk.ts >= a - DAY/2 && tk.ts <= end + DAY/2, `${name}: tick in range`); if(i) assert.ok(tk.ts > ticks[i-1].ts, `${name}: ascending`); });
    assert.strictEqual(new Set(ticks.map(x => x.label)).size, ticks.length, `${name}: unique labels ${ticks.map(x => x.label)}`);
  }
});

t("ticks: 5Y reads as month+year, ALL reads as years, 1M as month+day", () => {
  const end = dayEpoch("2026-10-09");
  assert.ok(niceTimeTicks(end - 1830*DAY, end, 6).every(x => /^[A-Z][a-z]{2} ’\d\d$|^\d{4}$/.test(x.label)));
  assert.ok(niceTimeTicks(end - 12800*DAY, end, 6).every(x => /^\d{4}$/.test(x.label)));
  assert.ok(niceTimeTicks(end - 31*DAY, end, 6).every(x => /^[A-Z][a-z]{2} \d{1,2}$/.test(x.label)));
});

t("ticks: month ticks fall on the 1st, year ticks on Jan 1, weekly ticks on Mondays", () => {
  const end = dayEpoch("2026-10-09");
  niceTimeTicks(end - 366*DAY, end, 6).forEach(x => assert.ok(iso(x.ts).endsWith("-01"), iso(x.ts)));
  niceTimeTicks(end - 12800*DAY, end, 6).forEach(x => assert.ok(iso(x.ts).endsWith("-01-01"), iso(x.ts)));
  const wk = niceTimeTicks(end - 60*DAY, end, 6);          // 60 days -> weekly/biweekly steps
  if(wk.length && /^[A-Z][a-z]{2} \d/.test(wk[0].label) && (wk[1].ts - wk[0].ts) >= 7*DAY) wk.forEach(x => assert.strictEqual(new Date(x.ts*1000).getUTCDay(), 1));
});

t("ticks: evenly spaced in time (calendar steps differ by at most a few days)", () => {
  const end = dayEpoch("2026-10-09");
  const tk = niceTimeTicks(end - 1830*DAY, end, 6), gaps = tk.slice(1).map((x, i) => (x.ts - tk[i].ts) / DAY);
  assert.ok(Math.max(...gaps) - Math.min(...gaps) <= 4, "gaps " + gaps);
});

/* ---- the reported bug: daily for the last year, weekly before that ---- */
function xsOf(pts, opts){
  const body = { innerHTML: "" };
  drawSmChart(body, pts, opts);
  const m = [...body.innerHTML.matchAll(/<path d="([^"]+)" fill="none"/g)];
  assert.ok(m.length, "line path present");
  return { xs: [...m[0][1].matchAll(/[ML] ([\d.]+) ([\d.]+)/g)].map(r => +r[1]), html: body.innerHTML };
}
const end = dayEpoch("2026-10-09");
const mixed = [];
for(let d = 1830; d > 400; d -= 7) mixed.push(end - d * DAY);          // old: weekly
for(let d = 400; d >= 0; d--) mixed.push(end - d * DAY);               // recent: daily
const pts = mixed.map((x, i) => ({ x, d: iso(x), v: 100 + i }));

t("5Y with mixed density: x position is proportional to DATE, not to point index", () => {
  const { xs } = xsOf(pts, { dateAxis: true, fmtTip: p => "" });
  const PL = 58, PR = 14, W = 760, w = W - PL - PR, x0 = pts[0].x, span = pts[pts.length-1].x - x0;
  pts.forEach((p, i) => assert.ok(Math.abs(xs[i] - (PL + w * (p.x - x0) / span)) < 0.11, `point ${i}`));
  // 28 days of old (weekly) data must span the same width as 28 days of recent (daily) data
  const w28old = xs[4] - xs[0], w28new = xs[xs.length-1] - xs[xs.length-29];
  assert.ok(Math.abs(w28old - w28new) < 0.3, `28-day width old ${w28old} vs recent ${w28new}`);
});

t("the old behaviour (index spacing) would have failed that check", () => {
  const { xs } = xsOf(pts, { fmtX0: () => "", fmtX1: () => "", fmtTip: () => "" });     // no dateAxis -> index mode
  assert.ok(Math.abs((xs[1] - xs[0]) / (xs[xs.length-1] - xs[xs.length-2]) - 1) < 0.01, "index mode spaces every point equally");
});

t("bottom axis: calendar ticks with years, no weekday-only end labels", () => {
  const { html: h } = xsOf(pts, { dateAxis: true, fmtTip: p => "" });
  const labels = [...h.matchAll(/text-anchor="(?:start|middle|end)" font-size="10.5" fill="var\(--muted\)" font-family="inherit">([^<]+)</g)].map(m => m[1]).filter(x => !/^\$/.test(x));
  assert.ok(labels.length >= 4, "labels: " + labels);
  assert.ok(labels.every(l => /^[A-Z][a-z]{2} ’\d\d$|^\d{4}$/.test(l)), "labels: " + labels);
  assert.ok(!labels.some(l => /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)/.test(l)), "no weekday labels");
});

t("single-day and two-point series don't blow up", () => {
  xsOf([{ x: end, d: iso(end), v: 1 }], { dateAxis: true, fmtTip: () => "" });
  xsOf([{ x: end - DAY, d: "", v: 1 }, { x: end, d: "", v: 2 }], { dateAxis: true, fmtTip: () => "" });
});

console.log(`\n${n} tests passed`);
