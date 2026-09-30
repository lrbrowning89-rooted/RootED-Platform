"""Central runtime-environment and database-target safety policy."""

from __future__ import annotations

import os

from .database_safety import (
    DatabaseSafetyError,
    LEGACY_DATABASE_PATH,
    NON_PRODUCTION_NAME_MARKERS,
    PRODUCTION_NAME_MARKERS,
    PROJECT_ROOT,
    VALID_ROOTED_ENVIRONMENTS,
    environment_flag,
    resolve_rooted_environment,
    validate_database_target,
)


def resolve_database_path() -> str:
    return str(
        validate_database_target(
            os.environ.get("NGSS_DB"),
            rooted_env=ROOTED_ENV,
        )
    )


ROOTED_ENV = resolve_rooted_environment()
DATABASE_PATH = resolve_database_path()
IS_PRODUCTION = ROOTED_ENV == "production"
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
