import importlib.util
import gc
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = ROOT / "app_core" / "schema.sql"
IMPORT_DIR = ROOT / "data" / "imports"
MIGRATION_PATH = ROOT / "migrations" / "add_ms_ls1_1a_question_metadata.py"

LAUNCH_OBJECTIVE_IDS = (
    "MS-LS1-1A",
    "MS-LS1-1B",
    "MS-LS1-2A",
    "MS-LS1-2B",
    "MS-LS1-3A",
    "MS-LS1-3B",
    "MS-LS1-3C",
)
LAUNCH_STANDARD_IDS = ("MS-LS1-1", "MS-LS1-2", "MS-LS1-3")
EXPECTED_LAUNCH_QUESTION_COUNT = 147
EXPECTED_OBJECTIVE_QUESTION_COUNT = 21


spec = importlib.util.spec_from_file_location(
    "add_ms_ls1_1a_question_metadata", MIGRATION_PATH
)
metadata_migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata_migration)


def load_artifact(objective_id: str) -> list[dict]:
    artifact_path = IMPORT_DIR / f"{objective_id}.json"
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    return payload["questions"]


def answer_choices(question: dict) -> tuple[str, str, str, str]:
    choices = question["answer_choices"]
    if isinstance(choices, list):
        return tuple(choices)
    return choices["A"], choices["B"], choices["C"], choices["D"]


def answer_key(question: dict) -> str:
    correct = question["correct_answer"]
    if correct in {"A", "B", "C", "D"}:
        return correct

    for letter, choice in zip(("A", "B", "C", "D"), answer_choices(question)):
        if choice == correct:
            return letter
    raise AssertionError(f"Could not derive answer key for {question['question_id']}")


def metadata_row(question: dict, source_artifact: str, imported_at: int = 1000) -> tuple:
    return (
        question["question_id"],
        question.get("difficulty"),
        metadata_migration.normalize_question_type(question.get("question_type")),
        question.get("explanation"),
        json.dumps(question.get("identifier_tags", [])),
        json.dumps(question.get("remediation_tags", [])),
        question.get("model_id"),
        source_artifact,
        imported_at,
    )


def standard_for_objective(objective_id: str) -> str:
    return re.sub(r"[A-Z]$", "", objective_id)


class QuestionMetadataMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "metadata_test.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        metadata_migration.ensure_question_metadata_table(self.conn)
        self.seed_launch_content()

    def tearDown(self):
        self.conn.close()
        gc.collect()
        self.temp_dir.cleanup()

    def seed_launch_content(self):
        self.conn.executemany(
            """
            INSERT INTO standards
              (standard_id, standard_name, core_idea, grade_band)
            VALUES (?, ?, 'LS1', 'MS')
            """,
            [(standard_id, standard_id) for standard_id in LAUNCH_STANDARD_IDS],
        )
        self.conn.executemany(
            """
            INSERT INTO objectives
              (objective_id, standard_id, objective_text, order_in_band)
            VALUES (?, ?, ?, ?)
            """,
            [
                (objective_id, standard_for_objective(objective_id), objective_id, index)
                for index, objective_id in enumerate(LAUNCH_OBJECTIVE_IDS, start=1)
            ],
        )

        all_questions = []
        existing_metadata = []
        for objective_id in LAUNCH_OBJECTIVE_IDS:
            questions = load_artifact(objective_id)
            self.assertEqual(len(questions), EXPECTED_OBJECTIVE_QUESTION_COUNT)
            for question in questions:
                choice_a, choice_b, choice_c, choice_d = answer_choices(question)
                all_questions.append(
                    (
                        question["question_id"],
                        question["objective_id"],
                        question["prompt"],
                        choice_a,
                        choice_b,
                        choice_c,
                        choice_d,
                        answer_key(question),
                    )
                )
                if objective_id != "MS-LS1-1A":
                    existing_metadata.append(
                        metadata_row(question, f"{objective_id}.json")
                    )

        self.conn.executemany(
            """
            INSERT INTO questions
              (question_id, objective_id, stem, choice_a, choice_b, choice_c,
               choice_d, answer_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            all_questions,
        )
        self.conn.executemany(
            """
            INSERT INTO question_metadata
              (question_id, difficulty, question_type, explanation,
               identifier_tags, remediation_tags, model_id, source_artifact,
               imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            existing_metadata,
        )
        self.conn.commit()

    def launch_metadata_count(self) -> int:
        return self.conn.execute(
            """
            SELECT COUNT(*)
            FROM questions q
            JOIN question_metadata qm ON qm.question_id = q.question_id
            WHERE q.objective_id IN (?, ?, ?, ?, ?, ?, ?)
            """,
            LAUNCH_OBJECTIVE_IDS,
        ).fetchone()[0]

    def test_all_147_launch_questions_have_metadata_after_migration(self):
        result = metadata_migration.run(self.db_path, create_backup=False)

        self.assertEqual(result["inserted"], EXPECTED_OBJECTIVE_QUESTION_COUNT)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*)
                FROM questions
                WHERE objective_id IN (?, ?, ?, ?, ?, ?, ?)
                """,
                LAUNCH_OBJECTIVE_IDS,
            ).fetchone()[0],
            EXPECTED_LAUNCH_QUESTION_COUNT,
        )
        self.assertEqual(self.launch_metadata_count(), EXPECTED_LAUNCH_QUESTION_COUNT)
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*)
                FROM question_metadata qm
                LEFT JOIN questions q ON q.question_id = qm.question_id
                WHERE q.question_id IS NULL
                """
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*)
                FROM question_metadata qm
                JOIN questions q ON q.question_id = qm.question_id
                WHERE q.objective_id NOT IN (?, ?, ?, ?, ?, ?, ?)
                """,
                LAUNCH_OBJECTIVE_IDS,
            ).fetchone()[0],
            0,
        )
        difficulty_counts = self.conn.execute(
                """
                SELECT difficulty, COUNT(*) AS count
                FROM question_metadata qm
                JOIN questions q ON q.question_id = qm.question_id
                WHERE q.objective_id = 'MS-LS1-1A'
                GROUP BY difficulty
                ORDER BY difficulty
                """
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in difficulty_counts],
            [
                ("Application", 7),
                ("Reasoning", 7),
                ("Recall", 7),
            ],
        )
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*)
                FROM question_metadata qm
                JOIN questions q ON q.question_id = qm.question_id
                WHERE q.objective_id = 'MS-LS1-1A'
                  AND qm.question_type = 'multiple_choice'
                  AND qm.model_id IS NULL
                """
            ).fetchone()[0],
            EXPECTED_OBJECTIVE_QUESTION_COUNT,
        )

    def test_rerunning_migration_creates_no_duplicates_or_changes(self):
        first = metadata_migration.run(self.db_path, create_backup=False)
        before = self.conn.execute(
            "SELECT * FROM question_metadata ORDER BY question_id"
        ).fetchall()

        second = metadata_migration.run(self.db_path, create_backup=False)
        after = self.conn.execute(
            "SELECT * FROM question_metadata ORDER BY question_id"
        ).fetchall()

        self.assertEqual(first["inserted"], EXPECTED_OBJECTIVE_QUESTION_COUNT)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["skipped"], EXPECTED_OBJECTIVE_QUESTION_COUNT)
        self.assertEqual(len(after), EXPECTED_LAUNCH_QUESTION_COUNT)
        self.assertEqual([tuple(row) for row in after], [tuple(row) for row in before])

    def test_existing_valid_metadata_is_not_overwritten(self):
        first_question = load_artifact("MS-LS1-1A")[0]
        self.conn.execute(
            """
            INSERT INTO question_metadata
              (question_id, difficulty, question_type, explanation,
               identifier_tags, remediation_tags, model_id, source_artifact,
               imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            metadata_row(first_question, "MS-LS1-1A.json", imported_at=111),
        )
        self.conn.commit()

        result = metadata_migration.run(self.db_path, create_backup=False)
        row = self.conn.execute(
            """
            SELECT imported_at
            FROM question_metadata
            WHERE question_id = ?
            """,
            (first_question["question_id"],),
        ).fetchone()

        self.assertEqual(result["inserted"], EXPECTED_OBJECTIVE_QUESTION_COUNT - 1)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(row["imported_at"], 111)


if __name__ == "__main__":
    unittest.main()
