import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from migrations.standard_links import (
    MVP_STANDARD_LINKS,
    ensure_standard_links_schema,
    seed_mvp_standard_links,
)

ROOT = Path(__file__).resolve().parents[1]


class StandardLinksSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "standard_links_test.db"
        self.connection = sqlite3.connect(self.database)
        self.connection.execute("CREATE TABLE progress_state (id INTEGER PRIMARY KEY)")

    def tearDown(self):
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_schema_is_explicit_without_legacy_progress_columns(self):
        ensure_standard_links_schema(self.connection)
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(progress_state)")
        }
        self.assertTrue(
            {"current_standard_id", "origin_standard_id", "active_route_type"}
            .isdisjoint(columns)
        )

    def test_seed_preserves_four_links_and_is_idempotent(self):
        self.assertEqual(seed_mvp_standard_links(self.connection, created_at=123), 4)
        self.assertEqual(seed_mvp_standard_links(self.connection, created_at=456), 0)
        rows = self.connection.execute(
            """SELECT from_standard_id, to_standard_id, link_type, source,
                      priority, notes
               FROM standard_links ORDER BY id"""
        ).fetchall()
        self.assertEqual(rows, list(MVP_STANDARD_LINKS))

    def test_direct_cli_execution_reaches_required_database_argument(self):
        for script_name in (
            "add_standard_links_graph.py",
            "seed_mvp_standard_links.py",
        ):
            with self.subTest(script=script_name):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "migrations" / script_name)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--database", result.stderr)
                self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
