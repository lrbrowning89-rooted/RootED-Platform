import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def resolve_database_path(db_arg: str | None = None) -> Path:
    if db_arg:
        return Path(db_arg).expanduser().resolve()

    env_db = os.environ.get("NGSS_DB")
    if env_db:
        return Path(env_db).expanduser().resolve()

    return (ROOT / "data" / "ngss.db").resolve()


def add_db_argument(parser) -> None:
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database. Defaults to NGSS_DB or data/ngss.db.",
    )


def print_database_path(db_path: Path) -> None:
    print("Database:")
    print(f"  {db_path}")
