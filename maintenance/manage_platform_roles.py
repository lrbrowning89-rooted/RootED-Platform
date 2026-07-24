import argparse
import sqlite3
import sys
import time
from pathlib import Path

from db_path import add_db_argument, print_database_path, resolve_database_path


PLATFORM_ROLE = "owner"


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def add_user_lookup(parser: argparse.ArgumentParser, prefix: str, label: str) -> None:
    lookup = parser.add_mutually_exclusive_group(required=True)
    lookup.add_argument(
        f"--{prefix}-user-id",
        type=int,
        dest=f"{prefix}_user_id",
        help=f"Existing users.id for the {label}.",
    )
    lookup.add_argument(
        f"--{prefix}-username",
        dest=f"{prefix}_username",
        help=f"Existing exact users.username for the {label}.",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Grant, revoke, or list RootED platform owner authority."
    )
    add_db_argument(parser)
    subparsers = parser.add_subparsers(dest="action", required=True)

    for action in ("grant-owner", "revoke-owner"):
        command = subparsers.add_parser(action)
        add_user_lookup(command, "target", "authority target")
        add_user_lookup(command, "actor", "operator performing the change")
        command.add_argument(
            "--reason",
            required=True,
            help="Required audit reason for the authority change.",
        )
        command.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and display the operation without writing.",
        )
        if action == "revoke-owner":
            command.add_argument(
                "--allow-no-owner",
                action="store_true",
                help="Explicitly permit revoking the last active owner.",
            )

    list_command = subparsers.add_parser("list-owners")
    list_command.add_argument(
        "--active-only",
        action="store_true",
        help="Show only active grants instead of complete grant history.",
    )
    return parser.parse_args()


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        fail(f"Database file not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def require_schema(conn: sqlite3.Connection) -> None:
    for table in ("users", "user_platform_roles"):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not row:
            fail(f"Required table is missing: {table}. Run the schema migration first.")

    required_columns = {
        "grant_id",
        "user_id",
        "platform_role",
        "granted_at",
        "granted_by",
        "revoked_at",
        "revoked_by",
        "grant_note",
        "revoke_note",
    }
    actual_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(user_platform_roles)")
    }
    missing = sorted(required_columns - actual_columns)
    if missing:
        fail(
            "user_platform_roles requires migration; missing columns: "
            + ", ".join(missing)
        )


def find_existing_user(
    conn: sqlite3.Connection,
    user_id: int | None,
    username: str | None,
    label: str,
    require_active: bool = False,
) -> sqlite3.Row:
    if user_id is not None:
        rows = conn.execute(
            "SELECT id, username, role, is_active FROM users WHERE id = ?",
            (user_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, username, role, is_active FROM users WHERE username = ?",
            ((username or "").strip(),),
        ).fetchall()
    if not rows:
        fail(f"No existing {label} user matched. Refusing to create a user.")
    if len(rows) != 1:
        fail(f"Multiple {label} users matched. Refusing an ambiguous change.")
    user = rows[0]
    if require_active and int(user["is_active"]) != 1:
        fail(f"The matched {label} user is inactive.")
    return user


def print_user(prefix: str, user: sqlite3.Row) -> None:
    print(f"{prefix}_user_id: {user['id']}")
    print(f"{prefix}_username: {user['username']}")
    print(f"{prefix}_classroom_role: {user['role']}")
    print(f"{prefix}_is_active: {int(user['is_active'])}")


def list_owners(conn: sqlite3.Connection, active_only: bool = False) -> None:
    where = "AND upr.revoked_at IS NULL" if active_only else ""
    rows = conn.execute(
        f"""
        SELECT upr.grant_id,
               u.id AS user_id,
               u.username,
               u.role,
               u.is_active,
               upr.granted_at,
               grantor.username AS granted_by_username,
               upr.grant_note,
               upr.revoked_at,
               revoker.username AS revoked_by_username,
               upr.revoke_note
        FROM user_platform_roles upr
        JOIN users u ON u.id = upr.user_id
        LEFT JOIN users grantor ON grantor.id = upr.granted_by
        LEFT JOIN users revoker ON revoker.id = upr.revoked_by
        WHERE upr.platform_role = ?
          {where}
        ORDER BY upr.granted_at DESC, upr.grant_id DESC
        """,
        (PLATFORM_ROLE,),
    ).fetchall()
    print(f"Owner grants: {len(rows)}")
    for row in rows:
        status = "active" if row["revoked_at"] is None else "revoked"
        print(
            f"grant_id={row['grant_id']}\tstatus={status}"
            f"\tuser_id={row['user_id']}\tusername={row['username']}"
            f"\tclassroom_role={row['role']}\taccount_active={int(row['is_active'])}"
            f"\tgranted_at={row['granted_at']}"
            f"\tgranted_by={row['granted_by_username'] or 'unknown'}"
            f"\tgrant_reason={row['grant_note'] or ''}"
            f"\trevoked_at={row['revoked_at'] or ''}"
            f"\trevoked_by={row['revoked_by_username'] or ''}"
            f"\trevoke_reason={row['revoke_note'] or ''}"
        )


def active_grant(conn: sqlite3.Connection, user_id: int):
    return conn.execute(
        """
        SELECT *
        FROM user_platform_roles
        WHERE user_id = ?
          AND platform_role = ?
          AND revoked_at IS NULL
        LIMIT 1
        """,
        (user_id, PLATFORM_ROLE),
    ).fetchone()


def active_owner_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM user_platform_roles
        WHERE platform_role = ?
          AND revoked_at IS NULL
        """,
        (PLATFORM_ROLE,),
    ).fetchone()
    return int(row["count"])


def change_owner(
    conn: sqlite3.Connection,
    action: str,
    target: sqlite3.Row,
    actor: sqlite3.Row,
    reason: str,
    dry_run: bool,
    allow_no_owner: bool = False,
) -> None:
    reason = reason.strip()
    if not reason:
        fail("Reason cannot be blank.")

    if not dry_run:
        conn.execute("BEGIN IMMEDIATE")
    existing = active_grant(conn, target["id"])
    print(f"Action: {action}")
    print_user("target", target)
    print_user("actor", actor)
    print(f"reason: {reason}")
    print(f"owner_before: {'yes' if existing else 'no'}")

    if action == "grant-owner":
        if int(target["is_active"]) != 1:
            fail("The matched target user is inactive.")
        if existing:
            if not dry_run:
                conn.rollback()
            print(
                f"Result: already an owner through grant_id={existing['grant_id']}; "
                "no change needed."
            )
            return
        if dry_run:
            print("Result: dry-run; a new owner grant would be recorded.")
            return
        cursor = conn.execute(
            """
            INSERT INTO user_platform_roles
              (user_id, platform_role, granted_at, granted_by, grant_note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (target["id"], PLATFORM_ROLE, int(time.time()), actor["id"], reason),
        )
        conn.commit()
        print(f"Result: owner granted with grant_id={cursor.lastrowid}.")
        return

    if not existing:
        if not dry_run:
            conn.rollback()
        print("Result: not an active owner; no change needed.")
        return
    if active_owner_count(conn) <= 1 and not allow_no_owner:
        fail(
            "Refusing to revoke the last active owner. "
            "Use --allow-no-owner only when intentionally leaving no active owner."
        )
    if dry_run:
        print(
            f"Result: dry-run; grant_id={existing['grant_id']} would be revoked "
            "and retained for audit."
        )
        return
    conn.execute(
        """
        UPDATE user_platform_roles
        SET revoked_at = ?,
            revoked_by = ?,
            revoke_note = ?
        WHERE grant_id = ?
          AND revoked_at IS NULL
        """,
        (int(time.time()), actor["id"], reason, existing["grant_id"]),
    )
    conn.commit()
    print(f"Result: grant_id={existing['grant_id']} revoked and retained for audit.")


def main() -> None:
    args = parse_args()
    db_path = resolve_database_path(args.db)
    print_database_path(db_path)
    conn = connect(db_path)
    try:
        require_schema(conn)
        if args.action == "list-owners":
            list_owners(conn, args.active_only)
            return

        target = find_existing_user(
            conn,
            args.target_user_id,
            args.target_username,
            "target",
        )
        actor = find_existing_user(
            conn,
            args.actor_user_id,
            args.actor_username,
            "actor",
            require_active=True,
        )
        change_owner(
            conn,
            args.action,
            target,
            actor,
            args.reason,
            args.dry_run,
            getattr(args, "allow_no_owner", False),
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
