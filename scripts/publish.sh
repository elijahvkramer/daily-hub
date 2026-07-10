#!/usr/bin/env bash
# Publish one day's data file to the Daily Hub site.
#
# Usage: publish.sh <kind> <date> <file>
#   kind: market | news | calendar
#   date: YYYY-MM-DD
#   file: path to the ENCRYPTED .json.enc payload (ALL kinds — the whole site is
#         passphrase-gated; encrypt with scripts/encrypt_calendar.py first)
#
# Reads the GitHub token from $GH_TOKEN_FILE, or the default workspace path.
set -euo pipefail

KIND="$1"; DATE="$2"; SRC="$3"
case "$KIND" in market|news|calendar) ;; *) echo "bad kind: $KIND" >&2; exit 1;; esac
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "bad date: $DATE" >&2; exit 1; }
[[ -s "$SRC" ]] || { echo "missing source file: $SRC" >&2; exit 1; }
SRC="$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")"   # absolute path; we cd later

TOKEN_FILE="${GH_TOKEN_FILE:-}"
if [[ -z "$TOKEN_FILE" ]]; then
  for c in "/sessions/"*"/mnt/Cowork OS/00_Resources/.github-token" \
           "$HOME/Documents/Cowork OS/00_Resources/.github-token"; do
    [[ -f $c ]] && TOKEN_FILE="$c" && break
  done
fi
[[ -n "$TOKEN_FILE" && -f "$TOKEN_FILE" ]] || { echo "GitHub token file not found" >&2; exit 1; }
TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git clone -q --depth 1 "https://x-access-token:${TOKEN}@github.com/elijahvkramer/daily-hub.git" "$WORK/repo"
cd "$WORK/repo"
git config user.email "elijahvkramer@gmail.com"
git config user.name "Daily Hub Bot"

EXT="json.enc"
# refuse to publish plaintext: the payload must be an encryption envelope
python3 -c "import json,sys; d=json.load(open('$SRC')); sys.exit(0 if {'salt','iv','ct'} <= set(d) else 1)" \
  || { echo "refusing to publish: $SRC is not an encrypted payload (encrypt it first)" >&2; exit 1; }
mkdir -p "data/$KIND"
cp "$SRC" "data/$KIND/$DATE.$EXT"

# Rebuild manifest from what's actually on disk.
python3 - <<'PY'
import json, os, datetime
m = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
for kind in ("market", "news", "calendar"):
    d = os.path.join("data", kind)
    dates = []
    if os.path.isdir(d):
        for f in os.listdir(d):
            base = f.split(".json")[0]
            if len(base) == 10 and base.count("-") == 2:
                dates.append(base)
    m[kind] = sorted(set(dates), reverse=True)
json.dump(m, open("data/manifest.json", "w"), indent=1)
PY

git add -A
if git diff --cached --quiet; then echo "nothing to publish"; exit 0; fi
git commit -qm "publish: $KIND $DATE"

# Push with retry — concurrent scheduled tasks can race on main.
for attempt in 1 2 3 4; do
  if git push -q origin main; then
    echo "published $KIND $DATE -> https://elijahvkramer.github.io/daily-hub/"
    exit 0
  fi
  sleep $((attempt * 3))
  git fetch -q origin main
  git rebase -q origin/main || { git rebase --abort; git reset --hard origin/main; cp "$SRC" "data/$KIND/$DATE.$EXT"; git add -A; git commit -qm "publish: $KIND $DATE (retry)"; }
done
echo "push failed after retries" >&2
exit 1
