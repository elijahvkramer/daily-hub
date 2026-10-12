#!/usr/bin/env python3
"""Move queued to-dos (data/todo-queue/*.json, sealed by queue_todo.py) into Firestore todos/main.

Runs in GitHub Actions ("To-do Intake" workflow), which -- unlike Claude's cloud sessions -- can
reach Google. Authenticates exactly like the website does: Firebase anonymous sign-in with the
site's public web API key (the Firestore rules require request.auth, and anonymous satisfies it).

Write safety mirrors sync-core.js: read the doc, add each item only if its id is absent, then
PATCH only the `items` field with a currentDocument.updateTime precondition, retrying on conflict.
So it never clobbers an edit made on the phone or computer at the same moment, and replaying a
queue file never creates a duplicate.

Usage: todo_intake.py <passphrase_file> [repo_dir] [--dry-run]
Deletes each queue file once its item is confirmed in Firestore; prints a summary.
"""
import base64
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dh_crypto import decrypt_json, read_passphrase  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

API_KEY = "AIzaSyBbS7KO_-ji_I82nZZ0UuorW47Unf3BNOE"     # the site's public Firebase web key (in index.html)
PROJECT = "daily-hub-todos"
DOC = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents/todos/main"
REFERER = "https://elijahvkramer.github.io/"            # in case the key is referrer-restricted


def http(method, url, body=None, token=None):
    h = {"Content-Type": "application/json", "Referer": REFERER}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


# ---- Firestore value <-> python (lossless for the types this doc uses) ----
def dec(v):
    if "nullValue" in v:
        return None
    if "booleanValue" in v:
        return v["booleanValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "stringValue" in v:
        return v["stringValue"]
    if "timestampValue" in v:
        return {"__ts": v["timestampValue"]}
    if "arrayValue" in v:
        return [dec(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return {k: dec(x) for k, x in v["mapValue"].get("fields", {}).items()}
    return {"__raw": v}


def enc(x):
    if x is None:
        return {"nullValue": None}
    if isinstance(x, bool):
        return {"booleanValue": x}
    if isinstance(x, int):
        return {"integerValue": str(x)}
    if isinstance(x, float):
        return {"doubleValue": x}
    if isinstance(x, str):
        return {"stringValue": x}
    if isinstance(x, list):
        return {"arrayValue": {"values": [enc(i) for i in x]}}
    if isinstance(x, dict):
        if set(x) == {"__ts"}:
            return {"timestampValue": x["__ts"]}
        if set(x) == {"__raw"}:
            return x["__raw"]
        return {"mapValue": {"fields": {k: enc(v) for k, v in x.items()}}}
    return {"stringValue": str(x)}


def private_key(repo, passphrase):
    with open(os.path.join(repo, "data", "inbox.key.enc")) as f:
        kf = decrypt_json(json.load(f), passphrase)
    return serialization.load_der_private_key(base64.b64decode(kf["pkcs8"]), password=None)


def unseal(env, priv):
    k = priv.decrypt(base64.b64decode(env["k"]),
                     padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return json.loads(AESGCM(k).decrypt(base64.b64decode(env["iv"]), base64.b64decode(env["ct"]), None))


def item_ids(fields):
    items = dec(fields["items"]) if "items" in fields else []
    return items, {t.get("id") for t in items if isinstance(t, dict)}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    passphrase = read_passphrase(args[0])
    repo = args[1] if len(args) > 1 else "."
    files = sorted(glob.glob(os.path.join(repo, "data", "todo-queue", "*.json")))
    if not files:
        print("to-do queue empty")
        return 0
    priv = private_key(repo, passphrase)
    queued = []
    for f in files:
        try:
            with open(f) as fh:
                queued.append((f, unseal(json.load(fh), priv)["item"]))
        except Exception as e:  # noqa: BLE001
            print(f"  ! could not open {os.path.basename(f)}: {e}", file=sys.stderr)
    if not queued:
        return 1

    st, auth = http("POST", f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={API_KEY}",
                    {"returnSecureToken": True})
    if st != 200 or "idToken" not in auth:
        print(f"::error::anonymous sign-in failed ({st}): {json.dumps(auth)[:300]}")
        return 2
    token = auth["idToken"]

    for attempt in range(6):
        st, doc = http("GET", DOC, token=token)
        if st == 404:
            fields, update_time = {}, None
        elif st != 200:
            print(f"::error::reading todos/main failed ({st}): {json.dumps(doc)[:300]}")
            return 2
        else:
            fields, update_time = doc.get("fields", {}), doc.get("updateTime")
        items, have = item_ids(fields)
        new = [it for _, it in queued if it["id"] not in have]
        if not new:
            break
        if dry:
            print("DRY RUN would add:", [it["text"] for it in new])
            return 0
        url = DOC + "?updateMask.fieldPaths=items"
        url += f"&currentDocument.updateTime={update_time}" if update_time else "&currentDocument.exists=false"
        st, res = http("PATCH", url, {"fields": {"items": enc(items + new)}}, token=token)
        if st == 200:
            print("  + added:", "; ".join(it["text"] for it in new))
            break
        if st in (409, 412) or "FAILED_PRECONDITION" in json.dumps(res):
            time.sleep(1 + attempt)          # someone edited in between -- reread and retry
            continue
        print(f"::error::writing todos/main failed ({st}): {json.dumps(res)[:300]}")
        return 2
    else:
        print("::error::gave up after repeated write conflicts")
        return 2

    # confirm, then clear the queue
    st, doc = http("GET", DOC, token=token)
    have = item_ids(doc.get("fields", {}))[1] if st == 200 else set()
    for f, it in queued:
        if it["id"] in have:
            os.remove(f)
    left = len(glob.glob(os.path.join(repo, "data", "todo-queue", "*.json")))
    print(f"to-do intake: {len(queued) - left} delivered, {left} still queued")
    return 0 if left == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
