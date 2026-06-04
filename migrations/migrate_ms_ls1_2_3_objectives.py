import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "ngss.db"
NOW = int(time.time())

MS_LS1_2_OBJECTIVES = [
    ("MS-LS1-2A", "Identify the cell as a system made of parts that work together.", 1),
    ("MS-LS1-2B", "Describe the function of the cell membrane in controlling what enters and leaves the cell.", 2),
    ("MS-LS1-2C", "Describe the function of the nucleus in controlling cell activities.", 3),
    ("MS-LS1-2D", "Describe how chloroplasts help plant cells make food.", 4),
    ("MS-LS1-2E", "Describe how mitochondria release energy for cell processes.", 5),
    ("MS-LS1-2F", "Describe the function of the cell wall in supporting and protecting plant cells.", 6),
    ("MS-LS1-2G", "Use models to explain how cell structures function together as a system.", 7),
]

MS_LS1_3_OBJECTIVES = [
    ("MS-LS1-3A", "Identify tissues as groups of similar cells working together to perform a function.", 1),
    ("MS-LS1-3B", "Identify organs as structures made of tissues that perform specific functions.", 2),
    ("MS-LS1-3C", "Identify organ systems as groups of organs that work together.", 3),
    ("MS-LS1-3D", "Explain how different organ systems interact to perform body functions.", 4),
    ("MS-LS1-3E", "Use models to explain how body systems work together as a larger system.", 5),
]

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

    # Archive current adaptation objectives/questions from MS-LS1-3A/B.
    for oid in ("MS-LS1-3A", "MS-LS1-3B"):
        obj = conn.execute("""
            SELECT objective_id, standard_id, objective_text, order_in_band
            FROM objectives
            WHERE objective_id = ?
        """, (oid,)).fetchone()

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
                "Archived adaptation objective during MS-LS1-2/MS-LS1-3 realignment",
                NOW,
            ))

        questions = conn.execute("""
            SELECT question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key
            FROM questions
            WHERE objective_id = ?
        """, (oid,)).fetchall()

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
                "Archived adaptation question during MS-LS1-2/MS-LS1-3 realignment",
                NOW,
            ))

        conn.execute("DELETE FROM questions WHERE objective_id = ?", (oid,))
        conn.execute("DELETE FROM objectives WHERE objective_id = ?", (oid,))

    # Archive old partial/obsolete MS-LS1-2 objective if present.
    oid = "OBJ-LS1-MS-2"
    obj = conn.execute("""
        SELECT objective_id, standard_id, objective_text, order_in_band
        FROM objectives
        WHERE objective_id = ?
    """, (oid,)).fetchone()

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
            "Archived partial legacy cell/tissue objective before rebuilding approved MS-LS1-2 structure",
            NOW,
        ))

        questions = conn.execute("""
            SELECT question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key
            FROM questions
            WHERE objective_id = ?
        """, (oid,)).fetchall()

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
                "Archived legacy question for review before new MS-LS1-2 question generation",
                NOW,
            ))

        conn.execute("DELETE FROM questions WHERE objective_id = ?", (oid,))
        conn.execute("DELETE FROM objectives WHERE objective_id = ?", (oid,))

    # Move existing body-system questions off MS-LS1-2A/B into temporary moved IDs before rebuilding.
    # These are preserved as migrated/salvageable MS-LS1-3 questions, but no new questions are generated.
    move_map = {
        "MS-LS1-2A": "MS-LS1-3D",
        "MS-LS1-2B": "MS-LS1-3B",
    }

    moved_count = 0
    for old_oid, new_oid in move_map.items():
        conn.execute("""
            UPDATE questions
            SET objective_id = ?
            WHERE objective_id = ?
        """, (new_oid, old_oid))
        moved_count += conn.total_changes

    # Remove all existing MS-LS1-2/MS-LS1-3 objectives so approved structure can be rebuilt cleanly.
    conn.execute("DELETE FROM objectives WHERE standard_id IN ('MS-LS1-2', 'MS-LS1-3')")

    # Rebuild approved MS-LS1-2 objective structure.
    for oid, text, order_num in MS_LS1_2_OBJECTIVES:
        conn.execute("""
            INSERT INTO objectives (objective_id, standard_id, objective_text, order_in_band)
            VALUES (?, 'MS-LS1-2', ?, ?)
        """, (oid, text, order_num))

    # Rebuild approved MS-LS1-3 objective structure.
    for oid, text, order_num in MS_LS1_3_OBJECTIVES:
        conn.execute("""
            INSERT INTO objectives (objective_id, standard_id, objective_text, order_in_band)
            VALUES (?, 'MS-LS1-3', ?, ?)
        """, (oid, text, order_num))

    conn.execute("""
        INSERT INTO migration_log (event, details, ts)
        VALUES (?, ?, ?)
    """, (
        "ms_ls1_2_3_objective_migration",
        "Archived adaptation objectives/questions; moved body-system questions to approved MS-LS1-3 objective IDs; rebuilt MS-LS1-2/MS-LS1-3 objective structures; no new questions generated.",
        NOW,
    ))

    conn.commit()

print("MS-LS1-2/MS-LS1-3 objective migration complete")