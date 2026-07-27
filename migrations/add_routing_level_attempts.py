"""Add explicit cumulative-mastery evidence boundaries.

Run with:
    python migrations/add_routing_level_attempts.py [database-path]
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "ngss.db"


def upgrade(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    response_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(responses)")
    }
    if "routing_level_attempt_id" not in response_columns:
        conn.execute(
            "ALTER TABLE responses ADD COLUMN routing_level_attempt_id TEXT"
        )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS routing_level_attempts (
          attempt_id TEXT PRIMARY KEY,
          student_id TEXT NOT NULL,
          standard_id TEXT NOT NULL,
          objective_id TEXT NOT NULL,
          level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3),
          status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('active', 'closed', 'invalidated')),
          started_at INTEGER NOT NULL,
          ended_at INTEGER,
          end_reason TEXT,
          final_correct_count INTEGER,
          final_response_count INTEGER,
          final_score REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_routing_level_attempt_active_student
          ON routing_level_attempts(student_id) WHERE status = 'active';
        CREATE INDEX IF NOT EXISTS idx_responses_routing_level_attempt
          ON responses(routing_level_attempt_id, id);
        """
    )

    rows = conn.execute(
        """
        SELECT r.id, r.student_id, r.standard_id, r.level, r.correct, r.ts,
               q.objective_id
        FROM responses r
        JOIN questions q ON q.question_id = r.question_id
        WHERE r.routing_level_attempt_id IS NULL
        ORDER BY r.student_id, r.ts, r.id
        """
    ).fetchall()
    groups: list[dict] = []
    for row in rows:
        key = (
            row["student_id"],
            row["standard_id"],
            row["objective_id"],
            int(row["level"]),
        )
        if not groups or groups[-1]["key"] != key:
            groups.append({"key": key, "rows": []})
        groups[-1]["rows"].append(row)

    latest = {}
    for group in groups:
        latest[group["key"][0]] = group
    for group in groups:
        student_id, standard_id, objective_id, level = group["key"]
        evidence = group["rows"]
        correct = sum(int(row["correct"]) for row in evidence)
        total = len(evidence)
        state = conn.execute(
            """
            SELECT ps.current_level, sos.current_objective_id
            FROM progress_state ps
            LEFT JOIN student_objective_state sos
              ON sos.student_id = ps.student_id
             AND sos.standard_id = ps.standard_id
             AND sos.status = 'active'
            WHERE ps.student_id = ? AND ps.standard_id = ?
            """,
            (student_id, standard_id),
        ).fetchone()
        active = bool(
            group is latest[student_id]
            and state
            and int(state["current_level"]) == level
            and state["current_objective_id"] == objective_id
        )
        existing_active = conn.execute(
            """
            SELECT * FROM routing_level_attempts
            WHERE student_id = ? AND status = 'active'
            """,
            (student_id,),
        ).fetchone()
        if (
            active
            and existing_active
            and existing_active["standard_id"] == standard_id
            and existing_active["objective_id"] == objective_id
            and int(existing_active["level"]) == level
        ):
            conn.executemany(
                "UPDATE responses SET routing_level_attempt_id = ? WHERE id = ?",
                [(existing_active["attempt_id"], row["id"]) for row in evidence],
            )
            continue
        if active and existing_active:
            conn.execute(
                """
                UPDATE routing_level_attempts
                SET status='closed', ended_at=?, end_reason='historical_boundary'
                WHERE attempt_id=?
                """,
                (int(evidence[0]["ts"]), existing_active["attempt_id"]),
            )
        attempt_id = f"RLA{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO routing_level_attempts
              (attempt_id, student_id, standard_id, objective_id, level, status,
               started_at, ended_at, end_reason, final_correct_count,
               final_response_count, final_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                student_id,
                standard_id,
                objective_id,
                level,
                "active" if active else "closed",
                int(evidence[0]["ts"]),
                None if active else int(evidence[-1]["ts"]),
                None if active else "historical_boundary",
                None if active else correct,
                None if active else total,
                None if active else correct / total,
            ),
        )
        conn.executemany(
            "UPDATE responses SET routing_level_attempt_id = ? WHERE id = ?",
            [(attempt_id, row["id"]) for row in evidence],
        )
    conn.commit()


if __name__ == "__main__":
    database = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    with sqlite3.connect(database) as connection:
        upgrade(connection)
    print(f"Routing-level attempts ready in {database}")
