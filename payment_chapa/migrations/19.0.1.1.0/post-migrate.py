"""Existing Chapa providers get the direct-charge form and the new default.

The provider record is `noupdate`, so the data file's `inline_form_view_id`
does not reach a database that already has it; and switching an existing
provider to direct mode is the point of this version.
"""

from odoo import SUPERUSER_ID, api

from odoo.addons.payment_chapa import const


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    inline = env.ref("payment_chapa.inline_form", raise_if_not_found=False)
    providers = env["payment.provider"].with_context(active_test=False).search([("code", "=", "chapa")])
    for provider in providers:
        values = {}
        if inline and provider.inline_form_view_id != inline:
            values["inline_form_view_id"] = inline.id
        if not provider.chapa_direct_methods:
            values["chapa_direct_methods"] = const.DEFAULT_DIRECT_METHODS
        if provider.chapa_checkout_mode != "direct":
            values["chapa_checkout_mode"] = "direct"
        if values:
            provider.write(values)
