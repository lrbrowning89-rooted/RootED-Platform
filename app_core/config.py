# config.py
import os
from pathlib import Path

from dotenv import load_dotenv


# Local development reads the repository-root .env file. Existing process
# variables always win, so production hosts continue to use their normal
# environment/secret configuration.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)

def resolve_database_path() -> str:
    """Return one absolute DB path; never pass an empty filename to SQLite."""
    configured_database = os.environ.get("NGSS_DB", "").strip()
    return str(
        Path(configured_database).expanduser().resolve()
        if configured_database
        else (PROJECT_ROOT / "data" / "ngss.db").resolve()
    )


DATABASE_PATH = resolve_database_path()

# FERPA defaults
FERPA_ENFORCED = False  # Allow teachers to toggle between masked/full
DEFAULT_PII_MODE = os.environ.get("PII_MODE", "full")  # "masked" | "full"

# Public access / SSO defaults
ENABLE_GOOGLE_AUTH = os.environ.get("ENABLE_GOOGLE_AUTH", "false").lower() == "true"
ENABLE_MICROSOFT_AUTH = os.environ.get("ENABLE_MICROSOFT_AUTH", "false").lower() == "true"
ALLOW_SSO_AUTO_CREATE = os.environ.get("ALLOW_SSO_AUTO_CREATE", "false").lower() == "true"

# For sessions (required to store the name-visibility toggle)
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me")
