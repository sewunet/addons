"""Star-Pay callback signature verification.

Mirrors the reference implementation in Star-Pay's docs, independently, so a
regression in the controller's HMAC shows up as a failing test rather than as
accepted forged callbacks.
"""

import hashlib
import hmac
import json

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


def reference_signature(secret, timestamp, body):
    """HMAC-SHA256 over `${timestamp}.${body}`, as the docs' JavaScript does."""
    return hmac.new(
        secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
    ).hexdigest()


# post_install, like Odoo's own: at_install tests run against a half-loaded
# registry, and a later module's NOT NULL column on res.partner makes every
# partner creation fail on an incremental -u run.
@tagged("post_install", "-at_install")
class TestStarPaySignature(TransactionCase):
    SECRET = "whsec_unit_test"
    TIMESTAMP = "1730000000000"
    BODY = '{"billRefNo":"33WJ8946WB","status":"PAID","amount":250}'

    def _controller_signature(self, secret, timestamp, body):
        """The exact computation the webhook controller performs."""
        return hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}.{body}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def test_matches_the_reference_implementation(self):
        self.assertEqual(
            self._controller_signature(self.SECRET, self.TIMESTAMP, self.BODY),
            reference_signature(self.SECRET, self.TIMESTAMP, self.BODY),
        )

    def test_a_tampered_amount_changes_the_signature(self):
        good = reference_signature(self.SECRET, self.TIMESTAMP, self.BODY)
        tampered = self.BODY.replace("250", "25000")
        self.assertNotEqual(
            self._controller_signature(self.SECRET, self.TIMESTAMP, tampered), good
        )

    def test_a_replayed_timestamp_changes_the_signature(self):
        good = reference_signature(self.SECRET, self.TIMESTAMP, self.BODY)
        self.assertNotEqual(
            self._controller_signature(self.SECRET, "1730000000001", self.BODY), good
        )

    def test_a_reserialised_body_does_not_match(self):
        """Why the webhook verifies raw bytes rather than a parsed dict."""
        good = reference_signature(self.SECRET, self.TIMESTAMP, self.BODY)
        reserialised = json.dumps(json.loads(self.BODY))
        self.assertNotEqual(
            self._controller_signature(self.SECRET, self.TIMESTAMP, reserialised), good
        )
