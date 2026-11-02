"""Direct charge: the member pays on our page and approves on the phone.

Chapa is never called for real. `_send_api_request` on the provider is
patched with the response shapes from Chapa's Direct Charge documentation,
and the assertions are about what this module sends and what it does with
the answer.
"""

import json
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.payment_chapa import const
from odoo.addons.payment_chapa.models import chapa_crypto

from .test_chapa_checkout_and_verify import ChapaCase

USSD_STARTED = {
    "auth_type": "ussd",
    "requestID": "66dPW486w0z6uibrcraZ2diYztK2lx2WaslwGnS18UBXTctDxRdAudYtq3jJtMu7CV6gzyCpBSfrm9kKFJBsA8Wq7zKvk0UxL",
    "meta": {
        "message": "Payment successfully initiated with telebirr",
        "status": "success",
        "ref_id": "CH3mhMQVhsHm2",
        "payment_status": "PENDING",
    },
    "mode": "test",
}
OTP_STARTED = {**USSD_STARTED, "auth_type": "otp", "meta": {**USSD_STARTED["meta"], "ref_id": "CHcuKjgnN0Dk0"}}
PORTAL_STARTED = {
    **USSD_STARTED,
    "auth_type": "portal_view",
    "meta": {**USSD_STARTED["meta"], "portal": "https://bank.example/pay/abc", "ref_id": "CHIao4sITVmom"},
}
VERIFIED_PAID = {"tx_ref": None, "status": "success", "amount": 50.0, "currency": "ETB", "reference": "APxxx"}
VERIFIED_PENDING = {"tx_ref": None, "status": "pending", "amount": 50.0, "currency": "ETB"}
KEY = "0123456789abcdef01234567"  # 24 bytes, like the dashboard key


@tagged("post_install", "-at_install")
class TestDirectChargeConfig(ChapaCase):
    def test_direct_is_the_default_and_builds_the_inline_form(self):
        provider = self.env["payment.provider"].create({"name": "Chapa (new)", "code": "chapa"})
        self.assertEqual(provider.chapa_checkout_mode, "direct")
        self.assertEqual(provider.chapa_direct_methods, const.DEFAULT_DIRECT_METHODS)
        self.assertTrue(provider._should_build_inline_form())
        provider.chapa_checkout_mode = "html"
        self.assertFalse(provider._should_build_inline_form())

    def test_the_wallets_are_offered_in_the_configured_order(self):
        self.provider.write({"chapa_checkout_mode": "direct", "chapa_direct_methods": "mpesa, telebirr"})
        self.assertEqual(self.provider._chapa_direct_methods(), [("mpesa", "M-Pesa"), ("telebirr", "telebirr")])

    def test_an_unknown_wallet_code_is_refused(self):
        with self.assertRaises(ValidationError):
            self.provider.write({"chapa_checkout_mode": "direct", "chapa_direct_methods": "telebirr,paypal"})

    def test_awash_birr_needs_the_encryption_key(self):
        self.provider.write({"chapa_checkout_mode": "direct", "chapa_encryption_key": False})
        with self.assertRaises(ValidationError):
            self.provider.chapa_direct_methods = "telebirr,awashbirr"
        self.provider.write({"chapa_encryption_key": KEY, "chapa_direct_methods": "telebirr,awashbirr"})

    def test_processing_values_carry_what_the_page_needs(self):
        self.provider.chapa_checkout_mode = "direct"
        tx = self._tx(operation="online_direct")
        values = tx._get_processing_values()
        self.assertTrue(values["chapa_direct"])
        self.assertTrue(tx._chapa_check_token(values["chapa_token"]))
        self.assertFalse(tx._chapa_check_token("nope"))
        self.assertEqual([c for c, _l in values["chapa_methods"]], ["telebirr", "cbebirr", "mpesa", "ebirr"])
        self.assertTrue(values["chapa_test_mode"])


@tagged("post_install", "-at_install")
class TestDirectChargePhones(ChapaCase):
    def test_every_way_members_type_a_number(self):
        for typed in ("+251 90 012 3456", "0900123456", "900123456", "251900123456", "0900 12 34 56"):
            self.assertEqual(const.local_mobile(typed), "0900123456", typed)
        self.assertEqual(const.local_mobile("+251700123456"), "0700123456")

    def test_what_is_not_an_ethiopian_mobile(self):
        for typed in ("", "12345", "+44 20 7946 0958", "0800123456", "091123456789"):
            self.assertEqual(const.local_mobile(typed), "", typed)


@tagged("post_install", "-at_install")
class TestDirectChargeCrypto(ChapaCase):
    def test_the_client_blob_round_trips(self):
        payload = {"requestID": "66dPW486", "otp": 12345}
        blob = chapa_crypto.encrypt_client(KEY, payload)
        self.assertEqual(chapa_crypto.decrypt_client(KEY, blob), payload)
        # 3DES/ECB with PKCS#7: whole 8-byte blocks, base64.
        import base64
        self.assertEqual(len(base64.b64decode(blob)) % 8, 0)

    def test_a_wrong_sized_key_is_refused_before_anything_is_sent(self):
        with self.assertRaises(ValueError):
            chapa_crypto.encrypt_client("short", {"otp": 1})


@tagged("post_install", "-at_install")
class TestDirectChargeFlow(ChapaCase):
    def setUp(self):
        super().setUp()
        self.provider.write(
            {"chapa_checkout_mode": "direct", "chapa_direct_methods": "telebirr,awashbirr,enat_bank",
             "chapa_encryption_key": KEY}
        )
        self.Provider = type(self.env["payment.provider"])

    def test_a_ussd_charge_sends_the_local_number_and_waits_for_the_phone(self):
        tx = self._tx(operation="online_direct")
        with patch.object(self.Provider, "_send_api_request", return_value=USSD_STARTED) as sent:
            result = tx._chapa_direct_charge("telebirr", "+251 90 012 3456")
        sent.assert_called_once()
        args, kwargs = sent.call_args
        self.assertEqual(args[:2], ("POST", const.CHARGE_PATH))
        self.assertEqual(kwargs["params"], {"type": "telebirr"})
        self.assertEqual(kwargs["data"]["mobile"], "0900123456")
        self.assertEqual(kwargs["data"]["tx_ref"], tx.reference)
        self.assertEqual(kwargs["data"]["currency"], "ETB")
        self.assertEqual(result["auth_type"], "ussd")
        self.assertIn("3456", result["message"])
        self.assertEqual(tx.state, "pending")
        self.assertEqual(tx.chapa_direct_method, "telebirr")
        self.assertEqual(tx.provider_reference, "CH3mhMQVhsHm2")
        self.assertEqual(tx.chapa_request_id, USSD_STARTED["requestID"])

    def test_nothing_is_sent_for_a_bad_wallet_or_number(self):
        tx = self._tx(operation="online_direct")
        with patch.object(self.Provider, "_send_api_request") as sent:
            with self.assertRaises(ValidationError):
                tx._chapa_direct_charge("mpesa", "0900123456")  # not offered by this provider
            with self.assertRaises(ValidationError):
                tx._chapa_direct_charge("telebirr", "12345")
        sent.assert_not_called()
        self.assertEqual(tx.state, "draft")

    def test_a_portal_wallet_hands_back_the_portal_url(self):
        tx = self._tx(operation="online_direct")
        with patch.object(self.Provider, "_send_api_request", return_value=PORTAL_STARTED):
            result = tx._chapa_direct_charge("enat_bank", "0900123456")
        self.assertEqual(result["auth_type"], "portal_view")
        self.assertEqual(result["portal_url"], "https://bank.example/pay/abc")

    def test_an_otp_wallet_sends_the_code_encrypted_then_verifies(self):
        tx = self._tx(operation="online_direct")
        with patch.object(self.Provider, "_send_api_request", return_value=OTP_STARTED):
            started = tx._chapa_direct_charge("awashbirr", "0900123456")
        self.assertEqual(started["auth_type"], "otp")

        calls = []

        def fake_send(provider, method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            if endpoint == const.VALIDATE_PATH:
                return {"message": "Payment is completed", "trx_ref": "CHS7WFpXdCMR0", "processor_id": None}
            return {**VERIFIED_PAID, "tx_ref": tx.reference}

        with patch.object(self.Provider, "_send_api_request", fake_send):
            result = tx._chapa_direct_authorize(" 12345 ")
        validate = next(c for c in calls if c[1] == const.VALIDATE_PATH)
        self.assertEqual(validate[2]["params"], {"type": "awashbirr"})
        self.assertTrue(validate[2]["raw"])
        self.assertEqual(validate[2]["data"]["reference"], "CHcuKjgnN0Dk0")
        self.assertEqual(
            chapa_crypto.decrypt_client(KEY, validate[2]["data"]["client"]),
            {"requestID": OTP_STARTED["requestID"], "otp": 12345},
        )
        self.assertEqual(result["state"], "done")
        self.assertEqual(tx.state, "done")

    def test_a_refused_code_is_reported_not_swallowed(self):
        tx = self._tx(operation="online_direct")
        with patch.object(self.Provider, "_send_api_request", return_value=OTP_STARTED):
            tx._chapa_direct_charge("awashbirr", "0900123456")
        refused = {"message": "Invalid client data or Transaction is nowhere to be found.", "status": "failed", "data": None}
        with patch.object(self.Provider, "_send_api_request", return_value=refused):
            with self.assertRaises(ValidationError):
                tx._chapa_direct_authorize("00000")
        self.assertEqual(tx.state, "pending")

    def test_polling_settles_when_chapa_says_paid(self):
        tx = self._tx(operation="online_direct")
        with patch.object(self.Provider, "_send_api_request", return_value=USSD_STARTED):
            tx._chapa_direct_charge("telebirr", "0900123456")
        with patch.object(self.Provider, "_send_api_request", return_value={**VERIFIED_PENDING, "tx_ref": tx.reference}):
            self.assertEqual(tx._chapa_direct_status()["state"], "pending")
        with patch.object(self.Provider, "_send_api_request", return_value={**VERIFIED_PAID, "tx_ref": tx.reference}):
            self.assertEqual(tx._chapa_direct_status()["state"], "done")
        self.assertEqual(tx.state, "done")

    def test_a_verify_chapa_cannot_answer_yet_is_still_pending(self):
        """Right after the charge Chapa may not know the tx_ref; that is not
        a failure to show the member, just "not yet"."""
        tx = self._tx(operation="online_direct")
        with patch.object(self.Provider, "_send_api_request", return_value=USSD_STARTED):
            tx._chapa_direct_charge("telebirr", "0900123456")
        with patch.object(self.Provider, "_send_api_request", side_effect=ValidationError("not found")):
            self.assertEqual(tx._chapa_direct_status()["state"], "pending")


@tagged("post_install", "-at_install")
class TestDirectChargeRoutes(HttpCase):
    """The JSON routes the page calls, and the token that guards them."""

    def setUp(self):
        super().setUp()
        self.provider = self.env.ref("payment_chapa.payment_provider_chapa")
        self.provider.write(
            {"chapa_secret_key": "CHASECK_TEST-000000000000000000000000", "state": "test",
             "chapa_checkout_mode": "direct", "chapa_direct_methods": "telebirr"}
        )
        partner = self.env["res.partner"].create({"name": "Route Payer", "phone": "0900123456"})
        self.tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.provider.id,
                "payment_method_id": self.env.ref("payment_chapa.payment_method_chapa").id,
                "amount": 50.0,
                "currency_id": self.env.ref("base.ETB").id,
                "partner_id": partner.id,
                "operation": "online_direct",
            }
        )
        self.Provider = type(self.env["payment.provider"])

    def _call(self, url, **params):
        response = self.url_open(
            url,
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": params}),
            headers={"Content-Type": "application/json"},
        )
        return response.json()

    def test_a_bare_reference_opens_nothing(self):
        with patch.object(self.Provider, "_send_api_request", return_value=USSD_STARTED) as sent:
            body = self._call(
                "/payment/chapa/charge", reference=self.tx.reference, token="forged", type="telebirr", mobile="0900123456"
            )
        self.assertIn("error", body)
        sent.assert_not_called()

    def test_the_page_can_start_and_poll_a_charge(self):
        token = self.tx._chapa_token()
        with patch.object(self.Provider, "_send_api_request", return_value=USSD_STARTED):
            body = self._call(
                "/payment/chapa/charge", reference=self.tx.reference, token=token, type="telebirr", mobile="0900123456"
            )
        self.assertEqual(body["result"]["auth_type"], "ussd")
        with patch.object(self.Provider, "_send_api_request", return_value={**VERIFIED_PAID, "tx_ref": self.tx.reference}):
            body = self._call("/payment/chapa/poll", reference=self.tx.reference, token=token)
        self.assertEqual(body["result"]["state"], "done")
