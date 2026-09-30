import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TEST_DIRECTORY = tempfile.TemporaryDirectory()
os.environ["ROOTED_ENV"] = "test"
os.environ["NGSS_DB"] = str(Path(TEST_DIRECTORY.name) / "config_test.db")

from app_core.config import (  # noqa: E402
    DatabaseSafetyError,
    LEGACY_DATABASE_PATH,
    resolve_rooted_environment,
    validate_database_target,
)


class DatabaseSafetyTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        TEST_DIRECTORY.cleanup()

    def test_development_and_test_require_explicit_database(self):
        for environment in ("development", "test"):
            with self.subTest(environment=environment):
                with self.assertRaises(DatabaseSafetyError):
                    validate_database_target(None, rooted_env=environment)

    def test_rooted_environment_is_always_explicit_even_on_render(self):
        with patch.dict(os.environ, {"RENDER": "true"}, clear=True):
            with self.assertRaises(DatabaseSafetyError):
                resolve_rooted_environment()

    def test_invalid_rooted_environment_fails_closed(self):
        with patch.dict(os.environ, {"ROOTED_ENV": "staging"}, clear=True):
            with self.assertRaises(DatabaseSafetyError):
                resolve_rooted_environment()

    def test_production_requires_explicit_database(self):
        with self.assertRaises(DatabaseSafetyError):
            validate_database_target(None, rooted_env="production")

    def test_development_and_test_reject_legacy_database(self):
        for environment in ("development", "test"):
            with self.subTest(environment=environment):
                with self.assertRaises(DatabaseSafetyError):
                    validate_database_target(
                        LEGACY_DATABASE_PATH, rooted_env=environment
                    )

    def test_environment_specific_name_guards(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaises(DatabaseSafetyError):
                validate_database_target(
                    root / "production.db", rooted_env="development"
                )
            with self.assertRaises(DatabaseSafetyError):
                validate_database_target(root / "rooted_dev.db", rooted_env="production")

    def test_dashboard_has_no_import_time_database_boot(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "import_test.db"
            dashboard_source = (ROOT / "app_core" / "dashboard_v5.py").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("with sqlite3.connect(DB) as _conn_boot", dashboard_source)
            self.assertIn("def initialize_database()", dashboard_source)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
