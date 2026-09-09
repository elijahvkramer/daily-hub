#!/usr/bin/env node
/* Build today's Crossword of the Day on the server and publish it encrypted:
 *   data/crossword/<YYYY-MM-DD>.json.enc   (the puzzle -- same object cwBuildAtSize returns)
 *   data/crossword/history.json.enc         ({ WORD: "YYYY-MM-DD" } -- when each answer last ran)
 *
 * Runs from the Calendar Refresh workflow. Idempotent: exits at once if today's file exists.
 *
 * Why (Sep 2026): the browser used to generate the grid itself on every device -- a 15-60
 * second CPU burn, a different "recently used" history on every phone/laptop (so the
 * repeat-avoidance was per-device and easily defeated), and no way to check the puzzle
 * before it was served. Now one puzzle is built once, on GitHub's servers, with one shared
 * history; the site just downloads it. The in-browser generator stays as the fallback.
 *
 * Usage: node scripts/build_crossword.js <passphrase_file> [repo_dir]
 * Always exits 0 -- a failed build must never fail the calendar refresh job.
 */
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { load, buildDaily, tagRecent } = require("./cw_lib");

const ITER = 300000;
const SITE_SALT = Buffer.from("6d4a7e1f9b2c58e30f7a41d6c9b8e2a5", "hex");   // = scripts/dh_crypto.py SITE_SALT
const keyCache = new Map();
function keyFor(pass, salt){
  const k = salt.toString("hex");
  if(!keyCache.has(k)) keyCache.set(k, crypto.pbkdf2Sync(pass, salt, ITER, 32, "sha256"));
  return keyCache.get(k);
}
function encrypt(obj, pass){
  const iv = crypto.randomBytes(12);
  const c = crypto.createCipheriv("aes-256-gcm", keyFor(pass, SITE_SALT), iv);
  const ct = Buffer.concat([c.update(Buffer.from(JSON.stringify(obj))), c.final(), c.getAuthTag()]);
  return { v:1, kdf:"PBKDF2-SHA256", iter:ITER, salt:SITE_SALT.toString("base64"), iv:iv.toString("base64"), ct:ct.toString("base64") };
}
function decrypt(payload, pass){
  const salt = Buffer.from(payload.salt, "base64"), iv = Buffer.from(payload.iv, "base64"), ct = Buffer.from(payload.ct, "base64");
  const key = crypto.pbkdf2Sync(pass, salt, payload.iter || ITER, 32, "sha256");
  const d = crypto.createDecipheriv("aes-256-gcm", key, iv);
  d.setAuthTag(ct.subarray(ct.length - 16));
  return JSON.parse(Buffer.concat([d.update(ct.subarray(0, ct.length - 16)), d.final()]).toString());
}
function readEnc(file, pass){
  if(!fs.existsSync(file)) return null;
  try{ return decrypt(JSON.parse(fs.readFileSync(file, "utf8")), pass); }
  catch(e){ console.error("  ! could not read " + file + ": " + e.message); return null; }
}

function main(){
  const passFile = process.argv[2], repo = process.argv[3] || ".";
  if(!passFile){ console.error("usage: build_crossword.js <passphrase_file> [repo_dir]"); return 0; }
  const pass = fs.readFileSync(passFile, "utf8").trim();
  const today = process.env.DH_DATE || new Date().toLocaleDateString("en-CA", { timeZone: "America/Chicago" });   // DH_DATE: test override
  const dir = path.join(repo, "data", "crossword");
  const out = path.join(dir, today + ".json.enc");
  if(fs.existsSync(out)){ console.log("crossword for " + today + " already published"); return 0; }
  fs.mkdirSync(dir, { recursive: true });

  const G = load(repo);
  const bank = {};
  Object.keys(G.CW_WORDS).forEach(L => { bank[L] = G.CW_WORDS[L].slice(); });
  // Word of the Day terms join the fill bank as bonus options, exactly as the browser does
  const words = readEnc(path.join(repo, "data", "words.json.enc"), pass) || [];
  words.forEach(w => {
    const term = String(w.term || "").toUpperCase().replace(/[^A-Z]/g, "");
    const L = term.length;
    if(bank[L] && L >= 3 && L <= 8 && !bank[L].some(wc => wc[0] === term)) bank[L].push([term, w.definition]);
  });

  const histFile = path.join(dir, "history.json.enc");
  const hist = readEnc(histFile, pass) || {};
  const tagged = tagRecent(G, bank, hist, today, 45);

  const t0 = Date.now();
  const puzzle = buildDaily(G, "cw-" + today, tagged);
  if(!puzzle){ console.error("  ! no grid could be built for " + today + "; the site will build one locally"); return 0; }
  const secs = ((Date.now() - t0) / 1000).toFixed(1);

  // sanity: every entry must spell out of the solution grid
  for(const e of puzzle.entries){
    let sp = "";
    for(let i=0;i<e.word.length;i++) sp += puzzle.solution[(e.row + (e.dir==="V"?i:0)) + "," + (e.col + (e.dir==="H"?i:0))];
    if(sp !== e.word){ console.error("  ! grid/answer mismatch: " + e.word + " vs " + sp); return 0; }
  }
  const repeats = puzzle.entries.filter(e => hist[e.word] && G.cwDaysBetween(hist[e.word], today) < 45).map(e => e.word);

  puzzle.entries.forEach(e => { hist[e.word] = today; });
  for(const w of Object.keys(hist)) if(G.cwDaysBetween(hist[w], today) > 180) delete hist[w];   // keep the file small

  fs.writeFileSync(out, JSON.stringify(encrypt({ date: today, seed: "cw-" + today, builtAt: new Date().toISOString(), puzzle }, pass)));
  fs.writeFileSync(histFile, JSON.stringify(encrypt(hist, pass)));
  const lens = {}; puzzle.entries.forEach(e => { lens[e.word.length] = (lens[e.word.length]||0) + 1; });
  console.log(`crossword ${today}: ${puzzle.entries.length} answers, tier ${puzzle.tier}, ${secs}s, lengths ${JSON.stringify(lens)}` +
    (repeats.length ? `, repeats inside 45d: ${repeats.join(",")}` : ", no repeats inside 45 days"));
  return 0;
}

try{ process.exit(main()); }
catch(e){ console.error("  ! build_crossword failed: " + (e && e.stack || e)); process.exit(0); }
