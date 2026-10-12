#!/usr/bin/env python3
"""Offline tests for the to-do queue: python3 scripts/test_todo_queue.py

Uses a throwaway keypair, so it needs no passphrase and touches no network."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import todo_intake as t  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


class Queue(unittest.TestCase):
    def test_seal_then_unseal(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with tempfile.TemporaryDirectory() as d:
            pub = os.path.join(d, "pub.pem")
            with open(pub, "wb") as f:
                f.write(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                      serialization.PublicFormat.SubjectPublicKeyInfo))
            out = subprocess.run([sys.executable, os.path.join(HERE, "queue_todo.py"), "Call the clerk",
                                  "--kind", "chill", "--due", "2026-10-20", "--out", d, "--pub", pub],
                                 capture_output=True, text=True, check=True).stdout.strip()
            with open(out) as f:
                env = json.load(f)
            self.assertEqual(env["v"], "pk1")
            self.assertNotIn("Call the clerk", json.dumps(env))
            item = t.unseal(env, key)["item"]
            self.assertEqual(item["text"], "Call the clerk")
            self.assertEqual(item["kind"], "chill")
            self.assertEqual(item["due"], "2026-10-20")
            self.assertFalse(item["done"])
            self.assertTrue(os.path.basename(out).startswith(item["id"]))

    def test_firestore_value_roundtrip(self):
        v = {"items": [{"id": "x", "n": 3, "f": 1.5, "b": True, "z": None, "subs": [{"id": "s", "done": False}]}],
             "t": {"__ts": "2026-10-12T00:00:00Z"}}
        self.assertEqual(t.dec(t.enc(v)), v)


if __name__ == "__main__":
    unittest.main(verbosity=2)
