"""CLI wrapper for standard-links schema creation on one explicit database."""

import argparse
import sqlite3
from pathlib import Path

try:
    from migrations.standard_links import ensure_standard_links_schema
except ModuleNotFoundError as exc:
    if exc.name != "migrations":
        raise
    from standard_links import ensure_standard_links_schema


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args(argv)
    database = args.database.expanduser().resolve(strict=True)
    if not database.is_file():
        parser.error("--database must identify an existing file")
    with sqlite3.connect(database) as connection:
        ensure_standard_links_schema(connection)
    print(f"standard_links graph infrastructure ready: {database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
