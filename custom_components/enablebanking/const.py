"""Constants for the Enable Banking integration."""
DOMAIN = "enablebanking"

CALLBACK_PATH = "/enablebanking/callback"
DEFAULT_ASPSP_NAME = "Mock ASPSP"
DEFAULT_ASPSP_COUNTRY = "FI"
DEFAULT_REDIRECT_BASE = "http://homeassistant.local:8123"

CONF_APPLICATION_ID = "application_id"
CONF_PRIVATE_KEY_PATH = "private_key_path"
CONF_ASPSP_NAME = "aspsp_name"
CONF_ASPSP_COUNTRY = "aspsp_country"
CONF_REDIRECT_URL = "redirect_url"
CONF_SESSION_ID = "session_id"
CONF_ACCOUNTS = "accounts"
CONF_VALID_UNTIL = "valid_until"

CONF_SCAN_INTERVAL_HOURS = "scan_interval_hours"
CONF_TRANSACTION_DAYS = "transaction_days"
DEFAULT_SCAN_INTERVAL_HOURS = 6
DEFAULT_TRANSACTION_DAYS = 0  # 0 = let the bank decide (no date filter)

MAX_PAGES = 5              # max pages of transactions fetched per refresh
MAX_TRANSACTIONS = 100     # max transactions kept in memory per account
FALLBACK_CONSENT_DAYS = 10  # used when the bank's maximum is unknown
MAX_CONSENT_DAYS = 90

DATA_VIEW_REGISTERED = "view_registered"
DATA_PENDING_STATES = "pending_states"

ATTR_LIMIT = "limit"
ATTR_IBAN = "iban"

# Which balance is shown as "the" balance (first one that exists wins).
BALANCE_PRIORITY = (
    "ITAV", "CLAV", "ITBD", "CLBD", "XPCD",
    "OPAV", "OPBD", "FWAV", "PRCD", "VALU", "INFO", "OTHR",
)
