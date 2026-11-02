# telebirr settles in birr only.
SUPPORTED_CURRENCIES = ["ETB"]

# telebirr trade statuses, mapped onto Odoo's transaction states.
PAYMENT_STATUS_MAPPING = {
    "pending": ["Pending", "PENDING", "Paying", "WAIT_PAY"],
    "done": ["Completed", "COMPLETED", "SUCCESS", "Success"],
    "cancel": ["Cancelled", "CANCELLED", "Closed", "CLOSED"],
    "error": ["Failed", "FAILED", "Expired", "EXPIRED"],
}

DEFAULT_PAYMENT_METHOD_CODES = {"telebirr"}

# Paths under the merchant gateway host issued by Ethio Telecom.
TOKEN_PATH = "apiaccess/payment/gateway/payment/v1/token"
PREORDER_PATH = "apiaccess/payment/gateway/payment/v1/merchant/preOrder"

# Appended to the H5 checkout URL built from the prepay id.
CHECKOUT_QUERY_SUFFIX = "&version=1.0&trade_type=Checkout"
