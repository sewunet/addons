import pprint

from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.http import request

from odoo.addons.payment.logging import get_payment_logger
from odoo.addons.payment_telebirr import telebirr_crypto

_logger = get_payment_logger(__name__)


class TelebirrController(http.Controller):
    _return_url = "/payment/telebirr/return"
    _webhook_url = "/payment/telebirr/webhook"

    @http.route(_return_url, type="http", methods=["GET", "POST"], auth="public", csrf=False)
    def telebirr_return_from_checkout(self, **data):
        """Handle the customer returning from the telebirr H5 page.

        The customer controls this trip, so nothing here moves the transaction:
        state comes from the signed notify callback.
        """
        _logger.info("Handling redirection from telebirr with data:\n%s", pprint.pformat(data))
        return request.redirect("/payment/status")

    @http.route(_webhook_url, type="http", methods=["POST"], auth="public", csrf=False)
    def telebirr_webhook(self):
        """Handle the signed notify callback from telebirr."""
        data = request.get_json_data()
        _logger.info("Notification received from telebirr with data:\n%s", pprint.pformat(data))

        tx_sudo = request.env["payment.transaction"].sudo()._search_by_reference("telebirr", data)
        if not tx_sudo:
            return request.make_json_response("")

        self._verify_signature(data, tx_sudo)
        tx_sudo._process("telebirr", data)
        return request.make_json_response("")

    @staticmethod
    def _verify_signature(data, tx_sudo):
        """Check the notify signature against Ethio Telecom's public key.

        Refusing when no public key is configured is deliberate: an unverified
        notification must never be able to mark a payment done.

        :raise Forbidden: if the signature is missing, unverifiable or wrong.
        """
        public_key = tx_sudo.provider_id.sudo().telebirr_public_key
        if not public_key:
            _logger.warning(
                "Cannot verify telebirr notification: no public key is configured."
            )
            raise Forbidden()
        signature = data.get("sign")
        if not telebirr_crypto.verify(data, signature, public_key):
            _logger.warning("Received telebirr payment data with an invalid signature.")
            raise Forbidden()
