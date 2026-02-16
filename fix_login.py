import os, sqlite3
from werkzeug.security import generate_password_hash

BASE = os.path.abspath(".")
dbs = [
    os.path.join(BASE, "data", "ngss.db"),
]

username = "localteacher"
password = "test123"
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
        print("Updated existing user:", username)
    else:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active) VALUES (?, ?, 'teacher', 1)",
            (username, pw_hash),
        )
        print("Created new user:", username)

    conn.commit()
    conn.close()

print("\nLOGIN:", username, "/", password)
