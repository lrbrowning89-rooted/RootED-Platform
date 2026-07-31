import gc
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEED_PATH = ROOT / "migrations" / "seed_model_assets_catalog.py"

spec = importlib.util.spec_from_file_location(
    "seed_model_assets_catalog",
    SEED_PATH,
)
seed_model_assets_catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_model_assets_catalog)


class SeedModelAssetsCatalogMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "seed-test.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()
        self.insert_question_metadata()
        self.insert_unrelated_rows()

    def tearDown(self):
        self.conn.close()
        gc.collect()
        self.temp_dir.cleanup()

    def create_tables(self):
        self.conn.execute(
            """
            CREATE TABLE model_assets (
              model_id   TEXT PRIMARY KEY CHECK (TRIM(model_id) <> ''),
              asset_type TEXT NOT NULL CHECK (asset_type IN ('image')),
              src        TEXT NOT NULL CHECK (TRIM(src) <> ''),
              alt_text   TEXT NOT NULL,
              title      TEXT,
              caption    TEXT,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE question_metadata (
              question_id TEXT PRIMARY KEY,
              model_id TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE unrelated (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def insert_question_metadata(self):
        self.conn.executemany(
            "INSERT INTO question_metadata (question_id, model_id) VALUES (?, ?)",
            [
                (f"Q_{index:03d}", row["model_id"])
                for index, row in enumerate(seed_model_assets_catalog.CATALOG, start=1)
            ],
        )
        self.conn.commit()

    def insert_unrelated_rows(self):
        self.conn.executemany(
            "INSERT INTO unrelated (id, name) VALUES (?, ?)",
            [(1, "keep"), (2, "preserve")],
        )
        self.conn.commit()

    def asset_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM model_assets").fetchone()[0]

    def asset_row(self, model_id):
        return self.conn.execute(
            "SELECT * FROM model_assets WHERE model_id = ?",
            (model_id,),
        ).fetchone()

    def unrelated_rows(self):
        return [
            tuple(row)
            for row in self.conn.execute("SELECT id, name FROM unrelated ORDER BY id")
        ]

    def test_missing_argument_rejected(self):
        with self.assertRaises(SystemExit) as raised:
            seed_model_assets_catalog.parse_args([])

        self.assertEqual(raised.exception.code, 2)

    def test_invalid_database_rejected(self):
        missing = Path(self.temp_dir.name) / "missing.db"
        directory = Path(self.temp_dir.name)

        self.assertEqual(seed_model_assets_catalog.main(["--database", str(missing)]), 2)
        self.assertEqual(seed_model_assets_catalog.main(["--database", str(directory)]), 2)

    def test_dry_run_makes_no_changes(self):
        before_rows = self.unrelated_rows()

        result = seed_model_assets_catalog.seed_model_assets_catalog(
            self.db_path,
            dry_run=True,
        )

        self.assertFalse(result["changed"])
        self.assertEqual(result["planned_action"], "upsert-catalog")
        self.assertEqual(result["inserted"], len(seed_model_assets_catalog.CATALOG))
        self.assertEqual(self.asset_count(), 0)
        self.assertEqual(self.unrelated_rows(), before_rows)

    def test_first_run_inserts_catalog_and_preserves_unrelated_rows(self):
        before_rows = self.unrelated_rows()

        result = seed_model_assets_catalog.seed_model_assets_catalog(self.db_path)

        self.assertTrue(result["changed"])
        self.assertEqual(result["inserted"], len(seed_model_assets_catalog.CATALOG))
        self.assertEqual(result["updated"], 0)
        self.assertEqual(self.asset_count(), len(seed_model_assets_catalog.CATALOG))
        self.assertEqual(self.unrelated_rows(), before_rows)

    def test_second_run_is_idempotent(self):
        seed_model_assets_catalog.seed_model_assets_catalog(self.db_path)
        before_rows = self.unrelated_rows()
        before_count = self.asset_count()

        result = seed_model_assets_catalog.seed_model_assets_catalog(self.db_path)

        self.assertFalse(result["changed"])
        self.assertEqual(result["planned_action"], "no-op")
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(self.asset_count(), before_count)
        self.assertEqual(self.unrelated_rows(), before_rows)

    def test_verify_only_passes_after_seed(self):
        seed_model_assets_catalog.seed_model_assets_catalog(self.db_path)

        result = seed_model_assets_catalog.seed_model_assets_catalog(
            self.db_path,
            verify_only=True,
        )

        self.assertTrue(result["verified"])
        self.assertEqual(result["planned_action"], "no-op")

    def test_verify_only_fails_before_seed(self):
        with self.assertRaisesRegex(RuntimeError, "Verification failed"):
            seed_model_assets_catalog.seed_model_assets_catalog(
                self.db_path,
                verify_only=True,
            )

    def test_stale_catalog_row_is_updated_without_touching_unrelated_rows(self):
        target = seed_model_assets_catalog.CATALOG[0]
        self.conn.execute(
            """
            INSERT INTO model_assets
              (model_id, asset_type, src, alt_text, title, caption, created_at, updated_at)
            VALUES (?, 'image', 'model_assets/obsolete.svg', 'Old alt', 'Old title',
                    'Old caption', 10, 20)
            """,
            (target["model_id"],),
        )
        self.conn.commit()
        before_rows = self.unrelated_rows()

        result = seed_model_assets_catalog.seed_model_assets_catalog(self.db_path)
        row = self.asset_row(target["model_id"])

        self.assertTrue(result["changed"])
        self.assertEqual(result["inserted"], len(seed_model_assets_catalog.CATALOG) - 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(row["src"], target["src"])
        self.assertEqual(row["alt_text"], target["alt_text"])
        self.assertEqual(row["title"], target["title"])
        self.assertEqual(row["caption"], target["caption"])
        self.assertEqual(row["created_at"], 10)
        self.assertGreaterEqual(row["updated_at"], 20)
        self.assertEqual(self.unrelated_rows(), before_rows)


if __name__ == "__main__":
    unittest.main()
