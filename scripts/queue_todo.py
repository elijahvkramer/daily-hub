#!/usr/bin/env python3
"""Queue a to-do for the Daily Hub list -- works from ANY Claude session (phone or computer).

Why (Oct 12, 2026): adding a to-do from a phone chat failed with "couldn't connect to your
computer". The list lives in Firestore (todos/main), and Claude's cloud sessions are not allowed
to reach Firestore, so the only working path ran through Eli's Mac. GitHub IS reachable from
every session, so a to-do now travels through the repo:

  1. this script seals the item with the site's PUBLIC key (data/inbox.pub.pem -- no passphrase,
     no secrets needed) into data/todo-queue/<id>.json
  2. commit + push that one file (git, or `gh api` PUT to the contents endpoint)
  3. the "To-do Intake" workflow fires on that push, opens it with the private key (unlocked by the
     CAL_PASSPHRASE secret), adds it to Firestore todos/main with a precondition-checked write
     (add-if-absent by id, so a replay never duplicates), and deletes the queue file.
     Hub Heartbeat also sweeps the queue every 15 minutes in case a push trigger is missed.

Usage:
  queue_todo.py "Call the county clerk" [--kind urgent|chill] [--list personal|work]
                [--due YYYY-MM-DD] [--note "..."] [--out DIR]
Prints the path of the file to commit. Default kind is urgent, list personal.
"""
import argparse
import base64
import datetime
import json
import os
import secrets
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = os.path.dirname(os.path.abspath(__file__))


def b64(b):
    return base64.b64encode(b).decode()


def gen_id():
    """Same shape as the site's genId(): base36 timestamp + random suffix."""
    n, s = int(time.time() * 1000), ""
    while n:
        n, r = divmod(n, 36)
        s = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + s
    return s + secrets.token_hex(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--kind", choices=["urgent", "chill"], default="urgent")
    ap.add_argument("--list", dest="lst", choices=["personal", "work"], default="personal")
    ap.add_argument("--due", default=None)
    ap.add_argument("--note", default=None)
    ap.add_argument("--out", default=os.path.join(HERE, "..", "data", "todo-queue"))
    ap.add_argument("--pub", default=os.path.join(HERE, "..", "data", "inbox.pub.pem"))
    a = ap.parse_args()
    if a.due:
        datetime.date.fromisoformat(a.due)        # validates
    item = {"id": gen_id(), "text": a.text.strip(), "kind": a.kind, "list": a.lst, "done": False}
    if a.due:
        item["due"] = a.due
    if a.note:
        item["note"] = a.note
    doc = {"item": item, "queued": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    pub = serialization.load_pem_public_key(open(a.pub, "rb").read())
    k, iv = AESGCM.generate_key(bit_length=256), os.urandom(12)
    ct = AESGCM(k).encrypt(iv, json.dumps(doc, ensure_ascii=False).encode(), None)
    wk = pub.encrypt(k, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    os.makedirs(a.out, exist_ok=True)
    path = os.path.normpath(os.path.join(a.out, item["id"] + ".json"))
    with open(path, "w") as f:
        json.dump({"v": "pk1", "alg": "RSA-OAEP-256+A256GCM", "k": b64(wk), "iv": b64(iv), "ct": b64(ct)}, f)
    print(path)


if __name__ == "__main__":
    main()
