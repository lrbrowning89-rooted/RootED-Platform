import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ngss.db"
NOW = int(time.time())

with sqlite3.connect(DB) as conn:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_assets (
            model_id TEXT PRIMARY KEY,
            asset_type TEXT NOT NULL,
            title TEXT,
            description TEXT,
            file_path TEXT,
            alt_text TEXT,
            caption TEXT,
            source TEXT,
            created_at INTEGER NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS migration_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT,
            details TEXT,
            ts INTEGER
        )
    """)

    conn.execute("""
        INSERT INTO migration_log (event, details, ts)
        VALUES (?, ?, ?)
    """, (
        "add_model_assets",
        "Created model_assets registry for reusable diagrams, images, tables, and graphs.",
        NOW,
    ))

    conn.commit()

print("model_assets table migration complete")
