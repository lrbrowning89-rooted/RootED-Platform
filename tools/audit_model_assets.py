#!/usr/bin/env python3
"""
Read-only inventory for RootED model asset readiness.

This script opens the SQLite database in read-only mode and reports whether
question_metadata.model_id values have matching model_assets rows and usable
static PNG/SVG files.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "ngss.db"
DEFAULT_STATIC_ROOT = ROOT / "app_core" / "static"
SUPPORTED_EXTENSIONS = {".png", ".svg"}
EXPECTED_MODEL_ASSET_COLUMNS = {
    "model_id",
    "asset_type",
    "src",
    "alt_text",
    "title",
    "caption",
    "created_at",
    "updated_at",
}


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    db_path = db_path.resolve()
    uri = f"file:{quote(str(db_path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def choose_column(columns: set[str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def normalize_static_filename(src: str) -> str | None:
    src = (src or "").strip().replace("\\", "/")
    if not src:
        return None
    if src.startswith(("http://", "https://", "//")):
        return None
    if src.startswith("/static/"):
        src = src[len("/static/") :]
    elif src.startswith("static/"):
        src = src[len("static/") :]
    elif src.startswith("app_core/static/"):
        src = src[len("app_core/static/") :]
    else:
        src = src.lstrip("/")

    normalized = os.path.normpath(src).replace("\\", "/")
    if normalized.startswith("../") or normalized == "..":
        return None
    return normalized


def inspect_static_file(src: str | None, static_root: Path) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if not src:
        return "n/a", warnings

    stripped = src.strip()
    if stripped.startswith(("http://", "https://", "//")):
        warnings.append("remote src is not supported by the current image MVP")
        return "unsupported-remote", warnings

    static_filename = normalize_static_filename(stripped)
    if not static_filename:
        warnings.append("src is not a safe static path")
        return "invalid-path", warnings

    extension = Path(static_filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        warnings.append(f"unsupported extension: {extension or '(none)'}")
        return "unsupported-extension", warnings

    static_root = static_root.resolve()
    candidate = (static_root / static_filename).resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError:
        warnings.append("src resolves outside static root")
        return "invalid-path", warnings

    if not candidate.is_file():
        warnings.append("static file missing")
        return "missing", warnings

    return "exists", warnings


def get_model_id_rows(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], list[str]]:
    warnings: list[str] = []
    if not table_exists(conn, "question_metadata"):
        return [], ["missing question_metadata table"]

    columns = table_columns(conn, "question_metadata")
    required = {"question_id", "model_id"}
    missing = sorted(required - columns)
    if missing:
        return [], [f"question_metadata missing required column(s): {', '.join(missing)}"]

    rows = conn.execute(
        """
        SELECT model_id,
               COUNT(*) AS question_count,
               COUNT(DISTINCT question_id) AS distinct_question_count
        FROM question_metadata
        WHERE model_id IS NOT NULL
          AND TRIM(model_id) <> ''
        GROUP BY model_id
        ORDER BY model_id
        """
    ).fetchall()
    return rows, warnings


def get_assets_by_model_id(
    conn: sqlite3.Connection,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    warnings: list[str] = []
    if not table_exists(conn, "model_assets"):
        return {}, ["missing model_assets table"]

    columns = table_columns(conn, "model_assets")
    missing_expected = sorted(EXPECTED_MODEL_ASSET_COLUMNS - columns)
    if missing_expected:
        warnings.append(
            "model_assets missing approved column(s): "
            + ", ".join(missing_expected)
        )

    if "model_id" not in columns:
        return {}, ["model_assets missing required column: model_id"]

    asset_type_col = choose_column(columns, ["asset_type", "type"])
    src_col = choose_column(columns, ["src", "path", "url"])
    alt_col = choose_column(columns, ["alt_text", "alt"])
    title_col = choose_column(columns, ["title"])

    selected = ["model_id"]
    for column in [asset_type_col, src_col, alt_col, title_col]:
        if column and column not in selected:
            selected.append(column)

    missing_optional = []
    if not asset_type_col:
        missing_optional.append("asset_type/type")
    if not src_col:
        missing_optional.append("src/path/url")
    if not alt_col:
        missing_optional.append("alt_text/alt")
    if missing_optional:
        warnings.append(
            "model_assets missing optional audit column(s): "
            + ", ".join(missing_optional)
        )

    rows = conn.execute(
        f"""
        SELECT {", ".join(selected)}
        FROM model_assets
        WHERE model_id IS NOT NULL
          AND TRIM(model_id) <> ''
        ORDER BY model_id
        """
    ).fetchall()

    assets: dict[str, dict[str, str]] = {}
    for row in rows:
        model_id = (row["model_id"] or "").strip()
        assets[model_id] = {
            "asset_type": (row[asset_type_col] if asset_type_col else "") or "",
            "src": (row[src_col] if src_col else "") or "",
            "alt_text": (row[alt_col] if alt_col else "") or "",
            "title": (row[title_col] if title_col else "") or "",
        }
    return assets, warnings


def print_report(db_path: Path, static_root: Path) -> int:
    print("RootED Model Asset Readiness Audit")
    print("=" * 36)
    print(f"Database: {db_path}")
    print(f"Static root: {static_root}")
    print("Mode: read-only")
    print()

    if not db_path.exists():
        print(f"ERROR: database file not found: {db_path}")
        return 2

    with connect_readonly(db_path) as conn:
        model_rows, metadata_warnings = get_model_id_rows(conn)
        has_model_assets = table_exists(conn, "model_assets")
        model_asset_columns = table_columns(conn, "model_assets")
        assets_by_model_id, asset_table_warnings = get_assets_by_model_id(conn)

    warnings: list[str] = []
    warnings.extend(metadata_warnings)
    warnings.extend(asset_table_warnings)

    print("Summary")
    print("-------")
    print(f"Distinct model_id values: {len(model_rows)}")
    print(f"model_assets table present: {'yes' if has_model_assets else 'no'}")
    if has_model_assets:
        expected = sorted(EXPECTED_MODEL_ASSET_COLUMNS)
        present = sorted(EXPECTED_MODEL_ASSET_COLUMNS & model_asset_columns)
        missing = sorted(EXPECTED_MODEL_ASSET_COLUMNS - model_asset_columns)
        print(f"approved columns present: {len(present)} / {len(expected)}")
        if missing:
            print(f"approved columns missing: {', '.join(missing)}")
    print(f"model_assets rows available for audit: {len(assets_by_model_id)}")
    print()

    if not model_rows:
        print("No model_id values found to audit.")
        if warnings:
            print()
            print("Warnings")
            print("--------")
            for warning in warnings:
                print(f"- {warning}")
        return 0

    print("Inventory")
    print("---------")
    for row in model_rows:
        model_id = row["model_id"]
        question_count = int(row["question_count"] or 0)
        asset = assets_by_model_id.get(model_id)
        row_warnings: list[str] = []

        if asset:
            asset_status = "present"
            asset_type = asset["asset_type"] or "(blank)"
            src = asset["src"] or "(blank)"
            alt_text = asset["alt_text"] or ""
            file_status, file_warnings = inspect_static_file(asset["src"], static_root)
            row_warnings.extend(file_warnings)
            if not alt_text.strip():
                row_warnings.append("empty alt text")
        else:
            asset_status = "missing"
            asset_type = "n/a"
            src = "n/a"
            alt_text = "n/a"
            file_status = "n/a"
            if "missing model_assets table" not in asset_table_warnings:
                row_warnings.append("missing model_assets row")

        print(f"- model_id: {model_id}")
        print(f"  questions: {question_count}")
        print(f"  model_assets row: {asset_status}")
        print(f"  asset_type: {asset_type}")
        print(f"  src: {src}")
        print(f"  alt_text: {alt_text if alt_text else '(blank)'}")
        print(f"  static_file: {file_status}")
        if row_warnings:
            for warning in row_warnings:
                print(f"  WARN: {warning}")
                warnings.append(f"{model_id}: {warning}")
        print()

    print("Warnings")
    print("--------")
    if warnings:
        for warning in warnings:
            print(f"- {warning}")
    else:
        print("None")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit RootED model asset readiness without writing to the database."
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help=f"Path to SQLite database. Default: {DEFAULT_DB}",
    )
    parser.add_argument(
        "--static-root",
        default=str(DEFAULT_STATIC_ROOT),
        help=f"Path to Flask static root. Default: {DEFAULT_STATIC_ROOT}",
    )
    args = parser.parse_args()

    return print_report(Path(args.db), Path(args.static_root))


if __name__ == "__main__":
    raise SystemExit(main())
