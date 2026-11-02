"""Merchant keys, in the shapes they are actually pasted.

Two of these shapes -- the PEM block flattened onto one line, and the bare
base64 body without its BEGIN/END lines -- made `cryptography` raise "Could
not deserialize key data ... unsupported" at the first payment, though the
key itself was good.
"""

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.payment_telebirr import telebirr_crypto as tc
from odoo.addons.payment_telebirr.pem_keys import load_private_key, load_public_key


@tagged("post_install", "-at_install")
class TestPastedKeys(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pkcs8 = cls.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        cls.pkcs1 = cls.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        ).decode()
        cls.public = cls.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        cls.encrypted = cls.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"secret"),
        ).decode()

    @staticmethod
    def _body(pem):
        return "".join(pem.strip().splitlines()[1:-1])

    def _same_key(self, loaded):
        self.assertEqual(loaded.private_numbers(), self.key.private_numbers())

    def test_every_pasted_shape_of_a_private_key_reads(self):
        shapes = {
            "intact PKCS#8": self.pkcs8,
            "intact PKCS#1": self.pkcs1,
            "one line": " ".join(self.pkcs8.strip().splitlines()),
            "literal \\n": self.pkcs8.replace("\n", "\\n"),
            "bare PKCS#8 base64": self._body(self.pkcs8),
            "bare PKCS#1 base64": self._body(self.pkcs1),
            "bare base64 wrapped": "\n".join(self.pkcs8.strip().splitlines()[1:-1]),
            "quoted, padded": '  "%s"  ' % self.pkcs8.replace("\n", "\\n"),
        }
        for name, text in shapes.items():
            with self.subTest(shape=name):
                self._same_key(load_private_key(text))

    def test_every_pasted_shape_of_a_public_key_reads(self):
        for text in (self.public, " ".join(self.public.strip().splitlines()), self._body(self.public)):
            self.assertEqual(load_public_key(text).public_numbers(), self.key.public_key().public_numbers())

    def test_signing_works_with_the_bare_base64_the_portal_shows(self):
        payload = {"timestamp": "100", "biz_content": {"merch_order_id": "SO1"}}
        signature = tc.sign(payload, self._body(self.pkcs8))
        self.assertTrue(tc.verify(payload, signature, self._body(self.public)))

    def test_an_unreadable_key_says_why_instead_of_an_openssl_tuple(self):
        cases = {
            "": "No telebirr merchant private key is set",
            self.encrypted: "password-protected",
            self.pkcs8.strip().splitlines()[0] + "\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC": "cut short",
            "this is not a key": "not a PEM key or a base64 key",
            self._body(self.pkcs8)[:200]: "could not be read",
        }
        for text, message in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                tc.sign({"a": "1"}, text)

    def test_the_public_half_pasted_as_the_private_key_is_named(self):
        with self.assertRaisesRegex(ValueError, "not the public one"):
            load_private_key(self.public)
