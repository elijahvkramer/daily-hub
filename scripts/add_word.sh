#!/usr/bin/env bash
# Add today's Word of the Day to the Daily Hub quiz bank (data/words.json.enc).
#
# Usage: add_word.sh <word.json>
#   word.json: plain JSON object {term, ipa, respell, pos, definition, example, date}
#
# Decrypts the existing bank, appends (deduped by term), re-encrypts, commits, pushes.
set -euo pipefail

SRC="$1"
[[ -s "$SRC" ]] || { echo "missing word file: $SRC" >&2; exit 1; }
SRC="$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")"

# Secrets: prefer pre-fetched local files (GH_TOKEN_FILE / CAL_PASS_FILE env vars).
# Callers should set these when the mounted 00_Resources path has thrown
# "Resource deadlock avoided" (EDEADLK) on direct reads -- fetch the values via
# the Read tool instead and write them to local temp files, then point these
# env vars at them. Falls back to auto-discovering the mounted files if unset.
RES_DIR=""
for c in "/sessions/"*"/mnt/Cowork OS/00_Resources" "$HOME/Documents/Cowork OS/00_Resources"; do
  [[ -f "$c/.github-token" ]] && RES_DIR="$c" && break
done

PASS_FILE="${CAL_PASS_FILE:-}"
if [[ -z "$PASS_FILE" ]]; then
  [[ -n "$RES_DIR" ]] || { echo "resources dir with .calendar-passphrase not found (and CAL_PASS_FILE not set)" >&2; exit 1; }
  PASS_FILE="$RES_DIR/.calendar-passphrase"
fi

# Two ways to run:
#  (a) inside an already-authenticated checkout (GitHub Actions -- the daily brief): no token
#      needed, work in place and let the caller's git credentials push;
#  (b) from anywhere else with a token file: clone, edit, push (the original Cowork flow).
IN_REPO=0
if [[ -z "${GH_TOKEN_FILE:-}" && -z "$RES_DIR" && -f "scripts/dh_crypto.py" && -d ".git" ]]; then
  IN_REPO=1
  echo "no token file; running inside the current checkout"
else
  TOKEN_FILE="${GH_TOKEN_FILE:-}"
  if [[ -z "$TOKEN_FILE" ]]; then
    [[ -n "$RES_DIR" ]] || { echo "resources dir with .github-token not found (and GH_TOKEN_FILE not set)" >&2; exit 1; }
    TOKEN_FILE="$RES_DIR/.github-token"
  fi
  TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  git clone -q --depth 1 "https://x-access-token:${TOKEN}@github.com/elijahvkramer/daily-hub.git" "$WORK/repo"
  cd "$WORK/repo"
  git config user.email "elijahvkramer@gmail.com"
  git config user.name "Daily Hub Bot"
fi

WORD_SRC="$SRC" PASS_FILE="$PASS_FILE" python3 - <<'PY'
import json, os, sys
sys.path.insert(0, "scripts")
from dh_crypto import decrypt_json, encrypt_json   # fixed site salt -- see scripts/dh_crypto.py

passphrase = open(os.environ["PASS_FILE"]).read().strip().encode()
new = json.load(open(os.environ["WORD_SRC"]))
assert {"term","definition"} <= set(new), "word object needs at least term + definition"

words = []
if os.path.exists("data/words.json.enc"):
    words = decrypt_json(json.load(open("data/words.json.enc")), passphrase)

words = [w for w in words if w["term"].lower() != new["term"].lower()]  # dedupe / update
words.append(new)
words.sort(key=lambda w: w.get("date",""))

json.dump(encrypt_json(words, passphrase), open("data/words.json.enc","w"))
print(f"word bank now has {len(words)} words (added {new['term']})")
PY

git add data/words.json.enc
if git diff --cached --quiet; then echo "nothing to publish"; exit 0; fi
git commit -qm "words: update quiz bank"
if [[ "$IN_REPO" == "1" ]]; then echo "word committed in place (push with the rest of today's publish)"; exit 0; fi
for attempt in 1 2 3 4; do
  if git push -q origin main; then echo "word published"; exit 0; fi
  sleep $((attempt * 3)); git fetch -q origin main; git rebase -q origin/main || { git rebase --abort; exit 1; }
done
echo "push failed after retries" >&2; exit 1
