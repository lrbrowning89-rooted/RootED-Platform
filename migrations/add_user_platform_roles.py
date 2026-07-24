import os
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("NGSS_DB", ROOT / "data" / "ngss.db"))


def run(db_path: Path = DB) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        table_exists = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'user_platform_roles'
            """
        ).fetchone()
        if table_exists:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(user_platform_roles)")
            }
            if "grant_id" not in columns:
                conn.execute(
                    "ALTER TABLE user_platform_roles RENAME TO user_platform_roles_legacy"
                )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_platform_roles (
              grant_id      INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id       INTEGER NOT NULL,
              platform_role TEXT NOT NULL CHECK(platform_role IN ('owner')),
              granted_at    INTEGER NOT NULL,
              granted_by    INTEGER,
              revoked_at    INTEGER,
              revoked_by    INTEGER,
              grant_note    TEXT,
              revoke_note   TEXT,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
              FOREIGN KEY(granted_by) REFERENCES users(id) ON DELETE SET NULL,
              FOREIGN KEY(revoked_by) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        legacy_exists = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'user_platform_roles_legacy'
            """
        ).fetchone()
        if legacy_exists:
            conn.execute(
                """
                INSERT INTO user_platform_roles
                  (user_id, platform_role, granted_at, grant_note)
                SELECT user_id, platform_role, granted_at,
                       'Migrated from Owner Role Phase 1'
                FROM user_platform_roles_legacy
                """
            )
            conn.execute("DROP TABLE user_platform_roles_legacy")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_user_platform_roles_active
            ON user_platform_roles(user_id, platform_role)
            WHERE revoked_at IS NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_platform_roles_history
            ON user_platform_roles(platform_role, revoked_at, user_id, granted_at)
            """
        )
        conn.commit()


if __name__ == "__main__":
    run()
    print("user_platform_roles table migration complete")
