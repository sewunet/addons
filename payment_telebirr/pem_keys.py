"""Reading an RSA key the way merchants actually paste it.

The gateways hand their merchants keys in whatever shape their portal or
their integration pack produces, and the key then travels through e-mail, a
chat, a spreadsheet and the Odoo settings form before it is used. What
arrives is rarely a tidy PEM file. The shapes seen in practice:

  * the PEM block intact;
  * the PEM block on one line, its line breaks turned into spaces;
  * the PEM block with literal "\\n" in place of line breaks (copied out of
    a JSON file or an .env line);
  * the bare base64 body with no -----BEGIN/END----- lines at all, which is
    how telebirr and ChatBirr portals commonly display it.

`cryptography` reads only the first, and for every other shape it raises
"Could not deserialize key data ... unsupported", which says nothing about
what is wrong with the key -- the key is usually fine. These functions put
the key back into a shape the library reads, and when it truly cannot be
read (a password-protected key, an EC key, a truncated paste) they raise a
ValueError that says which.

Kept free of Odoo imports, like the crypto module beside it. The same file
lives in payment_telebirr and football_club_chatbirr: neither module
depends on the other, and a key reader is not worth a dependency.
"""

import base64
import binascii
import re

_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)


def _clean(text):
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = (text or "").strip().strip('"').strip("'")
    return text.replace("\\r", "").replace("\\n", "\n").strip()


def _pem(label, body):
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return ("-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(lines), label)).encode()


def _decode(text, what):
    """Return ("pem", bytes) or ("der", bytes) for the key in `text`."""
    text = _clean(text)
    if not text:
        raise ValueError("No %s is set." % what)
    block = _PEM_BLOCK.search(text)
    if block:
        label, body = block.group(1), block.group(2)
        if "ENCRYPTED" in label or "Proc-Type:" in body:
            raise ValueError(
                "The %s is password-protected. Export it again without a "
                "password (unencrypted PKCS#8 or PKCS#1 PEM) and paste that." % what
            )
        return "pem", _pem(label, re.sub(r"\s+", "", body))
    if "-----BEGIN" in text:
        raise ValueError(
            "The %s is cut short: it has a -----BEGIN line but no matching "
            "-----END line. Paste the whole key." % what
        )
    try:
        return "der", base64.b64decode(re.sub(r"\s+", "", text), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(
            "The %s is not a PEM key or a base64 key. Paste the key exactly as "
            "the gateway gave it, with or without its -----BEGIN/END----- lines." % what
        ) from None


def load_private_key(text, what="merchant private key"):
    """The RSA private key in `text`, however it was pasted."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    kind, data = _decode(text, what)
    try:
        if kind == "pem":
            key = serialization.load_pem_private_key(data, password=None)
        else:
            key = serialization.load_der_private_key(data, password=None)
    except TypeError:
        raise ValueError(
            "The %s is password-protected. Export it again without a password "
            "and paste that." % what
        ) from None
    except ValueError:
        raise ValueError(
            "The %s could not be read: the text is not a complete RSA private "
            "key. Check that the whole key was pasted and that it is the "
            "private key, not the public one." % what
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("The %s is not an RSA key; the gateway signs with RSA." % what)
    return key


def load_public_key(text, what="gateway public key"):
    """The RSA public key in `text`, however it was pasted."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    kind, data = _decode(text, what)
    try:
        if kind == "pem":
            key = serialization.load_pem_public_key(data)
        else:
            key = serialization.load_der_public_key(data)
    except ValueError:
        raise ValueError(
            "The %s could not be read: the text is not a complete RSA public "
            "key. Check that the whole key was pasted." % what
        ) from None
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("The %s is not an RSA key." % what)
    return key
