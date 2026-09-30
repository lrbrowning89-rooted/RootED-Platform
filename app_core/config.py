"""Central runtime-environment and database-target safety policy."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_DATABASE_PATH = (PROJECT_ROOT / "data" / "ngss.db").resolve()
VALID_ROOTED_ENVIRONMENTS = frozenset({"development", "test", "production"})
NON_PRODUCTION_NAME_MARKERS = ("dev", "test", "synthetic", "temp", "tmp")
PRODUCTION_NAME_MARKERS = ("prod", "production", "live")


class DatabaseSafetyError(RuntimeError):
    """Raised before SQLite is opened when database policy is violated."""


def environment_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolve_rooted_environment() -> str:
    """Return the required, explicitly configured RootED environment."""
    configured = os.environ.get("ROOTED_ENV", "").strip().lower()
    if configured not in VALID_ROOTED_ENVIRONMENTS:
        raise DatabaseSafetyError(
            "ROOTED_ENV is required and must be one of: "
            "development, test, production"
        )
    return configured


def _path_contains_marker(path: Path, markers: tuple[str, ...]) -> bool:
    parts = tuple(part.lower() for part in path.parts)
    return any(marker in part for part in parts for marker in markers)


def validate_database_target(
    configured_database: str | os.PathLike[str] | None,
    *,
    rooted_env: str,
) -> Path:
    """Validate a target path without opening, reading, or creating it."""
    if rooted_env not in VALID_ROOTED_ENVIRONMENTS:
        raise DatabaseSafetyError(f"Unsupported ROOTED_ENV: {rooted_env!r}")

    raw_path = str(configured_database or "").strip()
    if not raw_path:
        raise DatabaseSafetyError(
            f"NGSS_DB must be explicitly configured for ROOTED_ENV={rooted_env}"
        )

    target = Path(raw_path).expanduser().resolve(strict=False)
    if rooted_env in {"development", "test"}:
        if target == LEGACY_DATABASE_PATH or target.name.lower() == "ngss.db":
            raise DatabaseSafetyError(
                "data/ngss.db and files named ngss.db are prohibited in "
                f"ROOTED_ENV={rooted_env}"
            )
        if _path_contains_marker(target, PRODUCTION_NAME_MARKERS):
            raise DatabaseSafetyError(
                "Production-looking database paths are prohibited in "
                f"ROOTED_ENV={rooted_env}: {target}"
            )
        if not _path_contains_marker(target, NON_PRODUCTION_NAME_MARKERS):
            raise DatabaseSafetyError(
                "Development/test database paths must visibly contain one of: "
                + ", ".join(NON_PRODUCTION_NAME_MARKERS)
            )
    elif _path_contains_marker(target, NON_PRODUCTION_NAME_MARKERS):
        raise DatabaseSafetyError(
            f"Development/test/synthetic database paths are prohibited in production: {target}"
        )

    return target


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
