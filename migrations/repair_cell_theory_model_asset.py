import argparse
import sqlite3
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATIC_ROOT = ROOT / "app_core" / "static"

MODEL_ID = "MS-LS1-1B_model_cell_theory_basic_01"
TARGET_ASSET = {
    "model_id": MODEL_ID,
    "asset_type": "image",
    "src": "model_assets/ms-ls1-1b-model-cell-theory-basic-01.png",
    "alt_text": "A model showing living things made of cells to support basic cell theory.",
    "title": "Basic Cell Theory Model",
    "caption": "Supports questions about evidence for the idea that organisms are made of cells.",
}
CHECK_COLUMNS = ("asset_type", "src", "alt_text", "title", "caption")


def normalize_static_filename(src: str) -> str:
    value = (src or "").strip().replace("\\", "/")
    if value.startswith("/static/"):
        return value[len("/static/") :]
    if value.startswith("static/"):
        return value[len("static/") :]
    if value.startswith("app_core/static/"):
        return value[len("app_core/static/") :]
    return value.lstrip("/")


def expected_static_path(static_root: Path) -> Path:
    return static_root / normalize_static_filename(TARGET_ASSET["src"])


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
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def current_row(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT model_id, asset_type, src, alt_text, title, caption, created_at, updated_at
        FROM model_assets
        WHERE model_id = ?
        LIMIT 1
        """,
        (MODEL_ID,),
    ).fetchone()


def row_matches(row) -> bool:
    if not row:
        return False
    return all((row[column] or "") == TARGET_ASSET[column] for column in CHECK_COLUMNS)


def planned_action(row) -> str:
    if not row:
        return "insert"
    if row_matches(row):
        return "no-op"
    return "update"


def describe_row(row) -> str:
    if not row:
        return "missing"
    return (
        f"asset_type={row['asset_type']!r}, src={row['src']!r}, "
        f"title={row['title']!r}"
    )


def validate_static_file(static_root: Path) -> Path:
    asset_path = expected_static_path(static_root).resolve()
    static_root = static_root.resolve()
    try:
        asset_path.relative_to(static_root)
    except ValueError as exc:
        raise RuntimeError(f"Resolved asset path escapes static root: {asset_path}") from exc
    if not asset_path.is_file():
        raise RuntimeError(f"Required PNG is missing: {asset_path}")
    if asset_path.suffix.lower() != ".png":
        raise RuntimeError(f"Required asset is not a PNG: {asset_path}")
    return asset_path


def validate_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "model_assets"):
        raise RuntimeError("model_assets table is missing. Run add_model_assets_table first.")
    required = {
        "model_id",
        "asset_type",
        "src",
        "alt_text",
        "title",
        "caption",
        "created_at",
        "updated_at",
    }
    missing = sorted(required - table_columns(conn, "model_assets"))
    if missing:
        raise RuntimeError("model_assets missing required column(s): " + ", ".join(missing))


def repair(database: Path, static_root: Path, *, dry_run: bool, verify_only: bool) -> dict:
    asset_path = validate_static_file(static_root)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        validate_schema(conn)
        row = current_row(conn)
        action = planned_action(row)
        result = {
            "database": str(database),
            "static_root": str(static_root),
            "asset_path": str(asset_path),
            "model_id": MODEL_ID,
            "current": describe_row(row),
            "planned_action": action,
        }

        if verify_only:
            if not row_matches(row):
                raise RuntimeError(
                    f"Verification failed for {MODEL_ID}; current row is {describe_row(row)}"
                )
            result["verified"] = True
            return result

        if dry_run or action == "no-op":
            result["changed"] = False
            return result

        now = int(time.time())
        if action == "insert":
            conn.execute(
                """
                INSERT INTO model_assets
                  (model_id, asset_type, src, alt_text, title, caption, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    TARGET_ASSET["model_id"],
                    TARGET_ASSET["asset_type"],
                    TARGET_ASSET["src"],
                    TARGET_ASSET["alt_text"],
                    TARGET_ASSET["title"],
                    TARGET_ASSET["caption"],
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE model_assets
                SET asset_type = ?,
                    src = ?,
                    alt_text = ?,
                    title = ?,
                    caption = ?,
                    updated_at = ?
                WHERE model_id = ?
                """,
                (
                    TARGET_ASSET["asset_type"],
                    TARGET_ASSET["src"],
                    TARGET_ASSET["alt_text"],
                    TARGET_ASSET["title"],
                    TARGET_ASSET["caption"],
                    now,
                    TARGET_ASSET["model_id"],
                ),
            )
        conn.commit()
        result["changed"] = True
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def print_result(result: dict, *, dry_run: bool, verify_only: bool) -> None:
    print("RootED model asset metadata repair")
    print(f"database: {result['database']}")
    print(f"static_root: {result['static_root']}")
    print(f"model_id: {result['model_id']}")
    print(f"target_src: {TARGET_ASSET['src']}")
    print(f"static_file: exists ({result['asset_path']})")
    print(f"current_row: {result['current']}")
    print(f"planned_action: {result['planned_action']}")
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
        description="Repair the single approved MS-LS1-1B cell theory model asset row."
    )
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="Explicit path to the SQLite database to inspect or repair.",
    )
    parser.add_argument(
        "--static-root",
        default=DEFAULT_STATIC_ROOT,
        type=Path,
        help="Path to the deployed static directory. Defaults to app_core/static.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the planned insert/update without writing.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify the row and static file without writing.",
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
    try:
        result = repair(
            args.database,
            args.static_root,
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
