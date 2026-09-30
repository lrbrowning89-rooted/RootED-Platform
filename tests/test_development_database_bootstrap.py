import importlib
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app_core.database.bootstrap as bootstrap
from app_core.database.inspection import table_names, unique_column_sets
from app_core.database.metadata import read_database_metadata
from app_core.database.schema import REQUIRED_TABLES, SCHEMA_VERSION, canonical_schema_checksum
from app_core.database_safety import DatabaseSafetyError


class DevelopmentDatabaseBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rooted-synthetic-dev-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.target = self.directory / "rooted_synthetic_dev.db"
        self.environment = {"ROOTED_ENV": "development"}

    def working_files(self):
        return list(self.directory.glob("*.bootstrap-*.tmp"))

    def assert_rejected_environment(self, value):
        environment = {} if value is None else {"ROOTED_ENV": value}
        with self.assertRaises(DatabaseSafetyError):
            bootstrap.create_development_database(self.target, environ=environment)
        self.assertFalse(self.target.exists())

    def test_missing_environment_rejected(self):
        self.assert_rejected_environment(None)

    def test_blank_environment_rejected(self):
        self.assert_rejected_environment("  ")

    def test_production_environment_rejected(self):
        self.assert_rejected_environment("production")

    def test_test_environment_rejected(self):
        self.assert_rejected_environment("test")

    def test_invalid_environment_rejected(self):
        self.assert_rejected_environment("staging")

    def test_unsafe_target_rejected(self):
        with self.assertRaises(DatabaseSafetyError):
            bootstrap.create_development_database(
                Path("C:/neutral/rooted.db"), environ=self.environment
            )

    def test_production_looking_target_rejected(self):
        with self.assertRaises(DatabaseSafetyError):
            bootstrap.create_development_database(
                self.directory / "production.db", environ=self.environment
            )

    def test_ngss_basename_rejected(self):
        with self.assertRaises(DatabaseSafetyError):
            bootstrap.create_development_database(
                self.directory / "ngss.db", environ=self.environment
            )

    def test_missing_database_argument_rejected(self):
        with self.assertRaises(SystemExit) as raised:
            bootstrap.build_parser().parse_args(["create-development-database"])
        self.assertEqual(raised.exception.code, 2)

    def test_nonexistent_parent_rejected(self):
        with self.assertRaisesRegex(bootstrap.DevelopmentBootstrapError, "parent directory"):
            bootstrap.create_development_database(
                self.directory / "missing" / "synthetic_dev.db",
                environ=self.environment,
            )

    def test_existing_target_rejected_without_sqlite_open(self):
        original = b"existing synthetic sentinel"
        self.target.write_bytes(original)
        with patch.object(bootstrap.sqlite3, "connect") as connect:
            with self.assertRaisesRegex(bootstrap.DevelopmentBootstrapError, "already exists"):
                bootstrap.create_development_database(self.target, environ=self.environment)
        connect.assert_not_called()
        self.assertEqual(self.target.read_bytes(), original)

    def test_ngss_db_disagreement_rejected(self):
        environment = {
            "ROOTED_ENV": "development",
            "NGSS_DB": str(self.directory / "different_synthetic_dev.db"),
        }
        with self.assertRaisesRegex(DatabaseSafetyError, "same target"):
            bootstrap.create_development_database(self.target, environ=environment)

    def test_matching_ngss_db_is_allowed(self):
        environment = {"ROOTED_ENV": "development", "NGSS_DB": str(self.target)}
        self.assertEqual(
            bootstrap.create_development_database(self.target, environ=environment),
            self.target.resolve(),
        )

    def test_safe_target_is_created(self):
        result = bootstrap.create_development_database(self.target, environ=self.environment)
        self.assertEqual(result, self.target.resolve())
        self.assertTrue(self.target.is_file())

    def test_created_metadata_is_exactly_one_synthetic_row(self):
        bootstrap.create_development_database(self.target, environ=self.environment)
        with closing(sqlite3.connect(self.target)) as connection:
            rows = connection.execute("SELECT COUNT(*) FROM rooted_database_metadata").fetchone()[0]
            metadata = read_database_metadata(connection)
        self.assertEqual(rows, 1)
        self.assertEqual(metadata["singleton_id"], 1)
        self.assertEqual(metadata["environment"], "development")
        self.assertEqual(metadata["data_classification"], "synthetic")
        self.assertEqual(metadata["schema_version"], SCHEMA_VERSION)
        self.assertEqual(metadata["canonical_schema_checksum"], canonical_schema_checksum())
        self.assertEqual(metadata["created_by_tool"], bootstrap.CREATED_BY_TOOL)
        self.assertTrue(metadata["database_identifier"])
        self.assertEqual(metadata["content_release"], bootstrap.CONTENT_RELEASE)
        self.assertIsNone(metadata["synthetic_fixture_version"])

    def test_created_schema_has_canonical_tables(self):
        bootstrap.create_development_database(self.target, environ=self.environment)
        with closing(sqlite3.connect(self.target)) as connection:
            self.assertEqual(table_names(connection), REQUIRED_TABLES)

    def test_operational_and_content_tables_are_empty(self):
        bootstrap.create_development_database(self.target, environ=self.environment)
        with closing(sqlite3.connect(self.target)) as connection:
            for table in bootstrap.EMPTY_TABLES:
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_integrity_checks_pass(self):
        bootstrap.create_development_database(self.target, environ=self.environment)
        with closing(sqlite3.connect(self.target)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_standard_links_uniqueness_exists(self):
        bootstrap.create_development_database(self.target, environ=self.environment)
        with closing(sqlite3.connect(self.target)) as connection:
            self.assertIn(
                ("from_standard_id", "to_standard_id", "link_type"),
                unique_column_sets(connection, "standard_links"),
            )

    def test_import_creates_no_files(self):
        before = set(self.directory.iterdir())
        with patch.dict(os.environ, {}, clear=True):
            importlib.reload(bootstrap)
        self.assertEqual(set(self.directory.iterdir()), before)

    def assert_stage_failure_cleans_working_file(self, attribute):
        with patch.object(bootstrap, attribute, side_effect=RuntimeError("simulated failure")):
            with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                bootstrap.create_development_database(self.target, environ=self.environment)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.working_files(), [])

    def test_schema_failure_cleans_only_working_file(self):
        sentinel = self.directory / "unrelated-synthetic.txt"
        sentinel.write_bytes(b"keep")
        self.assert_stage_failure_cleans_working_file("apply_canonical_schema")
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_metadata_failure_cleans_working_file(self):
        self.assert_stage_failure_cleans_working_file("insert_database_metadata")

    def test_verification_failure_cleans_working_file(self):
        self.assert_stage_failure_cleans_working_file("verify_development_database")

    def test_finalization_race_never_overwrites_target(self):
        racer = b"created by racing process"

        def race(_working, target):
            target.write_bytes(racer)
            raise FileExistsError("simulated no-overwrite race")

        with patch.object(bootstrap, "_finalize_no_overwrite", side_effect=race):
            with self.assertRaises(FileExistsError):
                bootstrap.create_development_database(self.target, environ=self.environment)
        self.assertEqual(self.target.read_bytes(), racer)
        self.assertEqual(self.working_files(), [])

    def test_success_leaves_no_working_file(self):
        bootstrap.create_development_database(self.target, environ=self.environment)
        self.assertEqual(self.working_files(), [])

    def test_handled_failure_leaves_no_working_file(self):
        self.assert_stage_failure_cleans_working_file("verify_development_database")


if __name__ == "__main__":
    unittest.main()
