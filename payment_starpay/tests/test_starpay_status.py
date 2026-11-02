"""How a Star-Pay payment's status moves: the callback, and trdp/verify.

The callback (Star-Pay calling the callbackURL) moves the transaction once
its signature verifies. trdp/verify is asked by everything else: the payer
coming back from the checkout page, the app's poll, the cron that catches a
lost callback, and the button on the transaction form.
"""

import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

ORDER_ID = "6UE7B4M00P5"
SECRET = "whsec_test"


class StarpayStatusCommon(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref("payment_starpay.payment_provider_starpay")
        cls.provider.write({"starpay_webhook_secret": SECRET, "starpay_api_secret": "sk_test"})
        cls.partner = cls.env["res.partner"].create({"name": "Star Payer", "phone": "0911223347"})

    def _tx(self, reference="MEM/ST/000001", state="pending", age_minutes=0, order=ORDER_ID):
        tx = self.env["payment.transaction"].create({
            "provider_id": self.provider.id,
            "payment_method_id": self.env.ref("payment_starpay.payment_method_starpay").id,
            "amount": 200.0,
            "currency_id": self.env.ref("base.ETB").id,
            "partner_id": self.partner.id,
            "reference": reference,
            "provider_reference": order,
        })
        if state == "pending":
            tx._set_pending()
        if age_minutes:
            self.env.cr.execute(
                "UPDATE payment_transaction SET create_date = %s WHERE id = %s",
                (fields.Datetime.now() - timedelta(minutes=age_minutes), tx.id),
            )
            tx.invalidate_recordset(["create_date"])
        return tx

    def _verify_answers(self, data):
        """Star-Pay's trdp/verify, answering with `data`."""
        return patch.object(type(self.provider), "_send_api_request", return_value=data)

    def _callback(self, body, secret=SECRET):
        raw = json.dumps(body)
        timestamp = "1727265186"
        signature = hmac.new(secret.encode(), f"{timestamp}.{raw}".encode(), hashlib.sha256).hexdigest()
        return self.url_open(
            "/payment/starpay/webhook", data=raw,
            headers={"Content-Type": "application/json", "X-Timestamp": timestamp, "X-Signature": signature},
        )


@tagged("post_install", "-at_install")
class TestStarpayCallback(StarpayStatusCommon):
    def test_a_signed_paid_callback_moves_the_payment_to_done(self):
        tx = self._tx()
        response = self._callback({
            "order_id": ORDER_ID, "status": "PAID", "amount": "200", "currency": "WALLET_CURRENCY_ETB",
            "payment_type": "USSD_PUSH", "receipt_url": "https://receipt.starpayethiopia.com/r/1",
            "metadata": {"reference": tx.reference},
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(tx.state, "done")
        # Only the callback says how they paid; verify does not.
        self.assertEqual(tx.starpay_payment_type, "USSD_PUSH")
        self.assertEqual(tx.starpay_receipt_url, "https://receipt.starpayethiopia.com/r/1")

    def test_a_paid_callback_settles_the_fee_at_once(self):
        # Post-processing is what reconciles the fee's invoice; it runs in
        # the callback rather than waiting for the framework's cron.
        tx = self._tx()
        with patch.object(type(tx), "_post_process", autospec=True) as settle:
            self._callback({"order_id": ORDER_ID, "status": "PAID", "amount": "200"})
        self.assertEqual(tx.state, "done")
        settle.assert_called_once()

    def test_a_concurrent_settlement_is_left_for_odoo_to_retry(self):
        # Seen on UAT: the return page and Odoo's status page settled one
        # payment at once. That error must reach Odoo's request retry, not be
        # swallowed as "the cron will retry".
        from psycopg2 import errors

        tx = self._tx()
        with self._verify_answers({"order_id": ORDER_ID, "status": "PAID", "amount": "200"}), \
                patch.object(type(tx), "_post_process", autospec=True,
                             side_effect=errors.SerializationFailure("concurrent update")):
            with self.assertRaises(errors.SerializationFailure):
                tx._starpay_check_status()

    def test_a_settlement_that_fails_leaves_the_payment_done(self):
        # The money is in either way; the cron retries the settlement. With
        # accounting installed this happens for real to a transaction with no
        # payment journal -- forced here so it does not depend on that.
        tx = self._tx()
        with patch.object(type(tx), "_post_process", autospec=True, side_effect=RuntimeError("no journal")):
            response = self._callback({"order_id": ORDER_ID, "status": "PAID", "amount": "200"})
        self.assertEqual(response.status_code, 200, "Star-Pay is told the callback arrived")
        self.assertEqual(tx.state, "done")
        self.assertFalse(tx.is_post_processed)

    def test_the_callback_moves_it_without_asking_verify(self):
        tx = self._tx()
        with self._verify_answers({}) as verify:
            self._callback({"order_id": ORDER_ID, "status": "PAID", "amount": "200"})
        self.assertEqual(tx.state, "done")
        verify.assert_not_called()

    def test_a_callback_for_the_wrong_amount_does_not_pay_the_fee(self):
        tx = self._tx()
        self._callback({"order_id": ORDER_ID, "status": "PAID", "amount": "2", "currency": "ETB"})
        self.assertNotEqual(tx.state, "done")

    def test_a_failed_callback_fails_the_payment(self):
        tx = self._tx()
        self._callback({"order_id": ORDER_ID, "status": "FAILED", "amount": "200"})
        self.assertEqual(tx.state, "error")

    def test_an_unsigned_paid_is_not_believed(self):
        # The body says PAID, the signature does not verify, Star-Pay says
        # UNPAID: the payment stays open. Only Star-Pay's own answer counts.
        tx = self._tx()
        with self._verify_answers({"order_id": ORDER_ID, "status": "UNPAID"}) as verify:
            response = self._callback({"order_id": ORDER_ID, "status": "PAID", "amount": "200"}, secret="not-it")
        self.assertEqual(response.status_code, 200)
        verify.assert_called_once()
        self.assertEqual(verify.call_args.kwargs["json"], {"orderId": ORDER_ID})
        self.assertEqual(tx.state, "pending")

    def test_an_unsigned_callback_still_settles_through_verify(self):
        tx = self._tx()
        with self._verify_answers({"order_id": ORDER_ID, "status": "PAID", "amount": "200"}):
            self._callback({"order_id": ORDER_ID, "status": "PAID", "amount": "200"}, secret="not-it")
        self.assertEqual(tx.state, "done")
        entry = self.env["starpay.request.log"].search([("endpoint", "=", "starpay/webhook")], limit=1)
        self.assertEqual(entry.state, "failed", "the signature problem stays visible in the log")
        self.assertIn("trdp/verify instead", entry.error_message)

    def test_a_verified_callback_that_leaves_it_open_is_verified_too(self):
        # A signed PENDING callback: applied, and then Star-Pay is asked.
        tx = self._tx()
        with self._verify_answers({"order_id": ORDER_ID, "status": "PAID", "amount": "200"}) as verify:
            self._callback({"order_id": ORDER_ID, "status": "PENDING", "amount": "200"})
        verify.assert_called_once()
        self.assertEqual(tx.state, "done")

    def test_the_callback_star_pay_actually_sends(self):
        # A real successful callback, verbatim. No metadata, so no club
        # reference: the order is billRefNo, the same id trdp/order returned
        # as order_id (the receipt is WST-<that id>). The amount is a number
        # and no currency is sent.
        tx = self._tx("MEM/REAL/000001", order="33WJ8946WB")
        tx.amount = 1.0
        response = self._callback({
            "billRefNo": "33WJ8946WB",
            "status": "PAID",
            "timestamp": "2025-12-10T11:05:37.566Z",
            "message": "Payment successful",
            "merchantId": "6888dc21ee7cbfe63657144f",
            "customerId": "656445e6-20fa-440d-b8c9-0a588d1ca05b",
            "externalReferenceId": "CLA9SR5XR9",
            "amount": 1,
            "payment_type": "USSD_PUSH",
            "receipt_url": "https://receipt.starpayethiopia.com/receiptqa/WST-33WJ8946WB",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(tx.state, "done")
        self.assertEqual(tx.starpay_payment_type, "USSD_PUSH")
        self.assertEqual(tx.starpay_receipt_url, "https://receipt.starpayethiopia.com/receiptqa/WST-33WJ8946WB")
        self.assertEqual(tx.starpay_external_reference, "CLA9SR5XR9", "what ChatBirr gets as ftNumber")
        entry =self.env["starpay.request.log"].search([("endpoint", "=", "starpay/webhook")], limit=1)
        self.assertEqual(entry.provider_reference, "33WJ8946WB")
        self.assertEqual(entry.reference, "MEM/REAL/000001", "the log names the fee even when Star-Pay does not")
        self.assertEqual(entry.state, "ok")

    def test_a_callback_found_by_order_id_alone(self):
        # No metadata echoed back: the stored Star-Pay order id finds it.
        tx = self._tx()
        self._callback({"order_id": ORDER_ID, "status": "PAID", "amount": "200"})
        self.assertEqual(tx.state, "done")


@tagged("post_install", "-at_install")
class TestStarpayVerify(StarpayStatusCommon):
    def test_unpaid_means_still_waiting_not_failed(self):
        # What the sandbox reports before the payer finishes. It used to be an
        # "unknown status", and an unknown status failed the payment.
        tx = self._tx()
        with self._verify_answers({"order_id": ORDER_ID, "status": "UNPAID"}):
            tx._starpay_check_status()
        self.assertEqual(tx.state, "pending")

    def test_an_unknown_status_leaves_the_payment_where_it_was(self):
        tx = self._tx()
        with self._verify_answers({"order_id": ORDER_ID, "status": "SOMETHING_NEW"}):
            tx._starpay_check_status()
        self.assertEqual(tx.state, "pending")

    def test_verify_sends_the_order_id_and_applies_the_answer(self):
        tx = self._tx()
        with self._verify_answers({"order_id": ORDER_ID, "status": "PAID", "amount": "200"}) as verify, \
                patch.object(type(tx), "_post_process", autospec=True) as settle:
            self.assertTrue(tx._starpay_check_status())
        self.assertEqual(verify.call_args.args[:2], ("POST", "trdp/verify"))
        self.assertEqual(verify.call_args.kwargs["json"], {"orderId": ORDER_ID})
        self.assertEqual(tx.state, "done")
        settle.assert_called_once()

    def test_a_payer_coming_back_is_verified_not_trusted(self):
        tx = self._tx()
        # The query string says PAID; only Star-Pay's own answer counts.
        with self._verify_answers({"order_id": ORDER_ID, "status": "UNPAID"}):
            self.url_open(f"/payment/starpay/return?order_id={ORDER_ID}&status=PAID")
        self.assertEqual(tx.state, "pending")
        with self._verify_answers({"order_id": ORDER_ID, "status": "PAID", "amount": "200"}):
            self.url_open(f"/payment/starpay/return?order_id={ORDER_ID}")
        self.assertEqual(tx.state, "done")

    def test_a_bare_return_with_the_club_reference_is_verified(self):
        # What Star-Pay actually does: back to the redirect URL, adding
        # nothing -- so the ?ref= the order put there is all there is.
        tx = self._tx("MEM/RET/000001")
        with self._verify_answers({"order_id": ORDER_ID, "status": "PAID", "amount": "200"}) as verify:
            self.url_open("/payment/starpay/return?ref=MEM%2FRET%2F000001")
        verify.assert_called_once()
        self.assertEqual(tx.state, "done")
        entry = self.env["starpay.request.log"].search([("endpoint", "=", "starpay/return")], limit=1)
        self.assertEqual(entry.state, "ok")
        self.assertEqual(entry.reference, "MEM/RET/000001")

    def test_the_button_reports_what_star_pay_said(self):
        tx = self._tx()
        with self._verify_answers({"order_id": ORDER_ID, "status": "PAID", "amount": "200"}):
            action = tx.action_starpay_check_status()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(tx.state, "done")

    def test_the_button_says_so_when_there_is_no_order(self):
        tx = self._tx(order=False)
        with self.assertRaises(UserError):
            tx.action_starpay_check_status()


@tagged("post_install", "-at_install")
class TestStarpayCron(StarpayStatusCommon):
    def test_the_cron_verifies_open_payments_whose_callback_never_came(self):
        lost = self._tx("MEM/CRON/000001", age_minutes=30, order="ORD-LOST")
        fresh = self._tx("MEM/CRON/000002", age_minutes=2, order="ORD-FRESH")
        stale = self._tx("MEM/CRON/000003", age_minutes=60 * 30, order="ORD-STALE")
        with self._verify_answers({"order_id": "ORD-LOST", "status": "PAID", "amount": "200"}) as verify:
            self.env["payment.transaction"]._cron_starpay_check_pending()
        asked = [call.kwargs["json"]["orderId"] for call in verify.call_args_list]
        self.assertEqual(asked, ["ORD-LOST"], "not the payer still on the page, not the long-dead order")
        self.assertEqual(lost.state, "done")
        self.assertEqual(fresh.state, "pending")
        self.assertEqual(stale.state, "pending")

    def test_one_unreachable_order_does_not_stop_the_rest(self):
        first = self._tx("MEM/CRON/000011", age_minutes=40, order="ORD-A")
        second = self._tx("MEM/CRON/000012", age_minutes=30, order="ORD-B")

        def answer(method, endpoint, json=None, **kwargs):
            if json["orderId"] == "ORD-A":
                raise RuntimeError("Star-Pay is down")
            return {"order_id": "ORD-B", "status": "PAID", "amount": "200"}

        with patch.object(type(self.provider), "_send_api_request", side_effect=answer):
            self.env["payment.transaction"]._cron_starpay_check_pending()
        self.assertEqual(first.state, "pending")
        self.assertEqual(second.state, "done")


@tagged("post_install", "-at_install")
class TestStarpaySignatureVerdict(StarpayStatusCommon):
    """Which signatures count, and what the log says when one does not."""

    BODY = '{"billRefNo":"33WJ8946WB","status":"PAID","amount":1}'
    TS = "1765364737566"

    def _verdict(self, headers):
        from odoo.addons.payment_starpay.controllers.main import StarPayController
        return StarPayController._signature_verdict(self.BODY.encode(), headers, self.provider)

    def _sig(self, secret, message, encoding="hex"):
        import base64
        digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
        return digest.hex() if encoding == "hex" else base64.b64encode(digest).decode()

    def test_the_documented_form_verifies(self):
        ok, verdict = self._verdict({"X-Timestamp": self.TS, "X-Signature": self._sig(SECRET, f"{self.TS}.{self.BODY}")})
        self.assertTrue(ok)
        self.assertEqual(verdict, "Verified: webhook secret, hex, over timestamp.compact JSON.")

    def test_other_genuine_forms_verify_and_say_which(self):
        cases = [
            ({"X-Timestamp": self.TS, "X-Signature": self._sig(SECRET, f"{self.TS}.{self.BODY}", "base64")}, "base64"),
            ({"X-Timestamp": self.TS, "X-Signature": "sha256=" + self._sig(SECRET, f"{self.TS}.{self.BODY}")}, "hex"),
            ({"X-Signature": self._sig(SECRET, self.BODY)}, "over compact JSON."),
            ({"X-Timestamp": self.TS, "X-Signature": self._sig("sk_test", f"{self.TS}.{self.BODY}")}, "API secret"),
            ({"X-Starpay-Timestamp": self.TS, "X-Starpay-Signature": self._sig(SECRET, f"{self.TS}.{self.BODY}")}, "timestamp.compact JSON"),
        ]
        for headers, named in cases:
            ok, verdict = self._verdict(headers)
            self.assertTrue(ok, headers)
            self.assertIn(named, verdict)

    def test_the_documented_signature_over_a_body_sent_with_spaces(self):
        # Star-Pay signs JSON.stringify(payload): compact. A body that arrives
        # spaced out (or re-escaped) must still verify against that.
        spaced = '{"billRefNo": "33WJ8946WB", "status": "PAID", "amount": 1}'
        compact = '{"billRefNo":"33WJ8946WB","status":"PAID","amount":1}'
        from odoo.addons.payment_starpay.controllers.main import StarPayController
        ok, verdict = StarPayController._signature_verdict(
            spaced.encode(),
            {"X-Timestamp": self.TS, "X-Signature": self._sig(SECRET, f"{self.TS}.{compact}")},
            self.provider,
        )
        self.assertTrue(ok)
        self.assertIn("compact JSON", verdict)

    def test_the_documented_python_reference_verifies(self):
        # Star-Pay's own Python example, verbatim in spirit: parse, then
        # json.dumps(payload, separators=(",", ":")) -- ASCII-escaped, so a
        # name in Amharic is signed as \u escapes.
        payload = {"billRefNo": "33WJ8946WB", "status": "PAID", "amount": 1, "customerName": "አበበ"}
        signed = json.dumps(payload, separators=(",", ":"))
        from odoo.addons.payment_starpay.controllers.main import StarPayController
        ok, verdict = StarPayController._signature_verdict(
            json.dumps(payload, ensure_ascii=False).encode(),
            {"X-Timestamp": self.TS, "X-Signature": self._sig(SECRET, f"{self.TS}.{signed}")},
            self.provider,
        )
        self.assertTrue(ok, verdict)

    def test_a_whole_amount_is_signed_as_javascript_writes_it(self):
        compact = '{"billRefNo":"33WJ8946WB","status":"PAID","amount":200}'
        from odoo.addons.payment_starpay.controllers.main import StarPayController
        ok, _verdict = StarPayController._signature_verdict(
            b'{"billRefNo": "33WJ8946WB", "status": "PAID", "amount": 200.0}',
            {"X-Timestamp": self.TS, "X-Signature": self._sig(SECRET, f"{self.TS}.{compact}")},
            self.provider,
        )
        self.assertTrue(ok)

    def test_a_wrong_secret_is_refused_and_says_what_was_tried(self):
        ok, verdict = self._verdict({"X-Timestamp": self.TS, "X-Signature": self._sig("not-it", f"{self.TS}.{self.BODY}")})
        self.assertFalse(ok)
        self.assertIn("matches none", verdict)
        self.assertIn("Webhook Secret", verdict)

    def test_a_missing_signature_header_is_named(self):
        ok, verdict = self._verdict({"X-Timestamp": self.TS})
        self.assertFalse(ok)
        self.assertIn("No signature header", verdict)

    def test_no_secret_on_the_provider_is_named(self):
        self.provider.write({"starpay_webhook_secret": False, "starpay_api_secret": False})
        ok, verdict = self._verdict({"X-Timestamp": self.TS, "X-Signature": "abc"})
        self.assertFalse(ok)
        self.assertIn("No webhook secret", verdict)

    def test_a_verified_callback_logs_its_headers_and_how_it_verified(self):
        tx = self._tx("MEM/SIGLOG/000001")
        self._callback({"order_id": ORDER_ID, "status": "PAID", "amount": "200"})
        self.assertEqual(tx.state, "done")
        entry = self.env["starpay.request.log"].search([("endpoint", "=", "starpay/webhook")], limit=1)
        self.assertIn("X-Signature", entry.request_headers)
        self.assertNotIn("Cookie", entry.request_headers)
        self.assertTrue(entry.signature_check.startswith("Verified: webhook secret, hex, over timestamp."))
