import base64
import hashlib
import hmac
import json
import pprint

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.payment.logging import get_payment_logger
from odoo.addons.payment_starpay import const

_logger = get_payment_logger(__name__)

# Where the signature and its timestamp may arrive. Star-Pay's docs name
# X-Signature and X-Timestamp; the others are the spellings gateways commonly
# use, looked at only so a callback that signs under another name is
# recognised -- and the log says which one it was.
SIGNATURE_HEADERS = ("X-Signature", "X-Starpay-Signature", "X-Webhook-Signature")
TIMESTAMP_HEADERS = ("X-Timestamp", "X-Starpay-Timestamp", "X-Signature-Timestamp")

# Never written to the log.
PRIVATE_HEADERS = {"cookie", "authorization", "x-api-secret", "x-api-key"}


class StarPayController(http.Controller):
    _return_url = "/payment/starpay/return"
    _webhook_url = "/payment/starpay/webhook"

    @http.route(_return_url, type="http", methods=["GET", "POST"], auth="public", csrf=False)
    def starpay_return_from_checkout(self, **data):
        """Handle the customer coming back from the Star-Pay checkout page.

        The return trip proves nothing -- the customer controls it -- so its
        query string never moves the transaction. Star-Pay is asked instead,
        with trdp/verify, and the payer lands on the status page with the
        answer already applied.
        """
        _logger.info("Handling redirection from Star-Pay with data:\n%s", pprint.pformat(data))
        # Logged because nothing documents what Star-Pay puts on this URL,
        # and the transaction can only be found if it puts something.
        tx_sudo = request.env["payment.transaction"].sudo()._search_by_reference("starpay", data)
        self._log_inbound(
            "starpay/return", data, tx_sudo,
            error=None if tx_sudo else "No transaction matches what Star-Pay sent back.",
        )
        if tx_sudo:
            self._check_status(tx_sudo)
        return request.redirect("/payment/status")

    @http.route(_webhook_url, type="http", methods=["POST"], auth="public", csrf=False)
    def starpay_webhook(self):
        """Handle a callback from Star-Pay: the callbackURL on every order.

        The signature is checked against the raw request body, because
        Star-Pay signs `${timestamp}.${JSON.stringify(payload)}` and
        re-serialising a parsed dict does not reproduce those bytes. Once it
        verifies, the callback's own status moves the transaction: it is
        signed, it is the first word of a payment, and it is the only message
        that carries the method the payer used and Star-Pay's receipt. The
        amount is still checked against the transaction before anything moves.
        """
        raw_body = request.httprequest.get_data()
        data = request.get_json_data()
        _logger.info("Notification received from Star-Pay with data:\n%s", pprint.pformat(data))

        # Every callback is logged, and the ones that fail are the ones worth
        # keeping: a signature that does not verify means Star-Pay changed
        # how it signs, or somebody is posting at the club. "Failed" is about
        # the exchange, not the payment -- a PENDING callback that verifies
        # is an OK exchange.
        tx_sudo = request.env["payment.transaction"].sudo()._search_by_reference("starpay", data)
        if not tx_sudo:
            self._log_inbound(
                "starpay/webhook", data, tx_sudo, http_status=200,
                error="No transaction matches this callback.",
            )
            return request.make_json_response("")

        verified, verdict = self._signature_verdict(
            raw_body, request.httprequest.headers, tx_sudo.provider_id
        )
        if verified:
            self._log_inbound("starpay/webhook", data, tx_sudo, http_status=200, signature=verdict)
            tx_sudo._starpay_handle_callback(data)
        else:
            # The body is not trusted -- an unsigned "PAID" could come from
            # anyone -- but it names the order, and that is enough to ask
            # Star-Pay itself below. Nothing in it moves the payment.
            _logger.warning(
                "Star-Pay callback for %s did not verify (%s); asking trdp/verify instead.",
                tx_sudo.reference, verdict,
            )
            self._log_inbound(
                "starpay/webhook", data, tx_sudo, http_status=200,
                error=(
                    "The signature did not verify, so this callback's own status was not used; "
                    f"Star-Pay was asked with trdp/verify instead. {verdict}"
                ),
                signature=verdict,
            )
        # While the payment is still open, the gateway's own answer settles it.
        if tx_sudo.state in ("draft", "pending"):
            self._check_status(tx_sudo)
        return request.make_json_response("")

    @staticmethod
    def _log_inbound(endpoint, data, tx_sudo, http_status=None, error=None, signature=None):
        """One log entry for something Star-Pay (or the payer) sent to the club."""
        data = data or {}
        # The headers are where a callback's signature lives; without them a
        # refused callback cannot be diagnosed.
        headers = {
            name: value
            for name, value in request.httprequest.headers.items()
            if name.lower() not in PRIVATE_HEADERS
        }
        request.env["starpay.request.log"].sudo()._record({
            "provider_id": tx_sudo.provider_id.id if tx_sudo else False,
            "direction": "in",
            "endpoint": endpoint,
            "method": request.httprequest.method,
            "url": request.httprequest.url,
            # The club's own reference, as sent in metadata, or the one the
            # matched transaction carries when Star-Pay did not echo it.
            "reference": (data.get("metadata") or {}).get("reference")
            or (tx_sudo.reference if tx_sudo else False)
            or False,
            "provider_reference": const.order_id(data) or False,
            "request_body": data,
            "request_headers": headers,
            "signature_check": signature or False,
            "http_status": http_status,
            "error_message": (error or "")[:500] or False,
            "state": "failed" if error else "ok",
        })

    @staticmethod
    def _signature_verdict(raw_body, headers, provider):
        """Whether a callback is signed by Star-Pay, and a sentence saying why.

        Star-Pay documents HMAC-SHA256 over `${timestamp}.${body}`, hex, in
        X-Signature, keyed with the webhook secret. The first real callback
        did not verify that way, and a bare "refused" said nothing about
        which part differed. So the forms a genuine signature takes are all
        tried -- the webhook secret or the API secret; over timestamp.body or
        the body alone; hex or base64, with or without a "sha256=" prefix --
        and the verdict names the one that matched, or everything that was
        tried. Every one of them needs a secret only the club and Star-Pay
        hold, so accepting them does not accept a forgery. Compared in
        constant time.
        """
        received = next((headers.get(h) for h in SIGNATURE_HEADERS if headers.get(h)), "")
        timestamp = next((headers.get(h) for h in TIMESTAMP_HEADERS if headers.get(h)), "")
        if not received:
            return False, "No signature header arrived (looked for %s)." % ", ".join(SIGNATURE_HEADERS)
        secrets = [
            (name, (value or "").strip())
            for name, value in (
                ("webhook secret", provider.starpay_webhook_secret),
                ("API secret", provider.starpay_api_secret),
            )
            if (value or "").strip()
        ]
        if not secrets:
            return False, "No webhook secret is set on the Star-Pay provider."

        got = received.strip()
        if got.lower().startswith("sha256="):
            got = got[len("sha256="):]
        body = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else (raw_body or "")
        # Star-Pay signs JSON.stringify(payload) -- the payload re-serialised
        # compactly, not the bytes as they arrive (its Python reference parses
        # the body and signs json.dumps(payload, separators=(",", ":"))). When
        # the sender or anything between spaces or escapes the body another
        # way, the raw bytes no longer match what was signed. So the compact
        # form is tried first, with characters kept as JSON.stringify keeps
        # them and escaped as Python's default does; the raw body after.
        bodies = []
        try:
            payload = StarPayController._js_numbers(json.loads(body))
        except ValueError:
            payload = None
        if payload is not None:
            bodies.append(("compact JSON", json.dumps(payload, separators=(",", ":"), ensure_ascii=False)))
            bodies.append(("compact JSON, ASCII", json.dumps(payload, separators=(",", ":"))))
        bodies.append(("raw body", body))
        seen, messages = set(), []
        for body_name, text in bodies:
            for message_name, message in (
                ([(f"timestamp.{body_name}", f"{timestamp}.{text}")] if timestamp else [])
                + [(body_name, text)]
            ):
                if message not in seen:
                    seen.add(message)
                    messages.append((message_name, message))
        for secret_name, secret in secrets:
            for message_name, message in messages:
                digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
                for encoding, expected, candidate in (
                    ("hex", digest.hex(), got.lower()),
                    ("base64", base64.b64encode(digest).decode(), got),
                ):
                    if hmac.compare_digest(expected, candidate):
                        return True, f"Verified: {secret_name}, {encoding}, over {message_name}."
        return False, (
            "A signature arrived but matches none of: webhook or API secret, over "
            "timestamp + the compact JSON payload (as Star-Pay documents) or the raw body, "
            "hex or base64."
            + ("" if timestamp else " No timestamp header arrived (looked for %s)." % ", ".join(TIMESTAMP_HEADERS))
            + " Check the provider's Webhook Secret against Star-Pay's merchant portal."
        )

    @staticmethod
    def _js_numbers(value):
        """Whole-number floats as JavaScript writes them: 200, not 200.0.

        JSON.stringify prints the number 200.0 as "200"; json.dumps prints a
        Python float as "200.0", and one character is a different signature.
        """
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {k: StarPayController._js_numbers(v) for k, v in value.items()}
        if isinstance(value, list):
            return [StarPayController._js_numbers(v) for v in value]
        return value

    @staticmethod
    def _check_status(tx_sudo):
        """Verify with Star-Pay; an unreachable gateway must not break the page."""
        try:
            tx_sudo._starpay_check_status()
        except ValidationError:
            _logger.error("Unable to verify Star-Pay transaction %s.", tx_sudo.reference)
