import argparse
import getpass
import re
import sqlite3
import sys
from pathlib import Path

from db_path import add_db_argument, print_database_path, resolve_database_path

REQUIRED_COLUMNS = {"username", "password_hash", "role", "is_active"}
ROLE_CHOICES = {"teacher", "owner"}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap exactly one initial approved RootED owner/teacher account."
    )
    add_db_argument(parser)
    parser.add_argument("--username", required=True, help="Username for the initial account.")
    parser.add_argument("--email", required=True, help="Email for the initial account.")
    parser.add_argument(
        "--role",
        default="teacher",
        choices=sorted(ROLE_CHOICES),
        help="Role for the initial account. Owner is allowed only if the current DB supports it.",
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


def table_sql(conn: sqlite3.Connection, table_name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row["sql"] if row and row["sql"] else ""


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def require_users_shape(conn: sqlite3.Connection) -> set[str]:
    if not table_exists(conn, "users"):
        fail("Required table is missing: users")
    columns = table_columns(conn, "users")
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        fail(f"users table is missing required columns: {', '.join(missing)}")
    return columns


def users_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
    return int(row["count"])


def role_is_supported(conn: sqlite3.Connection, role: str) -> bool:
    if role == "teacher":
        return True

    create_sql = table_sql(conn, "users").lower()
    if "check" not in create_sql:
        return True

    role_checks = re.findall(r"role\s+in\s*\(([^)]*)\)", create_sql)
    if not role_checks:
        return False
    allowed = {
        item.strip().strip("'\"").lower()
        for check in role_checks
        for item in check.split(",")
    }
    return role.lower() in allowed


def validate_inputs(username: str, email: str) -> None:
    if not username.strip():
        fail("Username cannot be blank.")
    if not email.strip() or "@" not in email:
        fail("A valid email is required.")


def prompt_password() -> str:
    password = getpass.getpass("New password: ")
    confirm = getpass.getpass("Confirm new password: ")
    if password != confirm:
        fail("Passwords did not match.")
    if len(password) < 12:
        fail("Password must be at least 12 characters.")
    return password


def safe_email(email: str) -> str:
    local, sep, domain = email.partition("@")
    if not sep:
        return "***"
    prefix = local[:2] if len(local) >= 2 else local[:1]
    return f"{prefix}***@{domain}"


def main() -> None:
    args = parse_args()
    db_path = resolve_database_path(args.db)
    username = args.username.strip()
    email = args.email.strip()

    validate_inputs(username, email)

    conn = connect(db_path)
    try:
        columns = require_users_shape(conn)
        count = users_count(conn)
        if count != 0:
            fail(
                "users table is not empty. Refusing initial bootstrap because "
                f"{count} user row(s) already exist."
            )
        if not role_is_supported(conn, args.role):
            fail(
                f"Role '{args.role}' is not supported by the current users table. "
                "Use --role teacher for the current app."
            )

        has_sso_email = "sso_email" in columns
        planned = {
            "username": username,
            "email": safe_email(email),
            "role": args.role,
            "is_active": 1,
            "sso_email_column_present": "yes" if has_sso_email else "no",
            "sso_provider": "not set",
            "sso_subject": "not set",
            "password_hash": "will be created",
        }

        print("Initial approved owner bootstrap plan")
        print_database_path(db_path)
        for key, value in planned.items():
            print(f"{key}: {value}")

        if args.dry_run:
            print("Mode: dry-run; no changes written.")
            return

        from werkzeug.security import generate_password_hash

        password = prompt_password()
        password_hash = generate_password_hash(password)

        insert_columns = ["username", "password_hash", "role", "is_active"]
        values = [username, password_hash, args.role, 1]
        if has_sso_email:
            insert_columns.append("sso_email")
            values.append(email)

        placeholders = ", ".join("?" for _ in insert_columns)
        column_sql = ", ".join(insert_columns)
        cursor = conn.execute(
            f"INSERT INTO users ({column_sql}) VALUES ({placeholders})",
            values,
        )
        conn.commit()

        print("Initial approved owner bootstrap complete.")
        print(f"user_id: {cursor.lastrowid}")
        print(f"username: {username}")
        print(f"email: {safe_email(email)}")
        print(f"role: {args.role}")
        print("is_active: 1")
        print("password_hash: created")
        print("sso_provider: not set")
        print("sso_subject: not set")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
