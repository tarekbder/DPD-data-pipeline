"""Configuration from environment variables (see the plan's Configuration table)."""
import os


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


PLACEHOLDERS_RAW = [p.strip() for p in os.getenv("PLACEHOLDERS", "N/A,null,-,?,none").split(",") if p.strip()]
PLACEHOLDERS = {p.lower() for p in PLACEHOLDERS_RAW}

TOP_N = _int("TOP_N", 20)
PAGINATION_OVERRIDES_RAW = os.getenv("PAGINATION_OVERRIDES", "")
MAX_PAGES = _int("MAX_PAGES", 1000)
REQUEST_TIMEOUT_S = _float("REQUEST_TIMEOUT_S", 30)
FETCH_DEADLINE_S = _float("FETCH_DEADLINE_S", 300)
MAX_RESPONSE_MB = _float("MAX_RESPONSE_MB", 500)
MAX_RESPONSE_BYTES = int(MAX_RESPONSE_MB * 1024 * 1024)
EXCEL_MAX_ROWS = _int("EXCEL_MAX_ROWS", 500_000)
EXCEL_MAX_COLS = _int("EXCEL_MAX_COLS", 1000)
EXCEL_MAX_CELL_CHARS = 32_767
TYPE_PARSE_THRESHOLD = _float("TYPE_PARSE_THRESHOLD", 0.9)

# Optional: comma-separated UI origins. Only needed when UI and server are on different origins.
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

# Provider hosts the pipelines may fetch from (comma-separated). Endpoint host must also equal the counterparty host.
_DEFAULT_PROVIDER_HOSTS = ",".join([
    "annualtrafficnumbers.connector.mds.think-it.io",
    "opendatahub-prod-1.connector.mds.think-it.io",
    "ua-mds-prod-1.connector.mds.think-it.io",
    "connector-mds-prod.edc.aws.bmw.cloud",
])
ALLOWED_PROVIDER_HOSTS = frozenset(
    h.strip().lower().rstrip(".")
    for h in os.getenv("ALLOWED_PROVIDER_HOSTS", _DEFAULT_PROVIDER_HOSTS).split(",")
    if h.strip()
)

# API keys for /v1/* (header X-Pipelines-Key). _NEXT allows rotation. No key set = every /v1 call is refused.
API_KEYS = tuple(k for k in (os.getenv("PIPELINES_API_KEY", ""), os.getenv("PIPELINES_API_KEY_NEXT", "")) if k)
