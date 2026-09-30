"""Read-only structural inspection for already-open SQLite connections."""

from __future__ import annotations

import sqlite3


def table_names(connection: sqlite3.Connection) -> frozenset[str]:
    """Return user-defined table names."""
    return frozenset(row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ))


def table_columns(connection: sqlite3.Connection, table: str) -> tuple[tuple, ...]:
    """Return PRAGMA table_info rows for a caller-selected table."""
    return tuple(connection.execute(f"PRAGMA table_info({table!r})"))


def index_names(connection: sqlite3.Connection, table: str | None = None) -> frozenset[str]:
    """Return explicit index names, optionally restricted to one table."""
    if table is None:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        )
    else:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL AND tbl_name=?",
            (table,),
        )
    return frozenset(row[0] for row in rows)


def unique_column_sets(connection: sqlite3.Connection, table: str) -> frozenset[tuple[str, ...]]:
    """Return ordered column tuples for every unique index on a table."""
    result: set[tuple[str, ...]] = set()
    for row in connection.execute(f"PRAGMA index_list({table!r})"):
        if row[2]:
            result.add(tuple(item[2] for item in connection.execute(
                f"PRAGMA index_info({row[1]!r})"
            )))
    return frozenset(result)


def foreign_keys(connection: sqlite3.Connection, table: str) -> tuple[tuple, ...]:
    """Return foreign-key definitions for a table."""
    return tuple(connection.execute(f"PRAGMA foreign_key_list({table!r})"))


def metadata_schema_version(connection: sqlite3.Connection) -> int | None:
    """Return schema version when the metadata table and singleton row exist."""
    if "rooted_database_metadata" not in table_names(connection):
        return None
    row = connection.execute(
        "SELECT schema_version FROM rooted_database_metadata WHERE singleton_id=1"
    ).fetchone()
    return None if row is None else row[0]
