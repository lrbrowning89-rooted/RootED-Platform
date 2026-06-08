import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ngss.db"
NOW = int(time.time())

with sqlite3.connect(DB) as conn:
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE IF NOT EXISTS archived_objectives (
            objective_id TEXT,
            original_standard_id TEXT,
            objective_text TEXT,
            order_in_band INTEGER,
            archive_reason TEXT,
            archived_at INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS archived_questions (
            question_id TEXT,
            original_objective_id TEXT,
            stem TEXT,
            choice_a TEXT,
            choice_b TEXT,
            choice_c TEXT,
            choice_d TEXT,
            answer_key TEXT,
            archive_reason TEXT,
            archived_at INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS migration_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT,
            details TEXT,
            ts INTEGER
        )
    """)

    obj = conn.execute("""
        SELECT objective_id, standard_id, objective_text, order_in_band
        FROM objectives
        WHERE objective_id = 'MS-LS1-1C'
    """).fetchone()

    if obj:
        conn.execute("""
            INSERT INTO archived_objectives
              (objective_id, original_standard_id, objective_text, order_in_band, archive_reason, archived_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            obj["objective_id"],
            obj["standard_id"],
            obj["objective_text"],
            obj["order_in_band"],
            "Retired legacy MS-LS1-1C after approved two-objective MS-LS1-1 architecture",
            NOW,
        ))

    questions = conn.execute("""
        SELECT question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key
        FROM questions
        WHERE objective_id = 'MS-LS1-1C'
    """).fetchall()

    for q in questions:
        conn.execute("""
            INSERT INTO archived_questions
              (question_id, original_objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key, archive_reason, archived_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            q["question_id"],
            q["objective_id"],
            q["stem"],
            q["choice_a"],
            q["choice_b"],
            q["choice_c"],
            q["choice_d"],
            q["answer_key"],
            "Retired legacy MS-LS1-1C question after approved two-objective MS-LS1-1 architecture",
            NOW,
        ))

    conn.execute("DELETE FROM questions WHERE objective_id = 'MS-LS1-1C'")
    conn.execute("DELETE FROM objectives WHERE objective_id = 'MS-LS1-1C'")

    conn.execute("""
        DELETE FROM responses
        WHERE standard_id='MS-LS1-1'
          AND level=3
    """)

    conn.execute("""
        DELETE FROM progress_state
        WHERE standard_id='MS-LS1-1'
          AND current_level=3
    """)

    conn.execute("""
        INSERT INTO migration_log (event, details, ts)
        VALUES (?, ?, ?)
    """, (
        "retire_ms_ls1_1c",
        f"Archived MS-LS1-1C objective and {len(questions)} questions; removed legacy level 3 response/progress state.",
        NOW,
    ))

    conn.commit()

print(f"Archived MS-LS1-1C questions: {len(questions)}")
print("Retired MS-LS1-1C objective")