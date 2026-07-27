"""Add presentation-only, monotonic student growth progress."""

import sqlite3

from app_core.config import resolve_database_path


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS student_growth_progress (
          attempt_id TEXT PRIMARY KEY,
          student_id TEXT NOT NULL,
          standard_id TEXT NOT NULL,
          objective_id TEXT NOT NULL,
          visible_stage INTEGER NOT NULL DEFAULT 1 CHECK(visible_stage BETWEEN 1 AND 4),
          presentation_state TEXT NOT NULL DEFAULT 'strengthening'
            CHECK(presentation_state IN ('growing', 'strengthening', 'reviewing', 'complete')),
          is_active INTEGER NOT NULL DEFAULT 1,
          started_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          completed_at INTEGER,
          FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
          FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
          FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_student_growth_active
          ON student_growth_progress(student_id) WHERE is_active = 1;
        """
    )


if __name__ == "__main__":
    with sqlite3.connect(resolve_database_path()) as connection:
        migrate(connection)
