import hashlib
import hmac
import pprint

from werkzeug.exceptions import Forbidden

from odoo import _, http
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.payment.logging import get_payment_logger

_logger = get_payment_logger(__name__)


class ChapaController(http.Controller):
    _return_url = "/payment/chapa/return"
    _webhook_url = "/payment/chapa/webhook"

    @http.route(_return_url, type="http", methods=["GET", "POST"], auth="public", csrf=False)
    def chapa_return_from_checkout(self, **data):
        """Handle the customer coming back from the Chapa checkout page.

        The customer controls this trip, so the gateway is asked for the real
        status rather than the query string being trusted.
        """
        _logger.info("Handling redirection from Chapa with data:\n%s", pprint.pformat(data))
        self._verify_and_process(data)
        return request.redirect("/payment/status")

    # --- direct charge -----------------------------------------------------
    #
    # Three JSON routes the payment form calls while the member stays on our
    # page: start the charge, send a one-time code, and ask whether the phone
    # has approved it yet. Each one carries the per-transaction token minted
    # in the processing values; a bare reference opens nothing.

    _charge_url = "/payment/chapa/charge"
    _authorize_url = "/payment/chapa/authorize"
    _poll_url = "/payment/chapa/poll"

    @staticmethod
    def _direct_tx(reference, token):
        tx_sudo = request.env["payment.transaction"].sudo().search(
            [("reference", "=", reference), ("provider_code", "=", "chapa")], limit=1
        )
        if not tx_sudo or not tx_sudo._chapa_check_token(token):
            raise ValidationError(_("This payment could not be found."))
        return tx_sudo

    @http.route(_charge_url, type="jsonrpc", auth="public")
    def chapa_direct_charge(self, reference, token, type, mobile, **kwargs):
        tx_sudo = self._direct_tx(reference, token)
        return tx_sudo._chapa_direct_charge(type, mobile)

    @http.route(_authorize_url, type="jsonrpc", auth="public")
    def chapa_direct_authorize(self, reference, token, otp, **kwargs):
        tx_sudo = self._direct_tx(reference, token)
        return tx_sudo._chapa_direct_authorize(otp)

    @http.route(_poll_url, type="jsonrpc", auth="public")
    def chapa_direct_poll(self, reference, token, **kwargs):
        tx_sudo = self._direct_tx(reference, token)
        return tx_sudo._chapa_direct_status()

    @http.route(_webhook_url, type="http", methods=["POST"], auth="public", csrf=False)
    def chapa_webhook(self):
        """Handle a Chapa webhook notification."""
        raw_body = request.httprequest.get_data()
        data = request.get_json_data()
        _logger.info("Notification received from Chapa with data:\n%s", pprint.pformat(data))

        tx_sudo = request.env["payment.transaction"].sudo()._search_by_reference("chapa", data)
        if not tx_sudo:
            return request.make_json_response("")

        self._verify_signature(raw_body, tx_sudo)
        self._verify_and_process(data, tx_sudo=tx_sudo)
        return request.make_json_response("")

    @staticmethod
    def _verify_signature(raw_body, tx_sudo):
        """Check the `x-chapa-signature` header against the raw request body.

        Chapa sends two headers. `chapa-signature` is an HMAC of the secret key
        *with itself*, so it is a constant per merchant and does not bind the
        payload -- anyone who ever saw it could replay any body. Only
        `x-chapa-signature`, which signs the payload, is checked here.

        The raw bytes are used because Chapa signs `JSON.stringify(body)`, and
        re-serialising a parsed dict in Python does not reproduce those bytes.

        :raise Forbidden: if the secret is unset, or the signature is missing
                          or wrong.
        """
        secret = tx_sudo.provider_id.chapa_webhook_secret
        if not secret:
            _logger.warning(
                "Cannot verify Chapa notification: no webhook secret is configured."
            )
            raise Forbidden()

        received = request.httprequest.headers.get("x-chapa-signature")
        if not received:
            _logger.warning("Received Chapa payment data with no x-chapa-signature.")
            raise Forbidden()

        body = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body
        expected = hmac.new(
            secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, received.strip().lower()):
            _logger.warning("Received Chapa payment data with an invalid signature.")
            raise Forbidden()

    @staticmethod
    def _verify_and_process(data, tx_sudo=None):
        """Ask Chapa for the authoritative status, then process it."""
        if tx_sudo is None:
            tx_sudo = request.env["payment.transaction"].sudo()._search_by_reference(
                "chapa", data
            )
        if not tx_sudo:
            return
        try:
            verified = tx_sudo._chapa_verify()
        except ValidationError:
            _logger.error("Unable to verify the Chapa payment data.")
        else:
            if verified:
                tx_sudo._process("chapa", verified)
                # Post-process here rather than leaving it to the status
                # page's poll: this is what posts the payment to the journal,
                # reconciles the invoice and marks the membership fee paid.
                # A member arriving back from Chapa should find all of that
                # already done.
                if tx_sudo.state == "done":
                    try:
                        with request.env.cr.savepoint(flush=False):
                            tx_sudo._post_process()
                    except Exception:  # noqa: BLE001 - the payment is confirmed; see action_chapa_verify
                        _logger.exception(
                            "Transaction %s confirmed but not post-processed; the cron will retry.",
                            tx_sudo.reference,
                        )
