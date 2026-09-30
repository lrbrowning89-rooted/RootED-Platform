"""Safe, explicit standard-links schema and MVP seed operations."""

from __future__ import annotations

import sqlite3
import time

MVP_STANDARD_LINKS = (
    ("4-LS1-1", "MS-LS1-1", "prerequisite", "NGSS LS1.A MVP pilot", 1, "Direct prerequisite support for MS-LS1-1"),
    ("MS-LS1-1", "MS-LS1-2", "progression", "NGSS LS1.A MVP pilot", 1, "Next LS1.A MVP progression target"),
    ("MS-LS1-1", "4-LS1-1", "remediation", "NGSS LS1.A MVP pilot", 1, "Lower-band remediation route for MS-LS1-1"),
    ("MS-LS1-2", "MS-LS1-3", "progression", "NGSS LS1.A MVP pilot", 1, "Next LS1.A MVP progression target"),
)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone() is not None


def ensure_standard_links_schema(connection: sqlite3.Connection) -> None:
    """Create or validate canonical graph infrastructure on an open connection."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS standard_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_standard_id TEXT NOT NULL,
            to_standard_id TEXT NOT NULL,
            link_type TEXT NOT NULL,
            source TEXT,
            priority INTEGER DEFAULT 1,
            notes TEXT,
            created_at INTEGER,
            UNIQUE(from_standard_id, to_standard_id, link_type)
        )
        """
    )
    required_columns = {
        "id", "from_standard_id", "to_standard_id", "link_type", "source",
        "priority", "notes", "created_at",
    }
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(standard_links)")
    }
    if not required_columns <= columns:
        raise ValueError("standard_links does not match the canonical column contract")
    unique_sets = {
        tuple(item[2] for item in connection.execute(f"PRAGMA index_info({row[1]!r})"))
        for row in connection.execute("PRAGMA index_list('standard_links')")
        if row[2]
    }
    if ("from_standard_id", "to_standard_id", "link_type") not in unique_sets:
        raise ValueError("standard_links canonical uniqueness is missing")


def seed_mvp_standard_links(
    connection: sqlite3.Connection, *, created_at: int | None = None
) -> int:
    """Idempotently insert the four approved MVP links; return rows inserted."""
    ensure_standard_links_schema(connection)
    timestamp = int(time.time()) if created_at is None else created_at
    inserted = 0
    for from_id, to_id, link_type, source, priority, notes in MVP_STANDARD_LINKS:
        existing = connection.execute(
            """SELECT source, priority, notes FROM standard_links
               WHERE from_standard_id=? AND to_standard_id=? AND link_type=?""",
            (from_id, to_id, link_type),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (source, priority, notes):
                raise ValueError(
                    f"conflicting standard link: {from_id} -> {to_id} ({link_type})"
                )
        else:
            connection.execute(
                """INSERT INTO standard_links
                     (from_standard_id, to_standard_id, link_type, source,
                      priority, notes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (from_id, to_id, link_type, source, priority, notes, timestamp),
            )
            inserted += 1
    return inserted


def verify_mvp_standard_links(connection: sqlite3.Connection) -> None:
    """Require exactly the four approved graph rows and canonical uniqueness."""
    ensure_standard_links_schema(connection)
    rows = connection.execute(
        """SELECT from_standard_id, to_standard_id, link_type, source,
                  priority, notes
           FROM standard_links"""
    ).fetchall()
    expected = set(MVP_STANDARD_LINKS)
    actual = {tuple(row) for row in rows}
    if len(rows) != len(expected) or actual != expected:
        raise ValueError("standard_links does not contain exactly the approved MVP graph")
