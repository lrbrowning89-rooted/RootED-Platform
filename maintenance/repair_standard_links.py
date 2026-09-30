"""Explicit owner-operated repair for the production standard_links table."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sqlite3
import sys

from app_core.database.inspection import table_columns, table_names, unique_column_sets
from app_core.database_safety import (
    DatabaseSafetyError,
    resolve_rooted_environment,
    validate_database_target,
)
from migrations.standard_links import (
    MVP_STANDARD_LINKS,
    ensure_standard_links_schema,
    seed_mvp_standard_links,
    verify_mvp_standard_links,
)


REQUIRED_COLUMNS = frozenset({
    "id", "from_standard_id", "to_standard_id", "link_type", "source",
    "priority", "notes", "created_at",
})
REQUIRED_UNIQUENESS = ("from_standard_id", "to_standard_id", "link_type")
EXPECTED_BY_KEY = {
    (from_id, to_id, link_type): (source, priority, notes)
    for from_id, to_id, link_type, source, priority, notes in MVP_STANDARD_LINKS
}


class StandardLinksRepairError(RuntimeError):
    """Raised when the narrow production repair cannot proceed safely."""


@dataclass(frozen=True)
class PreflightResult:
    target: str
    schema_status: str
    graph_status: str
    approved_rows_present: int
    approved_rows_missing: int
    quick_check: str
    foreign_key_check: str


def validate_production_target(
    database: str | os.PathLike[str] | None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Validate one explicit existing production target without opening it."""
    source = os.environ if environ is None else environ
    rooted_env = resolve_rooted_environment(source)
    if rooted_env != "production":
        raise DatabaseSafetyError(
            "standard_links production repair requires ROOTED_ENV=production"
        )
    raw = str(database or "").strip()
    if not raw:
        raise DatabaseSafetyError("--database is required")
    raw_path = Path(raw).expanduser()
    if not raw_path.is_absolute():
        raise DatabaseSafetyError("--database must be an absolute path")
    if raw_path.is_symlink():
        raise DatabaseSafetyError("--database may not be a symbolic link")
    target = validate_database_target(raw_path, rooted_env=rooted_env)
    configured = source.get("NGSS_DB")
    if configured is not None:
        configured_target = validate_database_target(configured, rooted_env=rooted_env)
        if configured_target != target:
            raise DatabaseSafetyError(
                "NGSS_DB and --database must resolve to the same target"
            )
    if target.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise DatabaseSafetyError("database target must use .db, .sqlite, or .sqlite3")
    if not target.exists() or not target.is_file():
        raise StandardLinksRepairError(f"database target must already exist: {target}")
    return target


def _standard_links_rows(connection: sqlite3.Connection) -> list[tuple]:
    return list(connection.execute(
        """SELECT from_standard_id, to_standard_id, link_type,
                  source, priority, notes
           FROM standard_links"""
    ))


def _inspect_graph(connection: sqlite3.Connection) -> tuple[str, str, int, int]:
    if "standard_links" not in table_names(connection):
        return "absent", "absent", 0, len(EXPECTED_BY_KEY)
    columns = {row[1] for row in table_columns(connection, "standard_links")}
    if columns != REQUIRED_COLUMNS:
        raise StandardLinksRepairError("standard_links schema is incompatible")
    if REQUIRED_UNIQUENESS not in unique_column_sets(connection, "standard_links"):
        raise StandardLinksRepairError("standard_links canonical uniqueness is missing")
    rows = _standard_links_rows(connection)
    seen: set[tuple[str, str, str]] = set()
    for from_id, to_id, link_type, source, priority, notes in rows:
        key = (from_id, to_id, link_type)
        expected = EXPECTED_BY_KEY.get(key)
        if expected is None:
            raise StandardLinksRepairError(
                "unexpected extra standard_links relationship requires owner review"
            )
        if key in seen:
            raise StandardLinksRepairError("duplicate approved standard_links relationship")
        seen.add(key)
        if (source, priority, notes) != expected:
            raise StandardLinksRepairError(
                "conflicting approved standard_links relationship requires owner review"
            )
    present = len(seen)
    missing = len(EXPECTED_BY_KEY) - present
    graph_status = "exact" if missing == 0 else ("empty" if present == 0 else "partial-compatible")
    return "canonical", graph_status, present, missing


def inspect_connection(
    connection: sqlite3.Connection, *, target_label: str
) -> PreflightResult:
    """Perform the narrow read-only schema, graph, and integrity inspection."""
    schema_status, graph_status, present, missing = _inspect_graph(connection)
    quick = connection.execute("PRAGMA quick_check").fetchone()
    if not quick or quick[0] != "ok":
        raise StandardLinksRepairError("SQLite quick_check failed")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise StandardLinksRepairError(
            f"SQLite foreign_key_check reported {len(violations)} violation(s)"
        )
    return PreflightResult(
        target=target_label,
        schema_status=schema_status,
        graph_status=graph_status,
        approved_rows_present=present,
        approved_rows_missing=missing,
        quick_check="ok",
        foreign_key_check="ok",
    )


def verify_database(
    database: str | os.PathLike[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> PreflightResult:
    """Open an explicit production target read-only and perform narrow preflight."""
    target = validate_production_target(database, environ)
    uri = f"{target.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        return inspect_connection(connection, target_label=str(target))


def migrate_connection(
    connection: sqlite3.Connection,
    *,
    target_label: str,
) -> PreflightResult:
    """Run the narrow repair in one transaction on an already-open connection."""
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise StandardLinksRepairError("could not enable SQLite foreign keys")
    inspect_connection(connection, target_label=target_label)
    connection.execute("BEGIN IMMEDIATE")
    try:
        ensure_standard_links_schema(connection)
        seed_mvp_standard_links(connection)
        verify_mvp_standard_links(connection)
        result = inspect_connection(connection, target_label=target_label)
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise


def migrate_database(
    database: str | os.PathLike[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> PreflightResult:
    """Open and repair one validated explicit existing production database."""
    target = validate_production_target(database, environ)
    uri = f"{target.as_uri()}?mode=rw"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return migrate_connection(connection, target_label=str(target))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("verify", "migrate"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--database", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = (
            verify_database(args.database)
            if args.command == "verify"
            else migrate_database(args.database)
        )
    except (DatabaseSafetyError, StandardLinksRepairError, sqlite3.Error, OSError) as exc:
        print(f"standard_links repair failed: {exc}", file=sys.stderr)
        return 1
    payload = asdict(result)
    payload["migration_status"] = "verified-only" if args.command == "verify" else "committed"
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
