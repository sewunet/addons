"""telebirr request signing.

The sign-string construction is asserted directly, because it is the piece
most likely to be subtly wrong and it fails opaquely at the gateway.
"""

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.payment_telebirr import telebirr_crypto as tc


# post_install, like Odoo's own: at_install tests run against a half-loaded
# registry, and a later module's NOT NULL column on res.partner makes every
# partner creation fail on an incremental -u run.
@tagged("post_install", "-at_install")
class TestTelebirrSignString(TransactionCase):
    def test_biz_content_is_flattened_alongside_top_level_fields(self):
        """biz_content's children sit beside the top-level keys, not under them."""
        payload = {
            "timestamp": "100",
            "nonce_str": "abc",
            "biz_content": {"merch_order_id": "SO1", "total_amount": "10.00"},
        }
        self.assertEqual(
            tc.build_sign_string(payload),
            "merch_order_id=SO1&nonce_str=abc&timestamp=100&total_amount=10.00",
        )

    def test_fields_are_sorted_not_left_in_insertion_order(self):
        payload = {"z": "1", "a": "2", "m": "3"}
        self.assertEqual(tc.build_sign_string(payload), "a=2&m=3&z=1")

    def test_excluded_fields_never_enter_the_signature(self):
        payload = {
            "a": "1",
            "sign": "SHOULD-NOT-APPEAR",
            "sign_type": "SHA256withRSA",
            "header": "x",
            "refund_info": "y",
            "openType": "z",
            "raw_request": "w",
        }
        self.assertEqual(tc.build_sign_string(payload), "a=1")

    def test_signing_is_stable_and_verifies_against_its_own_key(self):
        """Round-trip through a throwaway key pair, so padding stays consistent."""
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            self.skipTest("cryptography is not installed")

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        public_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

        payload = {"timestamp": "100", "biz_content": {"merch_order_id": "SO1"}}
        signature = tc.sign(payload, private_pem)
        self.assertTrue(tc.verify(payload, signature, public_pem))

        # A changed amount must invalidate the signature.
        tampered = {"timestamp": "100", "biz_content": {"merch_order_id": "SO2"}}
        self.assertFalse(tc.verify(tampered, signature, public_pem))

    def test_verify_refuses_malformed_input_instead_of_raising(self):
        self.assertFalse(tc.verify({"a": "1"}, None, "not-a-key"))
        self.assertFalse(tc.verify({"a": "1"}, "sig", None))
        self.assertFalse(tc.verify({"a": "1"}, "!!not-base64!!", "not-a-key"))

    def test_nonce_is_unique_per_call(self):
        self.assertNotEqual(tc.nonce_str(), tc.nonce_str())
