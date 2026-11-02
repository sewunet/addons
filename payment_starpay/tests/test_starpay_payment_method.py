"""The Star-Pay payment method record and its wiring to the provider.

Until this module shipped a `payment.method`, the provider declared its
default method code as `unknown`, which meant Odoo had nothing to activate:
the provider could be enabled and still not offer itself at checkout, and
transactions carried no payment method for reporting to group by.
"""

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.payment_starpay import const


# post_install, like Odoo's own: at_install tests run against a half-loaded
# registry, and a later module's NOT NULL column on res.partner makes every
# partner creation fail on an incremental -u run.
@tagged("post_install", "-at_install")
class TestStarpayPaymentMethod(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref("payment_starpay.payment_provider_starpay")
        cls.method = cls.env.ref("payment_starpay.payment_method_starpay")

    def test_the_method_code_matches_the_declared_default(self):
        """The code is the join between the record and the provider.

        A mismatch here is silent: nothing errors, the method simply never
        activates.
        """
        self.assertEqual(self.method.code, "starpay")
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
                "starpay_base_url": "https://sandbox-api.starpayethiopia.com/v1/starpay-api",
                "starpay_api_secret": "test-api-secret",
                "starpay_webhook_secret": "test-webhook-secret",
                "state": "test",
            }
        )
        self.assertTrue(
            self.env.ref("payment_starpay.payment_method_starpay").active,
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
        self.assertTrue(self.method.image, "Star-Pay has no checkout image")

    def test_the_provider_carries_an_image(self):
        """`image_128` is what GET /api/v1/payment/providers hands the mobile
        app as `image_url`; without it the app shows a nameless row."""
        self.assertTrue(self.provider.image_128, "Star-Pay has no provider image")

    def test_the_currencies_agree_with_the_provider(self):
        """Two places declare currency; they must not drift apart."""
        method_currencies = set(self.method.supported_currency_ids.mapped("name"))
        self.assertEqual(method_currencies, set(const.SUPPORTED_CURRENCIES))

    def test_birr_only(self):
        self.assertEqual(
            self.method.supported_currency_ids.mapped("name"),
            ["ETB"],
            "a Star-Pay wallet holds birr and nothing else",
        )

    def test_unsupported_features_are_declared_honestly(self):
        self.assertFalse(self.method.support_tokenization)
        self.assertFalse(self.method.support_express_checkout)
        self.assertEqual(self.method.support_manual_capture, "none")
        self.assertEqual(self.method.support_refund, "none")


@tagged("post_install", "-at_install")
class TestStarpayStatusMapping(TransactionCase):
    def test_every_odoo_state_is_covered(self):
        self.assertEqual(
            set(const.PAYMENT_STATUS_MAPPING),
            {"pending", "done", "cancel", "error"},
        )

    def test_no_gateway_status_maps_to_two_outcomes(self):
        seen = []
        for statuses in const.PAYMENT_STATUS_MAPPING.values():
            seen.extend(statuses)
        self.assertEqual(
            len(seen), len(set(seen)), "a status appears under two outcomes"
        )

    def test_only_paid_settles_the_fee(self):
        """PROCESSING is not PAID.

        Treating an in-flight payment as settled would grant a membership the
        club has not been paid for.
        """
        self.assertEqual(const.PAYMENT_STATUS_MAPPING["done"], ["PAID"])
        self.assertIn("PROCESSING", const.PAYMENT_STATUS_MAPPING["pending"])

    def test_an_expired_session_is_a_cancellation_not_a_failure(self):
        """A member who walked away has not had a payment fail."""
        self.assertIn("EXPIRED", const.PAYMENT_STATUS_MAPPING["cancel"])


@tagged("post_install", "-at_install")
class TestStarpayPhone(TransactionCase):
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
