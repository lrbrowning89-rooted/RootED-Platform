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
    """Create graph infrastructure on an explicitly supplied connection."""
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
            created_at INTEGER
        )
        """
    )
    if _table_exists(connection, "progress_state"):
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(progress_state)")
        }
        for name, definition in (
            ("current_standard_id", "TEXT"),
            ("origin_standard_id", "TEXT"),
            ("active_route_type", "TEXT DEFAULT 'normal'"),
        ):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE progress_state ADD COLUMN {name} {definition}"
                )


def seed_mvp_standard_links(
    connection: sqlite3.Connection, *, created_at: int | None = None
) -> int:
    """Idempotently insert the four approved MVP links; return rows inserted."""
    ensure_standard_links_schema(connection)
    timestamp = int(time.time()) if created_at is None else created_at
    inserted = 0
    for from_id, to_id, link_type, source, priority, notes in MVP_STANDARD_LINKS:
        existing = connection.execute(
            """SELECT id FROM standard_links
               WHERE from_standard_id=? AND to_standard_id=? AND link_type=?""",
            (from_id, to_id, link_type),
        ).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO standard_links
                     (from_standard_id, to_standard_id, link_type, source,
                      priority, notes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (from_id, to_id, link_type, source, priority, notes, timestamp),
            )
            inserted += 1
    return inserted
