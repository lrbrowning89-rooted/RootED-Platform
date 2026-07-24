import os
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("NGSS_DB", ROOT / "data" / "ngss.db"))

LEGACY_CATEGORIES = {
    "incorrect answer or scoring": "incorrect_answer",
    "confusing wording": "confusing_question",
    "image/model problem": "visual_problem",
    "formatting/rendering problem": "display_problem",
    "accessibility problem": "accessibility_problem",
    "duplicate question": "duplicate_question",
}


def create_question_flags_table(conn: sqlite3.Connection, table_name: str = "question_flags") -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
          flag_id             TEXT PRIMARY KEY,
          question_id         TEXT NOT NULL,
          objective_id        TEXT NOT NULL,
          standard_id         TEXT NOT NULL,
          reporter_user_id    INTEGER,
          reporter_role       TEXT NOT NULL CHECK(reporter_role IN ('teacher', 'student')),
          class_id            TEXT,
          student_id          TEXT,
          category            TEXT NOT NULL,
          comment             TEXT,
          page_context        TEXT,
          created_ts          INTEGER NOT NULL,
          status              TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'teacher_resolved', 'escalated', 'owner_reviewing', 'fixed', 'closed')),
          escalated_by_user_id INTEGER,
          escalated_at        INTEGER,
          escalation_note     TEXT,
          resolved_ts         INTEGER,
          resolution_note     TEXT,
          resolved_by_user_id INTEGER,
          FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE,
          FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE,
          FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
          FOREIGN KEY(class_id) REFERENCES class_sections(class_id) ON DELETE SET NULL,
          FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE SET NULL
        )
        """
    )


def run(db_path: Path = DB) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        create_question_flags_table(conn)
        row = conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'question_flags'
            """
        ).fetchone()
        if row and "('open', 'resolved')" in (row[0] or ""):
            conn.execute("ALTER TABLE question_flags RENAME TO question_flags_old")
            create_question_flags_table(conn)
            conn.execute(
                """
                INSERT INTO question_flags
                  (flag_id, question_id, objective_id, standard_id, reporter_user_id,
                   reporter_role, class_id, student_id, category, comment, page_context,
                   created_ts, status, resolved_ts, resolution_note, resolved_by_user_id)
                SELECT flag_id, question_id, objective_id, standard_id, reporter_user_id,
                       reporter_role, class_id, student_id, category, comment, page_context,
                       created_ts,
                       CASE WHEN status = 'resolved' THEN 'teacher_resolved' ELSE status END,
                       resolved_ts, resolution_note, resolved_by_user_id
                FROM question_flags_old
                """
            )
            conn.execute("DROP TABLE question_flags_old")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_question_flags_status_created
            ON question_flags(status, created_ts DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_question_flags_question_status
            ON question_flags(question_id, status)
            """
        )
        for col_name, col_type in [
            ("escalated_by_user_id", "INTEGER"),
            ("escalated_at", "INTEGER"),
            ("escalation_note", "TEXT"),
        ]:
            try:
                conn.execute(f"SELECT {col_name} FROM question_flags LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(f"ALTER TABLE question_flags ADD COLUMN {col_name} {col_type}")
        conn.execute(
            """
            UPDATE question_flags
            SET status = 'teacher_resolved'
            WHERE status = 'resolved'
            """
        )
        for legacy, code in LEGACY_CATEGORIES.items():
            conn.execute(
                "UPDATE question_flags SET category = ? WHERE category = ?",
                (code, legacy),
            )
        conn.commit()


if __name__ == "__main__":
    run()
    print("question_flags table migration complete")
