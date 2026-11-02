"""The Star-Pay request log: what it keeps, and how it judges an exchange.

Only the network is replaced here -- `requests.request` inside Odoo's own
`_send_api_request` -- so the framework's `_log_request` / `_log_response`
hooks and this module's overrides of them all run as they do in production.
Until these tests the log had never been exercised: the payment tests replace
`_send_api_request` whole, which skips it.
"""

import json
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase

REQUEST = "odoo.addons.payment.models.payment_provider.requests.request"
SANDBOX_ORDER = {
    "status": "success",
    "message": "Transaction created successfully",
    "data": {
        "order_id": "6UE7B4M00P5",
        "status": "UNPAID",
        "payment_url": "https://sandbox-checkout.starpayethiopia.com/en/pay/d/6UE7B4M00P5",
    },
}


def _response(status, body, url="https://sandbox-api.starpayethiopia.com/v1/starpay-api/trdp/order"):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode() if not isinstance(body, bytes) else body
    response.url = url
    response.reason = "OK" if status < 400 else "Bad Request"
    response.request = requests.Request("POST", url).prepare()
    return response


@tagged("post_install", "-at_install")
class TestStarpayLogOutbound(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref("payment_starpay.payment_provider_starpay")
        cls.provider.starpay_api_secret = "sk_log_test_secret"
        cls.Log = cls.env["starpay.request.log"]

    def _send(self, response, reference="MEM/LOG/000001"):
        with patch(REQUEST, return_value=response):
            return self.provider._send_api_request(
                "POST", "trdp/order", json={"amount": 200.0, "description": reference},
                reference=reference,
            )

    def _last(self):
        return self.Log.search([], limit=1)

    def _refused(self, call):
        """Run a call that must raise, without Odoo's assertRaises.

        Odoo's assertRaises rolls the block back in a savepoint. Under test the
        log writes on the test's own cursor, so that rollback would take the
        entry with it -- the very rollback production escapes by writing on a
        cursor of its own.
        """
        try:
            call()
        except ValidationError:
            return
        self.fail("Star-Pay's refusal should have raised a ValidationError")

    def test_an_accepted_order_is_one_ok_entry_with_both_references(self):
        before = self.Log.search_count([])
        self._send(_response(200, SANDBOX_ORDER))
        self.assertEqual(self.Log.search_count([]), before + 1, "one exchange, one row")
        entry = self._last()
        self.assertEqual(entry.direction, "out")
        self.assertEqual(entry.endpoint, "trdp/order")
        self.assertEqual(entry.method, "POST")
        self.assertEqual(entry.reference, "MEM/LOG/000001")
        self.assertEqual(entry.provider_reference, "6UE7B4M00P5")
        self.assertEqual(entry.http_status, 200)
        self.assertEqual(entry.state, "ok")
        self.assertIn('"amount": 200.0', entry.request_body, "bodies are kept, pretty-printed")
        self.assertIn("6UE7B4M00P5", entry.response_body)
        self.assertGreaterEqual(entry.duration_ms, 0)

    def test_the_api_secret_never_reaches_the_log(self):
        self._send(_response(200, SANDBOX_ORDER))
        entry = self._last()
        for text in (entry.request_body, entry.response_body, entry.url):
            self.assertNotIn("sk_log_test_secret", text or "")

    def test_a_refusal_with_an_error_object_is_failed_with_its_code(self):
        body = {"status": "error", "error": {"code": "GEN_004", "message": "Something went wrong"}}
        self._refused(lambda: self._send(_response(400, body)))
        entry = self._last()
        self.assertEqual(entry.state, "failed")
        self.assertEqual(entry.error_code, "GEN_004")
        self.assertEqual(entry.error_message, "Something went wrong")
        self.assertEqual(entry.http_status, 400)

    def test_a_refusal_inside_an_http_200_is_still_failed(self):
        # By status code alone this is fine; the envelope says otherwise.
        self._send(_response(200, {"status": "error", "code": "GEN_019", "message": "items.0.productId Required"}))
        entry = self._last()
        self.assertEqual(entry.state, "failed")
        self.assertEqual(entry.error_code, "GEN_019")
        self.assertEqual(entry.error_message, "items.0.productId Required")

    def test_a_call_that_never_got_an_answer_is_still_logged(self):
        before = self.Log.search_count([])
        with patch(REQUEST, side_effect=requests.exceptions.ConnectionError("down")):
            self._refused(lambda: self.provider._send_api_request(
                "POST", "trdp/verify", json={"orderId": "X"}, reference="MEM/LOG/2"
            ))
        self.assertEqual(self.Log.search_count([]), before + 1)
        entry = self._last()
        self.assertEqual(entry.state, "failed")
        self.assertEqual(entry.endpoint, "trdp/verify")
        self.assertEqual(entry.reference, "MEM/LOG/2")
        self.assertFalse(entry.http_status)
        self.assertTrue(entry.error_message)

    def test_a_body_that_is_not_json_is_kept_as_text(self):
        self._refused(lambda: self._send(_response(502, b"<html>Bad Gateway</html>")))
        entry = self._last()
        self.assertEqual(entry.state, "failed")
        self.assertIn("Bad Gateway", entry.response_body)

    def test_the_provider_button_opens_only_its_own_entries(self):
        self._send(_response(200, SANDBOX_ORDER))
        action = self.provider.action_view_starpay_log()
        self.assertEqual(action["res_model"], "starpay.request.log")
        self.assertEqual(action["domain"], [("provider_id", "=", self.provider.id)])


@tagged("post_install", "-at_install")
class TestStarpayLogRetention(TransactionCase):
    def _entry(self, days_old):
        entry = self.env["starpay.request.log"].create({"endpoint": "trdp/order"})
        self.env.cr.execute(
            "UPDATE starpay_request_log SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=days_old), entry.id),
        )
        return entry

    def test_the_cleanup_keeps_recent_entries_and_drops_old_ones(self):
        old, recent = self._entry(60), self._entry(3)
        self.env["starpay.request.log"]._gc()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_a_mistyped_retention_setting_does_not_stop_the_cleanup(self):
        self.env["ir.config_parameter"].sudo().set_param("payment_starpay.log_retention_days", "forty")
        old = self._entry(60)
        self.env["starpay.request.log"]._gc()  # the default applies; no exception
        self.assertFalse(old.exists())


@tagged("post_install", "-at_install")
class TestStarpayLogInbound(HttpCase):
    def _post(self, body, headers=None):
        return self.url_open(
            "/payment/starpay/webhook", data=json.dumps(body),
            headers={"Content-Type": "application/json", **(headers or {})},
        )

    def test_a_callback_for_no_known_transaction_is_logged_as_failed(self):
        before = self.env["starpay.request.log"].search_count([])
        response = self._post({"order_id": "NOPE-1", "status": "PAID", "metadata": {"reference": "NO/SUCH/1"}})
        self.assertEqual(response.status_code, 200)
        entry = self.env["starpay.request.log"].search([], limit=1)
        self.assertEqual(self.env["starpay.request.log"].search_count([]), before + 1)
        self.assertEqual(entry.direction, "in")
        self.assertEqual(entry.endpoint, "starpay/webhook")
        self.assertEqual(entry.reference, "NO/SUCH/1")
        self.assertEqual(entry.provider_reference, "NOPE-1")
        self.assertEqual(entry.state, "failed")
        self.assertIn("No transaction", entry.error_message)

    def test_a_callback_without_a_valid_signature_is_logged_and_verified(self):
        provider = self.env.ref("payment_starpay.payment_provider_starpay")
        partner = self.env["res.partner"].create({"name": "Star Payer", "phone": "0911223347"})
        self.env["payment.transaction"].create({
            "provider_id": provider.id,
            "payment_method_id": self.env.ref("payment_starpay.payment_method_starpay").id,
            "amount": 200.0,
            "currency_id": self.env.ref("base.ETB").id,
            "partner_id": partner.id,
            "reference": "MEM/SIG/000001",
            "provider_reference": "SIG-1",
        })
        with patch.object(type(provider), "_send_api_request", return_value={"order_id": "SIG-1", "status": "UNPAID"}) as verify:
            response = self._post(
                {"order_id": "SIG-1", "status": "PAID", "metadata": {"reference": "MEM/SIG/000001"}},
                headers={"X-Timestamp": "1", "X-Signature": "not-the-signature"},
            )
        # Not refused: logged as a failed signature, and Star-Pay was asked.
        self.assertEqual(response.status_code, 200)
        verify.assert_called_once()
        entry = self.env["starpay.request.log"].search([("endpoint", "=", "starpay/webhook")], limit=1)
        self.assertEqual(entry.state, "failed")
        self.assertIn("did not verify", entry.error_message)
        self.assertTrue(entry.signature_check, "the reason is recorded")
