"""The `client` field of Chapa's authorize call.

When a wallet authorises by one-time code (Awash Birr, Amole), the code is
sent to Chapa's `/v1/validate` inside `client`: the JSON
``{"requestID": ..., "otp": ...}`` encrypted with 3DES in ECB mode under the
merchant's encryption key from the Chapa dashboard, PKCS#7-padded to 8-byte
blocks, base64-encoded. That is Chapa's documented scheme, reproduced here;
ECB is their choice, not ours, and the plaintext is a short one-time value.
"""

import base64
import json

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, modes

try:  # cryptography >= 43 moved 3DES out of the mainstream algorithms.
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:  # pragma: no cover - older cryptography
    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES


def _key_bytes(key):
    raw = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    if len(raw) not in (16, 24):
        raise ValueError("The Chapa encryption key must be 16 or 24 characters long.")
    return raw


def encrypt_client(key, payload):
    """Encrypt ``payload`` (a dict) the way Chapa's `/v1/validate` expects."""
    padder = padding.PKCS7(64).padder()
    data = padder.update(json.dumps(payload, separators=(",", ":")).encode("utf-8")) + padder.finalize()
    encryptor = Cipher(TripleDES(_key_bytes(key)), modes.ECB(), backend=default_backend()).encryptor()
    return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode("ascii")


def decrypt_client(key, token):
    """The inverse, for tests and for reading back what was sent."""
    decryptor = Cipher(TripleDES(_key_bytes(key)), modes.ECB(), backend=default_backend()).decryptor()
    data = decryptor.update(base64.b64decode(token)) + decryptor.finalize()
    unpadder = padding.PKCS7(64).unpadder()
    return json.loads((unpadder.update(data) + unpadder.finalize()).decode("utf-8"))
