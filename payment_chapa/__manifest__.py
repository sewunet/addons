{
    "name": "Payment Provider: Chapa",
    "version": "19.0.1.1.0",
    "category": "Accounting/Payment Providers",
    "sequence": 350,
    "summary": "Ethiopian payment gateway covering banks, wallets and cards.",
    "description": """
Chapa
=====

Chapa is an Ethiopian payment aggregator. One hosted checkout page stands in
front of every rail it supports, so the payer chooses how to pay *after*
leaving Odoo: a card, a bank account, or one of the mobile wallets Chapa
lists - telebirr, M-Pesa, CBE Birr, Awash Birr, Yaya Wallet and Coopay-Ebirr
among them. Some of those authorise by USSD push to the account holder's
phone rather than on the page itself.

Odoo never learns which rail was used, and does not need to: the money and
the confirmation both come back from Chapa.

Accepts **ETB and USD**.

What this module implements
---------------------------

* **Direct Charge** (the default). The payer picks a wallet and types a phone
  number on our own page; `charges?type=<wallet>` asks Chapa to charge it and
  the wallet prompts the payer -- a USSD push (telebirr, CBE Birr, M-Pesa,
  Coopay-Ebirr, Yaya, BoA), a one-time code (Awash Birr, sent to
  `validate` encrypted with the dashboard encryption key), or the bank's own
  page (Enat). The page polls `verify` until it settles. No Chapa-branded
  checkout in between.
* The hosted checkout flows, for a merchant account without Direct Charge:
  the HTML form posted with the public key, and `transaction/initialize`
  opening a session the payer is sent to.
* `transaction/verify` as the single source of truth. Both the browser
  redirect and the webhook trigger a verify call rather than being trusted on
  their own, because the payer controls the trip back from the checkout page.
* Webhook signature verification against `x-chapa-signature`, computed over
  the raw request body. The constant `chapa-signature` header is deliberately
  ignored - it is an HMAC of the secret with itself, identical on every
  request, and so proves nothing about the payload.
* Both status vocabularies. Chapa reports `status` on verify and `event` on
  the webhook, and success is spelled differently in each.

What it does not
----------------

Refunds, split payments, virtual accounts and transfers are all documented by
Chapa but are not wired up here. Refund support in particular is declared as
`none` on the payment method rather than left to fail at the gateway.
""",
    "author": "ATLAS Computer",
    "website": "https://act.com.et",
    "license": "LGPL-3",
    "icon": "/payment_chapa/static/description/icon.png",
    "depends": ["payment"],
    "data": [
        "views/payment_chapa_templates.xml",
        "views/payment_provider_views.xml",
        "views/payment_transaction_views.xml",
        "data/ir_cron.xml",
        "data/payment_method_data.xml",
        "data/payment_provider_data.xml",  # Depends on the method above.
    ],
    "assets": {
        "web.assets_frontend": [
            "payment_chapa/static/src/interactions/**/*",
        ],
    },
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "installable": True,
}
