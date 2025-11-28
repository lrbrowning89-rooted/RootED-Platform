# config.py
import os

# FERPA defaults
FERPA_ENFORCED = False  # Allow teachers to toggle between masked/full
DEFAULT_PII_MODE = os.environ.get("PII_MODE", "full")  # "masked" | "full"

# For sessions (required to store the name-visibility toggle)
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me")
