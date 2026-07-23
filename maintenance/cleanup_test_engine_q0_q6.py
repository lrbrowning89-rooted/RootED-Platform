import os
import shutil
import sqlite3
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("NGSS_DB", ROOT / "data" / "ngss.db"))
BACKUP_DIR = ROOT / "data" / "backups"
TEST_STUDENT_ID = "TST01"
TEST_STANDARD_ID = "MS-LS1-1"
TEST_QUESTION_IDS = tuple(f"Q{i}" for i in range(7))


def placeholders(values) -> str:
    return ",".join("?" for _ in values)


def main() -> None:
    if not DB.exists():
        raise FileNotFoundError(f"Database not found: {DB}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"ngss_before_test_engine_q0_q6_cleanup_{int(time.time())}.db"
    shutil.copy2(DB, backup_path)

    question_sql = placeholders(TEST_QUESTION_IDS)
    params = (TEST_STUDENT_ID, TEST_STANDARD_ID, *TEST_QUESTION_IDS)

    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, student_id, standard_id, level, question_id, correct, ts
            FROM responses
            WHERE student_id = ?
              AND standard_id = ?
              AND question_id IN ({question_sql})
            ORDER BY id
            """,
            params,
        ).fetchall()

        conn.execute(
            f"""
            DELETE FROM responses
            WHERE student_id = ?
              AND standard_id = ?
              AND question_id IN ({question_sql})
            """,
            params,
        )
        conn.commit()

    print(f"Backup created: {backup_path}")
    print(f"Deleted test-generated responses: {len(rows)}")
    for row in rows:
        print(
            "  "
            f"id={row['id']} student_id={row['student_id']} "
            f"standard_id={row['standard_id']} question_id={row['question_id']}"
        )


if __name__ == "__main__":
    main()
