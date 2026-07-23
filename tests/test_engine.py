import hashlib
import gc
import importlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB = ROOT / "data" / "ngss.db"
SCHEMA_SQL = ROOT / "app_core" / "schema.sql"
STUDENT_ID = "TST_ENGINE"
STANDARD_ID = "MS-LS1-1"
OBJECTIVE_ID = "MS-LS1-1A"
QUESTION_IDS = tuple(f"Q_ENGINE_{i}" for i in range(7))


def production_db_fingerprint() -> dict:
    stat = PRODUCTION_DB.stat()
    digest = hashlib.sha256(PRODUCTION_DB.read_bytes()).hexdigest()
    with sqlite3.connect(f"file:{PRODUCTION_DB.as_posix()}?mode=ro", uri=True) as conn:
        return {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": digest,
            "q0_q6_responses": conn.execute(
                """
                SELECT COUNT(*)
                FROM responses
                WHERE question_id IN ('Q0', 'Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q6')
                """
            ).fetchone()[0],
            "engine_test_student_responses": conn.execute(
                "SELECT COUNT(*) FROM responses WHERE student_id = ?",
                (STUDENT_ID,),
            ).fetchone()[0],
        }


def import_engine_for_database(db_path: Path):
    os.environ["NGSS_DB"] = str(db_path)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    sys.modules.pop("adaptive_engine", None)
    sys.modules.pop("app_core.adaptive_engine", None)
    return importlib.import_module("app_core.adaptive_engine")


class AdaptiveEngineIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "engine_test.db"
        self.engine = import_engine_for_database(self.db_path)
        self.seed_database()

    def tearDown(self):
        sys.modules.pop("adaptive_engine", None)
        sys.modules.pop("app_core.adaptive_engine", None)
        self.engine = None
        gc.collect()
        self.temp_dir.cleanup()

    def seed_database(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS standard_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_standard_id TEXT NOT NULL,
                    to_standard_id TEXT NOT NULL,
                    link_type TEXT NOT NULL,
                    source TEXT,
                    priority INTEGER DEFAULT 1,
                    notes TEXT,
                    created_at INTEGER
                )
                """
            )
            conn.execute(
                """
                INSERT INTO standards (standard_id, standard_name, core_idea, grade_band)
                VALUES (?, ?, ?, ?)
                """,
                (STANDARD_ID, "MS LS1-1", "LS1", "MS"),
            )
            conn.execute(
                """
                INSERT INTO objectives (objective_id, standard_id, objective_text, order_in_band)
                VALUES (?, ?, ?, ?)
                """,
                (OBJECTIVE_ID, STANDARD_ID, "Cells are living units.", 1),
            )
            conn.execute(
                """
                INSERT INTO students (student_id, first_name, last_name, grade, class_period)
                VALUES (?, ?, ?, ?, ?)
                """,
                (STUDENT_ID, "Test", "Engine", 6, "T"),
            )
            conn.executemany(
                """
                INSERT INTO questions
                  (question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key)
                VALUES (?, ?, ?, 'A', 'B', 'C', 'D', 'A')
                """,
                [
                    (question_id, OBJECTIVE_ID, f"Engine question {index}")
                    for index, question_id in enumerate(QUESTION_IDS)
                ],
            )
            conn.commit()

    def test_log_and_rolling_avg_uses_isolated_database(self):
        for index, question_id in enumerate(QUESTION_IDS):
            self.engine.log_response(
                STUDENT_ID,
                STANDARD_ID,
                1,
                question_id,
                correct=(index % 2 == 0),
            )
            time.sleep(0.01)

        avg = self.engine.rolling7_avg(STUDENT_ID, STANDARD_ID, 1)

        self.assertGreater(avg, 0.55)
        self.assertLess(avg, 0.65)
        self.assertEqual(self.engine.response_count(STUDENT_ID, STANDARD_ID, 1), 7)
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        self.assertEqual(count, 7)

    def test_engine_behavior_does_not_modify_production_database(self):
        before = production_db_fingerprint()

        for index, question_id in enumerate(QUESTION_IDS):
            self.engine.log_response(
                STUDENT_ID,
                STANDARD_ID,
                1,
                question_id,
                correct=(index % 2 == 0),
            )

        after = production_db_fingerprint()

        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
