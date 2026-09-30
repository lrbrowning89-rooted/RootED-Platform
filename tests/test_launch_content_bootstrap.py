from contextlib import closing
import importlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app_core.database.bootstrap as bootstrap
from app_core.database.metadata import read_database_metadata
from app_core.database.schema import apply_canonical_schema
from migrations import install_launch_content as launch
from migrations.standard_links import (
    MVP_STANDARD_LINKS,
    seed_mvp_standard_links,
    verify_mvp_standard_links,
)


class LaunchContentConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = launch.load_package()

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys=ON")
        apply_canonical_schema(self.connection)

    def tearDown(self):
        self.connection.close()

    def install(self):
        return launch.install_launch_content(
            self.connection,
            package=self.package,
            database_identifier="synthetic-memory-launch-test",
            imported_at=123,
        )

    def test_launch_content_installs_into_blank_canonical_database(self):
        result = self.install()
        self.assertEqual(result["counts"]["inserted"], 304)

    def test_expected_source_and_installed_counts(self):
        self.install()
        expected = {
            "standards": 3, "objectives": 7, "questions": 147,
            "question_metadata": 147,
        }
        for table, count in expected.items():
            self.assertEqual(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], count)

    def test_content_release_is_recorded(self):
        result = self.install()
        row = self.connection.execute(
            "SELECT package_name,package_version,source_checksum FROM content_release_log"
        ).fetchone()
        self.assertEqual(row, (launch.PACKAGE_NAME, launch.DEFAULT_PACKAGE_VERSION, self.package.checksum))
        self.assertEqual(result["content_release"], launch.CONTENT_RELEASE)

    def test_exact_four_standard_links_are_installed(self):
        self.install()
        self.assertEqual(seed_mvp_standard_links(self.connection, created_at=123), 4)
        verify_mvp_standard_links(self.connection)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM standard_links").fetchone()[0], 4)

    def test_duplicate_link_installation_is_safe(self):
        seed_mvp_standard_links(self.connection, created_at=123)
        self.assertEqual(seed_mvp_standard_links(self.connection, created_at=456), 0)
        verify_mvp_standard_links(self.connection)

    def test_same_content_reinstall_is_deterministic(self):
        first = self.install()
        second = self.install()
        self.assertEqual(first["release_log"], "inserted")
        self.assertEqual(second["release_log"], "unchanged")
        self.assertEqual(second["counts"]["unchanged"], 304)

    def test_connection_installer_does_not_commit_caller_transaction(self):
        self.connection.execute("BEGIN")
        self.install()
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM standards").fetchone()[0], 0)

    def test_conflicting_same_identifier_fails_closed(self):
        self.install()
        question_id = next(iter(self.package.questions))
        self.connection.execute(
            "UPDATE questions SET stem='conflicting text' WHERE question_id=?",
            (question_id,),
        )
        with self.assertRaises(launch.LaunchContentError):
            self.install()
        self.assertEqual(self.connection.execute(
            "SELECT stem FROM questions WHERE question_id=?", (question_id,)
        ).fetchone()[0], "conflicting text")

    def test_partial_existing_content_fails_closed(self):
        values = next(iter(self.package.standards.values()))
        self.connection.execute(
            "INSERT INTO standards VALUES (:standard_id,:standard_name,:core_idea,:grade_band)",
            values,
        )
        with self.assertRaisesRegex(launch.LaunchContentError, "Partial"):
            self.install()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM objectives").fetchone()[0], 0)

    def test_no_operational_or_identity_rows_are_created(self):
        self.install()
        for table in ("users", "students", "attempts", "responses", "progress_state"):
            self.assertEqual(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_missing_elementary_curriculum_does_not_block_graph(self):
        self.install()
        seed_mvp_standard_links(self.connection, created_at=123)
        verify_mvp_standard_links(self.connection)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM standard_links WHERE from_standard_id='4-LS1-1' OR to_standard_id='4-LS1-1'"
        ).fetchone()[0], 2)

    def test_no_fake_elementary_content_is_created(self):
        self.install()
        seed_mvp_standard_links(self.connection, created_at=123)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM standards WHERE standard_id='4-LS1-1'"
        ).fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM objectives WHERE standard_id='4-LS1-1'"
        ).fetchone()[0], 0)
        progress_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(progress_state)")
        }
        self.assertTrue({
            "current_standard_id", "origin_standard_id", "active_route_type"
        }.isdisjoint(progress_columns))

    def test_integrity_checks_pass(self):
        self.install()
        seed_mvp_standard_links(self.connection, created_at=123)
        self.assertEqual(self.connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_model_references_are_preserved_without_assets(self):
        self.install()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM question_metadata WHERE model_id IS NOT NULL AND TRIM(model_id)<>''"
        ).fetchone()[0], 48)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM model_assets").fetchone()[0], 0)


class LaunchContentBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rooted-launch-synthetic-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.target = self.directory / "rooted_launch_synthetic_dev.db"
        self.environment = {"ROOTED_ENV": "development"}

    def working_files(self):
        return list(self.directory.glob("*.bootstrap-*.tmp"))

    def test_bootstrap_database_receives_content_graph_and_metadata_release(self):
        bootstrap.create_development_database(self.target, environ=self.environment)
        with closing(sqlite3.connect(self.target)) as connection:
            metadata = read_database_metadata(connection)
            self.assertEqual(metadata["content_release"], launch.CONTENT_RELEASE)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM standards").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM objectives").fetchone()[0], 7)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM questions").fetchone()[0], 147)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM standard_links").fetchone()[0], 4)
            for table in bootstrap.EMPTY_TABLES:
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_bootstrap_content_failure_cleans_working_file(self):
        with patch.object(bootstrap, "install_launch_content", side_effect=RuntimeError("content failure")):
            with self.assertRaisesRegex(RuntimeError, "content failure"):
                bootstrap.create_development_database(self.target, environ=self.environment)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.working_files(), [])

    def test_bootstrap_graph_metadata_and_verification_failures_clean_working_file(self):
        for stage in (
            "seed_mvp_standard_links",
            "update_database_content_release",
            "verify_mvp_standard_links",
        ):
            target = self.directory / f"{stage}_synthetic_dev.db"
            with self.subTest(stage=stage):
                with patch.object(bootstrap, stage, side_effect=RuntimeError(stage)):
                    with self.assertRaisesRegex(RuntimeError, stage):
                        bootstrap.create_development_database(target, environ=self.environment)
                self.assertFalse(target.exists())
                self.assertEqual(self.working_files(), [])

    def test_existing_final_target_is_never_overwritten(self):
        sentinel = b"preexisting target"
        self.target.write_bytes(sentinel)
        with self.assertRaises(bootstrap.DevelopmentBootstrapError):
            bootstrap.create_development_database(self.target, environ=self.environment)
        self.assertEqual(self.target.read_bytes(), sentinel)

    def test_import_causes_no_file_or_database_mutation(self):
        before = set(self.directory.iterdir())
        with patch.dict(os.environ, {}, clear=True):
            importlib.reload(launch)
        self.assertEqual(set(self.directory.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
