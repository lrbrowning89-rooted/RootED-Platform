"""Add provider-neutral OIDC identities and preserve legacy SSO links."""

import argparse
import sqlite3
import time
from pathlib import Path


def migrate(conn: sqlite3.Connection) -> None:
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "account_role" not in user_columns:
        conn.execute(
            """
            ALTER TABLE users ADD COLUMN account_role TEXT
            CHECK(account_role IN ('pending', 'student', 'teacher'))
            """
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_auth_identities (
          identity_id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          provider TEXT NOT NULL,
          provider_subject TEXT NOT NULL,
          verified_email TEXT,
          display_name TEXT,
          avatar_url TEXT,
          created_at INTEGER NOT NULL,
          last_login_at INTEGER NOT NULL,
          revoked_at INTEGER,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
          UNIQUE(provider, provider_subject)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_identities_email "
        "ON user_auth_identities(verified_email)"
    )
    now_ts = int(time.time())
    conn.execute(
        """
        INSERT OR IGNORE INTO user_auth_identities
          (user_id, provider, provider_subject, verified_email,
           created_at, last_login_at)
        SELECT id, sso_provider, sso_subject, sso_email,
               COALESCE(last_login_ts, ?), COALESCE(last_login_ts, ?)
        FROM users
        WHERE sso_provider IS NOT NULL AND sso_subject IS NOT NULL
        """,
        (now_ts, now_ts),
    )
    conn.commit()


def downgrade(conn: sqlite3.Connection) -> None:
    """Roll back only when all new data fits the legacy schema."""
    pending = conn.execute(
        "SELECT COUNT(*) FROM users WHERE account_role='pending'"
    ).fetchone()[0]
    extra_identities = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT user_id FROM user_auth_identities
          WHERE revoked_at IS NULL GROUP BY user_id HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    if pending or extra_identities:
        raise RuntimeError(
            "Rollback refused: pending accounts or multi-provider links would lose data."
        )
    conn.execute(
        """
        UPDATE users
        SET sso_provider=(SELECT provider FROM user_auth_identities
                          WHERE user_id=users.id AND revoked_at IS NULL LIMIT 1),
            sso_subject=(SELECT provider_subject FROM user_auth_identities
                         WHERE user_id=users.id AND revoked_at IS NULL LIMIT 1),
            sso_email=(SELECT verified_email FROM user_auth_identities
                       WHERE user_id=users.id AND revoked_at IS NULL LIMIT 1)
        WHERE EXISTS (SELECT 1 FROM user_auth_identities
                      WHERE user_id=users.id AND revoked_at IS NULL)
        """
    )
    conn.execute("DROP INDEX IF EXISTS idx_auth_identities_email")
    conn.execute("DROP TABLE user_auth_identities")
    conn.execute("ALTER TABLE users DROP COLUMN account_role")
    conn.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Add provider-neutral OIDC identities to an existing database."
    )
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="Exact path of the existing RootED SQLite database to migrate.",
    )
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    with sqlite3.connect(database) as connection:
        migrate(connection)
        identity_table_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='user_auth_identities'
            """
        ).fetchone()
        integrity_result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if not identity_table_exists or integrity_result != "ok":
            raise RuntimeError("Migration verification failed.")
    print(f"Migrated database: {database}")
    print("Verified table: user_auth_identities")
    print("SQLite integrity check: ok")
