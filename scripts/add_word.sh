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

RES_DIR=""
for c in "/sessions/"*"/mnt/Cowork OS/00_Resources" "$HOME/Documents/Cowork OS/00_Resources"; do
  [[ -f "$c/.github-token" ]] && RES_DIR="$c" && break
done
[[ -n "$RES_DIR" ]] || { echo "resources dir with .github-token not found" >&2; exit 1; }
TOKEN="$(tr -d '[:space:]' < "$RES_DIR/.github-token")"
PASS_FILE="$RES_DIR/.calendar-passphrase"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git clone -q --depth 1 "https://x-access-token:${TOKEN}@github.com/elijahvkramer/daily-hub.git" "$WORK/repo"
cd "$WORK/repo"
git config user.email "elijahvkramer@gmail.com"
git config user.name "Daily Hub Bot"

WORD_SRC="$SRC" PASS_FILE="$PASS_FILE" python3 - <<'PY'
import base64, json, os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

passphrase = open(os.environ["PASS_FILE"]).read().strip().encode()
new = json.load(open(os.environ["WORD_SRC"]))
assert {"term","definition"} <= set(new), "word object needs at least term + definition"

def key_for(salt): return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=300_000).derive(passphrase)

words = []
if os.path.exists("data/words.json.enc"):
    p = json.load(open("data/words.json.enc"))
    pt = AESGCM(key_for(base64.b64decode(p["salt"]))).decrypt(base64.b64decode(p["iv"]), base64.b64decode(p["ct"]), None)
    words = json.loads(pt)

words = [w for w in words if w["term"].lower() != new["term"].lower()]  # dedupe / update
words.append(new)
words.sort(key=lambda w: w.get("date",""))

salt, iv = os.urandom(16), os.urandom(12)
ct = AESGCM(key_for(salt)).encrypt(iv, json.dumps(words).encode(), None)
b64 = lambda b: base64.b64encode(b).decode()
json.dump({"v":1,"kdf":"PBKDF2-SHA256","iter":300_000,"salt":b64(salt),"iv":b64(iv),"ct":b64(ct)}, open("data/words.json.enc","w"))
print(f"word bank now has {len(words)} words (added {new['term']})")
PY

git add data/words.json.enc
if git diff --cached --quiet; then echo "nothing to publish"; exit 0; fi
git commit -qm "words: update quiz bank"
for attempt in 1 2 3 4; do
  if git push -q origin main; then echo "word published"; exit 0; fi
  sleep $((attempt * 3)); git fetch -q origin main; git rebase -q origin/main || { git rebase --abort; exit 1; }
done
echo "push failed after retries" >&2; exit 1
