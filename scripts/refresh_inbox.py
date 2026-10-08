#!/usr/bin/env python3
"""Gmail "Important + unread" snapshot -> data/inbox.json.enc  (feeds the Morning Brief)

Eli, Oct 2026: the morning brief should flag important email. "Important" means Gmail's own
Important marker, unread only, from the last 24 hours.

Auth: a personal OAuth refresh token with the read-only Gmail scope (a service account
cannot read a consumer @gmail.com inbox). Three env vars, all GitHub secrets:
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
If any are missing the script does nothing and exits 0 -- the site shows a calm
"inbox not connected" state instead of an error. Never fails the workflow.

What is written (encrypted, like every other data file -- the repo never holds plaintext):
  {"updated": iso, "connected": true, "total": n,
   "items": [{"id","threadId","from","email","subject","snippet","date"}]}
Only metadata + Gmail's own snippet are fetched (format=metadata); bodies are never read.

Usage: python3 refresh_inbox.py <passphrase_file> [repo_dir]
"""
import datetime
import email.utils
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dh_crypto import decrypt_json, encrypt_json, read_passphrase  # noqa: E402

QUERY = "is:unread is:important newer_than:1d -category:promotions -category:social"
MAX_ITEMS = 8
API = "https://gmail.googleapis.com/gmail/v1/users/me"


def http_json(url, data=None, token=None, timeout=20):
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def access_token():
    cid, sec, rt = (os.environ.get(k, "").strip() for k in
                    ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"))
    if not (cid and sec and rt):
        return None
    j = http_json("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": sec, "refresh_token": rt, "grant_type": "refresh_token"})
    return j.get("access_token")


def header(msg, name):
    for h in (msg.get("payload") or {}).get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 0
    passphrase = read_passphrase(sys.argv[1])
    repo = sys.argv[2] if len(sys.argv) > 2 else "."
    out_path = os.path.join(repo, "data", "inbox.json.enc")

    try:
        tok = access_token()
    except Exception as e:  # noqa: BLE001
        print(f"inbox: token refresh failed ({e}); leaving the previous file in place", file=sys.stderr)
        return 0
    if not tok:
        print("inbox: Gmail secrets not configured; skipping")
        return 0

    try:
        q = urllib.parse.urlencode({"q": QUERY, "maxResults": 25})
        listing = http_json(f"{API}/messages?{q}", token=tok)
        ids = [m["id"] for m in listing.get("messages", [])]
        total = listing.get("resultSizeEstimate", len(ids))
        items = []
        for mid in ids[:MAX_ITEMS]:
            mq = urllib.parse.urlencode([("format", "metadata"), ("metadataHeaders", "From"),
                                         ("metadataHeaders", "Subject"), ("metadataHeaders", "Date")])
            m = http_json(f"{API}/messages/{mid}?{mq}", token=tok)
            name, addr = email.utils.parseaddr(header(m, "From"))
            ts = int(m.get("internalDate", "0")) / 1000
            items.append({
                "id": m.get("id"), "threadId": m.get("threadId"),
                "from": (name or addr.split("@")[0] or "Unknown").strip('"'),
                "email": addr,
                "subject": header(m, "Subject") or "(no subject)",
                "snippet": (m.get("snippet") or "")[:220],
                "date": datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="seconds"),
            })
    except Exception as e:  # noqa: BLE001
        print(f"inbox: Gmail read failed ({e}); leaving the previous file in place", file=sys.stderr)
        return 0

    items.sort(key=lambda x: x["date"], reverse=True)
    core = {"connected": True, "total": max(total, len(items)) if items else 0, "items": items}

    # don't churn a commit every 30 minutes when nothing changed
    if os.path.exists(out_path):
        try:
            prev = decrypt_json(json.load(open(out_path)), passphrase)
            if {k: prev.get(k) for k in core} == core:
                print(f"inbox unchanged ({len(items)} items); not rewriting")
                return 0
        except Exception:  # noqa: BLE001
            pass

    payload = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **core}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(encrypt_json(payload, passphrase), f)
    print(f"wrote {out_path}: {len(items)} important unread")
    return 0


if __name__ == "__main__":
    sys.exit(main())
