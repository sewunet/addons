{
    "name": "Payment Provider: Star-Pay",
    "version": "19.0.1.3.0",
    "category": "Accounting/Payment Providers",
    "sequence": 350,
    "summary": "Ethiopian payment gateway covering local banks and wallets.",
    "description": """
Star-Pay
========

Star-Pay is an Ethiopian payment gateway that puts one integration in front
of more than fifteen local rails. The payer picks theirs on Star-Pay's page,
not in Odoo.

Banks it lists include Commercial Bank of Ethiopia, Awash, Dashen, Abay,
Amhara, Oromiya, Zemen, Wegagen, Hibret, Nib, Enat, Coop, Birhan, Ahadu,
Global, Sinqee, Tsedey, Tsehay, Addis International, Ambesa and ABBS.
Wallets include CBE Birr, Yaya Wallet and Kacha.

Settles in **ETB only**. The provider refuses other currencies outright: a
gateway that takes birr and is handed a dollar amount produces a silent
mis-charge rather than an error, and a membership fee is not worth that risk.

What this module implements
---------------------------

* The hosted checkout flow. Odoo opens a session and redirects the payer to
  Star-Pay; the amount and reference travel with it.
* Webhook verification against the configured webhook secret, so a callback
  that did not come from Star-Pay cannot settle a membership.
* The documented transaction statuses, with `PROCESSING` kept firmly separate
  from `PAID` - an in-flight payment must not grant a membership - and
  `EXPIRED` treated as a cancellation rather than a failure, because a member
  who walked away has not had anything go wrong.
* The rail the payer chose, recorded from the callback: USSD push, Pay by OTP,
  card. Star-Pay reports this as `payment_type` on the callback and nowhere
  else - `trdp/verify` does not carry it - so it is captured when the callback
  arrives, alongside the gateway's own receipt URL.

USSD push and Pay by OTP
------------------------

These are not separate integrations, and there is no endpoint to start one.
Star-Pay publishes a single order endpoint, `trdp/order`, which answers with a
hosted `paymentUrl`; the payer chooses USSD push, a one-time code or a card on
that page. Star-Pay's own documentation is explicit that "the integration flow
is the same for all StarPay payment methods" and that "no additional
integration changes are required" between them.

So what a merchant can do is name them where the payer chooses - the app's
wallet entry says "USSD push or a one-time code" - and record which was used
once the money has moved. Restricting the page to only those two is a setting
in Star-Pay's merchant portal, not an API flag.

Configuration
-------------

API URL, API Secret and Webhook Secret, all issued by Star-Pay. The sandbox
lives at `sandbox-api.starpayethiopia.com`; point the API URL there to test.

What it does not
----------------

Star-Pay also advertises payment links and QR payments; neither is wired up
here. No refund endpoint is documented in the published API, so refunds are
declared as `none`. The escrow endpoints (`trdp/escrow/*`) are documented but
have no use in a membership fee.
""",
    "author": "ATLAS Computer",
    "website": "https://act.com.et",
    "license": "LGPL-3",
    "icon": "/payment_starpay/static/description/icon.png",
    "depends": ["payment"],
    "data": [
        "security/ir.model.access.csv",
        "views/payment_starpay_templates.xml",
        "views/starpay_log_views.xml",
        "views/payment_provider_views.xml",
        "views/payment_transaction_views.xml",
        "data/payment_method_data.xml",
        "data/payment_provider_data.xml",  # Depends on the method above.
        "data/starpay_cron_data.xml",
    ],
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "installable": True,
}
