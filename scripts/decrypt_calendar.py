#!/usr/bin/env python3
"""Decrypt a Daily Hub data file (news/market/calendar/words .json.enc) back to plaintext.

Usage: python3 decrypt_calendar.py <in.json.enc> <passphrase-file> <out.json>

Counterpart to encrypt_calendar.py — same format:
PBKDF2-SHA256 (300,000 iterations, 16-byte salt) -> AES-256-GCM (12-byte IV).
Input: {"v":1,"kdf":"PBKDF2-SHA256","iter":300000,"salt":b64,"iv":b64,"ct":b64}

Used by scheduled tasks (e.g. daily-news-briefing) to read recent past
editions before publishing a new one, so they can check for repeated
stories/content rather than re-decrypting by hand each time.
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dh_crypto import decrypt_payload, read_passphrase

def main():
    enc_path, pass_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    passphrase = read_passphrase(pass_path)
    payload = json.load(open(enc_path))
    pt = decrypt_payload(payload, passphrase)

    with open(out_path, "wb") as f:
        f.write(pt)
    print(f"decrypted -> {out_path}")

if __name__ == "__main__":
    main()
