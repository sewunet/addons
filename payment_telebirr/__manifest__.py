{
    "name": "Payment Provider: telebirr",
    "version": "19.0.1.0.0",
    "category": "Accounting/Payment Providers",
    "sequence": 350,
    "summary": "Ethio Telecom's telebirr mobile money (C2B).",
    "description": """
telebirr
========

telebirr is Ethio Telecom's mobile money service and the most widely held
wallet in the country. Unlike an aggregator, there is exactly one rail: the
payer always pays from their telebirr balance, so a member either has a
wallet or they do not.

Settles in **ETB only** - a telebirr wallet holds birr and nothing else, so
an order in any other currency genuinely cannot be paid this way.

What this module implements
---------------------------

The customer-to-business (C2B) checkout from Ethio Telecom's merchant pack:

* A fabric token is fetched with the Fabric App ID and App Secret, then used
  to authorise the `preOrder` call.
* `preOrder` returns a prepay id, which becomes the H5 checkout URL the payer
  is redirected to (`trade_type=Checkout`).
* Requests are signed with the merchant's RSA private key; callbacks are
  verified against telebirr's public key. A callback that arrives with no
  public key configured is refused rather than trusted, because an unverified
  callback is an instruction from a stranger to mark a fee as paid.
* Both casings of every trade status. telebirr's documentation and its live
  responses disagree on case, and `Paying` and `WAIT_PAY` are different words
  for the same in-flight state.

Configuration
-------------

Gateway URL, Checkout URL, Fabric App ID, App Secret, Merchant App ID,
Merchant Short Code, and the two RSA keys - all issued by Ethio Telecom when
a merchant account is opened.

What it does not
----------------

The merchant pack describes a refund API, but this module's request signing
has not been verified against the live gateway, so refunds are declared as
`none` rather than offered as a button that fails.
""",
    "author": "ATLAS Computer",
    "website": "https://act.com.et",
    "license": "LGPL-3",
    "icon": "/payment_telebirr/static/description/icon.png",
    "depends": ["payment"],
    "data": [
        "views/payment_telebirr_templates.xml",
        "views/payment_provider_views.xml",
        "data/payment_method_data.xml",
        "data/payment_provider_data.xml",  # Depends on the method above.
    ],
    "external_dependencies": {"python": ["cryptography"]},
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "installable": True,
}
