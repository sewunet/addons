"""The telebirr payment method record and its wiring to the provider.

Until this module shipped a `payment.method`, the provider declared its
default method code as `unknown`, which meant Odoo had nothing to activate:
the provider could be enabled and still not offer itself at checkout, and
transactions carried no payment method for reporting to group by.
"""

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.payment_telebirr import const


# post_install, like Odoo's own: at_install tests run against a half-loaded
# registry, and a later module's NOT NULL column on res.partner makes every
# partner creation fail on an incremental -u run.
@tagged("post_install", "-at_install")
class TestTelebirrPaymentMethod(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref("payment_telebirr.payment_provider_telebirr")
        cls.method = cls.env.ref("payment_telebirr.payment_method_telebirr")

    def test_the_method_code_matches_the_declared_default(self):
        """The code is the join between the record and the provider.

        A mismatch here is silent: nothing errors, the method simply never
        activates.
        """
        self.assertEqual(self.method.code, "telebirr")
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
                "telebirr_base_url": "https://example.invalid/gateway",
                "telebirr_h5_url": "https://example.invalid/h5",
                "telebirr_fabric_app_id": "test-fabric-app-id",
                "telebirr_app_secret": "test-app-secret",
                "telebirr_merchant_app_id": "test-merchant-app-id",
                "telebirr_short_code": "000000",
                # No PEM parsing happens on write, so a marker string is
                # enough here and keeps a fake key out of the repository.
                "telebirr_private_key": "test-merchant-private-key",
                "state": "test",
            }
        )
        self.assertTrue(
            self.env.ref("payment_telebirr.payment_method_telebirr").active,
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
        self.assertTrue(self.method.image, "telebirr has no checkout image")

    def test_the_provider_carries_an_image(self):
        """`image_128` is what GET /api/v1/payment/providers hands the mobile
        app as `image_url`; without it the app shows a nameless row."""
        self.assertTrue(self.provider.image_128, "telebirr has no provider image")

    def test_the_currencies_agree_with_the_provider(self):
        """Two places declare currency; they must not drift apart."""
        method_currencies = set(self.method.supported_currency_ids.mapped("name"))
        self.assertEqual(method_currencies, set(const.SUPPORTED_CURRENCIES))

    def test_birr_only(self):
        self.assertEqual(
            self.method.supported_currency_ids.mapped("name"),
            ["ETB"],
            "a telebirr wallet holds birr and nothing else",
        )

    def test_refunds_are_not_advertised(self):
        """The merchant pack documents refunds; this module has not verified
        its request signing against the live gateway, so offering a Refund
        button would put a failing action in front of accounting staff."""
        self.assertEqual(self.method.support_refund, "none")

    def test_unsupported_features_are_declared_honestly(self):
        self.assertFalse(self.method.support_tokenization)
        self.assertFalse(self.method.support_express_checkout)
        self.assertEqual(self.method.support_manual_capture, "none")


@tagged("post_install", "-at_install")
class TestTelebirrStatusMapping(TransactionCase):
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

    def test_both_casings_are_accepted(self):
        """telebirr's documentation and its live responses disagree on case.

        The mapping carries both rather than lower-casing at the call site,
        because `Paying` and `WAIT_PAY` are different words, not different
        spellings.
        """
        done = const.PAYMENT_STATUS_MAPPING["done"]
        self.assertIn("Completed", done)
        self.assertIn("COMPLETED", done)

    def test_paying_is_not_paid(self):
        """`Paying` means the member is still on the wallet screen."""
        self.assertIn("Paying", const.PAYMENT_STATUS_MAPPING["pending"])
        self.assertNotIn("Paying", const.PAYMENT_STATUS_MAPPING["done"])
