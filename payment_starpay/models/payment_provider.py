import threading
import time

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import urls

from odoo.addons.payment_starpay import const
from odoo.addons.payment_starpay.controllers.main import StarPayController

#: Carries the request from _log_request to _log_response, per thread.
_pending = threading.local()


class PaymentProvider(models.Model):
    _inherit = "payment.provider"

    code = fields.Selection(
        selection_add=[("starpay", "Star-Pay")], ondelete={"starpay": "set default"}
    )
    starpay_base_url = fields.Char(
        string="Star-Pay API URL",
        default=const.SANDBOX_BASE_URL,
        required_if_provider="starpay",
        copy=False,
        help=(
            "API base without a trailing slash. "
            "Sandbox: https://sandbox-api.starpayethiopia.com/v1/starpay-api. "
            "Production: https://api.starpayethiopia.com, with no /v1/starpay-api. "
            "The two are separate accounts with separately generated keys. A "
            "key from one environment sent to the other is unrecognised, and "
            "Star-Pay reports that as GEN_004, 'Something went wrong', which "
            "looks like a fault in the request and is not one. Production also "
            "needs API Gateway switched on for the merchant, which is a step "
            "after account approval, not part of it."
        ),
    )
    starpay_api_secret = fields.Char(
        string="Star-Pay API Secret",
        required_if_provider="starpay",
        copy=False,
        groups="base.group_system",
    )
    starpay_webhook_secret = fields.Char(
        string="Star-Pay Webhook Secret",
        required_if_provider="starpay",
        copy=False,
        groups="base.group_system",
        help="Verifies the HMAC-SHA256 signature on Star-Pay callbacks.",
    )
    starpay_product_id = fields.Char(
        string="Star-Pay Product ID",
        copy=False,
        help=(
            "The product every membership fee is booked against in Star-Pay's "
            "own catalogue, if the merchant account has one. Star-Pay requires "
            "a productId on each order line and its examples are 24-character "
            "ids. Left empty, one is derived from the fee's reference, which "
            "is enough for a merchant that does not keep a catalogue. If "
            "Star-Pay refuses the order, paste the id from the merchant "
            "portal here."
        ),
    )

    starpay_redirect_url = fields.Char(
        string="Star-Pay Redirect URL",
        compute="_compute_starpay_redirect_url",
        store=True,
        readonly=False,
        copy=False,
        help=(
            "Where Star-Pay sends the payer back after its checkout page, sent "
            "as redirectUrl on every order. Filled in with this server's own "
            "return page; change it when the club is reached at another "
            "address than the one Odoo generated it from. Left empty, orders "
            "use the generated address. It is only the way back: the fee is "
            "settled from the callback and from Star-Pay's own answer, never "
            "from the return trip."
        ),
    )
    starpay_callback_url = fields.Char(
        string="Star-Pay Callback URL",
        compute="_compute_starpay_callback_url",
        store=True,
        readonly=False,
        copy=False,
        help=(
            "Where Star-Pay posts the payment result, sent as callbackURL on "
            "every order. This is what settles a payment the moment it is "
            "made, so it must be an address Star-Pay can reach from the "
            "internet: a server whose base URL is an internal address "
            "(http://172.16.x.x) generates one Star-Pay cannot call, and "
            "payments then wait for the payer's return or the 15-minute "
            "check. Filled in from the base URL; change it to the public "
            "domain. Left empty, orders use the generated address."
        ),
    )

    # === COMPUTE METHODS === #

    @api.depends("code")
    def _compute_starpay_redirect_url(self):
        """Fill the generated address once, then leave the field to whoever edits it."""
        for provider in self:
            if provider.code == "starpay" and not provider.starpay_redirect_url:
                provider.starpay_redirect_url = provider._starpay_default_redirect_url()
            else:
                provider.starpay_redirect_url = provider.starpay_redirect_url

    @api.depends("code")
    def _compute_starpay_callback_url(self):
        """Fill the generated address once, then leave the field to whoever edits it."""
        for provider in self:
            if provider.code == "starpay" and not provider.starpay_callback_url:
                provider.starpay_callback_url = provider._starpay_default_callback_url()
            else:
                provider.starpay_callback_url = provider.starpay_callback_url

    def _starpay_default_callback_url(self):
        """This server's Star-Pay callback endpoint, from the base URL Odoo knows."""
        self.ensure_one()
        return urls.urljoin(self.get_base_url(), StarPayController._webhook_url)

    def _starpay_callback_url(self):
        """What an order sends as callbackURL: the field, or the generated address."""
        self.ensure_one()
        return (self.starpay_callback_url or "").strip() or self._starpay_default_callback_url()

    def _starpay_default_redirect_url(self):
        """This server's Star-Pay return page, from the base URL Odoo knows."""
        self.ensure_one()
        return urls.urljoin(self.get_base_url(), StarPayController._return_url)

    def _starpay_redirect_url(self):
        """What an order sends as redirectUrl: the field, or the generated address."""
        self.ensure_one()
        return (self.starpay_redirect_url or "").strip() or self._starpay_default_redirect_url()

    @api.constrains("starpay_redirect_url", "starpay_callback_url")
    def _check_starpay_urls(self):
        for provider in self:
            for label, value in (
                (_("redirect URL"), provider.starpay_redirect_url),
                (_("callback URL"), provider.starpay_callback_url),
            ):
                url = (value or "").strip()
                if url and not url.lower().startswith(("https://", "http://")):
                    raise ValidationError(
                        _("The Star-Pay %s must be a full address starting with https://.", label)
                    )

    def _get_supported_currencies(self):
        """Override of `payment` to return the supported currencies."""
        supported_currencies = super()._get_supported_currencies()
        if self.code == "starpay":
            supported_currencies = supported_currencies.filtered(
                lambda c: c.name in const.SUPPORTED_CURRENCIES
            )
        return supported_currencies

    # === CRUD METHODS === #

    def _get_default_payment_method_codes(self):
        """Override of `payment` to return the default payment method codes."""
        self.ensure_one()
        if self.code != "starpay":
            return super()._get_default_payment_method_codes()
        return const.DEFAULT_PAYMENT_METHOD_CODES

    # === REQUEST HELPERS === #

    def _build_request_url(self, endpoint, **kwargs):
        """Override of `payment` to build the request URL."""
        if self.code != "starpay":
            return super()._build_request_url(endpoint, **kwargs)
        return f"{(self.starpay_base_url or '').rstrip('/')}/{endpoint.lstrip('/')}"

    def _build_request_headers(self, *args, **kwargs):
        """Override of `payment` to build the request headers.

        The secret travels in `x-api-secret` on every call, the order and the
        verify alike, and never in the body -- so it stays out of the request
        log, which keeps bodies. Trimmed, because a key pasted from an email
        often brings a space or a newline with it, and Star-Pay answers a key
        that is off by one character with the same GEN_004 as a wrong one.
        """
        if self.code != "starpay":
            return super()._build_request_headers(*args, **kwargs)
        return {"x-api-secret": (self.starpay_api_secret or "").strip()}

    def _parse_response_error(self, response):
        """Override of `payment` to parse the error message.

        Whatever comes back here is shown to the payer, so it must say
        something. Star-Pay answers a rejected order with an empty body often
        enough that taking `message` on trust left a member looking at "The
        payment provider rejected the request." and nothing else; the status
        line at least tells them, and the club's log, what happened.
        """
        if self.code != "starpay":
            return super()._parse_response_error(response)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        message = payload.get("message") or payload.get("error") or payload.get("detail") or ""
        if isinstance(message, dict):
            message = "; ".join(f"{field}: {reason}" for field, reason in message.items())
        message = str(message).strip()
        if message:
            return message
        reason = (response.reason or "").strip()
        return _(
            "Star-Pay refused the order (HTTP %(status)s%(reason)s). Try again, or pay at the club office.",
            status=response.status_code,
            reason=f" {reason}" if reason else "",
        )

    def _parse_response_content(self, response, **kwargs):
        """Override of `payment` to parse the response content.

        Star-Pay wraps every payload in an envelope and reports failure in the
        body rather than only by status code, so a non-success envelope is
        raised here instead of being handed on as if it were data.
        """
        if self.code != "starpay":
            return super()._parse_response_content(response, **kwargs)
        body = response.json()
        if body.get("status") != "success":
            return {}
        return body.get("data") or {}

    # === LOGGING === #
    #
    # The framework calls `_log_request` before the call and `_log_response`
    # after it, and neither is handed the other half. They are paired here
    # through a thread-local: one exchange, one row. A recordset cannot carry
    # a stray attribute (BaseModel defines __slots__), and the context is
    # immutable, so there is nowhere else to put it.

    def _log_request(self, method, url, payload, *, reference=None):
        res = super()._log_request(method, url, payload, reference=reference)
        if self.code == "starpay":
            _pending.exchange = {
                "started": time.monotonic(),
                "method": method,
                "url": url,
                "payload": payload,
                "reference": reference,
            }
        return res

    def _log_response(self, response, *, reference=None):
        res = super()._log_response(response, reference=reference)
        if self.code != "starpay":
            return res
        sent = getattr(_pending, "exchange", None) or {}
        _pending.exchange = None
        body = None
        try:
            body = response.json()
        except ValueError:
            body = response.text
        error_code, error_message = self._starpay_log_error(body)
        # Star-Pay reports some refusals inside an HTTP 200, as an envelope
        # whose status is not "success"; by status code alone they would be
        # logged as fine.
        refused = isinstance(body, dict) and body.get("status") not in (None, "success")
        self.env["starpay.request.log"]._record({
            "provider_id": self.id,
            "direction": "out",
            "endpoint": self._starpay_endpoint(sent.get("url") or response.url),
            "method": sent.get("method") or response.request.method,
            "url": sent.get("url") or response.url,
            "reference": sent.get("reference") or reference or False,
            "provider_reference": const.order_id(body.get("data")) or False
            if isinstance(body, dict) and isinstance(body.get("data"), dict)
            else False,
            "request_body": sent.get("payload"),
            "http_status": response.status_code,
            "response_body": body,
            "error_code": error_code,
            "error_message": error_message,
            "duration_ms": int((time.monotonic() - sent["started"]) * 1000)
            if sent.get("started")
            else 0,
            "state": "ok" if response.ok and not error_code and not refused else "failed",
        })
        return res

    @api.model
    def _starpay_log_error(self, body):
        """The code and message of a Star-Pay refusal, in whichever shape it came.

        Seen so far: `{"error": {"code", "message"}}`, and a flat
        `{"status": "error", "code", "message"}`. Anything else leaves the
        message for the response body, which is logged in full anyway.
        """
        if not isinstance(body, dict):
            return False, False
        error = body.get("error")
        if isinstance(error, dict):
            return error.get("code") or False, error.get("message") or False
        if body.get("status") in (None, "success"):
            return False, False
        code = body.get("code") or body.get("errorCode") or False
        message = body.get("message") or (error if isinstance(error, str) else False) or False
        return code, message

    def _send_api_request(self, method, endpoint, **kwargs):
        """Override of `payment` to record a call that never got a response.

        A connection error or a timeout never reaches `_log_response`, and an
        exchange that left no trace is the one you most want to see.
        """
        if self.code != "starpay":
            return super()._send_api_request(method, endpoint, **kwargs)
        try:
            return super()._send_api_request(method, endpoint, **kwargs)
        except ValidationError as error:
            sent = getattr(_pending, "exchange", None)
            if sent:  # unanswered: _log_response would have cleared it
                _pending.exchange = None
                self.env["starpay.request.log"]._record({
                    "provider_id": self.id,
                    "direction": "out",
                    "endpoint": endpoint,
                    "method": method,
                    "url": sent.get("url"),
                    "reference": sent.get("reference") or kwargs.get("reference") or False,
                    "request_body": sent.get("payload"),
                    "error_message": str(error)[:500],
                    "duration_ms": int((time.monotonic() - sent["started"]) * 1000),
                    "state": "failed",
                })
            raise

    @api.model
    def _starpay_endpoint(self, url):
        """The last two segments -- trdp/order -- so the column is readable."""
        parts = (url or "").rstrip("/").split("/")
        return "/".join(parts[-2:]) if len(parts) >= 2 else (url or "")

    def action_view_starpay_log(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Star-Pay Log",
            "res_model": "starpay.request.log",
            "view_mode": "list,form",
            "domain": [("provider_id", "=", self.id)],
            "context": {"create": False},
        }
