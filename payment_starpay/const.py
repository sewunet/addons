# Star-Pay settles in birr only. Sending anything else is a silent mis-charge
# rather than an error, so the provider refuses other currencies outright.
SUPPORTED_CURRENCIES = ["ETB"]

# Star-Pay's transaction statuses, mapped onto Odoo's states.
#
# UNPAID is not in the documentation; it is what the sandbox reports for an
# order the payer has not finished (trdp/order answers with it). Missing from
# this table it was an "unknown status", and an unknown status used to fail
# the transaction -- so the app's first poll after opening the checkout
# killed the payment.
#
# Only PAID settles a fee: it is what the real callback says, and a status
# nobody has seen Star-Pay send must not grant a membership. Anything
# unrecognised leaves the transaction where it is for verify to settle.
PAYMENT_STATUS_MAPPING = {
    "pending": ["UNPAID", "PENDING", "PROCESSING"],
    "done": ["PAID"],
    "cancel": ["CANCELLED", "EXPIRED"],
    "error": ["FAILED"],
}

DEFAULT_PAYMENT_METHOD_CODES = {"starpay"}

# The cron that verifies open payments whose callback never came: not before
# the payer has had time to finish, not after Star-Pay's 24-hour checkout
# lifetime (plus a margin) has run out.
CRON_MIN_AGE_MINUTES = 10
CRON_MAX_AGE_HOURS = 26

# --- reading Star-Pay's answers ------------------------------------------
#
# Star-Pay's documentation names an order `billRefNo` and its checkout
# `paymentUrl`. The sandbox answers `trdp/order` with neither:
#
#     {"status": "success", "data": {"order_id": "6UE7B4M00P5", "status": "UNPAID",
#      "amount": "200", "currency": "WALLET_CURRENCY_ETB",
#      "payment_url": "https://sandbox-checkout.starpayethiopia.com/en/pay/d/6UE7B4M00P5",
#      "redirectUrl": "...", "expires_at": "2026-09-26T10:33:06.648Z"}}
#
# Reading only the documented names failed every order with "did not return
# a checkout session", on the website and in the app alike.
#
# The callback, on the other hand, does use billRefNo -- for the same id:
#
#     {"billRefNo": "33WJ8946WB", "status": "PAID", "amount": 1,
#      "message": "Payment successful", "externalReferenceId": "CLA9SR5XR9",
#      "payment_type": "USSD_PUSH",
#      "receipt_url": "https://receipt.starpayethiopia.com/receiptqa/WST-33WJ8946WB", ...}
#
# No metadata comes back and no currency, so the transaction is found by that
# order id and the amount is compared in the transaction's own currency.
# externalReferenceId is Star-Pay's, not the club's reference. Every read goes
# through these, which take the running API's names first and the documented
# ones after, so neither shape breaks the other.


def payment_status(data):
    """The status of a payment, upper-cased, from a callback or a verify answer."""
    data = data or {}
    return str(data.get("status") or data.get("payment_status") or data.get("paymentStatus") or "").upper()


def order_id(data):
    """Star-Pay's id for an order: what `trdp/verify` takes as `orderId`."""
    data = data or {}
    return data.get("order_id") or data.get("orderId") or data.get("billRefNo") or ""


def checkout_url(data):
    """The hosted checkout page the payer is sent to, on the web or the phone."""
    data = data or {}
    return data.get("payment_url") or data.get("paymentUrl") or ""


def currency_code(value):
    """`WALLET_CURRENCY_ETB` -> `ETB`.

    The amount check compares this with the transaction's currency, and
    `WALLET_CURRENCY_ETB` against `ETB` would refuse a genuine payment.
    """
    value = (value or "").strip().upper()
    return value[len("WALLET_CURRENCY_"):] if value.startswith("WALLET_CURRENCY_") else value


# Ethiopia. Used to lift a locally written number into the international form
# Star-Pay documents.
COUNTRY_CODE = "251"


def e164_phone(value):
    """An Ethiopian number in the form Star-Pay's own examples use.

    `bare_phone` only strips the formatting, so "0911 22 33 47" stays
    "0911223347". Star-Pay documents `+251987654567` and verifies the payer
    by this number: handed a local one it has no country to resolve it in,
    and what comes back is a generic failure rather than a complaint about
    the field.

    A number that is already international is left alone. A number that is
    neither -- too short, or not starting the way Ethiopian mobiles do --
    is returned as its digits, because guessing at it would be worse than
    letting the gateway say so.
    """
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return ""
    if (value or "").strip().startswith("+"):
        return f"+{digits}"
    if digits.startswith(COUNTRY_CODE):
        return f"+{digits}"
    # 0911223347 -> +251911223347
    if digits.startswith("0") and len(digits) == 10:
        return f"+{COUNTRY_CODE}{digits[1:]}"
    # 911223347 -> +251911223347
    if len(digits) == 9 and digits[0] in "79":
        return f"+{COUNTRY_CODE}{digits}"
    return digits

# How Star-Pay names the rail the payer actually used, and how a member reads
# it. The gateway reports this in `payment_type` on the callback only -- the
# verify endpoint does not carry it -- and it is the single place the club
# learns whether a fee arrived by a USSD prompt or by a one-time code.
PAYMENT_TYPE_LABELS = {
    "USSD_PUSH": "USSD push",
    "USSD": "USSD push",
    "OTP": "Pay by OTP",
    "PAY_BY_OTP": "Pay by OTP",
    "OTP_PAYMENT": "Pay by OTP",
    "MPGS": "Card",
    "CARD": "Card",
}


def payment_type_label(value):
    """A member-readable name for a rail, including one added after us.

    An unknown code is titled rather than dropped: "BANK_TRANSFER" reads as
    "Bank transfer", which is worse than a proper name and much better than
    a receipt that says nothing about how the money arrived.
    """
    if not value:
        return None
    code = str(value).strip().upper()
    if code in PAYMENT_TYPE_LABELS:
        return PAYMENT_TYPE_LABELS[code]
    return code.replace("_", " ").capitalize()

# The two environments differ in host *and* in path: the sandbox carries a
# /v1/starpay-api prefix and production does not. They are also separate
# accounts with separately generated keys, so a production secret sent to the
# sandbox host is simply unrecognised -- and an unrecognised key answers
# GEN_004, "Something went wrong", which says nothing about the request.
SANDBOX_BASE_URL = "https://sandbox-api.starpayethiopia.com/v1/starpay-api"
PRODUCTION_BASE_URL = "https://api.starpayethiopia.com"


def bare_phone(value):
    """Reduce a display-formatted phone to the shape a gateway accepts.

    Odoo keeps numbers as people read them -- "+251 90 012 3456" -- and the
    gateways want them as machines read them -- "+251900123456". Everything
    except digits is formatting, apart from a single leading plus, which is
    the international prefix and stays.
    """
    if not value:
        return ""
    digits = "".join(ch for ch in value if ch.isdigit())
    return f"+{digits}" if value.strip().startswith("+") else digits
