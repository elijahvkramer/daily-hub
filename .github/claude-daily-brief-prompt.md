# Daily Hub — combined Market Update + News Briefing (GitHub Actions edition)

You are running unattended, once daily at 9:00am Central (cron in the workflow), inside a fresh checkout of the `elijahvkramer/daily-hub` repo. This combines what used to be two separate Cowork scheduled tasks (`market-update` and `daily-news-briefing`) into one run. The website JSON schemas, research bar, and writing style are UNCHANGED from those tasks — do not alter them. Work autonomously; note any assumptions in a closing chat/log summary.

## Where you're running

- `cwd` is the repo root (already checked out by `actions/checkout`). Git push works automatically — you're authenticated as the Claude GitHub App via this Action, so plain `git add` / `git commit` / `git push` from this checkout just works. Do NOT clone the repo again and do NOT look for a GitHub PAT — there isn't one in this environment and you don't need one.
- The site's encryption passphrase is available as the environment variable `CAL_PASSPHRASE` (from the repo secret of the same name) — write it to a temp file before use: `printf '%s' "$CAL_PASSPHRASE" > /tmp/cal_pass.txt` and pass that file to `scripts/encrypt_calendar.py` / `scripts/decrypt_calendar.py` as their passphrase-file argument. There is no EDEADLK/mount bug here (that was specific to the old Cowork desktop sandbox) — this is a normal Linux runner, plain file reads are fine.
- `data/manifest.json` lists published dates per kind (`market`, `news`, `calendar`). Never overwrite an already-published date's content without a good reason — you're publishing under today's date, which won't exist yet when this runs on schedule.

## Step 0: figure out what day it is

Run `date -u +%A` and `date -u +%F`. Because this fires at 9am US Central, the UTC date/weekday always matches Eli's local date/weekday at that hour — no conversion needed.

- Monday: news briefing covers the last 72 hours (Fri-Sun).
- Tuesday-Friday: news briefing covers the last 24 hours.
- Saturday/Sunday: still publish the news briefing (24h window), but SKIP the market section entirely — no US market data on weekends.
- Monday-Friday: ALSO produce the market section, summarizing the prior trading day's close (not same-day — this now runs at 9am, before/at open, not right after close). E.g. a Tuesday run summarizes Monday's close; a Monday run summarizes the prior Friday's close.

## Step 1: Market section (weekdays only)

Research via web search: index scoreboard (S&P 500, Nasdaq Composite, Dow Jones, Russell 2000, VIX — level, % change, point change), the 3 biggest drivers of that day, ~2 leaders/~2 laggards, sector performance (GICS sectors, as reported), rates/commodities/Fed (10Y yield, Fed funds target, crude, gold), ~3 forward-looking outlook items with dates, a bottom-line synthesis. Also check Eli's holdings for notable news while doing the searches above (no extra searches beyond at most one targeted follow-up): NVDA, SPY, AAPL, META, TSLA, NFLX, SPOT, INTC, IBM, BAI (iShares A.I. Innovation and Tech ETF), BPTRX (Baron Partners Fund), BTC. Eli holds Micron (MU) only indirectly through BAI (~6% weight) — attribute any Micron/memory-chip news to ticker "BAI", never a standalone "MU" entry, but name the underlying stock in the prose and say explicitly it's an indirect exposure via BAI. Apply the same logic to any other holding he owns only indirectly through a fund/ETF.

Write `/tmp/market.json` in this exact schema (field names must match — the site reads these verbatim):

```json
{
  "date": "YYYY-MM-DD",
  "dateline": "Weekday, Month D, YYYY",
  "headline": "Punchy two-line headline\nwith a literal \\n line break",
  "subtitle": "3-4 sentence narrative summary of the day.",
  "indices": [ {"name":"S&P 500","level":"7,543.65","pct":0.81,"pts":"+60.94"}, "...all 5, VIX last" ],
  "drivers": [ {"lead":"Bold lead sentence.","body":"Rest of the item."}, "x3" ],
  "indexChartCaption": "1-2 sentence caption for the index bar chart.",
  "winners": [ {"ticker":"SNDK","name":"SanDisk","pct":9.1} ],
  "losers": [ {"ticker":"PLTR","name":"Palantir","pct":-4.0} ],
  "moversCaption": "1-3 sentence caption.",
  "sectors": [ {"name":"Information Technology","pct":1.60}, "...sorted best to worst" ],
  "sectorCaption": "1-2 sentence caption.",
  "stats": [ {"label":"10Y Treasury","value":"~4.55%","note":"one-liner"}, "x4" ],
  "outlook": [ {"date":"Jul 14","lead":"Bold lead.","body":"Detail."}, "x3" ],
  "holdingsNews": [ {"ticker":"NVDA","lead":"Bold lead.","body":"1-2 sentences on the news and why it matters to this position."} ],
  "bottomLine": "The synthesis paragraph.",
  "sources": "Comma-separated source names · figures as reported near the close"
}
```

`holdingsNews`: 0-4 items, ONLY real notable news on Eli's holdings today — empty array if nothing notable, never padded. `pct` fields are numbers, negative for declines. Validate: `python3 -c "import json;json.load(open('/tmp/market.json'))"`.

Encrypt and publish:

```
printf '%s' "$CAL_PASSPHRASE" > /tmp/cal_pass.txt
python3 scripts/encrypt_calendar.py /tmp/market.json /tmp/cal_pass.txt /tmp/market.json.enc
mkdir -p data/market
cp /tmp/market.json.enc "data/market/$(date -u +%F).json.enc"
```

(Rebuild `data/manifest.json` the same way `scripts/publish.sh` does — regenerate the `market`/`news`/`calendar` arrays from what's actually on disk under `data/<kind>/`, sorted newest-first — then `git add -A && git commit -m "publish: market $(date -u +%F)"`. Do this once at the very end alongside the news commit, or as two separate commits — either is fine, just don't skip the manifest rebuild.)

## Step 2: News briefing (every day)

Minimize usage — no PDFs, no image rendering. ~9-11 searches total: roughly 1-2 per general topic below, plus one search per followed sports team, plus at most 1-2 optional follow-ups for a `detail` paragraph that needs more specifics.

Time window — strict: Tuesday-Friday, only cover things that happened in the past 24 hours. Monday, past 72 hours. Continued coverage of an older story does NOT qualify just because it's still trending — something genuinely new and dated must have happened in the window. Before including anything, ask "did the newsworthy thing itself happen in the window, or is this just an older story still surfacing in search?" If the latter, drop it.

Topics, in this order (each its own section; skip a topic with a one-line "nothing new" note rather than padding with stale stories):

1. US Military & Foreign Conflict
2. Politics & Policy
3. Tech & AI
4. Major International Headlines (2-3 significant non-US stories)
5. Sports — ONE section, two groups via a "group" field on each item:
   - Favorites (min 2 items/day): Kentucky Wildcats Men's Basketball, New York Giants (NFL), USC Trojans Football — one targeted search per team; broaden to recruiting/roster/beat-writer notes before giving up on hitting the floor.
   - General (min 2 items/day): NFL, Men's College Basketball, College Football, PGA, UFC, Men's Grand Slam Tennis, NBA — 1-2 searches covers this whole set.
   - Favorites items first, then General. A story fitting either goes to Favorites, never duplicated in both.

4-6 items per section (Sports typically runs 4-7 total, that's fine). Include a `detail` field (4-6 sentence expansion) on as many items as you reasonably can.

Repeat check (required, before finalizing): read `data/manifest.json`'s `news` array for the last 3 published dates, decrypt each (`python3 scripts/decrypt_calendar.py data/news/<date>.json.enc /tmp/cal_pass.txt /tmp/prev-<date>.json`), and drop any candidate story that covers the same underlying event as something already published in those 3 editions, unless there's a genuinely new dated development (say explicitly what changed). When in doubt, drop it — a short section beats a repeated one. Clean up `/tmp/prev-*.json` after.

Write `/tmp/news.json`:

```json
{
  "date": "YYYY-MM-DD",
  "dateline": "Weekday, Month D, YYYY",
  "eyebrow": "Weekday Roundup (or Weekend Roundup on Mondays)",
  "headline": "Punchy two-line synthesized headline\nwith a literal \\n break",
  "subtitle": "2-3 sentence summary of the day's throughline.",
  "glance": [ {"label":"Military & Conflict","teaser":"3-6 word chip"}, "x5, one per section incl. Sports" ],
  "sections": [
    {"title":"US Military & Foreign Conflict","items":[
      {"lead":"Bold lead sentence.","body":"1-2 punchy sentences.","detail":"optional 4-6 sentence expansion","imgQuery":"optional photographable subject","caption":"optional"}
    ]},
    "... x5 in topic order, the 5th titled \"Sports\" with every item carrying a group field: {\"lead\":\"...\",\"body\":\"...\",\"group\":\"Favorites\"}"
  ],
  "sources": "Reuters, AP, ..."
}
```

Photos: only set `imgQuery` when confident of a single concrete, depictable subject (person/place/institution/company/piece of hardware) — no auto-guess fallback exists on the site, so a missing photo is fine and a wrong one is the failure mode to avoid. Avoid repeating the same `imgQuery` twice in one section. Validate: `python3 -c "import json;json.load(open('/tmp/news.json'))"`.

Encrypt and publish the same way as market (own commit or combined):

```
python3 scripts/encrypt_calendar.py /tmp/news.json /tmp/cal_pass.txt /tmp/news.json.enc
mkdir -p data/news
cp /tmp/news.json.enc "data/news/$(date -u +%F).json.enc"
```

## Step 3: Brain Food + Word Quiz (every day, regardless of how Step 2 went)

The Games tab's three "Brain Food" cards (Fun Fact / History Tidbit / Word of the Day) and the Word Quiz bank have no other source.

1. Pick a `funFact` (surprising world fact) and `historyTidbit` (general world history, not tied to today's date) — check them against MEMORY.md's used-lists if that file is available in this checkout; otherwise just use good judgment to avoid obvious repeats from recent editions. Pick a `word` at a middle-difficulty tier (a well-read adult would recognize it, but it's a step up from everyday words — think ubiquitous, precarious, esoteric, tenuous, discerning — not painfully obscure). Include ipa/respell/pos/definition/example.
2. Decrypt today's already-published calendar file if it exists (from the separate `calendar-refresh.yml` workflow), merge in `close: {funFact, historyTidbit, word}` leaving every other field (`today`, `radar`, `weather`, `urgent`, `chill`) untouched, re-encrypt, and write to `data/calendar/$(date -u +%F).json.enc`. If today's calendar file doesn't exist yet, create a minimal one: `{"today":[],"radar":[],"weather":"","hourly":[],"urgent":[],"chill":[],"close":{...}}`.
3. Run `bash scripts/add_word.sh /tmp/word.json` (schema: `{"term":...,"ipa":...,"respell":...,"pos":...,"definition":...,"example":...,"date":"YYYY-MM-DD"}`) to append to the word bank. `GH_TOKEN_FILE`/`CAL_PASS_FILE` env vars aren't needed here either — same in-checkout git push as everything else; if the script insists on them, just export `CAL_PASS_FILE=/tmp/cal_pass.txt` and skip `GH_TOKEN_FILE` (git push already works without a token file in this environment).

## Step 4: finish

Rebuild `data/manifest.json` from what's actually on disk (mirror the logic in `scripts/publish.sh`: for each of `market`/`news`/`calendar`, list `data/<kind>/*.json.enc` basenames as dates, sorted newest-first). Commit everything that changed (`git add -A && git commit -m "publish: <date> daily brief"`) and `git push` — if the push is rejected because something else committed first (e.g. the 15-minute calendar refresher), `git pull --rebase` and retry a couple of times. NEVER commit any plaintext `.json` (only the `.json.enc` payloads) — clean up `/tmp/*.json`, `/tmp/*.txt`, `/tmp/prev-*` when done.

End with a short summary in the job log: today's date, whether market ran, a one-line gist of the news headline, and confirmation both/all files published successfully. If anything failed, say exactly what and why rather than silently skipping it.
