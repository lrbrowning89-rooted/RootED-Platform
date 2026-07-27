"""Add authoritative, replay-safe student question deliveries."""

import sqlite3

from app_core.config import resolve_database_path


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS student_question_deliveries (
          submission_token TEXT PRIMARY KEY,
          student_id TEXT NOT NULL,
          growth_attempt_id TEXT NOT NULL,
          standard_id TEXT NOT NULL,
          objective_id TEXT NOT NULL,
          level INTEGER NOT NULL,
          question_id TEXT NOT NULL,
          sequence_number INTEGER NOT NULL,
          served_at INTEGER NOT NULL,
          consumed_at INTEGER,
          invalidated_at INTEGER,
          FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
          FOREIGN KEY(growth_attempt_id) REFERENCES student_growth_progress(attempt_id) ON DELETE CASCADE,
          FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE
        )
        """
    )
    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(student_question_deliveries)"
        ).fetchall()
    }
    if "sequence_number" not in columns:
        conn.execute(
            "ALTER TABLE student_question_deliveries ADD COLUMN sequence_number INTEGER"
        )
    conn.execute(
        """
        UPDATE student_question_deliveries
        SET sequence_number = (
          SELECT COUNT(*) - 1
          FROM student_question_deliveries AS prior
          WHERE prior.growth_attempt_id =
                  student_question_deliveries.growth_attempt_id
            AND prior.level = student_question_deliveries.level
            AND (
              prior.served_at < student_question_deliveries.served_at
              OR (
                prior.served_at = student_question_deliveries.served_at
                AND prior.submission_token <=
                    student_question_deliveries.submission_token
              )
            )
        )
        WHERE sequence_number IS NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_student_question_delivery_active
        ON student_question_deliveries(student_id)
        WHERE consumed_at IS NULL AND invalidated_at IS NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_student_question_delivery_sequence
        ON student_question_deliveries(growth_attempt_id, level, sequence_number)
        """
    )


if __name__ == "__main__":
    with sqlite3.connect(resolve_database_path()) as connection:
        migrate(connection)
