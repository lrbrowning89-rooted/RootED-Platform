"""LEGACY — not approved for direct production or Render execution."""

import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ngss.db"
JSON_PATH = ROOT / "data" / "imports" / "MS-LS1-1A.json"
NOW = int(time.time())

with open(JSON_PATH, "r", encoding="utf-8") as f:
    pack = json.load(f)

assert pack["standard_id"] == "MS-LS1-1"
assert pack["objective_id"] == "MS-LS1-1A"
assert pack["status"] == "production_approved"

questions = pack["questions"]
assert len(questions) == 21

with sqlite3.connect(DB) as conn:
    conn.row_factory = sqlite3.Row

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

    legacy_questions = conn.execute("""
        SELECT question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key
        FROM questions
        WHERE objective_id = 'MS-LS1-1A'
    """).fetchall()

    for q in legacy_questions:
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
            "Archived legacy MS-LS1-1A prototype question before production import",
            NOW,
        ))

    conn.execute("DELETE FROM questions WHERE objective_id = 'MS-LS1-1A'")

    for q in questions:
        choices = q["answer_choices"]
        conn.execute("""
            INSERT INTO questions
              (question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            q["question_id"],
            q["objective_id"],
            q["prompt"],
            choices["A"],
            choices["B"],
            choices["C"],
            choices["D"],
            q["correct_answer"],
        ))

    conn.execute("""
        INSERT INTO migration_log (event, details, ts)
        VALUES (?, ?, ?)
    """, (
        "ms_ls1_1a_production_import",
        f"Archived {len(legacy_questions)} legacy MS-LS1-1A questions and imported {len(questions)} production-approved questions.",
        NOW,
    ))

    conn.commit()

print(f"Archived {len(legacy_questions)} legacy questions")
print(f"Imported {len(questions)} production questions")
print("MS-LS1-1A production migration complete")
