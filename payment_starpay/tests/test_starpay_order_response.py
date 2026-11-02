"""Reading what Star-Pay actually answers.

Star-Pay's documentation calls an order `billRefNo` and its checkout
`paymentUrl`; the sandbox answers with `order_id` and `payment_url`. Code
written from the documentation failed every real order with "did not return a
checkout session", on the website and in the app alike.
"""

from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.payment_starpay import const

# The `data` of a real sandbox answer to trdp/order, 2026-09-25.
SANDBOX_ORDER = {
    "order_id": "6UE7B4M00P5",
    "status": "UNPAID",
    "amount": "200",
    "currency": "WALLET_CURRENCY_ETB",
    "payment_url": "https://sandbox-checkout.starpayethiopia.com/en/pay/d/6UE7B4M00P5",
    "redirectUrl": "https://stgeorgesa.act.com.et/payment/starpay/return",
    "expires_at": "2026-09-26T10:33:06.648Z",
}


@tagged("post_install", "-at_install")
class TestStarpayOrderResponse(TransactionCase):
    def test_the_readers_take_the_sandbox_names_and_the_documented_ones(self):
        self.assertEqual(const.order_id(SANDBOX_ORDER), "6UE7B4M00P5")
        self.assertEqual(const.checkout_url(SANDBOX_ORDER), SANDBOX_ORDER["payment_url"])
        self.assertEqual(const.order_id({"billRefNo": "33WJ8946WB"}), "33WJ8946WB")
        self.assertEqual(const.checkout_url({"paymentUrl": "https://x/pay"}), "https://x/pay")
        self.assertEqual(const.order_id(None), "")

    def test_a_wallet_currency_is_read_as_the_currency_it_names(self):
        # Compared with the transaction's currency by the amount check: left
        # as it arrives, it would refuse a genuine payment.
        self.assertEqual(const.currency_code("WALLET_CURRENCY_ETB"), "ETB")
        self.assertEqual(const.currency_code("ETB"), "ETB")

    def test_the_secret_goes_in_the_header_and_never_in_the_body(self):
        provider = self.env.ref("payment_starpay.payment_provider_starpay")
        provider.starpay_api_secret = "  sk_sandbox_123\n"
        self.assertEqual(
            provider._build_request_headers("POST", "trdp/order"),
            {"x-api-secret": "sk_sandbox_123"},
        )
        tx = self._transaction(provider)
        with patch.object(type(provider), "_send_api_request", return_value=SANDBOX_ORDER) as sent:
            tx._get_specific_rendering_values({})
        self.assertEqual(sent.call_args.args[:2], ("POST", "trdp/order"))
        self.assertNotIn("sk_sandbox_123", str(sent.call_args.kwargs["json"]))

    def _transaction(self, provider):
        partner = self.env["res.partner"].create({"name": "Star Payer", "phone": "0911223347"})
        return self.env["payment.transaction"].create({
            "provider_id": provider.id,
            "payment_method_id": self.env.ref("payment_starpay.payment_method_starpay").id,
            "amount": 200.0,
            "currency_id": self.env.ref("base.ETB").id,
            "partner_id": partner.id,
            "reference": f"MEM/TEST/{self.env['payment.transaction'].search_count([]) + 1:06d}",
        })

    def test_the_website_redirects_to_the_payment_url_and_keeps_the_order_id(self):
        provider = self.env.ref("payment_starpay.payment_provider_starpay")
        tx = self._transaction(provider)
        with patch.object(type(provider), "_send_api_request", return_value=SANDBOX_ORDER):
            values = tx._get_specific_rendering_values({})
        self.assertEqual(values["api_url"], SANDBOX_ORDER["payment_url"])
        self.assertEqual(tx.starpay_checkout_url, SANDBOX_ORDER["payment_url"])
        self.assertEqual(tx.provider_reference, "6UE7B4M00P5")
        self.assertNotEqual(tx.state, "error")


@tagged("post_install", "-at_install")
class TestStarpayRedirectUrl(TransactionCase):
    """The redirect URL is a field: generated once, then whatever is typed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref("payment_starpay.payment_provider_starpay")

    def _order_payload(self):
        partner = self.env["res.partner"].create({"name": "Star Payer", "phone": "0911223347"})
        tx = self.env["payment.transaction"].create({
            "provider_id": self.provider.id,
            "payment_method_id": self.env.ref("payment_starpay.payment_method_starpay").id,
            "amount": 200.0,
            "currency_id": self.env.ref("base.ETB").id,
            "partner_id": partner.id,
            "reference": f"MEM/REDIR/{self.env['payment.transaction'].search_count([]) + 1:06d}",
        })
        with patch.object(type(self.provider), "_send_api_request", return_value=SANDBOX_ORDER) as sent:
            tx._get_specific_rendering_values({})
        return sent.call_args.kwargs["json"]

    def test_it_starts_as_the_generated_return_page(self):
        expected = self.provider.get_base_url().rstrip("/") + "/payment/starpay/return"
        self.assertEqual(self.provider.starpay_redirect_url, expected)
        self.assertTrue(self._order_payload()["redirectUrl"].startswith(expected + "?ref="))

    def test_an_address_typed_in_is_what_the_order_sends(self):
        self.provider.starpay_redirect_url = "https://stgeorgesa.act.com.et/payment/starpay/return"
        self.assertTrue(
            self._order_payload()["redirectUrl"].startswith(
                "https://stgeorgesa.act.com.et/payment/starpay/return?ref="
            )
        )

    def test_the_return_address_carries_the_club_reference(self):
        # Star-Pay adds nothing to the return; the reference is how the return
        # page finds the payment. Encoded: references contain slashes.
        payload = self._order_payload()
        tx = self.env["payment.transaction"].search([], order="id desc", limit=1)
        self.assertTrue(payload["redirectUrl"].endswith("?ref=" + tx.reference.replace("/", "%2F")))

    def test_a_redirect_url_that_already_has_a_query_keeps_it(self):
        self.provider.starpay_redirect_url = "https://stgeorgesa.act.com.et/payment/starpay/return?lang=am"
        self.assertIn("/return?lang=am&ref=", self._order_payload()["redirectUrl"])

    def test_an_emptied_field_falls_back_to_the_generated_address(self):
        self.provider.starpay_redirect_url = False
        self.assertTrue(
            self._order_payload()["redirectUrl"].startswith(
                self.provider._starpay_default_redirect_url() + "?ref="
            )
        )

    def test_the_callback_url_starts_as_the_generated_webhook(self):
        expected = self.provider.get_base_url().rstrip("/") + "/payment/starpay/webhook"
        self.assertEqual(self.provider.starpay_callback_url, expected)
        self.assertEqual(self._order_payload()["callbackURL"], expected)

    def test_a_callback_url_typed_in_is_what_the_order_sends(self):
        # The case that prompted it: a base URL of http://172.16.0.241, which
        # Star-Pay cannot call from the internet.
        self.provider.starpay_callback_url = "https://stgeorgesa.act.com.et/payment/starpay/webhook"
        payload = self._order_payload()
        self.assertEqual(payload["callbackURL"], "https://stgeorgesa.act.com.et/payment/starpay/webhook")

    def test_an_emptied_callback_url_falls_back_to_the_generated_one(self):
        self.provider.starpay_callback_url = False
        self.assertEqual(self._order_payload()["callbackURL"], self.provider._starpay_default_callback_url())

    def test_the_callback_url_must_be_a_full_address(self):
        with self.assertRaises(ValidationError):
            self.provider.starpay_callback_url = "stgeorgesa.act.com.et/payment/starpay/webhook"
            self.provider.flush_recordset()

    def test_it_must_be_a_full_address(self):
        with self.assertRaises(ValidationError):
            self.provider.starpay_redirect_url = "stgeorgesa.act.com.et/payment/starpay/return"
            self.provider.flush_recordset()
