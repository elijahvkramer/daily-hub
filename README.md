# Daily Hub

Live dashboard combining three automated daily briefings, published at
**https://elijahvkramer.github.io/daily-hub/**

- **Today** — personal daily briefing (calendar, to-dos, hourly weather, inbox)
- **News** — Daily News Briefing (Mon–Fri ~9am CT)
- **Markets** — Daily Market Brief (Mon–Fri ~3:15pm CT)
- **Portfolio** — live-priced Fidelity holdings view

**The entire site is passphrase-gated.** Every data file in `data/` (news, market,
calendar, holdings) is stored exclusively as AES-256-GCM ciphertext
(PBKDF2-SHA256, 300k iterations) produced by `scripts/encrypt_calendar.py`,
and decrypted in the browser via WebCrypto. The repo and the served site contain
no plaintext content; only the page shell and dates in `manifest.json` are public.

## How it updates

Each Cowork scheduled task writes the day's JSON, encrypts it, and runs
`scripts/publish.sh <kind> <date> <file.json.enc>`, which commits it to
`data/<kind>/`, rebuilds `data/manifest.json`, and pushes (with retry).
GitHub Pages serves the update within about a minute. `publish.sh` refuses
to publish anything that is not an encryption envelope.
