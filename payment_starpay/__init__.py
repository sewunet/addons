from . import controllers
from . import models

from odoo.addons.payment import reset_payment_provider, setup_provider


def post_init_hook(env):
    """Create the account.payment.method for this provider.

    The provider record itself comes from XML data, which bypasses the
    framework's setup. Without this the method never exists, so the journal
    payment-method line is never created, and a confirmed transaction fails
    at the very last step -- posting to the journal -- with "Please define a
    payment method line on your payment". Found live, on the first payment
    Chapa actually confirmed.
    """
    setup_provider(env, "starpay")


def uninstall_hook(env):
    reset_payment_provider(env, "starpay")
