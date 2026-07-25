"""Audit and configure RootED authority through verified SSO identities."""

import argparse
import secrets
import sqlite3
import sys
import time
import uuid
from pathlib import Path


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def masked_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}" if domain else "***"


def masked_subject(subject: str) -> str:
    if len(subject) <= 16:
        return f"{subject[:4]}...{subject[-4:]}"
    return f"{subject[:8]}...{subject[-8:]}"


def connect_existing(database: Path) -> sqlite3.Connection:
    database = database.expanduser().resolve(strict=True)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    required = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = {
        "users",
        "user_auth_identities",
        "user_platform_roles",
        "class_sections",
        "class_enrollments",
    } - required
    if missing:
        connection.close()
        fail("Required tables are missing: " + ", ".join(sorted(missing)))
    return connection


def identity_user(
    connection: sqlite3.Connection,
    provider: str,
    email: str | None = None,
    provider_subject: str | None = None,
) -> sqlite3.Row:
    if bool(email) == bool(provider_subject):
        fail("Specify exactly one identity selector.")
    if provider_subject:
        if provider != "microsoft":
            fail("--provider-subject requires --provider microsoft.")
        where = "i.provider_subject=? AND i.provider=?"
        parameters = (provider_subject, provider)
        missing_message = (
            "No active Microsoft SSO identity matched the exact provider subject."
        )
        ambiguous_message = (
            "Multiple active Microsoft identities matched that provider subject; "
            "refusing an ambiguous change."
        )
    else:
        where = "lower(trim(i.verified_email))=lower(trim(?)) AND i.provider=?"
        parameters = (email, provider)
        missing_message = (
            f"No active verified SSO identity matched {masked_email(email or '')}. "
            "Sign in with that provider account first."
        )
        ambiguous_message = (
            "Multiple active identities matched that verified email; "
            "refusing an ambiguous change."
        )
    rows = connection.execute(
        f"""
        SELECT u.id, u.username, u.account_role, u.role, u.is_active,
               u.linked_student_id, i.provider, i.identity_id,
               i.provider_subject, i.verified_email, i.display_name
        FROM user_auth_identities i
        JOIN users u ON u.id=i.user_id
        WHERE {where}
          AND i.revoked_at IS NULL
        """,
        parameters,
    ).fetchall()
    user_ids = {row["id"] for row in rows}
    if not rows:
        fail(missing_message)
    if len(user_ids) != 1:
        fail(ambiguous_message)
    if len(rows) != 1:
        fail(ambiguous_message)
    user = rows[0]
    if int(user["is_active"]) != 1:
        fail("The matched RootED account is inactive.")
    return user


def authorization_state(connection: sqlite3.Connection, user: sqlite3.Row) -> dict:
    owner = connection.execute(
        """
        SELECT grant_id FROM user_platform_roles
        WHERE user_id=? AND platform_role='owner' AND revoked_at IS NULL
        """,
        (user["id"],),
    ).fetchone()
    classes = connection.execute(
        """
        SELECT class_id, name, class_period
        FROM class_sections
        WHERE teacher_user_id=? AND is_active=1
        ORDER BY created_at, class_id
        """,
        (user["id"],),
    ).fetchall()
    memberships = 0
    if user["linked_student_id"]:
        memberships = connection.execute(
            """
            SELECT COUNT(*) FROM class_enrollments ce
            JOIN class_sections cs ON cs.class_id=ce.class_id
            WHERE ce.student_id=? AND cs.is_active=1
            """,
            (user["linked_student_id"],),
        ).fetchone()[0]
    return {"owner": owner, "classes": classes, "memberships": memberships}


def print_audit(user: sqlite3.Row, state: dict) -> None:
    if state["owner"]:
        expected_destination = "/owner"
    elif user["account_role"] == "teacher" and state["classes"]:
        expected_destination = "/dashboard"
    elif (
        user["account_role"] == "student"
        and user["linked_student_id"]
        and state["memberships"]
    ):
        expected_destination = "/student"
    else:
        expected_destination = "/restricted"
    print(
        "verified_email: "
        + (
            masked_email(user["verified_email"])
            if user["verified_email"]
            else "(not stored)"
        )
    )
    print(f"matched_identity_provider: {user['provider']}")
    print(f"matched_display_name: {user['display_name'] or '(not stored)'}")
    print(f"matched_provider_subject: {masked_subject(user['provider_subject'])}")
    print(f"user_id: {user['id']}")
    print(f"is_active: {int(user['is_active'])}")
    print(f"account_role: {user['account_role'] or '(legacy fallback)'}")
    print(f"deprecated_users_role: {user['role']}")
    print(f"active_owner_grant: {'yes' if state['owner'] else 'no'}")
    print(f"active_teacher_classes: {len(state['classes'])}")
    print(f"active_student_memberships: {state['memberships']}")
    print(f"expected_post_login_destination: {expected_destination}")
    for row in state["classes"]:
        print(
            f"class: id={row['class_id']} name={row['name']} "
            f"period={row['class_period'] or ''}"
        )


def backup_database(connection: sqlite3.Connection, database: Path) -> Path:
    backup_dir = database.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    backup_path = backup_dir / (
        f"{database.stem}_before_authorization_{stamp}_{uuid.uuid4().hex[:8]}"
        f"{database.suffix or '.db'}"
    )
    with sqlite3.connect(backup_path) as backup:
        connection.backup(backup)
    print(f"backup: {backup_path}")
    return backup_path


def grant_owner(
    connection: sqlite3.Connection,
    database: Path,
    target: sqlite3.Row,
    actor: sqlite3.Row,
    reason: str,
) -> None:
    if not reason:
        fail("An audit reason is required.")
    existing = connection.execute(
        """
        SELECT grant_id FROM user_platform_roles
        WHERE user_id=? AND platform_role='owner' AND revoked_at IS NULL
        """,
        (target["id"],),
    ).fetchone()
    if existing:
        print(f"result: already-owner grant_id={existing['grant_id']}; no change")
        return
    backup_database(connection, database)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        """
        INSERT INTO user_platform_roles
          (user_id, platform_role, granted_at, granted_by, grant_note)
        VALUES (?, 'owner', ?, ?, ?)
        """,
        (target["id"], int(time.time()), actor["id"], reason),
    )
    connection.commit()
    print("result: active owner authority granted")


def configure_teacher(
    connection: sqlite3.Connection,
    database: Path,
    user: sqlite3.Row,
    class_name: str,
    class_period: str,
) -> None:
    if not class_name:
        fail("A class name is required.")
    existing_class = connection.execute(
        """
        SELECT class_id FROM class_sections
        WHERE teacher_user_id=? AND is_active=1
        ORDER BY created_at, class_id LIMIT 1
        """,
        (user["id"],),
    ).fetchone()
    role_ready = user["account_role"] == "teacher"
    if role_ready and existing_class:
        print(
            f"result: already-authorized teacher class_id={existing_class['class_id']}; "
            "no change"
        )
        return
    backup_database(connection, database)
    now = int(time.time())
    connection.execute("BEGIN IMMEDIATE")
    if not role_ready:
        connection.execute(
            "UPDATE users SET account_role='teacher' WHERE id=?",
            (user["id"],),
        )
    if not existing_class:
        class_id = f"CLS-{uuid.uuid4().hex[:12].upper()}"
        join_code = secrets.token_urlsafe(9)
        connection.execute(
            """
            INSERT INTO class_sections
              (class_id, teacher_user_id, name, class_period, join_code,
               is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                class_id,
                user["id"],
                class_name,
                class_period,
                join_code,
                now,
                now,
            ),
        )
    connection.commit()
    print("result: teacher role and active empty class configured")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit or configure RootED authority by active verified SSO identity. "
            "This command never creates users or identities."
        )
    )
    parser.add_argument("--db", required=True, type=Path)
    commands = parser.add_subparsers(dest="action", required=True)

    def add_identity_selector(command, prefix: str = "") -> None:
        option_prefix = f"{prefix}-" if prefix else ""
        selector = command.add_mutually_exclusive_group(required=True)
        selector.add_argument(
            f"--{option_prefix}email",
            dest=f"{prefix}_email" if prefix else "email",
        )
        selector.add_argument(
            f"--{option_prefix}provider-subject",
            dest=f"{prefix}_provider_subject" if prefix else "provider_subject",
        )

    audit = commands.add_parser("audit")
    add_identity_selector(audit)
    audit.add_argument("--provider", required=True, choices=("google", "microsoft"))

    owner = commands.add_parser("grant-owner")
    add_identity_selector(owner)
    owner.add_argument("--provider", required=True, choices=("google", "microsoft"))
    add_identity_selector(owner, "actor")
    owner.add_argument(
        "--actor-provider",
        required=True,
        choices=("google", "microsoft"),
    )
    owner.add_argument("--reason", required=True)

    teacher = commands.add_parser("configure-teacher")
    add_identity_selector(teacher)
    teacher.add_argument("--provider", required=True, choices=("google", "microsoft"))
    teacher.add_argument("--class-name", required=True)
    teacher.add_argument("--class-period", default="Setup")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database = args.db.expanduser().resolve(strict=True)
    print(f"database: {database}")
    connection = connect_existing(database)
    try:
        user = identity_user(
            connection,
            args.provider,
            email=getattr(args, "email", None),
            provider_subject=getattr(args, "provider_subject", None),
        )
        before = authorization_state(connection, user)
        print_audit(user, before)
        if args.action == "audit":
            return
        if args.action == "grant-owner":
            actor = identity_user(
                connection,
                args.actor_provider,
                email=args.actor_email,
                provider_subject=args.actor_provider_subject,
            )
            grant_owner(connection, database, user, actor, args.reason.strip())
        else:
            configure_teacher(
                connection,
                database,
                user,
                args.class_name.strip(),
                args.class_period.strip(),
            )
        after_user = identity_user(
            connection,
            args.provider,
            email=getattr(args, "email", None),
            provider_subject=getattr(args, "provider_subject", None),
        )
        print_audit(after_user, authorization_state(connection, after_user))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
