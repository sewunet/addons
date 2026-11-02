# Chapa settles in birr, and supports USD for some merchants. ETB is listed
# first because it is what club membership is priced in.
SUPPORTED_CURRENCIES = ["ETB", "USD"]

# Chapa reports outcomes twice: a `status` on the verify response, and an
# `event` on the webhook. Both are mapped.
PAYMENT_STATUS_MAPPING = {
    "pending": ["pending"],
    "done": ["success", "charge.success"],
    "cancel": ["cancelled", "charge.cancelled"],
    "error": ["failed", "charge.failed", "charge.reversed", "charge.refunded"],
}

DEFAULT_PAYMENT_METHOD_CODES = {"chapa"}

API_BASE_URL = "https://api.chapa.co"
INITIALIZE_PATH = "v1/transaction/initialize"
VERIFY_PATH = "v1/transaction/verify"


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

# Chapa's HTML checkout: the browser POSTs the order straight to Chapa's hosted
# page, signed with the PUBLIC key, and the server never calls `initialize`.
# Verification still happens server-side with the secret key.
HOSTED_PAY_URL = "https://api.chapa.co/v1/hosted/pay"

# A transaction the webhook never confirmed is asked about after this long.
# Long enough for a member to finish paying; short enough that a paid fee
# does not sit unsettled for the length of a cron interval.
VERIFY_AFTER_MINUTES = 2
# ...and given up on after this long: Chapa keeps sessions for a day or so,
# and a week-old draft is an abandoned checkout, not a payment in flight.
VERIFY_WITHIN_DAYS = 3

# --- Direct Charge ---------------------------------------------------------
#
# Chapa's Direct Charge API: the member types their phone number on OUR page,
# Odoo asks Chapa to charge it, and the wallet pushes an approval prompt to
# the phone (USSD), asks for a one-time code (OTP), or opens the bank's own
# portal. No Chapa-branded checkout page in between. This is what Chapa
# recommends for ERPs, and it is the default mode of this module.
CHARGE_PATH = "v1/charges"
VALIDATE_PATH = "v1/validate"

# The wallets Chapa can charge directly, in the order they are offered.
# Codes are the `type` query parameter of the charge endpoint; the auth type
# is reported back by Chapa per charge (`data.auth_type`), so it is not
# hard-coded here.
DIRECT_METHODS = [
    ("telebirr", "telebirr"),
    ("cbebirr", "CBE Birr"),
    ("mpesa", "M-Pesa"),
    ("ebirr", "Coopay-Ebirr"),
    ("awashbirr", "Awash Birr"),
    ("yaya", "Yaya Wallet"),
    ("boa_ussd", "BoA (USSD)"),
    ("enat_bank", "Enat Bank"),
]
DIRECT_METHOD_LABELS = dict(DIRECT_METHODS)
# How each wallet is expected to authorise, from Chapa's per-wallet guides.
# Chapa reports the real answer on every charge (`data.auth_type`); this is
# for an app deciding which screen to prepare before the charge is made.
DIRECT_METHOD_AUTH = {
    "telebirr": "ussd",
    "cbebirr": "ussd",
    "mpesa": "ussd",
    "ebirr": "ussd",
    "awashbirr": "otp",
    "yaya": "ussd",
    "boa_ussd": "ussd",
    "enat_bank": "portal_view",
}
# What a new provider offers. Awash Birr needs the encryption key (OTP);
# Enat opens a bank portal in a new tab -- both are opt-in.
DEFAULT_DIRECT_METHODS = "telebirr,cbebirr,mpesa,ebirr"

# Chapa's test numbers: only these succeed in test mode, everything else is
# refused -- which is how a failure is rehearsed. Awash Birr and Amole take
# OTP 12345.
TEST_MOBILES = ["0900123456", "0900112233", "0900881111", "0700123456", "0700112233", "0700881111"]
TEST_OTP = "12345"

# How long the browser waits for the phone before giving up on the page.
DIRECT_POLL_SECONDS = 4
DIRECT_WAIT_SECONDS = 180


def local_mobile(value):
    """An Ethiopian number in the 09xxxxxxxx / 07xxxxxxxx shape Chapa charges.

    Members type numbers every way there is -- "+251 90 012 3456",
    "0900123456", "900123456", "251900123456" -- and Odoo stores the display
    form. Returns "" when what is left is not an Ethiopian mobile number.
    """
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if digits.startswith("251") and len(digits) == 12:
        digits = "0" + digits[3:]
    elif len(digits) == 9 and digits[0] in "79":
        digits = "0" + digits
    if len(digits) == 10 and digits[:2] in ("09", "07"):
        return digits
    return ""


def mask_mobile(number):
    """"0900123456" -> "09••••3456": the ends a member recognises, not the number."""
    if not number or len(number) < 6:
        return number or ""
    return number[:2] + "\u2022" * (len(number) - 6) + number[-4:]
