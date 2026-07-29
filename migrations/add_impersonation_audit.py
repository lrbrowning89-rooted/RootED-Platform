import os
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("NGSS_DB", ROOT / "data" / "ngss.db"))


def run(db_path: Path = DB) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS impersonation_audit (
              audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
              correlation_id TEXT NOT NULL UNIQUE,
              acting_owner_user_id INTEGER NOT NULL,
              target_user_id INTEGER NOT NULL,
              target_role TEXT NOT NULL,
              start_timestamp INTEGER NOT NULL,
              stop_timestamp INTEGER,
              source_ip TEXT,
              outcome TEXT NOT NULL DEFAULT 'active',
              termination_reason TEXT,
              FOREIGN KEY(acting_owner_user_id) REFERENCES users(id) ON DELETE RESTRICT,
              FOREIGN KEY(target_user_id) REFERENCES users(id) ON DELETE RESTRICT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS impersonated_action_audit (
              action_audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
              impersonation_audit_id INTEGER NOT NULL,
              acting_owner_user_id INTEGER NOT NULL,
              effective_user_id INTEGER NOT NULL,
              request_method TEXT NOT NULL,
              request_path TEXT NOT NULL,
              response_status INTEGER NOT NULL,
              created_at INTEGER NOT NULL,
              FOREIGN KEY(impersonation_audit_id)
                REFERENCES impersonation_audit(audit_id) ON DELETE RESTRICT
            )
            """
        )


if __name__ == "__main__":
    run()
