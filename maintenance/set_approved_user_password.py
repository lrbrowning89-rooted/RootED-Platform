import argparse
import getpass
import sqlite3
import sys
from pathlib import Path

from db_path import add_db_argument, print_database_path, resolve_database_path

ALLOWED_ROLES = {"teacher"}
REQUIRED_COLUMNS = {"id", "username", "password_hash", "role", "is_active"}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set/reset a password for one existing approved RootED user."
    )
    add_db_argument(parser)
    lookup = parser.add_mutually_exclusive_group(required=True)
    lookup.add_argument("--username", help="Existing users.username to update.")
    lookup.add_argument("--email", help="Existing users.sso_email to update.")
    parser.add_argument(
        "--role",
        default="teacher",
        choices=sorted(ALLOWED_ROLES),
        help="Role to ensure for this approved account.",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Set is_active to 1. Required unless --dry-run is used for inspection.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print planned changes without writing to the database.",
    )
    return parser.parse_args()


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        fail(f"Database file not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def require_users_shape(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "users"):
        fail("Required table is missing: users")
    columns = table_columns(conn, "users")
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        fail(f"users table is missing required columns: {', '.join(missing)}")


def find_user(conn: sqlite3.Connection, username: str | None, email: str | None) -> sqlite3.Row:
    if username:
        rows = conn.execute(
            """
            SELECT id, username, role, is_active, sso_provider, sso_email
            FROM users
            WHERE username = ?
            """,
            (username,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, username, role, is_active, sso_provider, sso_email
            FROM users
            WHERE lower(sso_email) = lower(?)
            """,
            (email,),
        ).fetchall()

    if not rows:
        fail("No existing approved user matched. Refusing to create a user.")
    if len(rows) > 1:
        fail("Multiple users matched. Refusing to update an ambiguous account.")
    return rows[0]


def prompt_password() -> str:
    password = getpass.getpass("New password: ")
    confirm = getpass.getpass("Confirm new password: ")
    if password != confirm:
        fail("Passwords did not match.")
    if len(password) < 12:
        fail("Password must be at least 12 characters.")
    return password


def safe_email(email: str | None) -> str:
    if not email:
        return ""
    local, sep, domain = email.partition("@")
    if not sep:
        return "***"
    prefix = local[:2] if len(local) >= 2 else local[:1]
    return f"{prefix}***@{domain}"


def main() -> None:
    args = parse_args()
    db_path = resolve_database_path(args.db)

    if args.role not in ALLOWED_ROLES:
        fail(f"Role is not allowed for this recovery script: {args.role}")
    if not args.activate and not args.dry_run:
        fail("--activate is required for writes so the intended status change is explicit.")

    conn = connect(db_path)
    try:
        require_users_shape(conn)
        user = find_user(conn, args.username, args.email)

        target_active = 1 if args.activate else int(user["is_active"])
        planned = {
            "id": user["id"],
            "username": user["username"],
            "sso_provider": user["sso_provider"] or "",
            "sso_email": safe_email(user["sso_email"]),
            "role_before": user["role"],
            "role_after": args.role,
            "is_active_before": int(user["is_active"]),
            "is_active_after": target_active,
            "password_hash": "will change",
        }

        print("Approved user password reset plan")
        print_database_path(db_path)
        for key, value in planned.items():
            print(f"{key}: {value}")

        if args.dry_run:
            print("Mode: dry-run; no changes written.")
            return

        from werkzeug.security import generate_password_hash

        password = prompt_password()
        password_hash = generate_password_hash(password)
        conn.execute(
            """
            UPDATE users
            SET password_hash = ?,
                role = ?,
                is_active = ?
            WHERE id = ?
            """,
            (password_hash, args.role, target_active, user["id"]),
        )
        conn.commit()

        print("Password reset complete.")
        print(f"user_id: {user['id']}")
        print(f"username: {user['username']}")
        print(f"role: {user['role']} -> {args.role}")
        print(f"is_active: {int(user['is_active'])} -> {target_active}")
        print("password_hash: changed")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
