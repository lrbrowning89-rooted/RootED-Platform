import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ngss.db"
NOW = int(time.time())

with sqlite3.connect(DB) as conn:
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

    conn.execute(
        """
        INSERT INTO migration_log (event, details, ts)
        VALUES (?, ?, ?)
        """,
        (
            "add_model_assets_table",
            "Created model_assets table for static model asset metadata. No asset rows inserted.",
            NOW,
        ),
    )

    conn.commit()

print("model_assets table migration complete")
