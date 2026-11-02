from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import consteq, urls

from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment.logging import get_payment_logger
from odoo.addons.payment_chapa import const
from odoo.addons.payment_chapa.controllers.main import ChapaController

from . import chapa_crypto

_logger = get_payment_logger(__name__)


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    # Direct charge bookkeeping. `requestID` and Chapa's `ref_id` are what
    # the authorize call needs; the rest is for the office reading the record.
    chapa_direct_method = fields.Selection(
        const.DIRECT_METHODS, string="Chapa Wallet", readonly=True, copy=False
    )
    chapa_auth_type = fields.Char(string="Chapa Authorisation", readonly=True, copy=False)
    chapa_request_id = fields.Char(readonly=True, copy=False, groups="base.group_system")
    chapa_portal_url = fields.Char(readonly=True, copy=False)
    # The phone that was prompted, masked: "09••••3456". Enough for the
    # member ("was that the right phone?") and the office, without keeping
    # another copy of a number that is already on the partner.
    chapa_mobile = fields.Char(string="Charged Phone", readonly=True, copy=False)

    def _compute_reference(self, provider_code, prefix=None, separator="-", **kwargs):
        """Override of `payment` to give Chapa a reference it will accept.

        Chapa's `tx_ref` may only contain letters, digits, hyphens, underscores
        and dots. Odoo derives the default reference from the document being
        paid -- `INV/2026/00011-1` -- and the slashes are refused outright with
        HTTP 400 before the member ever sees a checkout page.

        The reference is fixed at the source rather than sanitised on the way
        out, because it is also what the webhook and the verify endpoint hand
        back as `tx_ref`. If the outbound value differed from the stored one,
        `_search_by_reference` could never match the callback to its
        transaction, and paid fees would sit unsettled.
        """
        if provider_code == "chapa":
            prefix = payment_utils.singularize_reference_prefix()
        return super()._compute_reference(
            provider_code, prefix=prefix, separator=separator, **kwargs
        )

    def _chapa_customer_values(self):
        """The customer and order fields both checkout modes send to Chapa."""
        self.ensure_one()
        base_url = self.provider_id.get_base_url()
        first_name, last_name = payment_utils.split_partner_name(self.partner_name)
        values = {
            "amount": str(self.amount),
            "currency": self.currency_id.name,
            # Chapa requires an email; a club member registered by phone alone
            # would otherwise be rejected by the gateway with a vague error.
            "email": self.partner_email or "",
            "first_name": first_name or self.partner_name or "",
            "last_name": last_name or "-",
            "tx_ref": self.reference,
            "callback_url": urls.urljoin(base_url, ChapaController._webhook_url),
            "return_url": urls.urljoin(base_url, ChapaController._return_url),
        }
        if self.partner_phone:
            # Odoo stores phones in display form, "+251 90 012 3456"; Chapa
            # wants the bare number, "+251900123456".
            values["phone_number"] = const.bare_phone(self.partner_phone)
        return values

    def _get_specific_rendering_values(self, processing_values):
        """Override of `payment` to open a Chapa checkout.

        Two modes, chosen on the provider:

        * **api** -- Odoo calls `initialize` with the secret key and redirects
          the member to the session URL Chapa returns.
        * **html** -- the member's browser POSTs the order to Chapa's hosted
          page with the public key. Odoo makes no outbound call at all until
          verification, so a checkout can start even if the server cannot
          reach api.chapa.co at that moment. This is Chapa's documented
          "HTML Checkout" integration.
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != "chapa":
            return res

        values = self._chapa_customer_values()
        if not values["email"]:
            self._set_error(_("Chapa requires an email address for the payer."))
            return {}

        if self.provider_id.chapa_checkout_mode == "html":
            values.update(
                {
                    "api_url": const.HOSTED_PAY_URL,
                    "checkout_mode": "html",
                    "public_key": self.provider_id.chapa_public_key or "",
                    "title": (self.company_id.name or "")[:16],
                    "description": self.reference,
                }
            )
            return values

        # API mode: open the session server-side and redirect to it.
        payload = dict(values)
        payload["customization[title]"] = (self.company_id.name or "")[:16]
        payload["customization[description]"] = self.reference
        try:
            data = self._send_api_request("POST", const.INITIALIZE_PATH, json=payload)
        except ValidationError as error:
            self._set_error(str(error))
            return {}
        if not data.get("checkout_url"):
            self._set_error(_("Chapa did not return a checkout session."))
            return {}
        return {"api_url": data["checkout_url"], "checkout_mode": "api"}

    # --- direct charge -----------------------------------------------------
    #
    # The member stays on our page. `_chapa_direct_charge` asks Chapa to
    # charge the phone; the wallet prompts the member; the browser polls
    # `_chapa_direct_status` until verify says it is settled. Every call from
    # the browser carries `chapa_token`, minted here per transaction, so a
    # reference alone cannot push prompts to somebody's phone.

    def _get_specific_processing_values(self, processing_values):
        res = super()._get_specific_processing_values(processing_values)
        if self.provider_code != "chapa":
            return res
        provider = self.provider_id
        res.update(
            {
                "chapa_direct": provider.chapa_checkout_mode == "direct",
                "chapa_token": self._chapa_token(),
                "chapa_methods": provider._chapa_direct_methods(),
                "chapa_test_mode": provider.state == "test",
            }
        )
        return res

    def _chapa_token(self):
        self.ensure_one()
        return payment_utils.generate_access_token(self.reference, self.id, env=self.env)

    def _chapa_check_token(self, token):
        self.ensure_one()
        return bool(token) and consteq(token, self._chapa_token())

    def _chapa_direct_charge(self, method_code, mobile):
        """Ask Chapa to charge `mobile` through `method_code`.

        Returns what the browser needs next: the authorisation type Chapa
        reported (ussd / otp / portal_view), a message for the member, and
        the portal URL when there is one.
        """
        self.ensure_one()
        provider = self.provider_id
        if self.provider_code != "chapa" or provider.chapa_checkout_mode != "direct":
            raise ValidationError(_("This transaction is not a Chapa direct charge."))
        if self.state not in ("draft", "pending"):
            raise ValidationError(_("This payment can no longer be started."))
        if method_code not in dict(provider._chapa_direct_methods()):
            raise ValidationError(_("That wallet is not offered here."))
        number = const.local_mobile(mobile)
        if not number:
            raise ValidationError(_("Enter an Ethiopian mobile number, for example 0911 234 567."))

        first_name, last_name = payment_utils.split_partner_name(self.partner_name)
        payload = {
            "amount": str(self.amount),
            "currency": self.currency_id.name,
            "tx_ref": self.reference,
            "mobile": number,
            "first_name": first_name or self.partner_name or "",
            "last_name": last_name or "-",
        }
        if self.partner_email:
            payload["email"] = self.partner_email
        data = provider._send_api_request(
            "POST", const.CHARGE_PATH, params={"type": method_code}, data=payload
        )
        meta = data.get("meta") or {}
        if not data or (meta.get("status") or "success") != "success":
            raise ValidationError(meta.get("message") or _("Chapa could not start the charge."))
        auth_type = (data.get("auth_type") or "ussd").lower()
        self.write(
            {
                "chapa_direct_method": method_code,
                "chapa_auth_type": auth_type,
                "chapa_request_id": data.get("requestID") or "",
                "chapa_portal_url": meta.get("portal") or "",
                "chapa_mobile": const.mask_mobile(number),
                "provider_reference": meta.get("ref_id") or self.provider_reference,
            }
        )
        label = const.DIRECT_METHOD_LABELS.get(method_code, method_code)
        if auth_type == "otp":
            message = _("%s sent a one-time code to %s. Enter it below.", label, number)
        elif auth_type == "portal_view":
            message = _("%s opens its own page to approve the payment.", label)
        else:
            message = _("Check %s on the phone ending %s and approve the payment.", label, number[-4:])
        self._set_pending(state_message=message, extra_allowed_states=("pending",))
        return {"auth_type": auth_type, "message": message, "portal_url": self.chapa_portal_url or ""}

    def _chapa_direct_authorize(self, otp):
        """Send the one-time code for a wallet that authorises that way."""
        self.ensure_one()
        provider = self.provider_id
        otp = "".join(ch for ch in (otp or "") if ch.isdigit())
        if not otp:
            raise ValidationError(_("Enter the code the wallet sent you."))
        if not self.chapa_request_id or not self.provider_reference:
            raise ValidationError(_("Start the payment before entering a code."))
        if not provider.chapa_encryption_key:
            raise ValidationError(_("This wallet needs the Chapa Encryption Key to be configured."))
        client = chapa_crypto.encrypt_client(
            # Chapa's example sends the code as a number.
            provider.chapa_encryption_key, {"requestID": self.chapa_request_id, "otp": int(otp)}
        )
        body = provider._send_api_request(
            "POST",
            const.VALIDATE_PATH,
            params={"type": self.chapa_direct_method},
            data={"reference": self.provider_reference, "client": client},
            raw=True,
        )
        message = (body or {}).get("message") or ""
        if (body or {}).get("status") == "failed":
            raise ValidationError(message or _("Chapa refused the code."))
        return self._chapa_direct_status()

    def _chapa_direct_status(self):
        """Verify with Chapa and settle; what the browser polls."""
        self.ensure_one()
        if self.state in ("draft", "pending", "authorized"):
            try:
                verified = self._chapa_verify()
            except ValidationError as error:
                # Chapa may not know the tx_ref for a moment after the charge;
                # that is "still pending", not an error to show the member.
                _logger.info("Chapa verify not ready for %s: %s", self.reference, error)
                verified = None
            if verified:
                self._process("chapa", verified)
                if self.state == "done":
                    try:
                        with self.env.cr.savepoint(flush=False):
                            self._post_process()
                    except Exception:  # noqa: BLE001 - confirmed paid; the cron retries
                        _logger.exception(
                            "Transaction %s confirmed but not post-processed; the cron will retry.",
                            self.reference,
                        )
        return {"state": self.state, "message": self.state_message or ""}

    @api.model
    def _extract_reference(self, provider_code, payment_data):
        """Override of `payment` to extract the reference from the payment data."""
        if provider_code != "chapa":
            return super()._extract_reference(provider_code, payment_data)
        return payment_data.get("tx_ref") or payment_data.get("trx_ref")

    def _extract_amount_data(self, payment_data):
        """Override of `payment` to extract the amount and currency."""
        if self.provider_code != "chapa":
            return super()._extract_amount_data(payment_data)
        amount = payment_data.get("amount")
        if amount is None:
            return None
        return {
            "amount": float(amount),
            "currency_code": payment_data.get("currency") or self.currency_id.name,
        }

    def _apply_updates(self, payment_data):
        """Override of `payment` to update the transaction from the payment data."""
        if self.provider_code != "chapa":
            return super()._apply_updates(payment_data)

        if payment_data.get("reference"):
            self.provider_reference = payment_data["reference"]

        # A webhook reports an `event`; the verify endpoint reports a `status`.
        status = (payment_data.get("event") or payment_data.get("status") or "").lower()
        if status in const.PAYMENT_STATUS_MAPPING["pending"]:
            self._set_pending()
        elif status in const.PAYMENT_STATUS_MAPPING["done"]:
            self._set_done()
        elif status in const.PAYMENT_STATUS_MAPPING["cancel"]:
            self._set_canceled()
        elif status in const.PAYMENT_STATUS_MAPPING["error"]:
            self._set_error(
                _("Chapa reported that the payment failed (status %s).", status)
            )
        else:
            _logger.warning(
                "Received data with invalid payment status (%s) for transaction %s.",
                status,
                self.reference,
            )
            self._set_error(_("Unknown Chapa payment status: %s", status))

    def _chapa_verify(self):
        """Ask Chapa for the authoritative status of this transaction."""
        self.ensure_one()
        return self._send_api_request(
            "GET", f"{const.VERIFY_PATH}/{self.reference}"
        )

    # --- verification ------------------------------------------------------
    #
    # Chapa tells us a payment succeeded two ways: a webhook, and the verify
    # endpoint. The webhook needs a public URL it can reach; on a laptop, or
    # behind a firewall, or when it simply fails, nothing arrives and the
    # transaction sits in draft while the money has moved. Verify is the
    # authority either way, so it is exposed as an action and run on a
    # schedule for anything the webhook never confirmed.

    def action_chapa_verify(self):
        """Ask Chapa what happened and settle accordingly.

        On success the transaction is marked done AND post-processed straight
        away, rather than leaving that to the status-page poll or the
        post-processing cron: post-processing is what posts the payment to
        the journal, reconciles the invoice, and marks the membership fee
        paid. Someone pressing this button wants all of that to happen now.

        Errors are recorded on the transaction, never raised, so a batch does
        not stop at the first one Chapa has never heard of.
        """
        for tx in self.filtered(lambda t: t.provider_code == "chapa"):
            if tx.state == "done":
                continue
            try:
                data = tx._chapa_verify()
            except ValidationError as error:
                # Chapa has no record of it, or refused the key. The member
                # may simply never have reached the checkout page.
                tx._set_error(_("Chapa verification failed: %s", error))
                continue
            if not data:
                tx._set_error(_("Chapa returned no data for this transaction."))
                continue
            tx._process("chapa", data)
            if tx.state == "done":
                # Chapa has confirmed the money moved; that is the fact this
                # record exists to hold, and nothing downstream may undo it.
                # Post-processing -- journal, reconciliation, membership -- is
                # attempted now for immediacy, but a failure there (a missing
                # payment method line, an unconfigured journal) is recorded
                # and left for the post-processing cron, which retries
                # anything done-but-not-post-processed. Letting it raise here
                # rolled back the `done` state on the first live payment.
                try:
                    with self.env.cr.savepoint(flush=False):
                        tx._post_process()
                except Exception as error:  # noqa: BLE001 - see above
                    _logger.exception(
                        "Transaction %s is confirmed paid but could not be "
                        "post-processed; the cron will retry.",
                        tx.reference,
                    )
                    # payment.transaction has no chatter; state_message is
                    # what the form shows, so the office sees why.
                    tx.state_message = _(
                        "Payment confirmed by Chapa. Posting it to accounting "
                        "failed and will be retried automatically: %s",
                        error,
                    )
        return True

    @api.model
    def _cron_chapa_verify_pending(self):
        """Re-check transactions the webhook never confirmed.

        Scoped to a window: newer than VERIFY_WITHIN_DAYS (older ones are
        abandoned checkouts, and Chapa would not remember them anyway) and
        older than VERIFY_AFTER_MINUTES (a member still on the payment page
        should be left to finish). Anything in between that is still draft or
        pending is asked about.
        """
        from datetime import timedelta

        from odoo import fields

        now = fields.Datetime.now()
        candidates = self.search(
            [
                ("provider_code", "=", "chapa"),
                ("state", "in", ("draft", "pending")),
                ("create_date", "<=", now - timedelta(minutes=const.VERIFY_AFTER_MINUTES)),
                ("create_date", ">=", now - timedelta(days=const.VERIFY_WITHIN_DAYS)),
            ]
        )
        candidates.action_chapa_verify()
        return len(candidates)
