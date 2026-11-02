"""The Chapa payment method record and its wiring to the provider.

Until this module shipped a `payment.method`, the provider declared its
default method code as `unknown`, which meant Odoo had nothing to activate:
the provider could be enabled and still not offer itself at checkout, and
transactions carried no payment method for reporting to group by.
"""

import re

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.payment_chapa import const


# post_install, like Odoo's own: at_install tests run against a half-loaded
# registry, and a later module's NOT NULL column on res.partner makes every
# partner creation fail on an incremental -u run.
@tagged("post_install", "-at_install")
class TestChapaPaymentMethod(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref("payment_chapa.payment_provider_chapa")
        cls.method = cls.env.ref("payment_chapa.payment_method_chapa")

    def test_the_method_code_matches_the_declared_default(self):
        """The code is the join between the record and the provider.

        A mismatch here is silent: nothing errors, the method simply never
        activates.
        """
        self.assertEqual(self.method.code, "chapa")
        self.assertEqual(
            self.provider._get_default_payment_method_codes(), {self.method.code}
        )

    def test_the_provider_carries_the_method(self):
        """`active_test=False` is required, not incidental.

        `payment_method_ids` is an ordinary relational field, so it hides
        inactive records the way every relational read does -- and the method
        ships inactive on purpose. Reading it without this context returns an
        empty set even when the link is present in the database.
        """
        self.assertIn(
            self.method,
            self.provider.with_context(active_test=False).payment_method_ids,
        )

    def test_the_method_ships_inactive(self):
        """It should not appear at checkout for a club that never enabled Chapa."""
        self.assertFalse(self.method.active)

    def test_enabling_the_provider_activates_the_method(self):
        """The path a club actually walks: fill credentials, then enable.

        `payment.method.write` refuses to activate a method while every
        provider supporting it is disabled, and `payment.provider.write`
        calls `_activate_default_pms` itself once a provider goes live -- so
        enabling is both necessary and sufficient, and neither half can be
        skipped in a test.
        """
        self.provider.write(
            {
                "chapa_secret_key": "CHASECK_TEST-000000000000000000000000",
                "state": "test",
            }
        )
        self.assertTrue(
            self.env.ref("payment_chapa.payment_method_chapa").active,
            "enabling the provider must make its method available",
        )

    def test_a_method_cannot_be_activated_without_a_live_provider(self):
        """Odoo refuses an orphan method, and we rely on that.

        It is why the record ships inactive: a method with no enabled provider
        behind it would offer members a way to pay that goes nowhere.
        """
        from odoo.exceptions import UserError

        with self.assertRaises(UserError):
            self.method.active = True

    def test_the_method_carries_an_image(self):
        """A blank tile at checkout reads as a broken page.

        The image is loaded from a file at install time, so a rename or a
        missing PNG fails here rather than showing the member an empty box.
        """
        self.assertTrue(self.method.image, "Chapa has no checkout image")

    def test_the_provider_carries_an_image(self):
        """`image_128` is what GET /api/v1/payment/providers hands the mobile
        app as `image_url`; without it the app shows a nameless row."""
        self.assertTrue(self.provider.image_128, "Chapa has no provider image")

    def test_the_currencies_agree_with_the_provider(self):
        """Two places declare currency; they must not drift apart."""
        method_currencies = set(self.method.supported_currency_ids.mapped("name"))
        self.assertEqual(method_currencies, set(const.SUPPORTED_CURRENCIES))

    def test_the_payer_country_is_not_restricted(self):
        """Diaspora members pay Ethiopian dues from abroad.

        `supported_country_ids` filters on the payer's country, so setting it
        to Ethiopia would hide Chapa from exactly those members.
        """
        self.assertFalse(self.method.supported_country_ids)

    def test_unsupported_features_are_declared_honestly(self):
        """A capability we cannot perform must not be advertised."""
        self.assertFalse(self.method.support_tokenization)
        self.assertFalse(self.method.support_express_checkout)
        self.assertEqual(self.method.support_manual_capture, "none")
        self.assertEqual(self.method.support_refund, "none")


@tagged("post_install", "-at_install")
class TestChapaCheckoutDefault(TransactionCase):
    def test_a_new_chapa_provider_defaults_to_direct_charge(self):
        provider = self.env["payment.provider"].create({"name": "Chapa (default)", "code": "chapa"})
        self.assertEqual(provider.chapa_checkout_mode, "direct")


@tagged("post_install", "-at_install")
class TestChapaStatusMapping(TransactionCase):
    """The gateway's vocabulary, mapped onto Odoo's four outcomes."""

    def test_every_odoo_state_is_covered(self):
        self.assertEqual(
            set(const.PAYMENT_STATUS_MAPPING),
            {"pending", "done", "cancel", "error"},
        )

    def test_no_gateway_status_maps_to_two_outcomes(self):
        """An overlap would make the outcome depend on branch order."""
        seen = []
        for statuses in const.PAYMENT_STATUS_MAPPING.values():
            seen.extend(statuses)
        self.assertEqual(
            len(seen), len(set(seen)), "a status appears under two outcomes"
        )

    def test_both_vocabularies_are_mapped(self):
        """Chapa reports `status` on verify and `event` on the webhook.

        Both reach `_apply_updates`, so both spellings of success have to be
        recognised or a paid membership silently stays unpaid.
        """
        done = const.PAYMENT_STATUS_MAPPING["done"]
        self.assertIn("success", done, "the verify endpoint's spelling")
        self.assertIn("charge.success", done, "the webhook's spelling")

    def test_a_refund_is_not_treated_as_a_payment(self):
        self.assertIn("charge.refunded", const.PAYMENT_STATUS_MAPPING["error"])


@tagged("post_install", "-at_install")
class TestChapaReference(TransactionCase):
    """Chapa's `tx_ref` rule, pinned.

    Found live: Odoo's default reference is derived from the document being
    paid -- `INV/2026/00011-1` -- and Chapa refuses it with HTTP 400 because
    of the slashes. The member saw "Payment processing failed" and nothing
    else. This is the failure that should never come back.
    """

    CHAPA_TX_REF = re.compile(r"^[A-Za-z0-9._-]+$")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref("payment_chapa.payment_provider_chapa")
        cls.method = cls.env.ref("payment_chapa.payment_method_chapa")
        cls.currency = cls.env.ref("base.ETB")
        cls.partner = cls.env["res.partner"].create(
            {"name": "Dawit Demissie", "email": "dawit@example.com"}
        )

    def _tx(self, **values):
        base = {
            "provider_id": self.provider.id,
            "payment_method_id": self.method.id,
            "amount": 50.0,
            "currency_id": self.currency.id,
            "partner_id": self.partner.id,
        }
        base.update(values)
        return self.env["payment.transaction"].create(base)

    def test_the_reference_only_uses_characters_chapa_accepts(self):
        tx = self._tx()
        self.assertRegex(tx.reference, self.CHAPA_TX_REF)

    def test_an_invoice_style_prefix_is_not_passed_through(self):
        """The document name is what Odoo would use by default; it must not
        reach Chapa."""
        tx = self._tx(reference=self.env["payment.transaction"]._compute_reference(
            "chapa", prefix="INV/2026/00011"
        ))
        self.assertNotIn("/", tx.reference)
        self.assertRegex(tx.reference, self.CHAPA_TX_REF)

    def test_two_transactions_get_distinct_references(self):
        """Uniqueness must survive the safe prefix; Chapa also requires it."""
        first, second = self._tx(), self._tx()
        self.assertNotEqual(first.reference, second.reference)


@tagged("post_install", "-at_install")
class TestChapaErrorParsing(TransactionCase):
    """Chapa's validation errors, made readable."""

    class _Response:
        def __init__(self, body, text="raw"):
            self._body = body
            self.text = text

        def json(self):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref("payment_chapa.payment_provider_chapa")

    def test_a_validation_dict_keeps_the_explanations(self):
        """Rendered naively, this dict became "email and tx_ref" -- the field
        names with every reason dropped."""
        body = {
            "message": {
                "email": ["validation.email"],
                "tx_ref": ["The tx ref may only contain letters, numbers, hyphens, underscores, and dots."],
            },
            "status": "failed",
        }
        text = self.provider._parse_response_error(self._Response(body))
        self.assertIn("tx_ref: The tx ref may only contain", text)
        self.assertIn("email: validation.email", text)

    def test_a_plain_message_passes_through(self):
        text = self.provider._parse_response_error(
            self._Response({"message": "Invalid API key", "status": "failed"})
        )
        self.assertEqual(text, "Invalid API key")

    def test_a_non_json_body_does_not_crash_the_error_path(self):
        text = self.provider._parse_response_error(
            self._Response(ValueError("not json"), text="<html>502</html>")
        )
        self.assertIn("502", text)


@tagged("post_install", "-at_install")
class TestChapaPhone(TransactionCase):
    """The phone number as the gateway wants it, not as Odoo displays it.

    Found live: Odoo handed over "+251 90 012 3456" and the gateway wanted
    "+251900123456". Spaces are formatting, not part of the number.
    """

    def test_display_spacing_is_stripped(self):
        self.assertEqual(const.bare_phone("+251 90 012 3456"), "+251900123456")

    def test_dashes_and_brackets_are_stripped(self):
        self.assertEqual(const.bare_phone("(0)911-22-33-47"), "0911223347")

    def test_a_leading_plus_survives_but_only_leading(self):
        self.assertEqual(const.bare_phone("+251911223347"), "+251911223347")
        self.assertEqual(const.bare_phone("0911 223 347"), "0911223347")

    def test_an_already_bare_number_is_unchanged(self):
        self.assertEqual(const.bare_phone("+251900123456"), "+251900123456")

    def test_empty_stays_empty(self):
        """A missing phone must stay falsy so the caller's own check fires."""
        self.assertEqual(const.bare_phone(False), "")
        self.assertEqual(const.bare_phone(""), "")
