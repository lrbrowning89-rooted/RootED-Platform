import ast
import gc
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPAIR_PATH = ROOT / "migrations" / "repair_cell_theory_model_asset.py"
SEED_PATH = ROOT / "migrations" / "seed_model_assets_catalog.py"

spec = importlib.util.spec_from_file_location(
    "repair_cell_theory_model_asset",
    REPAIR_PATH,
)
repair_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair_module)


class ModelAssetMetadataRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "repair-test.db"
        self.static_root = Path(self.temp_dir.name) / "static"
        self.asset_path = (
            self.static_root
            / "model_assets"
            / "ms-ls1-1b-model-cell-theory-basic-01.png"
        )
        self.asset_path.parent.mkdir(parents=True)
        self.asset_path.write_bytes(b"\x89PNG\r\n\x1a\nRootED test png")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.create_table()

    def tearDown(self):
        self.conn.close()
        gc.collect()
        self.temp_dir.cleanup()

    def create_table(self):
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
        self.conn.commit()

    def row(self, model_id=repair_module.MODEL_ID):
        return self.conn.execute(
            "SELECT * FROM model_assets WHERE model_id = ?",
            (model_id,),
        ).fetchone()

    def insert_obsolete_target_row(self):
        self.conn.execute(
            """
            INSERT INTO model_assets
              (model_id, asset_type, src, alt_text, title, caption, created_at, updated_at)
            VALUES (?, 'image', 'model_assets/ms-ls1-1b-cell-theory-basic-01.svg',
                    'Old alt', 'Old title', 'Old caption', 10, 20)
            """,
            (repair_module.MODEL_ID,),
        )
        self.conn.commit()

    def insert_unrelated_row(self):
        self.conn.execute(
            """
            INSERT INTO model_assets
              (model_id, asset_type, src, alt_text, title, caption, created_at, updated_at)
            VALUES ('UNRELATED_MODEL', 'image', 'model_assets/unrelated.svg',
                    'Unrelated alt', 'Unrelated title', 'Unrelated caption', 30, 40)
            """
        )
        self.conn.commit()

    def test_repository_seed_catalog_uses_png_and_not_obsolete_svg(self):
        tree = ast.parse(SEED_PATH.read_text(encoding="utf-8"))
        catalog = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                if any(isinstance(target, ast.Name) and target.id == "CATALOG" for target in node.targets):
                    catalog = ast.literal_eval(node.value)
                    break

        self.assertIsNotNone(catalog)
        target = next(
            row for row in catalog
            if row["model_id"] == repair_module.MODEL_ID
        )
        self.assertEqual(target["src"], repair_module.TARGET_ASSET["src"])
        self.assertNotIn(
            "model_assets/ms-ls1-1b-cell-theory-basic-01.svg",
            SEED_PATH.read_text(encoding="utf-8"),
        )

    def test_first_execution_corrects_row_second_execution_is_noop_and_unrelated_preserved(self):
        self.insert_obsolete_target_row()
        self.insert_unrelated_row()
        before_unrelated = dict(self.row("UNRELATED_MODEL"))

        first = repair_module.repair(
            self.db_path,
            self.static_root,
            dry_run=False,
            verify_only=False,
        )
        corrected = dict(self.row())
        first_updated_at = corrected["updated_at"]

        self.assertTrue(first["changed"])
        self.assertEqual(first["planned_action"], "update")
        self.assertEqual(corrected["asset_type"], "image")
        self.assertEqual(corrected["src"], repair_module.TARGET_ASSET["src"])
        self.assertEqual(corrected["alt_text"], repair_module.TARGET_ASSET["alt_text"])
        self.assertEqual(corrected["title"], repair_module.TARGET_ASSET["title"])
        self.assertEqual(corrected["caption"], repair_module.TARGET_ASSET["caption"])
        self.assertEqual(dict(self.row("UNRELATED_MODEL")), before_unrelated)

        second = repair_module.repair(
            self.db_path,
            self.static_root,
            dry_run=False,
            verify_only=False,
        )
        self.assertFalse(second["changed"])
        self.assertEqual(second["planned_action"], "no-op")
        self.assertEqual(self.row()["updated_at"], first_updated_at)
        self.assertEqual(dict(self.row("UNRELATED_MODEL")), before_unrelated)

    def test_first_execution_inserts_missing_row(self):
        result = repair_module.repair(
            self.db_path,
            self.static_root,
            dry_run=False,
            verify_only=False,
        )
        row = self.row()

        self.assertTrue(result["changed"])
        self.assertEqual(result["planned_action"], "insert")
        self.assertIsNotNone(row)
        self.assertEqual(row["model_id"], repair_module.MODEL_ID)
        self.assertEqual(row["asset_type"], "image")
        self.assertEqual(row["src"], repair_module.TARGET_ASSET["src"])

    def test_dry_run_does_not_write_and_verify_only_passes_after_repair(self):
        self.insert_obsolete_target_row()

        dry_run = repair_module.repair(
            self.db_path,
            self.static_root,
            dry_run=True,
            verify_only=False,
        )
        self.assertFalse(dry_run["changed"])
        self.assertEqual(dry_run["planned_action"], "update")
        self.assertEqual(
            self.row()["src"],
            "model_assets/ms-ls1-1b-cell-theory-basic-01.svg",
        )

        repair_module.repair(
            self.db_path,
            self.static_root,
            dry_run=False,
            verify_only=False,
        )
        verify = repair_module.repair(
            self.db_path,
            self.static_root,
            dry_run=False,
            verify_only=True,
        )
        self.assertTrue(verify["verified"])
        self.assertEqual(verify["planned_action"], "no-op")

    def test_verify_only_fails_when_static_png_is_missing(self):
        self.asset_path.unlink()

        with self.assertRaisesRegex(RuntimeError, "Required PNG is missing"):
            repair_module.repair(
                self.db_path,
                self.static_root,
                dry_run=False,
                verify_only=True,
            )


if __name__ == "__main__":
    unittest.main()
