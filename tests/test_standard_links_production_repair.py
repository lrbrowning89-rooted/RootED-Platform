from contextlib import closing
import importlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app_core.database_safety import DatabaseSafetyError
from maintenance import repair_standard_links as repair
from migrations.standard_links import ensure_standard_links_schema, seed_mvp_standard_links


class StandardLinksRepairTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys=ON")

    def tearDown(self):
        self.connection.close()

    def migrate(self):
        return repair.migrate_connection(self.connection, target_label="synthetic-memory")

    def test_missing_table_is_created_and_seeded(self):
        result = self.migrate()
        self.assertEqual(result.schema_status, "canonical")
        self.assertEqual(result.graph_status, "exact")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM standard_links").fetchone()[0], 4)

    def test_empty_canonical_table_is_seeded(self):
        ensure_standard_links_schema(self.connection)
        self.connection.commit()
        self.assertEqual(self.migrate().approved_rows_present, 4)

    def test_exact_existing_graph_is_noop(self):
        ensure_standard_links_schema(self.connection)
        seed_mvp_standard_links(self.connection, created_at=1)
        self.connection.commit()
        before = list(self.connection.execute("SELECT * FROM standard_links ORDER BY id"))
        self.migrate()
        self.assertEqual(list(self.connection.execute("SELECT * FROM standard_links ORDER BY id")), before)

    def test_partial_compatible_graph_is_completed(self):
        ensure_standard_links_schema(self.connection)
        seed_mvp_standard_links(self.connection, created_at=1)
        self.connection.execute("DELETE FROM standard_links WHERE id IN (3,4)")
        self.connection.commit()
        self.assertEqual(self.migrate().approved_rows_present, 4)

    def test_incompatible_schema_fails_closed(self):
        self.connection.execute("CREATE TABLE standard_links(id INTEGER PRIMARY KEY)")
        self.connection.commit()
        with self.assertRaisesRegex(repair.StandardLinksRepairError, "incompatible"):
            self.migrate()
        self.assertEqual([row[1] for row in self.connection.execute("PRAGMA table_info(standard_links)")], ["id"])

    def test_conflicting_approved_relationship_fails_closed(self):
        ensure_standard_links_schema(self.connection)
        self.connection.execute(
            """INSERT INTO standard_links
               (from_standard_id,to_standard_id,link_type,source,priority,notes,created_at)
               VALUES ('MS-LS1-1','MS-LS1-2','progression','conflict',1,'conflict',1)"""
        )
        self.connection.commit()
        with self.assertRaisesRegex(repair.StandardLinksRepairError, "conflicting"):
            self.migrate()

    def test_unexpected_extra_relationship_fails_closed(self):
        ensure_standard_links_schema(self.connection)
        self.connection.execute(
            """INSERT INTO standard_links
               (from_standard_id,to_standard_id,link_type,source,priority,notes,created_at)
               VALUES ('MS-X','MS-Y','progression','extra',1,'extra',1)"""
        )
        self.connection.commit()
        with self.assertRaisesRegex(repair.StandardLinksRepairError, "unexpected extra"):
            self.migrate()

    def test_seed_failure_rolls_back_table_creation(self):
        with patch.object(repair, "seed_mvp_standard_links", side_effect=RuntimeError("seed failure")):
            with self.assertRaisesRegex(RuntimeError, "seed failure"):
                self.migrate()
        self.assertNotIn("standard_links", {
            row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        })

    def test_verification_failure_rolls_back_table_creation(self):
        with patch.object(repair, "verify_mvp_standard_links", side_effect=RuntimeError("verify failure")):
            with self.assertRaisesRegex(RuntimeError, "verify failure"):
                self.migrate()
        self.assertNotIn("standard_links", {
            row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        })

    def test_repeated_migration_is_idempotent(self):
        first = self.migrate()
        second = self.migrate()
        self.assertEqual((first.graph_status, second.graph_status), ("exact", "exact"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM standard_links").fetchone()[0], 4)

    def test_operational_tables_are_not_modified(self):
        self.connection.executescript(
            """CREATE TABLE users(id INTEGER PRIMARY KEY, marker TEXT);
               CREATE TABLE students(id INTEGER PRIMARY KEY, marker TEXT);
               CREATE TABLE attempts(id INTEGER PRIMARY KEY, marker TEXT);
               CREATE TABLE responses(id INTEGER PRIMARY KEY, marker TEXT);
               CREATE TABLE progress_state(id INTEGER PRIMARY KEY, marker TEXT);
               INSERT INTO users VALUES(1,'keep'); INSERT INTO students VALUES(1,'keep');
               INSERT INTO attempts VALUES(1,'keep'); INSERT INTO responses VALUES(1,'keep');
               INSERT INTO progress_state VALUES(1,'keep');"""
        )
        self.connection.commit()
        before = {
            table: list(self.connection.execute(f"SELECT * FROM {table}"))
            for table in ("users", "students", "attempts", "responses", "progress_state")
        }
        self.migrate()
        self.assertEqual(before, {
            table: list(self.connection.execute(f"SELECT * FROM {table}"))
            for table in before
        })

    def test_curriculum_and_metadata_tables_are_not_modified(self):
        tables = ("standards", "objectives", "questions", "content_release_log", "rooted_database_metadata")
        for table in tables:
            self.connection.execute(f"CREATE TABLE {table}(marker TEXT)")
            self.connection.execute(f"INSERT INTO {table} VALUES('keep')")
        self.connection.commit()
        self.migrate()
        for table in tables:
            self.assertEqual(self.connection.execute(f"SELECT marker FROM {table}").fetchall(), [("keep",)])


class StandardLinksRepairCliSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rooted-standard-links-repair-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.database = self.directory / "synthetic_production_repair.db"

    def create_new_database(self):
        with closing(sqlite3.connect(self.database)):
            pass

    def production_environment(self):
        return {"ROOTED_ENV": "production"}

    def test_production_environment_is_required(self):
        for environment in ({}, {"ROOTED_ENV": ""}, {"ROOTED_ENV": "invalid"}):
            with self.subTest(environment=environment):
                with self.assertRaises(DatabaseSafetyError):
                    repair.validate_production_target("C:/rooted-production/data.db", environment)

    def test_development_and_test_are_rejected(self):
        for value in ("development", "test"):
            with self.assertRaises(DatabaseSafetyError):
                repair.validate_production_target(
                    "C:/rooted-production/data.db", {"ROOTED_ENV": value}
                )

    def test_unsafe_production_path_is_rejected(self):
        with self.assertRaises(DatabaseSafetyError):
            repair.validate_production_target(
                "C:/rooted-production/synthetic_dev.db", self.production_environment()
            )

    def test_nonexistent_target_is_rejected(self):
        with self.assertRaises(repair.StandardLinksRepairError):
            repair.validate_production_target(
                "C:/rooted-production/missing.db", self.production_environment()
            )

    def test_explicit_absolute_target_is_required(self):
        for target in (None, "", "relative-production.db"):
            with self.subTest(target=target):
                with self.assertRaises(DatabaseSafetyError):
                    repair.validate_production_target(target, self.production_environment())

    def test_verify_only_performs_no_writes(self):
        self.create_new_database()
        with patch.object(repair, "validate_production_target", return_value=self.database):
            result = repair.verify_database(self.database, environ=self.production_environment())
        self.assertEqual(result.schema_status, "absent")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertNotIn("standard_links", {
                row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            })

    def test_migrate_database_repairs_new_disposable_file(self):
        self.create_new_database()
        with patch.object(repair, "validate_production_target", return_value=self.database):
            result = repair.migrate_database(
                self.database, environ=self.production_environment()
            )
        self.assertEqual(result.graph_status, "exact")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM standard_links").fetchone()[0],
                4,
            )

    def test_import_has_no_file_or_database_side_effect(self):
        before = set(self.directory.iterdir())
        with patch.dict(os.environ, {}, clear=True):
            importlib.reload(repair)
        self.assertEqual(set(self.directory.iterdir()), before)

    def test_missing_database_cli_argument_is_rejected(self):
        with self.assertRaises(SystemExit) as raised:
            repair.build_parser().parse_args(["verify"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
