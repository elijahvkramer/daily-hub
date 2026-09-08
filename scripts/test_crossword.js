#!/usr/bin/env node
/* Crossword regression test.  Run:  node scripts/test_crossword.js
 *
 * Guards the three things that were actually broken in September 2026:
 *   1. Answers were capped at 6 letters (maxLenCap) and the grid was accepted on
 *      block COUNT alone, so ~60% of every puzzle was 3-letter words.
 *   2. The recency filter only ran on the first (usually failing) attempt tier,
 *      so the same crosswordese repeated day after day.
 *   3. The dictionary contained clues that gave away their own answer, and
 *      clues shared by two different answers — which makes a correct solve look
 *      wrong to the solver.
 * If you change the generator, this must still pass.
 */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
function grab(name){
  const i = html.indexOf("function " + name + "(");
  if(i < 0) throw new Error("missing function " + name);
  let k = html.indexOf("{", i), depth = 0, instr = null, esc = false;
  for(; k < html.length; k++){
    const ch = html[k];
    if(instr){ if(esc) esc = false; else if(ch === "\\") esc = true; else if(ch === instr) instr = null; }
    else if(ch === '"' || ch === "'" || ch === "`") instr = ch;
    else if(ch === "{") depth++;
    else if(ch === "}"){ depth--; if(!depth) return html.slice(i, k+1); }
  }
  throw new Error("unbalanced " + name);
}
const bankSrc = html.match(/^const CW_WORDS = .*?;$/m)[0];
const src = bankSrc + "\n" + ["hashStr","seedRand","cwGenBlocks","cwSlotsFor","cwNumber","cwSolveCSP","cwBuildAtSize"]
  .map(grab).join("\n") + "\nmodule.exports={CW_WORDS,cwBuildAtSize};";
const tmp = path.join(require("os").tmpdir(), "cw-regression-" + process.pid + ".js");
fs.writeFileSync(tmp, src);
const G = require(tmp);
fs.unlinkSync(tmp);

let failures = 0;
const fail = m => { console.error("FAIL: " + m); failures++; };
const ok   = m => console.log("ok   " + m);

/* ---------- 1. dictionary hygiene ---------- */
const B = G.CW_WORDS;
const total = Object.values(B).reduce((n, a) => n + a.length, 0);
if(total < 6000) fail(`dictionary too small (${total}); a thin bank is what forced repeats`);
else ok(`dictionary has ${total} entries`);

["4","5","6","7"].forEach(L => {
  if((B[L] || []).length < 800) fail(`length-${L} bucket only ${(B[L]||[]).length}; needs 800+ for varied fill`);
});
if(!failures) ok("every 4-7 letter bucket is deep enough");

const clueOwner = new Map();
let selfRef = 0, shared = 0, malformed = 0, dupWord = 0;
Object.keys(B).forEach(L => {
  const seen = new Set();
  B[L].forEach(([w, c]) => {
    if(w.length !== +L || !/^[A-Z]+$/.test(w)) malformed++;
    if(seen.has(w)) dupWord++;
    seen.add(w);
    if(c.toLowerCase().includes(w.toLowerCase())) { selfRef++; if(selfRef<4) console.error("   self-ref: " + w + " :: " + c); }
    const k = c.toLowerCase().trim();
    if(clueOwner.has(k) && clueOwner.get(k) !== w){ shared++; if(shared<4) console.error(`   shared clue: "${c}" -> ${clueOwner.get(k)} / ${w}`); }
    else clueOwner.set(k, w);
  });
});
if(malformed) fail(`${malformed} malformed entries`); else ok("all entries well formed");
if(dupWord)   fail(`${dupWord} duplicate words inside one length bucket`); else ok("no duplicate words");
if(selfRef)   fail(`${selfRef} clues contain their own answer`); else ok("no clue gives away its answer");
if(shared)    fail(`${shared} clues are shared by two different answers (ambiguous solve)`); else ok("every clue maps to exactly one answer");

/* ---------- 2. generated puzzle shape ---------- */
const bank = {}; Object.keys(B).forEach(L => bank[L] = B[L].slice());
const targets = [38,40,42,44,46];
const days = ["2026-10-01","2026-10-02","2026-10-03","2026-10-04"];
const sets = [], dist = {};
let built = 0;
for(const d of days){
  let g = null;
  const tiers = [
    [targets, 7, 0.35, 2, 18000],
    [targets, 7, 0.38, 1, 22000],
    [[48,50,52,54,56,58,60], 6, null, 0, 20000]
  ];
  for(let i = 0; i < tiers.length && !g; i++){
    const [t, capL, three, long, dl] = tiers[i];
    g = G.cwBuildAtSize("cw-"+d+(i?"-try"+i:""), bank, 12, t, 40, capL, Date.now()+dl, 300, 3000, 8, 650, three, long);
  }
  if(!g){ fail("no grid built for " + d); continue; }
  built++;
  g.entries.forEach(e => {
    let spelled = "";
    for(let i = 0; i < e.word.length; i++){
      const r = e.row + (e.dir === "V" ? i : 0), c = e.col + (e.dir === "H" ? i : 0);
      spelled += g.solution[r + "," + c];
    }
    if(spelled !== e.word) fail(`grid/answer mismatch on ${d}: ${e.word} vs ${spelled}`);
    dist[e.word.length] = (dist[e.word.length] || 0) + 1;
  });
  const byCell = {};
  g.entries.forEach(e => { const k = e.row+","+e.col; (byCell[k] = byCell[k] || []).push(e.num); });
  Object.values(byCell).forEach(nums => { if(new Set(nums).size !== 1) fail("inconsistent numbering on " + d); });
  sets.push(new Set(g.entries.map(e => e.word)));
}
if(built === days.length) ok(`built a full grid for all ${days.length} sample days`);

const n = Object.values(dist).reduce((a,b) => a+b, 0);
const threeShare = 100 * (dist[3] || 0) / n;
if(threeShare > 40) fail(`3-letter answers are ${threeShare.toFixed(0)}% of the grid (must stay under 40%)`);
else ok(`3-letter share is ${threeShare.toFixed(0)}%`);
if(!(dist[6] || dist[7])) fail("no answers longer than 5 letters appeared");
else ok(`long answers present (6-letter: ${dist[6]||0}, 7-letter: ${dist[7]||0})`);

let worst = 0;
for(let i = 0; i < sets.length; i++) for(let j = i+1; j < sets.length; j++){
  const o = 100 * [...sets[i]].filter(w => sets[j].has(w)).length / sets[i].size;
  if(o > worst) worst = o;
}
if(worst > 15) fail(`two days share ${worst.toFixed(0)}% of their answers (must stay under 15%)`);
else ok(`worst day-to-day answer overlap is ${worst.toFixed(0)}%`);

console.log(failures ? `\n${failures} FAILURE(S)` : "\nAll crossword checks passed.");
process.exit(failures ? 1 : 0);
