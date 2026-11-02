from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.urls import urljoin as url_join

from odoo.addons.payment_chapa import const


class PaymentProvider(models.Model):
    _inherit = "payment.provider"

    code = fields.Selection(
        selection_add=[("chapa", "Chapa")], ondelete={"chapa": "set default"}
    )
    chapa_secret_key = fields.Char(
        string="Chapa Secret Key",
        required_if_provider="chapa",
        copy=False,
        groups="base.group_system",
        help="CHASECK_TEST-... in test mode, CHASECK-... in production.",
    )
    chapa_webhook_secret = fields.Char(
        string="Chapa Webhook Secret",
        copy=False,
        groups="base.group_system",
        help=(
            "The secret hash configured on the Chapa dashboard. Used to verify "
            "the x-chapa-signature header. Without it, webhooks are refused."
        ),
    )
    chapa_public_key = fields.Char(
        string="Chapa Public Key",
        copy=False,
        help=(
            "CHAPUBK_TEST-... in test mode, CHAPUBK-... in production. Only "
            "needed for the HTML checkout, where the browser posts the order "
            "to Chapa directly. Safe to expose: it cannot verify or refund."
        ),
    )
    # Direct is the default: the member never leaves the club's page. They
    # pick a wallet and type their phone number here, Odoo asks Chapa to
    # charge it, and the wallet prompts them on the phone. The two hosted
    # modes stay for a merchant account without Direct Charge enabled.
    chapa_checkout_mode = fields.Selection(
        [
            ("direct", "Direct charge (pay on our page, approve on the phone)"),
            ("html", "Self-hosted HTML checkout form"),
            ("api", "Hosted checkout via API"),
        ],
        string="Checkout Mode",
        default="direct",
        required=True,
        help=(
            "Direct (default): Chapa's Direct Charge API. The member chooses a "
            "wallet and enters a phone number on this site; the wallet pushes "
            "an approval prompt to the phone (telebirr, CBE Birr, M-Pesa, "
            "Coopay-Ebirr, Yaya), asks for a one-time code (Awash Birr), or "
            "opens the bank's portal (Enat). HTML: the member's browser posts "
            "the order to Chapa's hosted checkout page with the public key. "
            "API: Odoo opens a hosted session with the secret key and "
            "redirects to it. All three verify server-side with the secret key."
        ),
    )
    chapa_direct_methods = fields.Char(
        string="Direct Charge Wallets",
        default=const.DEFAULT_DIRECT_METHODS,
        help=(
            "Comma-separated wallet codes offered in direct mode, in order. "
            "Available: " + ", ".join(f"{code} ({label})" for code, label in const.DIRECT_METHODS) + ". "
            "awashbirr needs the encryption key; enat_bank opens the bank's "
            "own page in a new tab."
        ),
    )
    chapa_encryption_key = fields.Char(
        string="Chapa Encryption Key",
        copy=False,
        groups="base.group_system",
        help=(
            "From Settings > API on the Chapa dashboard. Only needed for wallets "
            "that authorise with a one-time code (Awash Birr): the code is sent "
            "to Chapa encrypted with it."
        ),
    )

    @api.constrains("chapa_checkout_mode", "chapa_direct_methods", "chapa_encryption_key", "state")
    def _check_direct_methods(self):
        for provider in self.filtered(lambda p: p.code == "chapa"):
            codes = provider._chapa_direct_method_codes()
            unknown = [c for c in codes if c not in const.DIRECT_METHOD_LABELS]
            if unknown:
                raise ValidationError(
                    _("Unknown Chapa wallet code(s): %s. Use: %s")
                    % (", ".join(unknown), ", ".join(c for c, _l in const.DIRECT_METHODS))
                )
            if provider.chapa_checkout_mode == "direct" and provider.state != "disabled":
                if not codes:
                    raise ValidationError(_("Direct charge needs at least one wallet to offer."))
                if "awashbirr" in codes and not provider.chapa_encryption_key:
                    raise ValidationError(
                        _("Awash Birr authorises with a one-time code, which needs the Chapa Encryption Key.")
                    )

    def _chapa_direct_method_codes(self):
        self.ensure_one()
        return [c.strip().lower() for c in (self.chapa_direct_methods or "").split(",") if c.strip()]

    def _chapa_direct_methods(self):
        """(code, label) pairs to offer on the page, in the configured order."""
        self.ensure_one()
        return [(c, const.DIRECT_METHOD_LABELS[c]) for c in self._chapa_direct_method_codes() if c in const.DIRECT_METHOD_LABELS]

    def _should_build_inline_form(self, is_validation=False):
        """Override of `payment`: the inline form is the direct-charge form."""
        if self.code != "chapa":
            return super()._should_build_inline_form(is_validation=is_validation)
        return self.chapa_checkout_mode == "direct" and not is_validation

    @api.constrains("chapa_checkout_mode", "chapa_public_key", "state")
    def _check_html_checkout_has_public_key(self):
        """The HTML form is signed with the public key; without it the form
        posts and Chapa refuses, which the member sees and the office does
        not. Catch it at configuration time instead."""
        for provider in self:
            if (
                provider.code == "chapa"
                and provider.state != "disabled"
                and provider.chapa_checkout_mode == "html"
                and not provider.chapa_public_key
            ):
                raise ValidationError(
                    _("The HTML checkout needs the Chapa Public Key.")
                )

    # === COMPUTE METHODS === #

    def _get_supported_currencies(self):
        """Override of `payment` to return the supported currencies."""
        supported_currencies = super()._get_supported_currencies()
        if self.code == "chapa":
            supported_currencies = supported_currencies.filtered(
                lambda c: c.name in const.SUPPORTED_CURRENCIES
            )
        return supported_currencies

    # === CRUD METHODS === #

    def _get_default_payment_method_codes(self):
        """Override of `payment` to return the default payment method codes."""
        self.ensure_one()
        if self.code != "chapa":
            return super()._get_default_payment_method_codes()
        return const.DEFAULT_PAYMENT_METHOD_CODES

    # === REQUEST HELPERS === #

    def _build_request_url(self, endpoint, **kwargs):
        """Override of `payment` to build the request URL."""
        if self.code != "chapa":
            return super()._build_request_url(endpoint, **kwargs)
        return url_join(f"{const.API_BASE_URL}/", endpoint)

    def _build_request_headers(self, *args, **kwargs):
        """Override of `payment` to build the request headers."""
        if self.code != "chapa":
            return super()._build_request_headers(*args, **kwargs)
        return {"Authorization": f"Bearer {self.chapa_secret_key}"}

    def _parse_response_error(self, response):
        """Override of `payment` to parse the error message.

        Chapa reports two shapes. A plain string for most failures, and for
        validation a dict of field -> [reasons]:

            {"message": {"email": ["validation.email"],
                         "tx_ref": ["may only contain letters, numbers, ..."]}}

        Handed on as-is, Odoo rendered that dict as "email and tx_ref" -- the
        field names with every explanation dropped, which is exactly the part
        somebody debugging it needs. Flatten it into readable lines instead.
        """
        if self.code != "chapa":
            return super()._parse_response_error(response)
        try:
            message = response.json().get("message", "")
        except ValueError:
            return response.text[:200]
        if isinstance(message, dict):
            return "; ".join(
                f"{field}: {' '.join(map(str, reasons)) if isinstance(reasons, list) else reasons}"
                for field, reasons in message.items()
            )
        return str(message)

    def _parse_response_content(self, response, **kwargs):
        """Override of `payment` to parse the response content.

        Chapa reports failure in the envelope as well as by status code, so a
        non-success envelope is not handed on as if it were data.
        """
        if self.code != "chapa":
            return super()._parse_response_content(response, **kwargs)
        body = response.json()
        if kwargs.get("raw"):
            # `/v1/validate` answers {"message": "Payment is completed",
            # "trx_ref": ...} with no status or data envelope at all.
            return body
        if body.get("status") != "success":
            return {}
        return body.get("data") or {}
