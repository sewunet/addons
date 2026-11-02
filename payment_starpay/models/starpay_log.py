import json
import logging

from odoo import api, fields, models
from odoo.tools import config

_logger = logging.getLogger(__name__)

#: How long an entry is kept, unless `payment_starpay.log_retention_days` says
#: otherwise. Long enough to investigate a complaint from last month, short
#: enough that the table does not become a second database.
DEFAULT_RETENTION_DAYS = 45


class StarPayRequestLog(models.Model):
    """Every exchange with Star-Pay, kept where somebody can read it.

    Star-Pay answers a rejected order with `GEN_004: Something went wrong`,
    which is the same thing it says for an unrecognised API key, a merchant
    without API Gateway access, and an unhandled fault of its own. None of
    those can be told apart from the message, so the only way to work out
    what happened is to look at what was actually sent and what actually came
    back. That used to mean reading the server log over somebody's shoulder.

    Entries are written on a separate cursor and committed immediately. A
    failed payment usually ends in a rollback -- that is what a failure is --
    and a record of the failure that disappears with it would be worth
    nothing at exactly the moment it is wanted.
    """

    _name = "starpay.request.log"
    _description = "Star-Pay Request Log"
    _order = "create_date desc, id desc"
    _rec_name = "endpoint"

    provider_id = fields.Many2one("payment.provider", ondelete="cascade", index=True)
    direction = fields.Selection(
        [("out", "Sent to Star-Pay"), ("in", "Received from Star-Pay")],
        required=True,
        default="out",
        index=True,
    )
    endpoint = fields.Char(required=True, index=True)
    method = fields.Char()
    url = fields.Char()
    # Kept as text rather than a link to payment.transaction: a log entry is
    # written before the transaction it describes is committed, and sometimes
    # for a transaction that never gets one.
    reference = fields.Char(string="Fee Reference", index=True)
    provider_reference = fields.Char(string="Star-Pay Reference", index=True)
    request_body = fields.Text()
    # Inbound only: where a callback's signature lives. Cookies and
    # authorization headers are left out.
    request_headers = fields.Text()
    signature_check = fields.Char(
        help="For a callback: which form of signature verified, or why none did."
    )
    http_status = fields.Integer()
    response_body = fields.Text()
    error_code = fields.Char(index=True)
    error_message = fields.Char()
    duration_ms = fields.Integer(string="Duration (ms)")
    state = fields.Selection(
        [("ok", "OK"), ("failed", "Failed")], required=True, default="ok", index=True
    )

    @api.model
    def _pretty(self, payload):
        """Readable JSON, because nobody can scan a single-line body."""
        if payload is None:
            return False
        if isinstance(payload, (dict, list)):
            try:
                return json.dumps(payload, indent=2, sort_keys=True, default=str)
            except (TypeError, ValueError):
                return str(payload)
        text = str(payload)
        try:
            return json.dumps(json.loads(text), indent=2, sort_keys=True)
        except (TypeError, ValueError):
            return text

    @api.model
    def _record(self, values):
        """Write one entry, and do not let logging break a payment.

        On its own cursor, so the entry survives the rollback that usually
        follows a failure. In tests the ordinary cursor is used instead: a
        second connection would sit outside the test's transaction and leave
        rows behind after it rolls back.
        """
        values["request_body"] = self._pretty(values.get("request_body"))
        values["response_body"] = self._pretty(values.get("response_body"))
        values["request_headers"] = self._pretty(values.get("request_headers"))
        if config["test_enable"]:
            return self.sudo().create(values)
        try:
            with self.pool.cursor() as cr:
                self.with_env(self.env(cr=cr)).sudo().create(values)
        except Exception:  # noqa: BLE001 - a log must never break a payment
            _logger.exception("Could not write the Star-Pay log entry.")
        return self.browse()

    @api.model
    def _gc(self):
        """Drop what is too old to be evidence. Called by cron."""
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "payment_starpay.log_retention_days", DEFAULT_RETENTION_DAYS
        )
        # A mistyped setting must not stop the cleanup for good: the cron
        # would fail every night and the table would grow without limit.
        try:
            days = int(raw)
        except (TypeError, ValueError):
            _logger.warning(
                "payment_starpay.log_retention_days is %r, not a number of days; using %s.",
                raw, DEFAULT_RETENTION_DAYS,
            )
            days = DEFAULT_RETENTION_DAYS
        days = max(days, 1)
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        stale = self.sudo().search([("create_date", "<", cutoff)])
        count = len(stale)
        stale.unlink()
        _logger.info("Star-Pay log: removed %s entries older than %s days.", count, days)
        return count
