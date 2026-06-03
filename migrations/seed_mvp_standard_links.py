import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "ngss.db"

ROWS = [
    ("4-LS1-1", "MS-LS1-1", "prerequisite", "NGSS LS1.A MVP pilot", 1, "Direct prerequisite support for MS-LS1-1"),
    ("MS-LS1-1", "MS-LS1-2", "progression", "NGSS LS1.A MVP pilot", 1, "Next LS1.A MVP progression target"),
    ("MS-LS1-1", "4-LS1-1", "remediation", "NGSS LS1.A MVP pilot", 1, "Lower-band remediation route for MS-LS1-1"),
    ("MS-LS1-2", "MS-LS1-3", "progression", "NGSS LS1.A MVP pilot", 1, "Next LS1.A MVP progression target"),
]

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

    for from_id, to_id, link_type, source, priority, notes in ROWS:
        existing = conn.execute("""
            SELECT id FROM standard_links
            WHERE from_standard_id = ?
              AND to_standard_id = ?
              AND link_type = ?
        """, (from_id, to_id, link_type)).fetchone()

        if not existing:
            conn.execute("""
                INSERT INTO standard_links
                  (from_standard_id, to_standard_id, link_type, source, priority, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (from_id, to_id, link_type, source, priority, notes, int(time.time())))

    conn.commit()

print("Seeded MVP standard_links rows")