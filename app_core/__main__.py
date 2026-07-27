import os

from .dashboard_v5 import app


def enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    production = enabled("RENDER") or (
        os.environ.get("APP_ENV", "").strip().lower() == "production"
    )
    app.run(
        host=os.environ.get("HOST", "0.0.0.0" if production else "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=enabled("FLASK_DEBUG") and not production,
    )
