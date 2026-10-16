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
    /* the older, coarse stretch: CNBC labels a weekly bar with the Sunday that STARTS the week but gives it the
       week's last close, so re-date it to that Friday (a Saturday label goes back to Friday) */
    const moved = new Map();
    days.forEach(d => {
      if(d >= from || !isWeekendDay(d)){ moved.set(d, byDay.get(d)); return; }
      const w = new Date(d + "T12:00:00Z").getUTCDay();
      const nd = new Date(Date.parse(d + "T12:00:00Z") + (w === 0 ? 5 : -1)*864e5).toISOString().slice(0,10);
      if(nd < from && !moved.has(nd)) moved.set(nd, [dayEpoch(nd), byDay.get(d)[1]]);
    });
    days = [...moved.keys()].sort();
    return { t: days.map(d => moved.get(d)[0]), c: days.map(d => moved.get(d)[1]) };
  }
  return { t: days.map(d => byDay.get(d)[0]), c: days.map(d => byDay.get(d)[1]) };
};
window.drawSmChart = function drawSmChart(body, pts, opts){
  const W=760, H=230, PL=58, PR=14, PT=12, PB=28;
  /* Your-cost line: only stretch the scale to include it when it sits near the prices. Before Oct 2026 a cost of
     $182 against a $343 price pulled the axis down to $182, and a 0.6% intraday move became a flat line. When it
     is far off the chart, it is named in a corner label instead of being drawn. */
  if(opts.refLine != null){
    const pv = pts.map(p=>p.v), lo = Math.min(...pv), hi = Math.max(...pv), sp = (hi - lo) || hi*0.01;
    if(opts.refLine < lo - sp*0.6 || opts.refLine > hi + sp*0.6){
      opts = {...opts, refOff: opts.refLine < lo ? "below" : "above", refOffLabel: opts.refLabel}; opts.refLine = null;
    }
  }
  const vals = pts.map(p=>p.v).concat(opts.refLine!=null ? [opts.refLine] : []);
  let min=Math.min(...vals), max=Math.max(...vals);
  const pad=(max-min)*0.06||max*0.01; min-=pad; max+=pad;
  const mode = opts.timeAxis ? "time" : (opts.dateAxis ? "date" : "index");
  const span = mode === "time" ? ((opts.end-opts.start)||1) : null;
  const dx0 = pts[0].x, dx1 = pts[pts.length-1].x, dspan = (dx1-dx0)||1;
  const X = (p,i) => mode === "time"
    ? PL + (W-PL-PR) * Math.max(0, Math.min(1, (p.x-opts.start)/span))
    : mode === "date"
      ? PL + (W-PL-PR) * (pts.length===1 ? 0 : (p.x-dx0)/dspan)
      : PL + (W-PL-PR) * (pts.length===1 ? 0 : i/(pts.length-1));
  const Y = v => PT + (H-PT-PB) * (1 - (v-min)/((max-min)||1));
  const delta = pts[pts.length-1].v - pts[0].v;
  const col = delta >= 0 ? "var(--up)" : "var(--down)";
  let line="", area=`M ${X(pts[0],0).toFixed(1)} ${H-PB}`;
  pts.forEach((p,i) => { const x=X(p,i).toFixed(1), y=Y(p.v).toFixed(1); line += `${i?"L":"M"} ${x} ${y} `; area += ` L ${x} ${y}`; });
  area += ` L ${X(pts[pts.length-1],pts.length-1).toFixed(1)} ${H-PB} Z`;
  const grid = [min+pad,(min+max)/2,max-pad].map(v =>
    `<line x1="${PL}" x2="${W-PR}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="var(--hairline)" stroke-width="1"/>
     <text x="${PL-8}" y="${(Y(v)+4).toFixed(1)}" text-anchor="end" font-size="10.5" fill="var(--muted)" font-family="inherit">${opts.fmtY ? opts.fmtY(v) : usd(v,2)}</text>`).join("");
  const refSvg = opts.refLine!=null
    ? `<line x1="${PL}" x2="${W-PR}" y1="${Y(opts.refLine).toFixed(1)}" y2="${Y(opts.refLine).toFixed(1)}" stroke="var(--topbar-muted)" stroke-width="1.5" stroke-dasharray="5,4"/>
       <text x="${W-PR}" y="${Math.max(PT+9, Y(opts.refLine)-6).toFixed(1)}" text-anchor="end" font-size="10" fill="var(--muted)" font-family="inherit" font-weight="700">${esc(opts.refLabel||"")}</text>`
    : opts.refOff ? `<text x="${W-PR}" y="${opts.refOff === "below" ? H-PB-6 : PT+10}" text-anchor="end" font-size="10" fill="var(--muted)" font-family="inherit" font-weight="700">${esc(opts.refOffLabel||"")} ${opts.refOff === "below" ? "\u2193" : "\u2191"} off chart</text>`
    : "";
  let axis;
  if(mode === "date"){
    axis = dateAxisSVG(dx0, dx1, {PL, PR, W, H, PB, fs:10.5});
  } else if(mode === "index" && opts.dayTicks){
    const dk = ts => new Date(ts*1000).toLocaleDateString("en-US",{timeZone:"America/New_York",weekday:"short",day:"numeric"});
    let prevK = null; axis = "";
    pts.forEach((p,i) => {
      const k = dk(p.x); if(k === prevK) return; prevK = k;
      const x = X(p,i), anchor = x < PL + 22 ? "start" : "middle";
      axis += `<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${H-PB}" y2="${H-PB+4}" stroke="var(--hairline)" stroke-width="1"/>
        <text x="${x.toFixed(1)}" y="${H-PB+17}" text-anchor="${anchor}" font-size="10.5" fill="var(--muted)" font-family="inherit">${esc(k)}</text>`;
    });
  } else {
    axis = `<text x="${PL}" y="${H-7}" font-size="10.5" fill="var(--muted)" font-family="inherit">${esc(opts.fmtX0(pts[0]))}</text>
    <text x="${W-PR}" y="${H-7}" text-anchor="end" font-size="10.5" fill="var(--muted)" font-family="inherit">${esc(opts.fmtX1(pts[pts.length-1]))}</text>`;
  }
  body.innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;display:block" id="smSvg">
    <defs><linearGradient id="smGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${col}" stop-opacity=".18"/><stop offset="100%" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
    ${grid}
    ${refSvg}
    <path d="${area}" fill="url(#smGrad)"/>
    <path d="${line}" fill="none" stroke="${col}" stroke-width="2.25" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${X(pts[pts.length-1],pts.length-1).toFixed(1)}" cy="${Y(pts[pts.length-1].v).toFixed(1)}" r="3.5" fill="${col}"/>
    <line id="smHair" x1="0" x2="0" y1="${PT}" y2="${H-PB}" stroke="var(--gold)" stroke-width="1" style="display:none"/>
    <circle id="smDot" r="4" fill="var(--gold)" style="display:none"/>
    ${axis}
    <rect x="${PL}" y="${PT}" width="${W-PL-PR}" height="${H-PT-PB}" fill="transparent" id="smHit"/>
  </svg><div class="hist-tip" id="smTip"></div>`;
  const svg = $("#smSvg"), tip = $("#smTip"), hair = $("#smHair"), dot = $("#smDot");
  $("#smHit").addEventListener("mousemove", e => {
    const r = svg.getBoundingClientRect();
    const fx = (e.clientX - r.left) / r.width * W;
    let idx;
    if(mode === "time" || mode === "date"){
      const frac = Math.max(0, Math.min(1, (fx-PL)/(W-PL-PR)));
      const targetX = mode === "time" ? opts.start + frac * span : dx0 + frac * dspan;
      /* points are sorted by x: binary search for the nearest */
      let lo = 0, hi = pts.length - 1;
      while(lo < hi){ const mid = (lo + hi) >> 1; if(pts[mid].x < targetX) lo = mid + 1; else hi = mid; }
      idx = (lo > 0 && Math.abs(pts[lo-1].x - targetX) <= Math.abs(pts[lo].x - targetX)) ? lo - 1 : lo;
    } else {
      idx = Math.max(0, Math.min(pts.length-1, Math.round((fx-PL)/((W-PL-PR)/(pts.length-1||1)))));
    }
    const px = X(pts[idx],idx), py = Y(pts[idx].v);
    hair.setAttribute("x1", px); hair.setAttribute("x2", px); hair.style.display = "block";
    dot.setAttribute("cx", px); dot.setAttribute("cy", py); dot.style.display = "block";
    placeChartTip(tip, body, px, py, W, H, opts.fmtTip(pts[idx]));
  });
  $("#smHit").addEventListener("mouseleave", () => { tip.style.display = "none"; hair.style.display = "none"; dot.style.display = "none"; });
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
  /* built now, not at the start: a key that was just rejected must not still read "Claude key ✓" */
  const keyBtnNow = `<button class="ins-key-btn${insKeyBad() ? " warn" : ""}" id="insKeyBtn" type="button">${insKeyBad() ? "Replace Claude key" : insApiKey() ? "Claude key ✓" : "Turn on Claude"}</button>`;
  if(srcEl) srcEl.insertAdjacentHTML("beforeend", keyBtnNow); wire();
};
window.insTakeLocal = async function insTakeLocal(p, st, spy, token, tw, setSub, setSrc){
  const up = p.dayPct >= 0, mv = Math.abs(p.dayPct || 0);
  const typical = st && st.dayVol ? mv / st.dayVol : null;
  const size = typical == null ? "" : typical < .6 ? "a quiet session" : typical < 1.4 ? "an ordinary-sized move" : typical < 2.5 ? "a big move for it" : "an outlier move";
  const navPending = isMutualFund(p.fetchSymbol || p.ticker) && !(p.dayPct);
  let head = navPending
    ? `**${p.ticker} hasn't repriced yet today — mutual funds strike one price after the close, so today's move shows up this evening.**\n`
    : `**${p.ticker} is ${up ? "up" : "down"} ${mv.toFixed(2)}% today${size ? " — " + size : ""}.**\n`;
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
"injected2";