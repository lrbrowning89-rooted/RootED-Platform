import argparse
import sqlite3
import sys
import time
from pathlib import Path


MIGRATION_EVENT = "add_model_assets_table"
MIGRATION_DETAILS = (
    "Created model_assets table for static model asset metadata. No asset rows inserted."
)

MODEL_ASSETS_COLUMNS = {
    "model_id",
    "asset_type",
    "src",
    "alt_text",
    "title",
    "caption",
    "created_at",
    "updated_at",
}


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = ?
            LIMIT 1
            """,
            (table_name,),
        ).fetchone()
        is not None
    )


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def migration_logged(conn: sqlite3.Connection) -> bool:
    if not table_exists(conn, "migration_log"):
        return False
    return (
        conn.execute(
            "SELECT 1 FROM migration_log WHERE event = ? LIMIT 1",
            (MIGRATION_EVENT,),
        ).fetchone()
        is not None
    )


def schema_is_valid(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    problems = []
    if not table_exists(conn, "migration_log"):
        problems.append("missing migration_log table")
    if not table_exists(conn, "model_assets"):
        problems.append("missing model_assets table")
    else:
        missing = sorted(MODEL_ASSETS_COLUMNS - table_columns(conn, "model_assets"))
        if missing:
            problems.append("model_assets missing column(s): " + ", ".join(missing))
    if not migration_logged(conn):
        problems.append(f"missing migration_log event: {MIGRATION_EVENT}")
    return not problems, problems


def ensure_model_assets_schema(
    database: Path,
    *,
    dry_run: bool = False,
    verify_only: bool = False,
) -> dict:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        before_valid, before_problems = schema_is_valid(conn)
        before_log_count = (
            conn.execute(
                "SELECT COUNT(*) FROM migration_log WHERE event = ?",
                (MIGRATION_EVENT,),
            ).fetchone()[0]
            if table_exists(conn, "migration_log")
            else 0
        )
        result = {
            "database": str(database),
            "before_valid": before_valid,
            "before_problems": before_problems,
            "planned_action": "no-op" if before_valid else "create-or-repair-schema",
            "changed": False,
        }

        if verify_only:
            if not before_valid:
                raise RuntimeError("Verification failed: " + "; ".join(before_problems))
            result["verified"] = True
            return result

        if dry_run or before_valid:
            return result

        now = int(time.time())
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS migration_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event TEXT,
              details TEXT,
              ts INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_assets (
              model_id   TEXT PRIMARY KEY CHECK (TRIM(model_id) <> ''),
              asset_type TEXT NOT NULL CHECK (asset_type IN ('image')),
              src        TEXT NOT NULL CHECK (TRIM(src) <> ''),
              alt_text   TEXT NOT NULL,
              title      TEXT,
              caption    TEXT,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            )
            """
        )
        if not migration_logged(conn):
            conn.execute(
                """
                INSERT INTO migration_log (event, details, ts)
                VALUES (?, ?, ?)
                """,
                (MIGRATION_EVENT, MIGRATION_DETAILS, now),
            )
        conn.commit()

        after_valid, after_problems = schema_is_valid(conn)
        if not after_valid:
            raise RuntimeError("Migration did not produce valid schema: " + "; ".join(after_problems))
        after_log_count = conn.execute(
            "SELECT COUNT(*) FROM migration_log WHERE event = ?",
            (MIGRATION_EVENT,),
        ).fetchone()[0]
        result["changed"] = True
        result["migration_log_rows_before"] = before_log_count
        result["migration_log_rows_after"] = after_log_count
        result["after_valid"] = after_valid
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def print_result(result: dict, *, dry_run: bool, verify_only: bool) -> None:
    print("RootED model_assets table migration")
    print(f"database: {result['database']}")
    print(f"planned_action: {result['planned_action']}")
    if result["before_problems"]:
        print("before:")
        for problem in result["before_problems"]:
            print(f"  - {problem}")
    else:
        print("before: schema already valid")
    if verify_only:
        print("VERIFY OK")
    elif dry_run:
        print("DRY RUN - no changes committed")
    elif result.get("changed"):
        print("APPLIED")
    else:
        print("NO CHANGE")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Create the RootED model_assets table safely on an explicit SQLite database."
    )
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="Explicit path to the SQLite database to migrate.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report planned schema work without writing.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify the migration is already applied without writing.",
    )
    args = parser.parse_args(argv)
    if args.dry_run and args.verify_only:
        parser.error("--dry-run and --verify-only cannot be used together")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.database.exists():
        print(f"ERROR: database does not exist: {args.database}", file=sys.stderr)
        return 2
    if not args.database.is_file():
        print(f"ERROR: database path is not a file: {args.database}", file=sys.stderr)
        return 2
    try:
        result = ensure_model_assets_schema(
            args.database,
            dry_run=args.dry_run,
            verify_only=args.verify_only,
        )
        print_result(result, dry_run=args.dry_run, verify_only=args.verify_only)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
