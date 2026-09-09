/* Loads the crossword generator straight out of index.html so Node (the regression test and
   the server-side daily build) runs the exact same code the browser does -- no second copy
   to drift. Exports the word bank plus the generator functions. */
const fs = require("fs");
const path = require("path");

function load(repoDir){
  const html = fs.readFileSync(path.join(repoDir, "index.html"), "utf8");
  function grab(name){
    const i = html.indexOf("function " + name + "(");
    if(i < 0) throw new Error("missing function " + name);
    let k = html.indexOf("{", i), depth = 0, instr = null, esc = false, cmt = null;
    for(; k < html.length; k++){
      const ch = html[k], nx = html[k+1];
      if(cmt){
        if(cmt === "//" && ch === "\n") cmt = null;
        else if(cmt === "/*" && ch === "*" && nx === "/"){ cmt = null; k++; }
        continue;
      }
      if(instr){ if(esc) esc = false; else if(ch === "\\") esc = true; else if(ch === instr) instr = null; continue; }
      if(ch === "/" && nx === "/"){ cmt = "//"; k++; continue; }
      if(ch === "/" && nx === "*"){ cmt = "/*"; k++; continue; }
      if(ch === '"' || ch === "'" || ch === "`") instr = ch;
      else if(ch === "{") depth++;
      else if(ch === "}"){ depth--; if(!depth) return html.slice(i, k+1); }
    }
    throw new Error("unbalanced " + name);
  }
  const bankSrc = html.match(/^const CW_WORDS = .*?;$/m)[0];
  const fns = ["hashStr","seedRand","cwGenBlocks","cwSlotsFor","cwNumber","cwSolveCSP","cwBuildAtSize","cwDaysBetween"];
  const src = bankSrc + "\n" + fns.map(grab).join("\n") + "\nmodule.exports={CW_WORDS," + fns.join(",") + "};";
  const tmp = path.join(require("os").tmpdir(), "cw-lib-" + process.pid + ".js");
  fs.writeFileSync(tmp, src);
  const G = require(tmp);
  fs.unlinkSync(tmp);
  return G;
}

/* Same tiers as cwBuild() in index.html, minus the worker plumbing. Returns the puzzle or null. */
function buildDaily(G, seedStr, bank){
  const targets = [38,40,42,44,46];
  const attempts = [
    { bank, deadline: 45000, cap: 24, p1: 6000, fills: 10, fillMs: 700, three: 0.32, long: 2 },
    { bank, deadline: 20000, cap: 16, p1: 5000, fills: 8, fillMs: 700, three: 0.38, long: 1 },
    { bank, deadline: 20000, cap: 16, p1: 5000, fills: 8, fillMs: 700, three: 0.38, long: 1 },
    { bank, deadline: 15000, cap: 20, p1: 5000, fills: 8, fillMs: 700, three: 0.45, long: 0 },
    { bank, deadline: 20000, cap: 60, p1: 3000, fills: 8, fillMs: 700, three: null, long: 0, loose: true }
  ];
  for(let i=0;i<attempts.length;i++){
    const a = attempts[i];
    const g = G.cwBuildAtSize(
      seedStr + (i ? "-try"+i : ""), a.bank, 12,
      a.loose ? [48,50,52,54,56,58,60] : targets,
      40, a.loose ? 6 : 7,
      Date.now()+a.deadline, a.cap, a.p1, a.fills, a.fillMs,
      a.three, a.long, 12   // compare up to a dozen complete fills, keep the one with the fewest repeats
    );
    if(g){ g.tier = i; return g; }
  }
  return null;
}

/* Port of cwTagRecentBank(): mark words used inside the window as stale (third element) so
   the solver tries them last. Never removes anything -- see the note in index.html. */
function tagRecent(G, bank, hist, today, windowDays){
  const out = {};
  Object.keys(bank).forEach(L => {
    out[L] = bank[L].map(wc => {
      const ago = hist[wc[0]] ? G.cwDaysBetween(hist[wc[0]], today) : Infinity;
      return ago < windowDays ? [wc[0], wc[1], windowDays - ago] : [wc[0], wc[1]];
    });
  });
  return out;
}

module.exports = { load, buildDaily, tagRecent };
