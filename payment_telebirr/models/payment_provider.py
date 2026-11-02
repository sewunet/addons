from odoo import fields, models
from odoo.exceptions import ValidationError

from odoo.addons.payment_telebirr import const


class PaymentProvider(models.Model):
    _inherit = "payment.provider"

    code = fields.Selection(
        selection_add=[("telebirr", "telebirr")], ondelete={"telebirr": "set default"}
    )
    telebirr_base_url = fields.Char(
        string="telebirr Gateway URL",
        required_if_provider="telebirr",
        copy=False,
        help="Merchant gateway host issued by Ethio Telecom, without a trailing slash.",
    )
    telebirr_h5_url = fields.Char(
        string="telebirr Checkout URL",
        required_if_provider="telebirr",
        copy=False,
        help="H5 checkout base that the customer is redirected to, ending in '?'.",
    )
    telebirr_fabric_app_id = fields.Char(
        string="Fabric App ID", required_if_provider="telebirr", copy=False
    )
    telebirr_app_secret = fields.Char(
        string="App Secret",
        required_if_provider="telebirr",
        copy=False,
        groups="base.group_system",
    )
    telebirr_merchant_app_id = fields.Char(
        string="Merchant App ID", required_if_provider="telebirr", copy=False
    )
    telebirr_short_code = fields.Char(
        string="Merchant Short Code", required_if_provider="telebirr", copy=False
    )
    telebirr_private_key = fields.Text(
        string="Merchant Private Key (PEM)",
        required_if_provider="telebirr",
        copy=False,
        groups="base.group_system",
        help="PEM private key used to sign requests. Never leaves the server.",
    )
    telebirr_public_key = fields.Text(
        string="telebirr Public Key (PEM)",
        copy=False,
        groups="base.group_system",
        help="Ethio Telecom's public key, used to verify notify callbacks.",
    )

    # === COMPUTE METHODS === #

    def _get_supported_currencies(self):
        """Override of `payment` to return the supported currencies."""
        supported_currencies = super()._get_supported_currencies()
        if self.code == "telebirr":
            supported_currencies = supported_currencies.filtered(
                lambda c: c.name in const.SUPPORTED_CURRENCIES
            )
        return supported_currencies

    # === CRUD METHODS === #

    def _get_default_payment_method_codes(self):
        """Override of `payment` to return the default payment method codes."""
        self.ensure_one()
        if self.code != "telebirr":
            return super()._get_default_payment_method_codes()
        return const.DEFAULT_PAYMENT_METHOD_CODES

    # === REQUEST HELPERS === #

    def _build_request_url(self, endpoint, **kwargs):
        """Override of `payment` to build the request URL."""
        if self.code != "telebirr":
            return super()._build_request_url(endpoint, **kwargs)
        return f"{(self.telebirr_base_url or '').rstrip('/')}/{endpoint.lstrip('/')}"

    def _build_request_headers(self, *args, fabric_token=None, **kwargs):
        """Override of `payment` to build the request headers.

        Every call but the token request itself carries a short-lived fabric
        token alongside the app id.
        """
        if self.code != "telebirr":
            return super()._build_request_headers(*args, **kwargs)
        headers = {
            "Content-Type": "application/json",
            "X-APP-Key": self.telebirr_fabric_app_id or "",
        }
        if fabric_token:
            headers["Authorization"] = fabric_token
        return headers

    def _parse_response_error(self, response):
        """Override of `payment` to parse the error message."""
        if self.code != "telebirr":
            return super()._parse_response_error(response)
        body = response.json()
        return body.get("msg") or body.get("error_msg") or ""

    def _parse_response_content(self, response, **kwargs):
        """Override of `payment` to parse the response content."""
        if self.code != "telebirr":
            return super()._parse_response_content(response, **kwargs)
        return response.json()

    # === BUSINESS METHODS === #

    def _telebirr_get_fabric_token(self):
        """Fetch the short-lived fabric token that authorises the other calls."""
        self.ensure_one()
        body = self._send_api_request(
            "POST",
            const.TOKEN_PATH,
            json={"appSecret": self.sudo().telebirr_app_secret},
        )
        token = (body or {}).get("token")
        if not token:
            raise ValidationError(
                self.env._("telebirr did not return a fabric token.")
            )
        return token
