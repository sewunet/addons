"""The HTML checkout form and the verify path.

Both exist because of the same fact: on a machine Chapa cannot reach, the
webhook never arrives. The HTML form lets a checkout start without Odoo
calling out at all; verify lets a paid transaction be settled without the
callback. Together they make the integration work from a laptop.
"""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.payment_chapa import const



# post_install, like Odoo's own: at_install tests run against a half-loaded
# registry, and a later module's NOT NULL column on res.partner makes every
# partner creation fail on an incremental -u run.
@tagged("post_install", "-at_install")
class ChapaCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Patch targets go on the registry class: `_send_api_request` and
        # `_post_process` are inherited from the base payment module and
        # do not exist on this module's class object.
        cls.Tx = type(cls.env["payment.transaction"])
        cls.provider = cls.env.ref("payment_chapa.payment_provider_chapa")
        cls.provider.write(
            {
                "chapa_secret_key": "CHASECK_TEST-000000000000000000000000",
                "chapa_public_key": "CHAPUBK_TEST-000000000000000000000000",
                "state": "test",
            }
        )
        cls.method = cls.env.ref("payment_chapa.payment_method_chapa")
        cls.currency = cls.env.ref("base.ETB")
        cls.partner = cls.env["res.partner"].create(
            {
                "name": "Dawit Haile Demissie",
                "email": "dawit@example.com",
                "phone": "+251 90 012 3456",
            }
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


@tagged("post_install", "-at_install")
class TestChapaHtmlCheckout(ChapaCase):
    def setUp(self):
        super().setUp()
        self.provider.chapa_checkout_mode = "html"

    def test_the_form_posts_to_the_hosted_page(self):
        values = self._tx()._get_specific_rendering_values({})
        self.assertEqual(values["api_url"], const.HOSTED_PAY_URL)
        self.assertEqual(values["checkout_mode"], "html")

    def test_every_required_field_is_present(self):
        """Chapa's HTML checkout contract, field by field."""
        values = self._tx()._get_specific_rendering_values({})
        for field in (
            "public_key",
            "tx_ref",
            "amount",
            "currency",
            "email",
            "first_name",
            "last_name",
            "callback_url",
            "return_url",
        ):
            self.assertTrue(values.get(field), f"{field} missing or empty")

    def test_the_public_key_goes_out_and_the_secret_does_not(self):
        """The form lands in the member's browser. The secret must not."""
        values = self._tx()._get_specific_rendering_values({})
        self.assertTrue(values["public_key"].startswith("CHAPUBK"))
        self.assertNotIn("CHASECK", str(values))

    def test_no_outbound_call_is_made(self):
        """That is the point of the HTML mode."""
        with patch.object(self.Tx, "_send_api_request") as sent:
            self._tx()._get_specific_rendering_values({})
        sent.assert_not_called()

    def test_the_phone_is_bare(self):
        values = self._tx()._get_specific_rendering_values({})
        self.assertEqual(values["phone_number"], "+251900123456")

    def test_the_reference_is_chapa_safe(self):
        values = self._tx()._get_specific_rendering_values({})
        self.assertNotIn("/", values["tx_ref"])

    def test_html_mode_refuses_to_enable_without_a_public_key(self):
        # Disabled providers may be half-configured; the check bites on
        # the way to live.
        self.provider.write({"state": "disabled", "chapa_public_key": False})
        with self.assertRaises(ValidationError):
            self.provider.state = "enabled"


@tagged("post_install", "-at_install")
class TestChapaApiCheckout(ChapaCase):
    def test_api_mode_still_calls_initialize(self):
        """The default mode is unchanged by the HTML addition."""
        self.provider.chapa_checkout_mode = "api"
        with patch.object(
            self.Tx, "_send_api_request",
            return_value={"checkout_url": "https://checkout.chapa.co/x"},
        ) as sent:
            values = self._tx()._get_specific_rendering_values({})
        sent.assert_called_once()
        self.assertEqual(values["api_url"], "https://checkout.chapa.co/x")
        self.assertEqual(values["checkout_mode"], "api")


@tagged("post_install", "-at_install")
class TestChapaVerify(ChapaCase):
    def test_a_successful_verify_marks_done_and_post_processes(self):
        """The whole point: journal, reconciliation and the membership fee,
        without waiting for a poll."""
        tx = self._tx()
        with patch.object(
            self.Tx, "_chapa_verify", return_value={"status": "success", "reference": "CH-1"}
        ), patch.object(self.Tx, "_post_process") as post:
            tx.action_chapa_verify()
        self.assertEqual(tx.state, "done")
        self.assertEqual(tx.provider_reference, "CH-1")
        post.assert_called_once()

    def test_a_pending_verify_does_not_post_process(self):
        tx = self._tx()
        with patch.object(self.Tx, "_chapa_verify", return_value={"status": "pending"}), patch.object(
            self.Tx, "_post_process"
        ) as post:
            tx.action_chapa_verify()
        self.assertEqual(tx.state, "pending")
        post.assert_not_called()

    def test_a_failed_verify_marks_error(self):
        tx = self._tx()
        with patch.object(self.Tx, "_chapa_verify", return_value={"status": "failed"}):
            tx.action_chapa_verify()
        self.assertEqual(tx.state, "error")

    def test_a_transaction_chapa_never_saw_is_recorded_not_raised(self):
        """A member who never reached the checkout page has no record at
        Chapa. That is an error on the row, not an exception in a batch."""
        first, second = self._tx(), self._tx()
        with patch.object(
            self.Tx, "_chapa_verify",
            side_effect=[ValidationError("Transaction not found"), {"status": "success"}],
        ), patch.object(self.Tx, "_post_process"):
            (first + second).action_chapa_verify()
        self.assertEqual(first.state, "error")
        self.assertIn("verification failed", first.state_message)
        self.assertEqual(second.state, "done")

    def test_an_already_done_transaction_is_left_alone(self):
        tx = self._tx()
        tx._set_done()
        with patch.object(self.Tx, "_chapa_verify") as verify:
            tx.action_chapa_verify()
        verify.assert_not_called()

    def test_the_cron_only_asks_about_the_right_window(self):
        """Too new: the member may still be paying. Too old: abandoned."""
        now = fields.Datetime.now()
        fresh, due, stale = self._tx(), self._tx(), self._tx()
        self.env.cr.execute(
            "UPDATE payment_transaction SET create_date = %s WHERE id = %s",
            (now - timedelta(minutes=const.VERIFY_AFTER_MINUTES + 1), due.id),
        )
        self.env.cr.execute(
            "UPDATE payment_transaction SET create_date = %s WHERE id = %s",
            (now - timedelta(days=const.VERIFY_WITHIN_DAYS + 1), stale.id),
        )
        self.env.invalidate_all()

        # Only `due` should be verified. `_chapa_verify` is patched per record
        # so the set of references it was asked about can be read back.
        asked = []

        def fake_verify(tx_self):
            asked.append(tx_self.reference)
            return {"status": "pending"}

        with patch.object(self.Tx, "_chapa_verify", autospec=True, side_effect=fake_verify):
            self.env["payment.transaction"]._cron_chapa_verify_pending()

        self.assertEqual(asked, [due.reference])

    def test_a_confirmed_payment_survives_a_journal_failure(self):
        """Found live: the first payment Chapa confirmed was rolled back to
        draft because posting it to the journal raised. The money had moved.
        `done` is the fact; accounting is the follow-up, retried by cron."""
        tx = self._tx()
        with patch.object(
            self.Tx, "_chapa_verify", return_value={"status": "success"}
        ), patch.object(
            self.Tx, "_post_process",
            side_effect=ValidationError("Please define a payment method line"),
        ):
            tx.action_chapa_verify()  # must not raise
        self.assertEqual(tx.state, "done")
        self.assertFalse(tx.is_post_processed, "left for the post-processing cron")
        self.assertIn(
            "retried automatically",
            tx.state_message or "",
            "the office should see why, on the record",
        )


@tagged("post_install", "-at_install")
class TestChapaInstallHook(ChapaCase):
    """post_install on purpose. Which of payment_chapa and account_payment
    installs first is decided by the dependency graph, not by us: if this
    module goes first, its own hook cannot create the method (the accounting
    override is not loaded yet) and account_payment backfills it afterwards.
    Either order is correct once everything is in, so that is when to look."""

    def test_the_provider_has_an_account_payment_method(self):
        """Without it, `_ensure_payment_method_line` returns early, no journal
        line is created, and every confirmed payment fails at posting time.
        The framework creates it in `_setup_provider`; a provider made from
        XML data never went through that, so the module must do it."""
        method = self.env["account.payment.method"].search([("code", "=", "chapa")])
        self.assertTrue(method, "post_init_hook should have created it")
        self.assertEqual(method.payment_type, "inbound")
