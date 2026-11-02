from odoo import _, api, models
from odoo.exceptions import ValidationError
from odoo.tools import urls

from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment.logging import get_payment_logger
from odoo.addons.payment_telebirr import const, telebirr_crypto
from odoo.addons.payment_telebirr.controllers.main import TelebirrController

_logger = get_payment_logger(__name__)


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    def _compute_reference(self, provider_code, prefix=None, separator="-", **kwargs):
        """Override of `payment` to send telebirr a reference it will accept.

        The reference goes out as `merch_order_id`. Odoo derives the default
        from the document being paid -- `INV/2026/00011-1` -- and Chapa was
        found to refuse the slashes outright. telebirr's rules for
        `merch_order_id` are not confirmed here (the merchant docs are
        login-gated), but an alphanumeric-and-hyphen reference is safe under
        any plausible rule, and nothing is lost: the transaction is linked to
        its invoice regardless of what the reference says.
        """
        if provider_code == "telebirr":
            prefix = payment_utils.singularize_reference_prefix()
        return super()._compute_reference(
            provider_code, prefix=prefix, separator=separator, **kwargs
        )

    def _get_specific_rendering_values(self, processing_values):
        """Override of `payment` to open a telebirr checkout session.

        telebirr is a three-step redirect flow, unlike most gateways:
          1. exchange the app secret for a short-lived fabric token,
          2. create a pre-order, which returns a prepay id,
          3. redirect the customer to the H5 page built from that prepay id.
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != "telebirr":
            return res

        provider = self.provider_id
        base_url = provider.get_base_url()
        try:
            fabric_token = provider._telebirr_get_fabric_token()
            payload = self._telebirr_prepare_preorder_payload(base_url)
            body = self._send_api_request(
                "POST", const.PREORDER_PATH, json=payload, fabric_token=fabric_token
            )
        except ValidationError as error:
            self._set_error(str(error))
            return {}

        prepay_id = ((body or {}).get("biz_content") or {}).get("prepay_id")
        if not prepay_id:
            self._set_error(_("telebirr did not return a checkout session."))
            return {}

        return {"api_url": self._telebirr_build_checkout_url(prepay_id)}

    def _telebirr_prepare_preorder_payload(self, base_url):
        """Build and sign the pre-order request."""
        self.ensure_one()
        provider = self.provider_id.sudo()
        payload = {
            "timestamp": telebirr_crypto.timestamp(),
            "nonce_str": telebirr_crypto.nonce_str(),
            "method": "payment.preorder",
            "version": "1.0",
            "biz_content": {
                "notify_url": urls.urljoin(base_url, TelebirrController._webhook_url),
                "redirect_url": urls.urljoin(base_url, TelebirrController._return_url),
                "appid": provider.telebirr_merchant_app_id,
                "merch_code": provider.telebirr_short_code,
                "merch_order_id": self.reference,
                "trade_type": "Checkout",
                "title": self.reference,
                "total_amount": f"{self.amount:.2f}",
                "trans_currency": self.currency_id.name,
                "timeout_express": "30m",
                "business_type": "BuyGoods",
                "payee_identifier": provider.telebirr_short_code,
                "payee_identifier_type": "04",
                "payee_type": "5000",
            },
            "sign_type": telebirr_crypto.SIGN_TYPE,
        }
        payload["sign"] = telebirr_crypto.sign(payload, provider.telebirr_private_key)
        return payload

    def _telebirr_build_checkout_url(self, prepay_id):
        """Build the signed H5 URL the customer is redirected to."""
        self.ensure_one()
        provider = self.provider_id.sudo()
        params = {
            "appid": provider.telebirr_merchant_app_id,
            "merch_code": provider.telebirr_short_code,
            "nonce_str": telebirr_crypto.nonce_str(),
            "prepay_id": prepay_id,
            "timestamp": telebirr_crypto.timestamp(),
        }
        signature = telebirr_crypto.sign(
            dict(params, sign_type=telebirr_crypto.SIGN_TYPE),
            provider.telebirr_private_key,
        )
        raw = "&".join(f"{key}={value}" for key, value in params.items())
        base = (provider.telebirr_h5_url or "").rstrip("&?")
        separator = "&" if "?" in base else "?"
        return (
            f"{base}{separator}{raw}"
            f"&sign={signature}&sign_type={telebirr_crypto.SIGN_TYPE}"
            f"{const.CHECKOUT_QUERY_SUFFIX}"
        )

    @api.model
    def _extract_reference(self, provider_code, payment_data):
        """Override of `payment` to extract the reference from the payment data."""
        if provider_code != "telebirr":
            return super()._extract_reference(provider_code, payment_data)
        content = payment_data.get("biz_content") or payment_data
        return content.get("merch_order_id") or content.get("outTradeNo")

    def _extract_amount_data(self, payment_data):
        """Override of `payment` to extract the amount and currency."""
        if self.provider_code != "telebirr":
            return super()._extract_amount_data(payment_data)
        content = payment_data.get("biz_content") or payment_data
        amount = content.get("total_amount") or content.get("totalAmount")
        if amount is None:
            return None
        return {
            "amount": float(amount),
            "currency_code": content.get("trans_currency") or self.currency_id.name,
        }

    def _apply_updates(self, payment_data):
        """Override of `payment` to update the transaction from the payment data."""
        if self.provider_code != "telebirr":
            return super()._apply_updates(payment_data)

        content = payment_data.get("biz_content") or payment_data
        if content.get("trade_no"):
            self.provider_reference = content["trade_no"]

        status = content.get("trade_status") or content.get("status") or ""
        if status in const.PAYMENT_STATUS_MAPPING["pending"]:
            self._set_pending()
        elif status in const.PAYMENT_STATUS_MAPPING["done"]:
            self._set_done()
        elif status in const.PAYMENT_STATUS_MAPPING["cancel"]:
            self._set_canceled()
        elif status in const.PAYMENT_STATUS_MAPPING["error"]:
            self._set_error(
                _("telebirr reported that the payment failed (status %s).", status)
            )
        else:
            _logger.warning(
                "Received data with invalid payment status (%s) for transaction %s.",
                status,
                self.reference,
            )
            self._set_error(_("Unknown telebirr payment status: %s", status))
