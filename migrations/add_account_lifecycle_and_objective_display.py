import os
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("NGSS_DB", ROOT / "data" / "ngss.db"))


def add_column(conn, table, name, definition):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def run(db_path: Path = DB) -> None:
    with sqlite3.connect(db_path) as conn:
        for name, definition in [
            ("is_active", "INTEGER NOT NULL DEFAULT 1"),
            ("archived_at", "INTEGER"),
            ("archived_by_user_id", "INTEGER"),
            ("archive_reason", "TEXT"),
            ("reactivated_at", "INTEGER"),
            ("reactivated_by_user_id", "INTEGER"),
        ]:
            add_column(conn, "class_enrollments", name, definition)
        add_column(conn, "objectives", "display_name", "TEXT")
        add_column(conn, "objectives", "student_description", "TEXT")
        conn.execute(
            """
            UPDATE objectives SET display_name='Cell Anatomy'
            WHERE objective_id='MS-LS1-2A'
              AND (display_name IS NULL OR TRIM(display_name)='')
            """
        )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS authorization_lifecycle_audit_log (
              audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
              actor_user_id INTEGER NOT NULL, target_user_id INTEGER NOT NULL,
              action TEXT NOT NULL CHECK(action IN ('teacher_revoked')),
              authorization_id INTEGER,
              outcome TEXT NOT NULL CHECK(outcome IN ('revoked','already_revoked','blocked_active_classes')),
              reason TEXT, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_lifecycle_audit_log (
              audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
              actor_user_id INTEGER NOT NULL, target_user_id INTEGER NOT NULL,
              action TEXT NOT NULL CHECK(action IN ('account_deactivated','account_reactivated')),
              outcome TEXT NOT NULL CHECK(outcome IN ('deactivated','reactivated','already_deactivated','already_active','blocked_last_owner')),
              reason TEXT, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS class_membership_audit_log (
              audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
              actor_user_id INTEGER NOT NULL, class_id TEXT NOT NULL,
              student_id TEXT NOT NULL,
              action TEXT NOT NULL CHECK(action IN ('membership_archived')),
              outcome TEXT NOT NULL CHECK(outcome IN ('archived','already_archived')),
              reason TEXT, created_at INTEGER NOT NULL
            );
            """
        )
        conn.commit()


if __name__ == "__main__":
    run()
    print("account lifecycle and objective display migration complete")
