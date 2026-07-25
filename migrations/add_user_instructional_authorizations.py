import os
import sqlite3
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("NGSS_DB", ROOT / "data" / "ngss.db"))


def run(db_path: Path = DB) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_instructional_authorizations (
              authorization_id   INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id            INTEGER NOT NULL,
              instructional_role TEXT NOT NULL
                                 CHECK(instructional_role IN ('teacher')),
              granted_at         INTEGER NOT NULL,
              granted_by         INTEGER,
              revoked_at         INTEGER,
              revoked_by         INTEGER,
              grant_note         TEXT,
              revoke_note        TEXT,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
              FOREIGN KEY(granted_by) REFERENCES users(id) ON DELETE SET NULL,
              FOREIGN KEY(revoked_by) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
              uq_user_instructional_authorizations_active
            ON user_instructional_authorizations(user_id, instructional_role)
            WHERE revoked_at IS NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
              idx_user_instructional_authorizations_history
            ON user_instructional_authorizations(
              instructional_role, revoked_at, user_id, granted_at
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS access_authorization_audit_log (
              audit_id          INTEGER PRIMARY KEY AUTOINCREMENT,
              actor_user_id     INTEGER NOT NULL,
              target_user_id    INTEGER NOT NULL,
              action            TEXT NOT NULL
                                CHECK(action IN ('teacher_authorized')),
              authorization_id  INTEGER,
              outcome           TEXT NOT NULL
                                CHECK(outcome IN ('granted', 'already_granted')),
              created_at        INTEGER NOT NULL,
              request_ip        TEXT,
              user_agent        TEXT,
              FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE RESTRICT,
              FOREIGN KEY(target_user_id) REFERENCES users(id) ON DELETE RESTRICT,
              FOREIGN KEY(authorization_id)
                REFERENCES user_instructional_authorizations(authorization_id)
                ON DELETE RESTRICT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_access_authorization_audit_target
            ON access_authorization_audit_log(
              target_user_id, created_at, audit_id
            )
            """
        )
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO user_instructional_authorizations
              (user_id, instructional_role, granted_at, grant_note)
            SELECT id, 'teacher', COALESCE(last_login_ts, ?),
                   'Backfilled from existing teacher authorization'
            FROM users
            WHERE COALESCE(account_role, role) = 'teacher'
              AND NOT EXISTS (
                SELECT 1
                FROM user_instructional_authorizations existing
                WHERE existing.user_id = users.id
                  AND existing.instructional_role = 'teacher'
              )
            """,
            (now,),
        )
        conn.commit()


if __name__ == "__main__":
    run()
    print("instructional authorization migration complete")
