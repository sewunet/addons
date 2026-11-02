import hashlib
from urllib.parse import urlencode
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from odoo.addons.payment.logging import get_payment_logger
from odoo.service.model import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY
from odoo.addons.payment_starpay import const

_logger = get_payment_logger(__name__)


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    # The hosted checkout Star-Pay opened for this transaction. On the website
    # the URL is used once, in the redirect, and forgotten; the mobile app is
    # handed it instead and may come back for it on any poll -- a member who
    # closed the page has not given up -- so it is kept rather than re-opened,
    # which would make a second order for one fee.
    starpay_checkout_url = fields.Char(readonly=True, copy=False)

    # Which rail the payer chose on Star-Pay's page -- a USSD prompt, a
    # one-time code, a card. Star-Pay does not let a merchant ask for one:
    # there is a single order endpoint, the page offers them all, and the
    # docs say the integration is identical whichever the payer picks. So
    # the only honest thing to hold is what they did pick, which arrives on
    # the callback and nowhere else -- `trdp/verify` does not report it.
    starpay_payment_type = fields.Char(
        string="Star-Pay Method", readonly=True, copy=False
    )
    starpay_receipt_url = fields.Char(readonly=True, copy=False)
    # Star-Pay's own reference for the money movement, `externalReferenceId`
    # on the callback (e.g. CLA9SR5XR9) -- not the club's reference and not
    # the order id. It is what ChatBirr is given as the payment's ftNumber
    # when the member is registered there. Like payment_type, only the
    # callback carries it.
    starpay_external_reference = fields.Char(
        string="Star-Pay External Reference", readonly=True, copy=False, index=True
    )

    def _starpay_items(self):
        """The one line an order carries, in the shape Star-Pay enforces.

        Star-Pay documents two different item shapes. Its prose example shows
        `name` / `quantity` / `price`; its OpenAPI schema and its running API
        want `productId` / `quantity` / `item_name` / `unit_price`, all four
        required. An order sent the documented way is refused outright:

            GEN_019: items.0.productId Required

        The schema is what the gateway actually checks, so the schema is what
        this sends.

        One line, because a membership fee is one thing bought once. There is
        no Star-Pay catalogue behind it, so `productId` is the fee's own
        reference: unique per fee, stable across retries of the same fee, and
        the number the club would quote if Star-Pay ever asked which order a
        line belonged to.
        """
        self.ensure_one()
        return [
            {
                "productId": self._starpay_product_id(),
                "quantity": 1,
                "item_name": self.reference,
                "unit_price": self.amount,
            }
        ]

    def _starpay_product_id(self):
        """What goes in `productId`, which Star-Pay requires and never explains.

        Its examples are 24-character hexadecimal ids -- the shape a Mongo
        `ObjectId` prints as -- so a reference like `MEM/2026/000087` is not
        merely unknown to Star-Pay, it is unparseable, and a backend that
        casts it fails somewhere deep and answers `GEN_004: Something went
        wrong`.

        So: the merchant's own catalogue id when the club has one configured,
        and otherwise a 24-hex digest of the reference. The digest is stable
        for a given fee, so a retry books against the same line, and it is
        at least the right shape.
        """
        self.ensure_one()
        configured = (self.provider_id.starpay_product_id or "").strip()
        if configured:
            return configured
        return hashlib.md5(self.reference.encode()).hexdigest()[:24]

    def _get_specific_rendering_values(self, processing_values):
        """Override of `payment` to open a Star-Pay checkout session.

        Star-Pay is a redirect gateway: it hands back a hosted checkout URL that
        the customer must be sent to.
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != "starpay":
            return res

        payload = {
            "amount": self.amount,
            "currency": self.currency_id.name,
            "description": self.reference,
            "customerName": self.partner_name or self.partner_id.display_name,
            # Star-Pay verifies the payer by phone, so a transaction without one
            # cannot be created; failing here is clearer than a gateway error.
            "customerPhoneNumber": const.e164_phone(self.partner_phone),
            "items": self._starpay_items(),
            # Both from the provider's fields: the generated address unless
            # someone has set the public one there.
            "callbackURL": self.provider_id._starpay_callback_url(),
            # The provider's field, with this transaction's reference on it:
            # Star-Pay sends the payer back to exactly this address and adds
            # nothing of its own, so without the reference the return page
            # cannot tell which payment came back.
            "redirectUrl": self._starpay_return_url(),
            "metadata": {"reference": self.reference},
        }
        if self.partner_email:
            payload["customerEmail"] = self.partner_email

        if not payload["customerPhoneNumber"]:
            self._set_error(_("Star-Pay requires a phone number for the payer."))
            return {}

        try:
            order = self._send_api_request("POST", "trdp/order", json=payload)
        except ValidationError as error:
            # Star-Pay's generic codes say nothing about which field it
            # disliked, so the order goes in the log beside the refusal. The
            # secret travels in a header, not in here.
            _logger.error(
                "Star-Pay refused the order for %s: %s. Payload: %s",
                self.reference,
                error,
                payload,
            )
            self._set_error(str(error))
            return {}

        checkout_url = const.checkout_url(order)
        if not checkout_url:
            self._set_error(_("Star-Pay did not return a checkout session."))
            return {}

        # The order id is what the webhook and the verify endpoint key on.
        self.provider_reference = const.order_id(order) or False
        self.starpay_checkout_url = checkout_url
        # The website redirects to it; the app is handed it as portal_url.
        return {"api_url": checkout_url}

    @api.model
    def _extract_reference(self, provider_code, payment_data):
        """Override of `payment` to extract the reference from the payment data."""
        if provider_code != "starpay":
            return super()._extract_reference(provider_code, payment_data)
        # metadata.reference on a callback; `ref` on the return address, put
        # there by _starpay_return_url. Either only finds the transaction:
        # the return page then asks Star-Pay, and a callback is applied only
        # once its signature verifies.
        reference = (payment_data.get("metadata") or {}).get("reference") or payment_data.get("ref")
        if reference:
            return reference
        # No metadata echoed back: find the transaction by Star-Pay's order id.
        order = const.order_id(payment_data)
        if order:
            tx = self.search([("provider_code", "=", "starpay"), ("provider_reference", "=", order)], limit=1)
            if tx:
                return tx.reference
        return order or None

    def _extract_amount_data(self, payment_data):
        """Override of `payment` to extract the amount and currency.

        Returning these lets the framework refuse a payment whose amount does
        not match the transaction, which is the check that matters most here.
        """
        if self.provider_code != "starpay":
            return super()._extract_amount_data(payment_data)
        amount = payment_data.get("amount")
        if amount is None:
            return None
        return {
            "amount": float(amount),
            "currency_code": const.currency_code(payment_data.get("currency")) or self.currency_id.name,
        }

    def _apply_updates(self, payment_data):
        """Override of `payment` to update the transaction from the payment data."""
        if self.provider_code != "starpay":
            return super()._apply_updates(payment_data)

        if const.order_id(payment_data):
            self.provider_reference = const.order_id(payment_data)
        # Written before the status is applied: settling the transaction
        # notifies the fee, and a receipt that arrives without the method on
        # it has to be corrected afterwards.
        if payment_data.get("payment_type"):
            self.starpay_payment_type = payment_data["payment_type"]
        if payment_data.get("receipt_url"):
            self.starpay_receipt_url = payment_data["receipt_url"]
        if payment_data.get("externalReferenceId"):
            self.starpay_external_reference = payment_data["externalReferenceId"]

        status = const.payment_status(payment_data)
        if status in const.PAYMENT_STATUS_MAPPING["pending"]:
            self._set_pending()
        elif status in const.PAYMENT_STATUS_MAPPING["done"]:
            self._set_done()
        elif status in const.PAYMENT_STATUS_MAPPING["cancel"]:
            self._set_canceled()
        elif status in const.PAYMENT_STATUS_MAPPING["error"]:
            self._set_error(
                _("Star-Pay reported that the payment failed (status %s).", status)
            )
        else:
            # Left where it is. Failing a payment over a word nobody has seen
            # before is worse than waiting: the next verify, or the cron,
            # asks again.
            _logger.warning(
                "Star-Pay reported an unknown status (%s) for transaction %s; left as %s.",
                status,
                self.reference,
                self.state,
            )

    def _starpay_return_url(self):
        """The provider's redirect URL, carrying this transaction's reference.

        The first real return from Star-Pay arrived as a bare GET on the
        redirect URL -- no order id, no reference -- and matched no
        transaction. The club's own reference goes on the address instead.
        """
        self.ensure_one()
        base = self.provider_id._starpay_redirect_url()
        return f"{base}{'&' if '?' in base else '?'}{urlencode({'ref': self.reference})}"

    # === STATUS: FROM THE CALLBACK AND FROM VERIFY === #

    def _starpay_verify(self):
        """Ask Star-Pay (trdp/verify) for the status of this transaction's order.

        Returns the verify answer's data, or {} when there is no order to ask
        about. Raises ValidationError when Star-Pay cannot be reached or refuses.
        """
        self.ensure_one()
        if not self.provider_reference:
            return {}
        return self._send_api_request(
            "POST", "trdp/verify", json={"orderId": self.provider_reference}
        )

    def _starpay_handle_callback(self, data):
        """Apply a callback Star-Pay sent to the callbackURL.

        Called only once the signature has verified, so the callback's own
        status moves the transaction -- and it is the only message that says
        which method the payer used and where Star-Pay's receipt is. The
        amount and currency are checked against the transaction by `_process`.
        """
        self.ensure_one()
        self._process("starpay", data)
        self._starpay_settle()

    def _starpay_check_status(self):
        """Ask Star-Pay with trdp/verify and apply its answer.

        For everything that is not a callback: the payer coming back from the
        checkout page, an administrator's button, and the cron that catches a
        callback that never arrived. Returns True when Star-Pay answered.
        """
        self.ensure_one()
        verified = self._starpay_verify()
        if not verified:
            return False
        self._process("starpay", verified)
        self._starpay_settle()
        return True

    def _starpay_settle(self):
        """Settle the fee as soon as the payment is done.

        `done` is not a settled fee: the invoice is reconciled in post
        processing, and without it the member still shows as owing until the
        framework's cron gets there. Its own savepoint, because the payment is
        confirmed either way and that cron retries whatever fails here.
        """
        self.ensure_one()
        if self.state != "done" or self.is_post_processed:
            return
        try:
            with self.env.cr.savepoint():
                self._post_process()
        except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
            # Another request is settling the same payment -- Odoo's own
            # payment-status page post-processes too, and a shop order's
            # confirmation and PDF can take long enough to overlap. The
            # transaction is aborted either way; letting this through is what
            # makes Odoo retry the whole request. Caught, it was swallowed
            # and the request ended on an aborted transaction.
            raise
        except Exception:  # noqa: BLE001 - confirmed; the cron retries
            _logger.exception(
                "Star-Pay transaction %s is paid but was not post-processed; the cron will retry.",
                self.reference,
            )

    def action_starpay_check_status(self):
        """The "Check with Star-Pay" button on the transaction form."""
        self.ensure_one()
        try:
            answered = self._starpay_check_status()
        except ValidationError as error:
            raise UserError(_("Star-Pay could not be asked: %s", error)) from error
        if not answered:
            raise UserError(_("This transaction has no Star-Pay order to check."))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Star-Pay"),
                "message": _("Star-Pay reports %(ref)s as %(state)s.", ref=self.reference, state=self.state),
                "type": "info",
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    @api.model
    def _cron_starpay_check_pending(self, limit=50):
        """Verify Star-Pay payments still open, in case their callback was lost.

        Older than a few minutes, so a payer still on the checkout page is not
        asked about; younger than Star-Pay's 24-hour checkout lifetime plus a
        margin, after which nothing will change. Each in its own savepoint, so
        one unreachable order does not stop the rest.
        """
        now = fields.Datetime.now()
        txs = self.search(
            [
                ("provider_code", "=", "starpay"),
                ("state", "in", ("draft", "pending")),
                ("provider_reference", "!=", False),
                ("create_date", "<", now - timedelta(minutes=const.CRON_MIN_AGE_MINUTES)),
                ("create_date", ">", now - timedelta(hours=const.CRON_MAX_AGE_HOURS)),
            ],
            limit=limit,
            order="create_date asc",
        )
        for tx in txs:
            try:
                with self.env.cr.savepoint():
                    tx._starpay_check_status()
            except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
                raise  # the run is aborted; the next one picks these up
            except Exception:  # noqa: BLE001 - one order must not stop the rest
                _logger.exception("Star-Pay check failed for %s.", tx.reference)
        return len(txs)
