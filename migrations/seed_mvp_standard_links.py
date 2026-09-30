"""CLI wrapper for seeding MVP links in one explicit existing database."""

import argparse
import sqlite3
from pathlib import Path

try:
    from migrations.standard_links import seed_mvp_standard_links
except ModuleNotFoundError as exc:
    if exc.name != "migrations":
        raise
    from standard_links import seed_mvp_standard_links


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args(argv)
    database = args.database.expanduser().resolve(strict=True)
    if not database.is_file():
        parser.error("--database must identify an existing file")
    with sqlite3.connect(database) as connection:
        inserted = seed_mvp_standard_links(connection)
    print(f"Seeded {inserted} MVP standard_links rows: {database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
