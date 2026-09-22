"""Constants for the Enable Banking integration."""
from datetime import timedelta

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
DEFAULT_SCAN_INTERVAL_HOURS = 6
MIN_SCAN_INTERVAL_HOURS = 6

FALLBACK_CONSENT_DAYS = 10  # used when the bank's maximum is unknown
MAX_CONSENT_DAYS = 180

DATA_VIEW_REGISTERED = "view_registered"
DATA_PENDING_STATES = "pending_states"

ATTR_LIMIT = "limit"
ATTR_IBAN = "iban"

# Which balance is shown as "the" balance (first one that exists wins).
BALANCE_PRIORITY = (
    "ITAV", "CLAV", "ITBD", "CLBD", "XPCD",
    "OPAV", "OPBD", "FWAV", "PRCD", "VALU", "INFO", "OTHR",
)

# ---------------------------------------------------------------------------
# "Hidden" parameters (not shown in the UI; change here if you need to).
# ---------------------------------------------------------------------------
DB_DIR = "enablebanking"
DELETE_DB_ON_REMOVE = True            # delete the local database when the integration is removed
USE_PSU_HEADERS_ON_INITIAL_SYNC = True  # send the browser's headers on the first download (PSU is present)

DAILY_SYNC_BUDGET = 4                 # max bank syncs per rolling 24 h (unless force=true)
MIN_MANUAL_INTERVAL = timedelta(minutes=30)
RATE_LIMIT_BLOCK = timedelta(hours=6)     # ASPSP_RATE_LIMIT_EXCEEDED -> wait this long
GENERIC_429_BLOCK = timedelta(hours=1)
RETRY_BACKOFF = (timedelta(hours=1), timedelta(hours=2), timedelta(hours=4), timedelta(hours=6))
OVERLAP_DAYS = 10                     # re-fetch this many days before the last stored transaction
TICK_INTERVAL = timedelta(minutes=10)     # how often we check whether a sync is due (no bank call)
EXPIRY_WARNING_DAYS = 14
MAX_TX_PAGES = 500                    # safety cap on continuation_key loops
PSU_HEADERS_TTL_SECONDS = 600
MAX_SERVICE_RESULTS = 500

DATA_PSU_HEADERS = "psu_headers"

EVENT_NEW_TRANSACTIONS = "enablebanking_new_transactions"
EVENT_SYNC_FINISHED = "enablebanking_sync_finished"

SESSION_OK = "ok"
SESSION_EXPIRED = "expired"
SESSION_AUTH_ERROR = "auth_error"

ISSUE_SESSION_EXPIRED = "session_expired"
ISSUE_CONSENT_EXPIRING = "consent_expiring"
ISSUE_CREDENTIALS = "credentials"
