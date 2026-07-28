import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "migrations" / "install_launch_content.py"
PACKAGES = ROOT / "data" / "imports"

from migrations import install_launch_content as installer


SCHEMA = """
PRAGMA foreign_keys=OFF;
CREATE TABLE standards(
  standard_id TEXT PRIMARY KEY, standard_name TEXT, core_idea TEXT, grade_band TEXT
);
CREATE TABLE objectives(
  objective_id TEXT PRIMARY KEY, standard_id TEXT, objective_text TEXT,
  order_in_band INTEGER, display_name TEXT, student_description TEXT,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id)
);
CREATE TABLE questions(
  question_id TEXT PRIMARY KEY, objective_id TEXT, stem TEXT, choice_a TEXT,
  choice_b TEXT, choice_c TEXT, choice_d TEXT, answer_key TEXT,
  FOREIGN KEY(objective_id) REFERENCES objectives(objective_id)
);
CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT);
CREATE TABLE user_auth_identities(id INTEGER PRIMARY KEY, user_id INTEGER);
CREATE TABLE user_platform_roles(id INTEGER PRIMARY KEY, user_id INTEGER);
CREATE TABLE user_instructional_authorizations(id INTEGER PRIMARY KEY, user_id INTEGER);
CREATE TABLE students(student_id TEXT PRIMARY KEY);
CREATE TABLE class_sections(class_id TEXT PRIMARY KEY);
CREATE TABLE class_enrollments(class_id TEXT, student_id TEXT);
CREATE TABLE responses(id INTEGER PRIMARY KEY, marker TEXT);
CREATE TABLE progress_state(id INTEGER PRIMARY KEY, marker TEXT);
CREATE TABLE student_objective_state(id INTEGER PRIMARY KEY, marker TEXT);
CREATE TABLE routing_level_attempts(attempt_id TEXT PRIMARY KEY, marker TEXT);
CREATE TABLE assignments(assignment_id TEXT PRIMARY KEY, marker TEXT);
CREATE TABLE assignment_recipients(assignment_id TEXT, student_id TEXT);
CREATE TABLE student_growth_progress(
  attempt_id TEXT PRIMARY KEY, student_id TEXT NOT NULL,
  standard_id TEXT NOT NULL, objective_id TEXT NOT NULL,
  FOREIGN KEY(student_id) REFERENCES students(student_id),
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id),
  FOREIGN KEY(objective_id) REFERENCES objectives(objective_id)
);
INSERT INTO users VALUES(1,'teacher');
INSERT INTO user_auth_identities VALUES(1,1);
INSERT INTO user_platform_roles VALUES(1,1);
INSERT INTO user_instructional_authorizations VALUES(1,1);
INSERT INTO students VALUES('S1');
INSERT INTO class_sections VALUES('C1');
INSERT INTO class_enrollments VALUES('C1','S1');
INSERT INTO responses VALUES(1,'preserve');
INSERT INTO progress_state VALUES(1,'preserve');
INSERT INTO student_objective_state VALUES(1,'preserve');
INSERT INTO routing_level_attempts VALUES('R1','preserve');
INSERT INTO assignments VALUES('A1','preserve');
INSERT INTO assignment_recipients VALUES('A1','S1');
INSERT INTO student_growth_progress
  VALUES('G1','S1','MS-LS1-1','MS-LS1-1A');
"""


class InstallLaunchContentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        self.db = self.root / "production-copy.db"
        with sqlite3.connect(self.db) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def copy_packages(self):
        target = self.root / "packages"
        target.mkdir()
        for name in installer.PACKAGE_FILES:
            shutil.copy2(PACKAGES / name, target / name)
        return target

    def mutate_package(self, name, callback):
        package_dir = self.copy_packages()
        path = package_dir / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        callback(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return package_dir

    def protected_snapshot(self):
        with self.connect() as conn:
            return {
                table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                for table in installer.PROTECTED_TABLES
                if installer.table_exists(conn, table)
            }

    def launch_counts(self):
        with self.connect() as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("standards", "objectives", "questions")
            }

    def test_cli_requires_database(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--database", result.stderr)

    def test_missing_and_invalid_database_are_rejected(self):
        with self.assertRaises(installer.LaunchContentError):
            installer.run(self.root / "missing.db")
        invalid = self.root / "invalid.db"
        invalid.write_text("not sqlite", encoding="utf-8")
        with self.assertRaises(installer.LaunchContentError):
            installer.run(invalid)

    def test_missing_package_file_is_rejected(self):
        package_dir = self.copy_packages()
        (package_dir / installer.PACKAGE_FILES[0]).unlink()
        with self.assertRaisesRegex(installer.LaunchContentError, "Missing package"):
            installer.run(self.db, package_dir=package_dir)

    def test_package_count_mismatch_is_rejected(self):
        def remove_question(payload):
            payload["questions"].pop()

        package_dir = self.mutate_package("MS-LS1-1A.json", remove_question)
        with self.assertRaisesRegex(installer.LaunchContentError, "21 questions"):
            installer.run(self.db, package_dir=package_dir)

    def test_duplicate_objective_id_is_rejected(self):
        def duplicate_objective(payload):
            payload["objective_id"] = "MS-LS1-1A"
            for question in payload["questions"]:
                question["objective_id"] = "MS-LS1-1A"

        package_dir = self.mutate_package("MS-LS1-1B.json", duplicate_objective)
        with self.assertRaisesRegex(installer.LaunchContentError, "Duplicate objective"):
            installer.run(self.db, package_dir=package_dir)

    def test_duplicate_question_id_is_rejected(self):
        def duplicate_question(payload):
            payload["questions"][1]["question_id"] = payload["questions"][0]["question_id"]

        package_dir = self.mutate_package("MS-LS1-2A.json", duplicate_question)
        with self.assertRaisesRegex(installer.LaunchContentError, "Duplicate question"):
            installer.run(self.db, package_dir=package_dir)

    def test_objective_with_wrong_or_duplicate_standard_is_rejected(self):
        def wrong_standard(payload):
            payload["standard_id"] = "MS-LS1-1"
            for question in payload["questions"]:
                question["standard_id"] = "MS-LS1-1"

        package_dir = self.mutate_package("MS-LS1-2A.json", wrong_standard)
        with self.assertRaisesRegex(installer.LaunchContentError, "missing standard"):
            installer.run(self.db, package_dir=package_dir)

    def test_question_with_missing_objective_is_rejected(self):
        def wrong_objective(payload):
            payload["questions"][0]["objective_id"] = "MISSING"

        package_dir = self.mutate_package("MS-LS1-3A.json", wrong_objective)
        with self.assertRaisesRegex(installer.LaunchContentError, "missing objective"):
            installer.run(self.db, package_dir=package_dir)

    def test_dry_run_leaves_database_unchanged(self):
        before_bytes = self.db.read_bytes()
        before = self.protected_snapshot()
        result = installer.run(self.db, dry_run=True)
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["counts"]["inserted"], 304)
        self.assertEqual(self.launch_counts(), {
            "standards": 0, "objectives": 0, "questions": 0
        })
        self.assertEqual(self.protected_snapshot(), before)
        self.assertEqual(self.db.read_bytes(), before_bytes)

    def test_first_run_installs_exact_launch_content_and_metadata(self):
        result = installer.run(self.db)
        self.assertEqual(result["counts"], {
            "inserted": 304, "updated": 0, "unchanged": 0, "rejected": 0
        })
        self.assertEqual(self.launch_counts(), {
            "standards": 3, "objectives": 7, "questions": 147
        })
        with self.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM question_metadata").fetchone()[0],
                147,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM content_release_log").fetchone()[0],
                1,
            )
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            standards = {
                row[0] for row in conn.execute("SELECT standard_id FROM standards")
            }
            self.assertEqual(standards, set(installer.LAUNCH_STANDARD_IDS))

    def test_second_run_is_idempotent_and_log_stays_single(self):
        installer.run(self.db)
        protected = self.protected_snapshot()
        result = installer.run(self.db)
        self.assertEqual(result["counts"], {
            "inserted": 0, "updated": 0, "unchanged": 304, "rejected": 0
        })
        self.assertEqual(result["release_log"], "unchanged")
        self.assertEqual(self.protected_snapshot(), protected)
        with self.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM content_release_log").fetchone()[0],
                1,
            )

    def test_protected_data_is_unchanged_and_growth_violations_are_repaired(self):
        before = self.protected_snapshot()
        with self.connect() as conn:
            self.assertEqual(len(conn.execute("PRAGMA foreign_key_check").fetchall()), 2)
        installer.run(self.db)
        self.assertEqual(self.protected_snapshot(), before)
        with self.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_verify_only_passes_and_checks_application_catalogs(self):
        installer.run(self.db)
        before = self.db.read_bytes()
        result = installer.run(self.db, verify_only=True)
        self.assertEqual(result["verification"]["assignment_catalog_standards"], 3)
        self.assertEqual(result["verification"]["question_preview_objectives"], 7)
        self.assertEqual(result["verification"]["foreign_key_violations"], 0)
        self.assertEqual(self.db.read_bytes(), before)

    def test_verify_only_fails_on_incomplete_or_modified_content(self):
        installer.run(self.db)
        with self.connect() as conn:
            conn.execute("DELETE FROM questions WHERE question_id=?", (
                next(iter(installer.load_package().questions)),))
            conn.commit()
        with self.assertRaises(installer.LaunchContentError):
            installer.run(self.db, verify_only=True)

    def test_no_unrelated_development_content_is_imported(self):
        installer.run(self.db)
        with self.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM standards WHERE standard_id NOT IN (?,?,?)",
                    installer.LAUNCH_STANDARD_IDS,
                ).fetchone()[0],
                0,
            )

    def test_forced_failure_rolls_back_everything(self):
        before = self.db.read_bytes()
        with self.assertRaisesRegex(installer.LaunchContentError, "Forced"):
            installer.run(self.db, force_failure_after=20)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.launch_counts(), {
            "standards": 0, "objectives": 0, "questions": 0
        })

    def test_existing_stable_id_relationship_conflict_rolls_back(self):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO objectives VALUES (?,?,?,?,?,?)",
                ("MS-LS1-1A", "OTHER", "x", 1, None, None),
            )
            conn.commit()
        before = self.db.read_bytes()
        with self.assertRaisesRegex(installer.LaunchContentError, "Objective conflict"):
            installer.run(self.db)
        self.assertEqual(self.db.read_bytes(), before)

    def test_package_checksum_conflict_is_rejected(self):
        installer.run(self.db)
        package_dir = self.copy_packages()
        path = package_dir / "MS-LS1-1A.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["questions"][0]["prompt"] += " Changed."
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(installer.LaunchContentError, "different checksum"):
            installer.run(self.db, package_dir=package_dir)

    def test_model_assets_table_is_not_required(self):
        result = installer.run(self.db)
        self.assertEqual(
            result["verification"]["model_assets"], "not required; table absent"
        )
        with self.connect() as conn:
            self.assertFalse(installer.table_exists(conn, "model_assets"))


if __name__ == "__main__":
    unittest.main()
