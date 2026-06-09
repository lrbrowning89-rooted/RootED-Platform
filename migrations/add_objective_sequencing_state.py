import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ngss.db"
NOW = int(time.time())

with sqlite3.connect(DB) as conn:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS student_objective_state (
            student_id TEXT NOT NULL,
            standard_id TEXT NOT NULL,
            current_objective_id TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            last_update INTEGER,
            PRIMARY KEY (student_id, standard_id)
        )
    """)

    conn.execute("""
        UPDATE objectives
        SET objective_text = 'Cell Structures and Functions'
        WHERE objective_id = 'MS-LS1-2A'
    """)

    conn.execute("""
        UPDATE objectives
        SET objective_text = 'Cell as a System'
        WHERE objective_id = 'MS-LS1-2B'
    """)

    conn.execute("""
        INSERT INTO migration_log (event, details, ts)
        VALUES (?, ?, ?)
    """, (
        "add_objective_sequencing_state",
        "Added student_objective_state table and corrected MS-LS1-2A/MS-LS1-2B objective text.",
        NOW,
    ))

    conn.commit()

print("Objective sequencing state migration complete")