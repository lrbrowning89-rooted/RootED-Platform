import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "ngss.db"

def add_column(conn, table, column, definition):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

with sqlite3.connect(DB) as conn:
    conn.execute("""
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
    """)

    add_column(conn, "progress_state", "current_standard_id", "TEXT")
    add_column(conn, "progress_state", "origin_standard_id", "TEXT")
    add_column(conn, "progress_state", "active_route_type", "TEXT DEFAULT 'normal'")

    conn.commit()

print("standard_links graph infrastructure migration complete")