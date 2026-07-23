import os
import sqlite3
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("NGSS_DB", ROOT / "data" / "ngss.db"))
NOW = int(time.time())

OBSOLETE_OBJECTIVE_IDS = (
    "MS-LS1-2C",
    "MS-LS1-2D",
    "MS-LS1-2E",
    "MS-LS1-2F",
    "MS-LS1-2G",
    "MS-LS1-3E",
)


def placeholders(values) -> str:
    return ",".join("?" for _ in values)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def ensure_archive_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS archived_objectives (
            objective_id TEXT,
            original_standard_id TEXT,
            objective_text TEXT,
            order_in_band INTEGER,
            archive_reason TEXT,
            archived_at INTEGER
        )
        """
    )
    conn.execute(
        """
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
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS migration_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT,
            details TEXT,
            ts INTEGER
        )
        """
    )


with sqlite3.connect(DB) as conn:
    conn.row_factory = sqlite3.Row
    ensure_archive_tables(conn)

    objective_sql = placeholders(OBSOLETE_OBJECTIVE_IDS)
    archive_reason = (
        "Retired obsolete classroom-launch objective after approved "
        "MS-LS1-1/2/3 seven-objective manifest"
    )
    question_archive_reason = (
        "Retired question attached to obsolete classroom-launch objective"
    )

    objectives = conn.execute(
        f"""
        SELECT objective_id, standard_id, objective_text, order_in_band
        FROM objectives
        WHERE objective_id IN ({objective_sql})
        ORDER BY standard_id, order_in_band, objective_id
        """,
        OBSOLETE_OBJECTIVE_IDS,
    ).fetchall()

    for obj in objectives:
        conn.execute(
            """
            INSERT INTO archived_objectives
              (objective_id, original_standard_id, objective_text, order_in_band, archive_reason, archived_at)
            SELECT ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1
                FROM archived_objectives
                WHERE objective_id = ?
                  AND archive_reason = ?
            )
            """,
            (
                obj["objective_id"],
                obj["standard_id"],
                obj["objective_text"],
                obj["order_in_band"],
                archive_reason,
                NOW,
                obj["objective_id"],
                archive_reason,
            ),
        )

    questions = conn.execute(
        f"""
        SELECT question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key
        FROM questions
        WHERE objective_id IN ({objective_sql})
        ORDER BY objective_id, question_id
        """,
        OBSOLETE_OBJECTIVE_IDS,
    ).fetchall()
    question_ids = tuple(q["question_id"] for q in questions)

    for q in questions:
        conn.execute(
            """
            INSERT INTO archived_questions
              (question_id, original_objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key, archive_reason, archived_at)
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1
                FROM archived_questions
                WHERE question_id = ?
                  AND original_objective_id = ?
                  AND archive_reason = ?
            )
            """,
            (
                q["question_id"],
                q["objective_id"],
                q["stem"],
                q["choice_a"],
                q["choice_b"],
                q["choice_c"],
                q["choice_d"],
                q["answer_key"],
                question_archive_reason,
                NOW,
                q["question_id"],
                q["objective_id"],
                question_archive_reason,
            ),
        )

    if question_ids:
        question_sql = placeholders(question_ids)
        if table_exists(conn, "diagnostic_items"):
            diagnostic_item_ids = tuple(
                row["id"]
                for row in conn.execute(
                    f"SELECT id FROM diagnostic_items WHERE question_id IN ({question_sql})",
                    question_ids,
                ).fetchall()
            )
            if diagnostic_item_ids and table_exists(conn, "diagnostic_responses"):
                diagnostic_item_sql = placeholders(diagnostic_item_ids)
                conn.execute(
                    f"""
                    DELETE FROM diagnostic_responses
                    WHERE diagnostic_item_id IN ({diagnostic_item_sql})
                    """,
                    diagnostic_item_ids,
                )
            conn.execute(
                f"DELETE FROM diagnostic_items WHERE question_id IN ({question_sql})",
                question_ids,
            )
        if table_exists(conn, "question_metadata"):
            conn.execute(
                f"DELETE FROM question_metadata WHERE question_id IN ({question_sql})",
                question_ids,
            )
        if table_exists(conn, "question_skills"):
            conn.execute(
                f"DELETE FROM question_skills WHERE question_id IN ({question_sql})",
                question_ids,
            )
        conn.execute(
            f"DELETE FROM attempts WHERE question_id IN ({question_sql})",
            question_ids,
        )
        conn.execute(
            f"DELETE FROM responses WHERE question_id IN ({question_sql})",
            question_ids,
        )

    if table_exists(conn, "objective_levels"):
        conn.execute(
            f"DELETE FROM objective_levels WHERE objective_id IN ({objective_sql})",
            OBSOLETE_OBJECTIVE_IDS,
        )
    if table_exists(conn, "frustration_events"):
        conn.execute(
            f"DELETE FROM frustration_events WHERE objective_id IN ({objective_sql})",
            OBSOLETE_OBJECTIVE_IDS,
        )
    if table_exists(conn, "student_objective_state"):
        conn.execute(
            f"""
            DELETE FROM student_objective_state
            WHERE current_objective_id IN ({objective_sql})
            """,
            OBSOLETE_OBJECTIVE_IDS,
        )
    if table_exists(conn, "lists"):
        conn.execute(
            f"""
            DELETE FROM lists
            WHERE objective_id IN ({objective_sql})
               OR advance_to IN ({objective_sql})
               OR remediation_to IN ({objective_sql})
            """,
            (*OBSOLETE_OBJECTIVE_IDS, *OBSOLETE_OBJECTIVE_IDS, *OBSOLETE_OBJECTIVE_IDS),
        )
    if table_exists(conn, "paths"):
        conn.execute(
            f"""
            DELETE FROM paths
            WHERE from_node IN ({objective_sql})
               OR to_node IN ({objective_sql})
            """,
            (*OBSOLETE_OBJECTIVE_IDS, *OBSOLETE_OBJECTIVE_IDS),
        )
    conn.execute(
        f"DELETE FROM questions WHERE objective_id IN ({objective_sql})",
        OBSOLETE_OBJECTIVE_IDS,
    )
    conn.execute(
        f"DELETE FROM objectives WHERE objective_id IN ({objective_sql})",
        OBSOLETE_OBJECTIVE_IDS,
    )
    conn.execute(
        """
        INSERT INTO migration_log (event, details, ts)
        VALUES (?, ?, ?)
        """,
        (
            "retire_obsolete_classroom_launch_objectives",
            (
                f"Archived {len(objectives)} obsolete objectives and "
                f"{len(questions)} dependent questions; removed dependent rows "
                "for classroom launch manifest."
            ),
            NOW,
        ),
    )
    conn.commit()

print(f"Archived obsolete objectives: {len(objectives)}")
print(f"Archived dependent questions: {len(questions)}")
print("Retired obsolete classroom launch objectives")
