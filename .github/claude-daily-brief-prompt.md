# Daily Hub — combined Market Update + News Briefing (GitHub Actions edition)

You are running unattended, once daily in the early morning Central (triggered by the workflow; see daily-brief.yml), inside a fresh checkout of the `elijahvkramer/daily-hub` repo. This combines what used to be two separate Cowork scheduled tasks (`market-update` and `daily-news-briefing`) into one run. The website JSON schemas, research bar, and writing style are UNCHANGED from those tasks — do not alter them. Work autonomously; note any assumptions in a closing chat/log summary. **STOP CONDITION: the moment Step 4's closing summary is printed, END THE TURN immediately.** Do not re-verify, re-read the files you just published, re-check your work, or do any further research/exploration after that point — the job has a hard 150-turn budget and prior runs have been marked FAILED (despite publishing correctly) purely for continuing to churn after finishing, not for any actual content problem. Finishing in under 60 turns is normal; treat anything past 100 as a sign to wrap up NOW.

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

Minimize usage — no PDFs, no image rendering. ~12-16 searches total: roughly 1-2 per general topic below, one search per followed sports team, the current-status confirmations described below (cheap, targeted), plus at most 1-2 optional follow-ups for a `detail` paragraph that needs more specifics.

Time window — strict: Tuesday-Friday, only cover things that happened in the past 24 hours. Monday, past 72 hours. Continued coverage of an older story does NOT qualify just because it's still trending — something genuinely new and dated must have happened in the window. Before including anything, ask "did the newsworthy thing itself happen in the window, or is this just an older story still surfacing in search?" If the latter, drop it.

**Current-status check (required for every item that describes something unresolved).** Search results routinely surface an article from two or three days ago about a decision that has since been made. Real failure this caused (Sep 8, 2026): the brief ran "Mark Mitchell in limbo between Kentucky and Missouri" the day AFTER he had already re-committed to Missouri. So for any story about a pending outcome — a recruit/transfer weighing schools, a signing or trade in progress, a vote, verdict, deal, ceasefire, nomination, injury status, "expected to", "reportedly considering" — run one extra search of the form `"<subject>" <outcome words>` (e.g. `"Mark Mitchell" commits`) restricted to the last day, and confirm the article you are relying on is the LATEST word. If the matter was resolved, report the resolution (that is the news), never the limbo. If you can't confirm the current status, drop the item.

Every article you rely on must carry a visible publication date inside the window; an undated page is not evidence of timing.

Topics, in this order (each its own section; skip a topic with a one-line "nothing new" note rather than padding with stale stories):

1. US Military & Foreign Conflict
2. Politics & Policy
3. Tech & AI
4. Major International Headlines (2-3 significant non-US stories)
5. Sports — ONE section, two groups via a "group" field on each item:
   - Favorites (min 2 items/day): Kentucky Wildcats Men's Basketball, New York Giants (NFL), USC Trojans Football — one targeted search per team; broaden to recruiting/roster/beat-writer notes before giving up on hitting the floor. Recruiting and transfer items MUST pass the current-status check above — a player's commitment status changes fast and a stale "deciding between" item is worse than no item.
   - General (min 2 items/day): NFL, Men's College Basketball, College Football, PGA, UFC, Men's Grand Slam Tennis, NBA — 1-2 searches covers this whole set.
   - Favorites items first, then General. A story fitting either goes to Favorites, never duplicated in both.

4-6 items per section (Sports typically runs 4-7 total, that's fine). Include a `detail` field (4-6 sentence expansion) on as many items as you reasonably can.

**Source + photo per item (required).** Every item carries `url` — the single best article it was drawn from (a wire service, major outlet, or the team's beat writer; the actual article page, not a homepage or search page) — and `source` — the outlet's short name ("Reuters", "AP", "ESPN", "Kentucky Sports Radio"). Then run **one** command for the whole edition to pull each article's lead photo:

```
python3 scripts/og_image.py <url1> <url2> ... > /tmp/imgs.tsv
```

and set each item's `img` to the image URL it printed (skip items that printed `-`). This is what puts the story's own photograph on the page. Do NOT hand-write image URLs, and do NOT reuse one article's photo for a different story. `imgQuery` (Wikipedia subject lookup) is now only a fallback for items with no usable `img`: set it only when confident of a single concrete, depictable subject (person/place/institution/company/hardware) — a wrong photo is the failure mode to avoid.

Repeat check (required, before finalizing): read `data/manifest.json`'s `news` array for the last 3 published dates, decrypt each (`python3 scripts/decrypt_calendar.py data/news/<date>.json.enc /tmp/cal_pass.txt /tmp/prev-<date>.json`), and compare your candidates against their items' `storyKey` values AND their leads. Drop any candidate that covers the same underlying event as something already published in those 3 editions, unless there's a genuinely new dated development — in which case the lead sentence must state what changed and when ("…re-committed to Missouri on Monday, ending a week of…"). When in doubt, drop it — a short section beats a repeated one. Clean up `/tmp/prev-*.json` after.

Every item also carries `storyKey`: a short stable slug for the underlying event (`mitchell-missouri-commit`, `fed-sept-rate-cut`, `giants-week1-loss`), so tomorrow's run can match repeats even when the wording differs.

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
      {"lead":"Bold lead sentence.","body":"1-2 punchy sentences.","detail":"optional 4-6 sentence expansion","storyKey":"short-event-slug","url":"https://…/the-article","source":"Outlet","img":"https://…/lead-photo.jpg (from og_image.py; omit if none)","imgQuery":"fallback only: a photographable subject","caption":"optional"}
    ]},
    "... x5 in topic order, the 5th titled \"Sports\" with every item carrying a group field: {\"lead\":\"...\",\"body\":\"...\",\"group\":\"Favorites\"}"
  ],
  "sources": "Reuters, AP, ..."
}
```

Photos: see "Source + photo per item" above — `img` from the article itself first, `imgQuery` only as a fallback, never the same `imgQuery` twice in one section. A missing photo is fine; a wrong one is not. Validate: `python3 -c "import json;json.load(open('/tmp/news.json'))"`.

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
3. Run `CAL_PASS_FILE=/tmp/cal_pass.txt bash scripts/add_word.sh /tmp/word.json` (schema: `{"term":...,"ipa":...,"respell":...,"pos":...,"definition":...,"example":...,"date":"YYYY-MM-DD"}`) to append to the word bank. With no `GH_TOKEN_FILE` set it edits and commits `data/words.json.enc` in place in this checkout (no clone, no push of its own) — the push happens with everything else in Step 4.

## Step 4: finish

Rebuild `data/manifest.json` from what's actually on disk (mirror the logic in `scripts/publish.sh`: for each of `market`/`news`/`calendar`, list `data/<kind>/*.json.enc` basenames as dates, sorted newest-first). Commit everything that changed (`git add -A && git commit -m "publish: <date> daily brief"`) and `git push` — if the push is rejected because something else committed first (e.g. the 15-minute calendar refresher), `git pull --rebase` and retry a couple of times. NEVER commit any plaintext `.json` (only the `.json.enc` payloads) — clean up `/tmp/*.json`, `/tmp/*.txt`, `/tmp/prev-*` when done.

End with a short summary in the job log: today's date, whether market ran, a one-line gist of the news headline, and confirmation both/all files published successfully. If anything failed, say exactly what and why rather than silently skipping it.

Reminder: as soon as this summary is printed, STOP. Do not continue working, re-checking, or exploring further.
