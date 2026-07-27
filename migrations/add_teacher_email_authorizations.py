"""Add pre-login teacher authorizations keyed by normalized verified email."""

import argparse
import sqlite3
import time
from pathlib import Path


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS teacher_email_authorizations (
          email_authorization_id INTEGER PRIMARY KEY AUTOINCREMENT,
          normalized_email TEXT NOT NULL,
          display_name TEXT,
          user_id INTEGER,
          instructional_authorization_id INTEGER,
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending', 'active', 'deactivated', 'revoked')),
          created_at INTEGER NOT NULL,
          created_by INTEGER NOT NULL,
          claimed_at INTEGER,
          deactivated_at INTEGER,
          deactivated_by INTEGER,
          revoked_at INTEGER,
          revoked_by INTEGER,
          internal_note TEXT,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
          FOREIGN KEY(instructional_authorization_id)
            REFERENCES user_instructional_authorizations(authorization_id)
            ON DELETE RESTRICT,
          FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE RESTRICT,
          FOREIGN KEY(deactivated_by) REFERENCES users(id) ON DELETE SET NULL,
          FOREIGN KEY(revoked_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_teacher_email_authorizations_open
        ON teacher_email_authorizations(normalized_email)
        WHERE status IN ('pending', 'active', 'deactivated')
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_teacher_email_authorizations_user
        ON teacher_email_authorizations(user_id, status)
        """
    )
    conn.commit()


def run(database: Path) -> None:
    with sqlite3.connect(database) as conn:
        migrate(conn)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args()
    run(args.database.resolve(strict=True))
    print(f"Teacher email authorization migration complete: {args.database}")
