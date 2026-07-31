import gc
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "migrations" / "add_model_assets_table.py"

spec = importlib.util.spec_from_file_location(
    "add_model_assets_table",
    MIGRATION_PATH,
)
add_model_assets_table = importlib.util.module_from_spec(spec)
spec.loader.exec_module(add_model_assets_table)


class AddModelAssetsTableMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "migration-test.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE unrelated (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL
            )
            """
        )
        self.conn.executemany(
            "INSERT INTO unrelated (id, name) VALUES (?, ?)",
            [(1, "keep"), (2, "preserve")],
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        gc.collect()
        self.temp_dir.cleanup()

    def table_exists(self, name: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            is not None
        )

    def unrelated_rows(self):
        return [
            tuple(row)
            for row in self.conn.execute("SELECT id, name FROM unrelated ORDER BY id")
        ]

    def migration_log_count(self):
        if not self.table_exists("migration_log"):
            return 0
        return self.conn.execute(
            "SELECT COUNT(*) FROM migration_log WHERE event=?",
            (add_model_assets_table.MIGRATION_EVENT,),
        ).fetchone()[0]

    def test_missing_argument_rejected(self):
        with self.assertRaises(SystemExit) as raised:
            add_model_assets_table.parse_args([])

        self.assertEqual(raised.exception.code, 2)

    def test_invalid_database_rejected(self):
        missing = Path(self.temp_dir.name) / "missing.db"
        directory = Path(self.temp_dir.name)

        self.assertEqual(add_model_assets_table.main(["--database", str(missing)]), 2)
        self.assertEqual(add_model_assets_table.main(["--database", str(directory)]), 2)

    def test_first_run_creates_tables_and_records_migration_once(self):
        before_rows = self.unrelated_rows()

        result = add_model_assets_table.ensure_model_assets_schema(self.db_path)

        self.assertTrue(result["changed"])
        self.assertTrue(self.table_exists("migration_log"))
        self.assertTrue(self.table_exists("model_assets"))
        self.assertEqual(self.migration_log_count(), 1)
        self.assertEqual(self.unrelated_rows(), before_rows)

    def test_second_run_is_idempotent(self):
        add_model_assets_table.ensure_model_assets_schema(self.db_path)
        before_rows = self.unrelated_rows()
        before_log_count = self.migration_log_count()

        result = add_model_assets_table.ensure_model_assets_schema(self.db_path)

        self.assertFalse(result["changed"])
        self.assertEqual(result["planned_action"], "no-op")
        self.assertEqual(self.migration_log_count(), before_log_count)
        self.assertEqual(self.unrelated_rows(), before_rows)

    def test_dry_run_makes_no_changes(self):
        before_rows = self.unrelated_rows()

        result = add_model_assets_table.ensure_model_assets_schema(
            self.db_path,
            dry_run=True,
        )

        self.assertFalse(result["changed"])
        self.assertEqual(result["planned_action"], "create-or-repair-schema")
        self.assertFalse(self.table_exists("migration_log"))
        self.assertFalse(self.table_exists("model_assets"))
        self.assertEqual(self.unrelated_rows(), before_rows)

    def test_verify_only_passes_after_migration(self):
        add_model_assets_table.ensure_model_assets_schema(self.db_path)

        result = add_model_assets_table.ensure_model_assets_schema(
            self.db_path,
            verify_only=True,
        )

        self.assertTrue(result["verified"])
        self.assertEqual(result["planned_action"], "no-op")
        self.assertEqual(self.migration_log_count(), 1)

    def test_verify_only_fails_before_migration(self):
        with self.assertRaisesRegex(RuntimeError, "Verification failed"):
            add_model_assets_table.ensure_model_assets_schema(
                self.db_path,
                verify_only=True,
            )


if __name__ == "__main__":
    unittest.main()
