import os
import sqlite3
import sys

from werkzeug.security import generate_password_hash

BASE = os.path.abspath(".")
dbs = [
    os.path.join(BASE, "data", "ngss.db"),
]

username = os.environ.get("ROOTED_MAINTENANCE_USERNAME", "").strip()
password = os.environ.get("ROOTED_MAINTENANCE_PASSWORD", "")
if not username or not password:
    print(
        "Set ROOTED_MAINTENANCE_USERNAME and ROOTED_MAINTENANCE_PASSWORD "
        "before running this maintenance helper.",
        file=sys.stderr,
    )
    raise SystemExit(2)

pw_hash = generate_password_hash(password)

for db_path in dbs:
    print("\n--- Updating DB:", db_path)
    conn = sqlite3.connect(db_path)

    # Ensure column exists (older DBs might not have it)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        print("Added is_active column.")
    except Exception:
        pass

    # Create or update the user + force active
    row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row:
        conn.execute(
            "UPDATE users SET password_hash=?, role='teacher', is_active=1 WHERE username=?",
            (pw_hash, username),
        )
        print("Updated the requested existing user.")
    else:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active) VALUES (?, ?, 'teacher', 1)",
            (username, pw_hash),
        )
        print("Created the requested user.")

    conn.commit()
    conn.close()

print("\nLogin credential updated. The password was not printed.")
