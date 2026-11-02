"""Chapa webhook signature verification.

Mirrors the JavaScript in Chapa's own webhook documentation, independently, so
a regression in the controller shows up as a failing test rather than as
accepted forged callbacks.
"""

import hashlib
import hmac
import json

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


def reference_signature(secret, body):
    """crypto.createHmac('sha256', secret).update(body).digest('hex')."""
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


# post_install, like Odoo's own: at_install tests run against a half-loaded
# registry, and a later module's NOT NULL column on res.partner makes every
# partner creation fail on an incremental -u run.
@tagged("post_install", "-at_install")
class TestChapaSignature(TransactionCase):
    SECRET = "chapa_webhook_secret"
    BODY = '{"event":"charge.success","tx_ref":"MEM-0001","amount":"500","currency":"ETB"}'

    def _controller_signature(self, secret, body):
        """The exact computation the webhook controller performs."""
        return hmac.new(
            secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def test_matches_the_documented_implementation(self):
        self.assertEqual(
            self._controller_signature(self.SECRET, self.BODY),
            reference_signature(self.SECRET, self.BODY),
        )

    def test_a_tampered_amount_changes_the_signature(self):
        good = reference_signature(self.SECRET, self.BODY)
        tampered = self.BODY.replace('"500"', '"50000"')
        self.assertNotEqual(self._controller_signature(self.SECRET, tampered), good)

    def test_a_wrong_secret_changes_the_signature(self):
        good = reference_signature(self.SECRET, self.BODY)
        self.assertNotEqual(self._controller_signature("other", self.BODY), good)

    def test_a_reserialised_body_does_not_match(self):
        """Why the webhook verifies raw bytes rather than a parsed dict."""
        good = reference_signature(self.SECRET, self.BODY)
        reserialised = json.dumps(json.loads(self.BODY))
        self.assertNotEqual(self._controller_signature(self.SECRET, reserialised), good)

    def test_the_constant_header_is_not_what_we_verify(self):
        """`chapa-signature` is an HMAC of the secret with itself.

        It is the same value for every request, so it cannot detect a tampered
        body. This pins the reason the controller ignores it.
        """
        constant_one = reference_signature(self.SECRET, self.SECRET)
        constant_two = reference_signature(self.SECRET, self.SECRET)
        self.assertEqual(constant_one, constant_two)
        self.assertNotEqual(constant_one, reference_signature(self.SECRET, self.BODY))
