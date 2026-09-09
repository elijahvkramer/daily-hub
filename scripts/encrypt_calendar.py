#!/usr/bin/env python3
"""Encrypt a calendar-briefing JSON for the Daily Hub site.

Usage: python3 encrypt_calendar.py <plaintext.json> <passphrase-file> <out.json.enc>

Format matches the site's WebCrypto decryptor:
PBKDF2-SHA256 (300,000 iterations, fixed site salt) -> AES-256-GCM (12-byte random IV).
Output: {"v":1,"kdf":"PBKDF2-SHA256","iter":300000,"salt":b64,"iv":b64,"ct":b64}
(ct includes the GCM tag, as WebCrypto expects.)
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dh_crypto import encrypt_bytes, read_passphrase  # fixed site salt -- see dh_crypto.py

def main():
    plain_path, pass_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    passphrase = read_passphrase(pass_path)
    data = open(plain_path, "rb").read()
    json.loads(data)  # validate JSON before encrypting

    payload = encrypt_bytes(data, passphrase)
    with open(out_path, "w") as f:
        json.dump(payload, f)
    print(f"encrypted -> {out_path}")

if __name__ == "__main__":
    main()
