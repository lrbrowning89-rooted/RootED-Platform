# config.py
import os
from pathlib import Path

from dotenv import load_dotenv


# Local development reads the repository-root .env file. Existing process
# variables always win, so production hosts continue to use their normal
# environment/secret configuration.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)


def environment_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolve_database_path() -> str:
    """Return one absolute DB path; never pass an empty filename to SQLite."""
    configured_database = os.environ.get("NGSS_DB", "").strip()
    return str(
        Path(configured_database).expanduser().resolve()
        if configured_database
        else (PROJECT_ROOT / "data" / "ngss.db").resolve()
    )


DATABASE_PATH = resolve_database_path()
IS_PRODUCTION = environment_flag("RENDER") or (
    os.environ.get("APP_ENV", "").strip().lower() == "production"
)
TRUST_PROXY_HEADERS = environment_flag("TRUST_PROXY_HEADERS", IS_PRODUCTION)
SESSION_COOKIE_SECURE = environment_flag("SESSION_COOKIE_SECURE", IS_PRODUCTION)

# FERPA defaults
FERPA_ENFORCED = False  # Allow teachers to toggle between masked/full
DEFAULT_PII_MODE = os.environ.get("PII_MODE", "full")  # "masked" | "full"

# Public access / SSO defaults
ENABLE_GOOGLE_AUTH = environment_flag("ENABLE_GOOGLE_AUTH")
ENABLE_MICROSOFT_AUTH = environment_flag("ENABLE_MICROSOFT_AUTH")
ALLOW_SSO_AUTO_CREATE = environment_flag("ALLOW_SSO_AUTO_CREATE")

# For sessions (required to store the name-visibility toggle)
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me")
