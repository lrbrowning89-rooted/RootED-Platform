# config.py
import os

# FERPA defaults
FERPA_ENFORCED = False  # Allow teachers to toggle between masked/full
DEFAULT_PII_MODE = os.environ.get("PII_MODE", "full")  # "masked" | "full"

# Public access / SSO defaults
ENABLE_GOOGLE_AUTH = os.environ.get("ENABLE_GOOGLE_AUTH", "false").lower() == "true"
ENABLE_MICROSOFT_AUTH = os.environ.get("ENABLE_MICROSOFT_AUTH", "false").lower() == "true"
ALLOW_SSO_AUTO_CREATE = os.environ.get("ALLOW_SSO_AUTO_CREATE", "false").lower() == "true"

# For sessions (required to store the name-visibility toggle)
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me")
