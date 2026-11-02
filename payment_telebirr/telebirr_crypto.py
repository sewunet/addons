"""Telebirr SuperApp (C2B) request signing.

Kept free of Odoo imports so the signing rules can be tested on their own --
this is the part that silently fails if it is even slightly wrong.

VERIFY BEFORE GOING LIVE. Ethio Telecom does not publish these rules openly;
they are reconstructed from the community integration libraries that merchants
use, and confirmed against several of them. Two details in particular must be
checked against the official merchant integration pack:

  * the RSA padding. Telebirr documents "SHA256withRSA", which in Java means
    PKCS#1 v1.5. Some community clients use PSS instead. PADDING below selects
    it; PKCS#1 v1.5 is the default because it is what the named algorithm
    normally denotes.
  * the exact `biz_content` field set, which differs between the H5 web flow
    and the SuperApp flow.

A wrong choice here produces a signature the gateway rejects, not a silent
mis-charge, so the failure is loud -- but it will block every payment.
"""

import base64
import time
import uuid

from .pem_keys import load_private_key, load_public_key

# Fields excluded from the signature, per the documented rule.
EXCLUDED_FIELDS = frozenset(
    {"sign", "sign_type", "header", "refund_info", "openType", "raw_request"}
)

SIGN_TYPE = "SHA256withRSA"

# "pkcs1v15" (what SHA256withRSA denotes in Java) or "pss".
PADDING = "pkcs1v15"


def build_sign_string(payload):
    """Flatten a request into the exact string Telebirr signs.

    Top-level fields and the fields nested under `biz_content` are flattened
    into one `key=value` list, sorted, and joined with `&`. The nesting is
    deliberately discarded -- `biz_content`'s children sit alongside the
    top-level keys rather than under them.
    """
    parts = []
    for key, value in payload.items():
        if key in EXCLUDED_FIELDS:
            continue
        if key == "biz_content":
            for inner_key, inner_value in (value or {}).items():
                parts.append(f"{inner_key}={inner_value}")
        else:
            parts.append(f"{key}={value}")
    parts.sort()
    return "&".join(parts)


def sign(payload, private_key_pem):
    """Return the base64 SHA256-with-RSA signature for a request payload.

    `private_key_pem` is the merchant's private key, as PEM or as the bare
    base64 the portal shows (see pem_keys). Imported lazily so that a
    database without `cryptography` still loads the module.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding

    key = load_private_key(private_key_pem, "telebirr merchant private key")
    message = build_sign_string(payload).encode("utf-8")
    if PADDING == "pss":
        pad = rsa_padding.PSS(
            mgf=rsa_padding.MGF1(hashes.SHA256()),
            salt_length=rsa_padding.PSS.MAX_LENGTH,
        )
    else:
        pad = rsa_padding.PKCS1v15()
    return base64.b64encode(key.sign(message, pad, hashes.SHA256())).decode("utf-8")


def verify(payload, signature, public_key_pem):
    """Check a Telebirr-signed payload (used on the notify callback).

    Returns False rather than raising on any malformed input: a notification
    with a bad signature is an untrusted request, not an internal error.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding

    if not signature or not public_key_pem:
        return False
    try:
        key = load_public_key(public_key_pem, "telebirr public key")
        pad = (
            rsa_padding.PSS(
                mgf=rsa_padding.MGF1(hashes.SHA256()),
                salt_length=rsa_padding.PSS.MAX_LENGTH,
            )
            if PADDING == "pss"
            else rsa_padding.PKCS1v15()
        )
        key.verify(
            base64.b64decode(signature),
            build_sign_string(payload).encode("utf-8"),
            pad,
            hashes.SHA256(),
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def nonce_str():
    return uuid.uuid4().hex


def timestamp():
    """Telebirr expects seconds since the epoch, as a string."""
    return str(int(time.time()))
