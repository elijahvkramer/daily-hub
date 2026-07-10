#!/usr/bin/env python3
"""Encrypt a calendar-briefing JSON for the Daily Hub site.

Usage: python3 encrypt_calendar.py <plaintext.json> <passphrase-file> <out.json.enc>

Format matches the site's WebCrypto decryptor:
PBKDF2-SHA256 (300,000 iterations, 16-byte salt) -> AES-256-GCM (12-byte IV).
Output: {"v":1,"kdf":"PBKDF2-SHA256","iter":300000,"salt":b64,"iv":b64,"ct":b64}
(ct includes the GCM tag, as WebCrypto expects.)
"""
import base64, json, os, sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

def main():
    plain_path, pass_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    passphrase = open(pass_path).read().strip().encode()
    data = open(plain_path, "rb").read()
    json.loads(data)  # validate JSON before encrypting

    salt = os.urandom(16)
    iv = os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=300_000).derive(passphrase)
    ct = AESGCM(key).encrypt(iv, data, None)

    b64 = lambda b: base64.b64encode(b).decode()
    payload = {"v": 1, "kdf": "PBKDF2-SHA256", "iter": 300_000,
               "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}
    with open(out_path, "w") as f:
        json.dump(payload, f)
    print(f"encrypted -> {out_path}")

if __name__ == "__main__":
    main()
