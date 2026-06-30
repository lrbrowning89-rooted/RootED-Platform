import argparse
import sqlite3
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ngss.db"

RESET_TABLES = (
    "attempts",
    "responses",
    "progress_state",
    "student_objective_state",
    "origin_links",
    "notifications",
)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def require_table(conn: sqlite3.Connection, table_name: str) -> None:
    if not table_exists(conn, table_name):
        fail(f"Required table is missing: {table_name}")


def resolve_student_id(conn: sqlite3.Connection, username: str) -> str:
    require_table(conn, "users")
    require_table(conn, "students")

    user = conn.execute(
        """
        SELECT username, linked_student_id
        FROM users
        WHERE username = ?
        """,
        (username,),
    ).fetchone()

    if user and user["linked_student_id"]:
        student = conn.execute(
            "SELECT student_id FROM students WHERE student_id = ?",
            (user["linked_student_id"],),
        ).fetchone()
        if not student:
            fail(
                "users.linked_student_id points to a missing roster student: "
                f"{user['linked_student_id']}"
            )
        return user["linked_student_id"]

    fallback = conn.execute(
        "SELECT student_id FROM students WHERE student_id = ?",
        (username,),
    ).fetchone()
    if fallback:
        return username

    if user:
        fail(
            "Username exists, but users.linked_student_id is empty and the "
            "username is not a students.student_id fallback."
        )

    fail(f"Username is not present in users and cannot be resolved: {username}")


def validate_target(
    conn: sqlite3.Connection,
    standard_id: str,
    objective_id: str,
) -> None:
    require_table(conn, "standards")
    require_table(conn, "objectives")

    standard = conn.execute(
        "SELECT standard_id FROM standards WHERE standard_id = ?",
        (standard_id,),
    ).fetchone()
    if not standard:
        fail(f"Target standard does not exist: {standard_id}")

    objective = conn.execute(
        "SELECT * FROM objectives WHERE objective_id = ?",
        (objective_id,),
    ).fetchone()
    if not objective:
        fail(f"Target objective does not exist: {objective_id}")

    if objective["standard_id"] != standard_id:
        fail(
            f"Target objective {objective_id} belongs to "
            f"{objective['standard_id']}, not {standard_id}."
        )

    objective_columns = set(objective.keys())
    active_columns = [name for name in ("is_active", "active", "status") if name in objective_columns]
    for name in active_columns:
        value = objective[name]
        if name in ("is_active", "active") and value in (0, "0", False):
            fail(f"Target objective is inactive according to objectives.{name}.")
        if name == "status" and str(value).strip().lower() in {"inactive", "retired", "archived"}:
            fail(f"Target objective is not active according to objectives.status={value!r}.")


def objective_level(conn: sqlite3.Connection, objective_id: str) -> int:
    if table_exists(conn, "objective_levels"):
        row = conn.execute(
            "SELECT level FROM objective_levels WHERE objective_id = ?",
            (objective_id,),
        ).fetchone()
        if row:
            try:
                level = int(row["level"])
            except (TypeError, ValueError):
                level = 1
            if level in (1, 2, 3):
                return level
    return 1


def count_existing_rows(conn: sqlite3.Connection, table_name: str, student_id: str) -> int:
    if not table_exists(conn, table_name):
        return 0
    columns = table_columns(conn, table_name)
    if "student_id" not in columns:
        fail(f"Refusing to reset {table_name}: no student_id column found.")
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM {table_name} WHERE student_id = ?",
        (student_id,),
    ).fetchone()
    return int(row["n"])


def delete_student_rows(conn: sqlite3.Connection, table_name: str, student_id: str) -> int:
    if not table_exists(conn, table_name):
        return 0
    cur = conn.execute(
        f"DELETE FROM {table_name} WHERE student_id = ?",
        (student_id,),
    )
    return cur.rowcount


def insert_progress_state(
    conn: sqlite3.Connection,
    student_id: str,
    standard_id: str,
    level: int,
    now: int,
) -> None:
    columns = table_columns(conn, "progress_state")
    values = {
        "student_id": student_id,
        "standard_id": standard_id,
        "current_level": level,
        "status": "practicing",
        "rolling_avg": 0.0,
        "locked": 0,
        "locked_reason": None,
        "last_update": now,
        "current_standard_id": standard_id,
        "origin_standard_id": standard_id,
        "active_route_type": "normal",
    }
    insert_columns = [name for name in values if name in columns]
    placeholders = ", ".join("?" for _ in insert_columns)
    conn.execute(
        f"""
        INSERT INTO progress_state ({", ".join(insert_columns)})
        VALUES ({placeholders})
        """,
        tuple(values[name] for name in insert_columns),
    )


def insert_student_objective_state(
    conn: sqlite3.Connection,
    student_id: str,
    standard_id: str,
    objective_id: str,
    now: int,
) -> None:
    columns = table_columns(conn, "student_objective_state")
    values = {
        "student_id": student_id,
        "standard_id": standard_id,
        "current_objective_id": objective_id,
        "status": "active",
        "last_update": now,
    }
    insert_columns = [name for name in values if name in columns]
    placeholders = ", ".join("?" for _ in insert_columns)
    conn.execute(
        f"""
        INSERT INTO student_objective_state ({", ".join(insert_columns)})
        VALUES ({placeholders})
        """,
        tuple(values[name] for name in insert_columns),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset one local development student to a target objective."
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--standard-id", required=True)
    parser.add_argument("--objective-id", required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if not args.confirm:
        fail("Refusing to run without --confirm.")
    return args


def main() -> None:
    args = parse_args()

    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")

        require_table(conn, "progress_state")
        require_table(conn, "student_objective_state")

        for table_name in RESET_TABLES:
            if table_exists(conn, table_name) and "student_id" not in table_columns(conn, table_name):
                fail(f"Refusing to reset {table_name}: no student_id column found.")

        student_id = resolve_student_id(conn, args.username)
        validate_target(conn, args.standard_id, args.objective_id)
        level = objective_level(conn, args.objective_id)
        counts = {
            table_name: count_existing_rows(conn, table_name, student_id)
            for table_name in RESET_TABLES
            if table_exists(conn, table_name)
        }

        print("Reset plan")
        print(f"Database: {DB}")
        print(f"Username: {args.username}")
        print(f"Resolved student_id: {student_id}")
        print(f"Target standard_id: {args.standard_id}")
        print(f"Target objective_id: {args.objective_id}")
        print(f"Seed progress_state.current_level: {level}")
        print("Rows to delete:")
        for table_name in RESET_TABLES:
            if table_name in counts:
                print(f"  {table_name}: {counts[table_name]}")
            else:
                print(f"  {table_name}: table missing, skipped")
        print("Rows to seed:")
        print("  progress_state: 1")
        print("  student_objective_state: 1")

        now = int(time.time())
        deleted = {
            table_name: delete_student_rows(conn, table_name, student_id)
            for table_name in RESET_TABLES
            if table_exists(conn, table_name)
        }
        insert_progress_state(conn, student_id, args.standard_id, level, now)
        insert_student_objective_state(
            conn,
            student_id,
            args.standard_id,
            args.objective_id,
            now,
        )
        conn.commit()

    print("Reset complete")
    print("Rows deleted:")
    for table_name in RESET_TABLES:
        if table_name in deleted:
            print(f"  {table_name}: {deleted[table_name]}")
        else:
            print(f"  {table_name}: table missing, skipped")
    print("Rows seeded:")
    print("  progress_state: 1")
    print("  student_objective_state: 1")


if __name__ == "__main__":
    main()
