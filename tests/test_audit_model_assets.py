import importlib.util
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_model_assets", ROOT / "tools" / "audit_model_assets.py"
)
audit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


class CombinedModelAssetAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.static = self.root / "app_core" / "static"
        self.library = self.root / "visual_library" / "model_assets"
        self.static.mkdir(parents=True)
        self.library.mkdir(parents=True)
        self.db = self.root / "audit.db"
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE question_metadata (question_id TEXT PRIMARY KEY, model_id TEXT);
                CREATE TABLE model_assets (
                    model_id TEXT PRIMARY KEY, asset_type TEXT, src TEXT,
                    alt_text TEXT, title TEXT, caption TEXT,
                    created_at INTEGER, updated_at INTEGER
                );
            """)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, src="model_assets/example.svg"):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO question_metadata VALUES (?, ?)", ("Q1", "MODEL_1"))
            conn.execute(
                "INSERT INTO model_assets VALUES (?, 'image', ?, 'alt', 'title', 'caption', 1, 1)",
                ("MODEL_1", src),
            )

    def test_svg_catalog_path_with_png_file_is_created_but_not_wired(self):
        self.seed()
        runtime = self.static / "model_assets" / "example.png"
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(b"png artwork")

        rows, warnings = audit.build_audit(self.db, self.static, self.library, self.root)

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.artwork_status, "exists")
        self.assertTrue(row.catalog_present)
        self.assertEqual(row.question_ids, ("Q1",))
        self.assertEqual(row.path_status, "mismatch:catalog-.svg/file-.png")
        self.assertEqual(row.git_status, "not-ready:untracked")

    def test_hashes_match_across_approved_export_and_runtime(self):
        self.seed("model_assets/example.png")
        payload = b"same image bytes"
        runtime = self.static / "model_assets" / "example.png"
        approved = self.library / "ms-ls1-1b" / "05_approved" / "example.png"
        export = self.library / "ms-ls1-1b" / "06_exports" / "example.png"
        for path in (runtime, approved, export):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        rows, _ = audit.build_audit(self.db, self.static, self.library, self.root)

        self.assertEqual(rows[0].path_status, "agrees")
        self.assertEqual(rows[0].hash_status, "match")

    def test_hash_mismatch_is_reported(self):
        self.seed("model_assets/example.png")
        runtime = self.static / "model_assets" / "example.png"
        approved = self.library / "ms-ls1-1b" / "05_approved" / "example.png"
        runtime.parent.mkdir(parents=True)
        approved.parent.mkdir(parents=True)
        runtime.write_bytes(b"runtime")
        approved.write_bytes(b"approved")

        rows, _ = audit.build_audit(self.db, self.static, self.library, self.root)

        self.assertEqual(rows[0].hash_status, "mismatch")
        self.assertIn("approved/export/runtime copies differ by SHA-256", rows[0].warnings)


if __name__ == "__main__":
    unittest.main()
