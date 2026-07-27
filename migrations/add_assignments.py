"""Add class-scoped assignments with snapshotted student recipients."""

import argparse
import sqlite3
from pathlib import Path


def migrate(conn: sqlite3.Connection) -> None:
    if "standard_name" not in {
        row[1] for row in conn.execute("PRAGMA table_info(standards)")
    }:
        conn.execute("ALTER TABLE standards ADD COLUMN standard_name TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS assignments (
          assignment_id TEXT PRIMARY KEY,
          teacher_user_id INTEGER NOT NULL,
          class_id TEXT NOT NULL,
          target_type TEXT NOT NULL CHECK(target_type IN ('standard','objective','competency')),
          target_id TEXT NOT NULL,
          recipient_scope TEXT NOT NULL CHECK(recipient_scope IN ('class','selected')),
          directions TEXT,
          assign_at INTEGER NOT NULL,
          due_at INTEGER,
          created_at INTEGER NOT NULL,
          archived_at INTEGER,
          archived_by INTEGER,
          FOREIGN KEY(teacher_user_id) REFERENCES users(id) ON DELETE RESTRICT,
          FOREIGN KEY(class_id) REFERENCES class_sections(class_id) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_assignments_teacher
          ON assignments(teacher_user_id,archived_at,created_at);
        CREATE INDEX IF NOT EXISTS idx_assignments_class
          ON assignments(class_id,archived_at,created_at);

        CREATE TABLE IF NOT EXISTS assignment_recipients (
          assignment_id TEXT NOT NULL,
          student_id TEXT NOT NULL,
          assigned_at INTEGER NOT NULL,
          PRIMARY KEY(assignment_id,student_id),
          FOREIGN KEY(assignment_id) REFERENCES assignments(assignment_id) ON DELETE RESTRICT,
          FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_assignment_recipients_student
          ON assignment_recipients(student_id,assignment_id);
        """
    )
    assignment_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(assignments)")
    }
    if "assign_at" not in assignment_columns:
        conn.execute("ALTER TABLE assignments ADD COLUMN assign_at INTEGER")
        conn.execute(
            "UPDATE assignments SET assign_at=created_at WHERE assign_at IS NULL"
        )
    conn.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args()
    with sqlite3.connect(args.database.resolve(strict=True)) as connection:
        migrate(connection)
    print(f"Assignment migration complete: {args.database}")
