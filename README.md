# Daily Hub

Live dashboard combining three automated daily briefings, published at
**https://elijahvkramer.github.io/daily-hub/**

- **Today** — personal daily briefing (calendar, to-dos, hourly weather, inbox)
- **News** — Daily News Briefing (Mon–Fri ~9am CT)
- **Markets** — Daily Market Brief (Mon–Fri ~3:15pm CT)
- **Portfolio** — live-priced Fidelity holdings view
- **Games** — daily crossword (pre-built server-side), mini, Case File, Freehand, word quiz

**The entire site is passphrase-gated.** Every data file in `data/` (news, market,
calendar, holdings) is stored exclusively as AES-256-GCM ciphertext
(PBKDF2-SHA256, 300k iterations) produced by `scripts/encrypt_calendar.py`,
and decrypted in the browser via WebCrypto. The repo and the served site contain
no plaintext content; only the page shell and dates in `manifest.json` are public.

## Encryption note

Every data file is encrypted with one fixed site salt (`scripts/dh_crypto.py`) and a random
IV, so the browser derives the key once per session instead of once per file (that was
~150ms–1s of PBKDF2 per file). All encrypting scripts go through `dh_crypto.py`; the
decryptor still honors per-file salts on older files.

## How it updates

Each Cowork scheduled task writes the day's JSON, encrypts it, and runs
`scripts/publish.sh <kind> <date> <file.json.enc>`, which commits it to
`data/<kind>/`, rebuilds `data/manifest.json`, and pushes (with retry).
GitHub Pages serves the update within about a minute. `publish.sh` refuses
to publish anything that is not an encryption envelope.

GitHub Actions (`.github/workflows/`):

- **Calendar Refresh** (`calendar-refresh.yml`, ~every 30 min, the one schedule GitHub honors
  reliably here) — `scripts/refresh_calendar.py` (Google Calendar → today's file), which then
  runs `scripts/refresh_quotes.py` (Yahoo → `data/quotes.json.enc`, the Portfolio's server-side
  price snapshot so the site never depends on a CORS proxy) and
  `scripts/build_crossword.js` (today's crossword → `data/crossword/<date>.json.enc`, built
  once with a shared repeat-avoidance history).
- **Daily Hub Brief** (`daily-brief.yml`, fires off Calendar Refresh / Morning Tick) — runs
  `.github/claude-daily-brief-prompt.md`: market brief, news (each story with its source
  article and that article's photo via `scripts/og_image.py`), Brain Food, Word of the Day.

Tests: `node scripts/test_crossword.js` (dictionary hygiene + generator shape).
