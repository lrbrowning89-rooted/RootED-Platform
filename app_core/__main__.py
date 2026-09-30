import os

from .config import ROOTED_ENV
from .dashboard_v5 import app, initialize_database


def enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    initialize_database()
    production = ROOTED_ENV == "production"
    app.run(
        host=os.environ.get("HOST", "0.0.0.0" if production else "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=enabled("FLASK_DEBUG") and not production,
    )
