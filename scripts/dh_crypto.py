#!/usr/bin/env python3
"""Shared encryption helpers for every Daily Hub data file.

Format (unchanged, matches the site's WebCrypto decryptor):
  PBKDF2-SHA256 (300,000 iterations) -> AES-256-GCM (12-byte random IV)
  {"v":1,"kdf":"PBKDF2-SHA256","iter":300000,"salt":b64,"iv":b64,"ct":b64}

What changed (Sep 2026): every file used to get its OWN random salt, which forced the
browser to run a full 300k-iteration PBKDF2 for every single file it opened -- the
calendar day, holdings, the market brief, the word bank, the quotes file... each one a
~150ms-1s stall depending on the device. That was a large share of the "sporadic lag"
on the Home tab. Files are now encrypted with one fixed site salt, so the browser derives
the key once per session and reuses it (it caches keys by salt, so older files with their
own salt still open -- nothing needs re-encrypting). A fixed salt is standard for a
single-user vault: the salt exists to defeat precomputed-table attacks across many users,
and the random per-file IV is what keeps two ciphertexts independent.
"""
import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITER = 300_000
# 16 bytes, fixed for the site. Changing this would only cost one extra key derivation
# per session for files made before the change -- it is not a secret.
SITE_SALT = bytes.fromhex("6d4a7e1f9b2c58e30f7a41d6c9b8e2a5")

_key_cache = {}


def key_for(passphrase: bytes, salt: bytes) -> bytes:
    k = (passphrase, salt)
    if k not in _key_cache:
        _key_cache[k] = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                                   iterations=ITER).derive(passphrase)
    return _key_cache[k]


def encrypt_bytes(data: bytes, passphrase: bytes) -> dict:
    iv = os.urandom(12)
    ct = AESGCM(key_for(passphrase, SITE_SALT)).encrypt(iv, data, None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITER,
            "salt": b64(SITE_SALT), "iv": b64(iv), "ct": b64(ct)}


def encrypt_json(obj, passphrase: bytes) -> dict:
    return encrypt_bytes(json.dumps(obj, ensure_ascii=False).encode(), passphrase)


def decrypt_payload(payload: dict, passphrase: bytes) -> bytes:
    salt = base64.b64decode(payload["salt"])
    iv = base64.b64decode(payload["iv"])
    ct = base64.b64decode(payload["ct"])
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=payload.get("iter", ITER)).derive(passphrase) \
        if salt != SITE_SALT else key_for(passphrase, SITE_SALT)
    return AESGCM(key).decrypt(iv, ct, None)


def decrypt_json(payload: dict, passphrase: bytes):
    return json.loads(decrypt_payload(payload, passphrase))


def read_passphrase(path: str) -> bytes:
    return open(path).read().strip().encode()
