#!/usr/bin/env python3
"""Seal a Gmail inbox snapshot for the Morning Brief with the site's PUBLIC key.

Why (Oct 12, 2026): the brief's email panel had never shown a single message. Its only feed,
refresh_inbox.py, needs three Gmail OAuth secrets in GitHub that were never created, so
data/inbox.json.enc never existed and the site always fell back to "Gmail isn't connected".

The inbox is now read by a Claude scheduled task through Eli's Gmail connector. That task has
no access to the site passphrase -- and doesn't need it: it seals the snapshot with an RSA
public key (data/inbox.pub.pem, safe to publish). Only the matching private key can open it,
and that key lives in data/inbox.key.enc, itself encrypted with the site passphrase like every
other data file. So the browser unlocks the private key with the passphrase, then the inbox.

  seal_inbox.py <items.json> <out.json.enc> [pub.pem]
      items.json: {"items":[{"id","threadId","from","email","subject","snippet","date",
                             "unread":bool,"important":bool}], "total": n}
      writes {"v":"pk1","alg":"RSA-OAEP-256+A256GCM","k","iv","ct"}

  seal_inbox.py --keygen <passphrase_file> <repo_dir>
      one-time: creates data/inbox.pub.pem and data/inbox.key.enc
"""
import base64
import datetime
import json
import os
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = os.path.dirname(os.path.abspath(__file__))
b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731


def keygen(pass_file, repo):
    sys.path.insert(0, HERE)
    from dh_crypto import encrypt_json, read_passphrase
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pkcs8 = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    open(os.path.join(repo, "data", "inbox.pub.pem"), "wb").write(pub)
    json.dump(encrypt_json({"pkcs8": b64(pkcs8)}, read_passphrase(pass_file)),
              open(os.path.join(repo, "data", "inbox.key.enc"), "w"))
    print("wrote data/inbox.pub.pem and data/inbox.key.enc")


def seal(items_path, out_path, pub_path):
    snap = json.load(open(items_path))
    items = snap.get("items", [])
    clean = []
    for m in items[:8]:
        clean.append({k: (str(m.get(k, ""))[:240] if k not in ("unread", "important") else bool(m.get(k)))
                      for k in ("id", "threadId", "from", "email", "subject", "snippet", "date", "unread", "important")})
    doc = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "connected": True, "source": "gmail-connector",
           "total": int(snap.get("total", len(clean)) or len(clean)),
           "unread": sum(1 for m in clean if m["unread"]), "items": clean}
    pub = serialization.load_pem_public_key(open(pub_path, "rb").read())
    k, iv = AESGCM.generate_key(bit_length=256), os.urandom(12)
    ct = AESGCM(k).encrypt(iv, json.dumps(doc, ensure_ascii=False).encode(), None)
    wk = pub.encrypt(k, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    json.dump({"v": "pk1", "alg": "RSA-OAEP-256+A256GCM", "k": b64(wk), "iv": b64(iv), "ct": b64(ct)}, open(out_path, "w"))
    print(f"sealed {len(clean)} message(s) -> {out_path}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--keygen"]:
        keygen(a[1], a[2])
    elif len(a) >= 2:
        seal(a[0], a[1], a[2] if len(a) > 2 else os.path.join(HERE, "..", "data", "inbox.pub.pem"))
    else:
        print(__doc__)
