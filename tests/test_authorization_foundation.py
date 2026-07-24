import gc
import importlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from werkzeug.exceptions import Forbidden


ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
TEST_DB = Path(TEST_DIR.name) / f"test_authorization_{uuid.uuid4().hex}.db"
os.environ["NGSS_DB"] = str(TEST_DB)
os.environ["SECRET_KEY"] = "test-secret"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

dash = importlib.import_module("app_core.dashboard_v5")


class AuthorizationFoundationTests(unittest.TestCase):
    def setUp(self):
        dash.DB = str(TEST_DB)
        dash.ae.DB_PATH = str(TEST_DB)
        self.conn = sqlite3.connect(TEST_DB)
        self.conn.row_factory = sqlite3.Row
        dash.ensure_schema(self.conn)
        self.conn.execute("DELETE FROM user_platform_roles")
        self.conn.execute("DELETE FROM users")
        self.conn.executemany(
            """
            INSERT INTO users
              (id, username, password_hash, role, linked_student_id, is_active)
            VALUES (?, ?, 'unused', ?, NULL, ?)
            """,
            [
                (1, "teacher1", "teacher", 1),
                (2, "student1", "student", 1),
                (3, "disabled_teacher", "teacher", 0),
                (4, "operator", "teacher", 1),
            ],
        )
        self.conn.commit()
        dash.app.config.update(TESTING=True)

    def tearDown(self):
        self.conn.close()

    @classmethod
    def tearDownClass(cls):
        boot_conn = getattr(dash, "_conn_boot", None)
        if boot_conn is not None:
            try:
                boot_conn.close()
            except sqlite3.Error:
                pass
        gc.collect()
        TEST_DIR.cleanup()

    def session_for(self, user_id, role):
        return {
            "user_id": user_id,
            "username": f"session-{user_id}",
            "role": role,
        }

    def test_schema_preserves_classroom_role_and_adds_platform_roles(self):
        users_sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()[0]
        platform_sql = self.conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type='table' AND name='user_platform_roles'
            """
        ).fetchone()[0]
        self.assertIn("'teacher', 'student'", users_sql)
        self.assertNotIn("'owner'", users_sql)
        self.assertIn("'owner'", platform_sql)

    def test_current_user_and_classroom_predicates_use_database_role(self):
        with dash.app.test_request_context("/"):
            dash.session.update(self.session_for(1, "student"))
            user = dash.current_user()
            self.assertEqual(user["username"], "teacher1")
            self.assertTrue(dash.is_teacher())
            self.assertFalse(dash.is_student())

    def test_forged_teacher_session_does_not_pass_teacher_required(self):
        protected = dash.teacher_required(lambda: "allowed")
        with dash.app.test_request_context("/"):
            dash.session.update(self.session_for(2, "teacher"))
            response = protected()
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith("/student"))

    def test_inactive_database_user_is_not_authenticated(self):
        protected = dash.login_required(lambda: "allowed")
        with dash.app.test_request_context("/"):
            dash.session.update(self.session_for(3, "teacher"))
            response = protected()
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith("/login"))
            self.assertNotIn("user_id", dash.session)

    def test_owner_authority_is_independent_of_classroom_role(self):
        self.conn.executemany(
            """
            INSERT INTO user_platform_roles
              (user_id, platform_role, granted_at, granted_by, grant_note)
            VALUES (?, 'owner', 123456, 4, 'Test owner grant')
            """,
            [(1,), (2,)],
        )
        self.conn.commit()

        for user_id, classroom_role in ((1, "teacher"), (2, "student")):
            with self.subTest(classroom_role=classroom_role):
                with dash.app.test_request_context("/"):
                    dash.session.update(self.session_for(user_id, classroom_role))
                    self.assertTrue(dash.is_owner())
                    self.assertTrue(dash.has_platform_role("owner"))
                    self.assertEqual(
                        dash.owner_required(lambda: "allowed")(),
                        "allowed",
                    )

    def test_non_owner_is_rejected_by_owner_required(self):
        with dash.app.test_request_context("/"):
            dash.session.update(self.session_for(1, "teacher"))
            self.assertFalse(dash.is_owner())
            with self.assertRaises(Forbidden):
                dash.owner_required(lambda: "allowed")()

    def test_unknown_platform_role_is_never_granted(self):
        with dash.app.test_request_context("/"):
            dash.session.update(self.session_for(1, "teacher"))
        self.assertFalse(dash.has_platform_role("admin"))

    def test_sso_callback_contains_no_email_based_teacher_promotion(self):
        source = Path(dash.__file__).read_text(encoding="utf-8")
        self.assertNotIn("lrbrowning89@gmail.com", source)
        self.assertNotIn("email.lower() ==", source)

    def test_legacy_dashboards_are_not_runtime_references(self):
        runtime_files = [
            ROOT / "app_core" / "__main__.py",
            *sorted(
                path
                for path in (ROOT / "tests").glob("*.py")
                if path.name != Path(__file__).name
            ),
            *sorted((ROOT / "maintenance").glob("*.py")),
            *sorted((ROOT / "migrations").glob("*.py")),
        ]
        runtime_source = "\n".join(
            path.read_text(encoding="utf-8") for path in runtime_files
        )
        self.assertNotIn("dashboard_v5_old", runtime_source)
        self.assertNotIn("dashboard_v5 6.3.26", runtime_source)
        self.assertIn("from .dashboard_v5 import app", (ROOT / "app_core" / "__main__.py").read_text(encoding="utf-8"))

    def run_role_command(self, *args):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "maintenance" / "manage_platform_roles.py"),
                "--db",
                str(TEST_DB),
                *args,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_maintenance_command_is_dry_run_safe_and_idempotent(self):
        dry_run = self.run_role_command(
            "grant-owner",
            "--target-username",
            "teacher1",
            "--actor-username",
            "operator",
            "--reason",
            "Initial approved owner",
            "--dry-run",
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM user_platform_roles").fetchone()[0],
            0,
        )

        command_args = (
            "grant-owner",
            "--target-username",
            "teacher1",
            "--actor-username",
            "operator",
            "--reason",
            "Initial approved owner",
        )
        first_grant = self.run_role_command(*command_args)
        second_grant = self.run_role_command(*command_args)
        self.assertEqual(first_grant.returncode, 0, first_grant.stderr)
        self.assertEqual(second_grant.returncode, 0, second_grant.stderr)
        self.assertIn("already an owner", second_grant.stdout)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM user_platform_roles").fetchone()[0],
            1,
        )

        grant = self.conn.execute(
            "SELECT * FROM user_platform_roles WHERE revoked_at IS NULL"
        ).fetchone()
        self.assertEqual(grant["granted_by"], 4)
        self.assertEqual(grant["grant_note"], "Initial approved owner")

        listed = self.run_role_command("list-owners")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("teacher1", listed.stdout)
        self.assertIn("status=active", listed.stdout)
        self.assertIn("granted_by=operator", listed.stdout)
        self.assertIn("grant_reason=Initial approved owner", listed.stdout)

        protected_revoke = self.run_role_command(
            "revoke-owner",
            "--target-user-id",
            "1",
            "--actor-user-id",
            "4",
            "--reason",
            "Ownership transfer",
        )
        self.assertNotEqual(protected_revoke.returncode, 0)
        self.assertIn("last active owner", protected_revoke.stderr)

        revoke_args = (
            "revoke-owner",
            "--target-user-id",
            "1",
            "--actor-user-id",
            "4",
            "--reason",
            "Ownership transfer",
            "--allow-no-owner",
        )
        first_revoke = self.run_role_command(*revoke_args)
        second_revoke = self.run_role_command(*revoke_args)
        self.assertEqual(first_revoke.returncode, 0, first_revoke.stderr)
        self.assertEqual(second_revoke.returncode, 0, second_revoke.stderr)
        self.assertIn("not an active owner", second_revoke.stdout)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM user_platform_roles").fetchone()[0],
            1,
        )
        revoked_grant = self.conn.execute(
            "SELECT * FROM user_platform_roles"
        ).fetchone()
        self.assertIsNotNone(revoked_grant["revoked_at"])
        self.assertEqual(revoked_grant["revoked_by"], 4)
        self.assertEqual(revoked_grant["revoke_note"], "Ownership transfer")

        with dash.app.test_request_context("/"):
            dash.session.update(self.session_for(1, "teacher"))
            self.assertFalse(dash.is_owner())

        history = self.run_role_command("list-owners")
        active_only = self.run_role_command("list-owners", "--active-only")
        self.assertIn("status=revoked", history.stdout)
        self.assertIn("revoked_by=operator", history.stdout)
        self.assertIn("revoke_reason=Ownership transfer", history.stdout)
        self.assertNotIn("teacher1", active_only.stdout)

        new_grant = self.run_role_command(
            "grant-owner",
            "--target-user-id",
            "1",
            "--actor-user-id",
            "4",
            "--reason",
            "Owner restored",
        )
        self.assertEqual(new_grant.returncode, 0, new_grant.stderr)
        grants = self.conn.execute(
            """
            SELECT *
            FROM user_platform_roles
            WHERE user_id = 1
            ORDER BY grant_id
            """
        ).fetchall()
        self.assertEqual(len(grants), 2)
        self.assertIsNotNone(grants[0]["revoked_at"])
        self.assertIsNone(grants[1]["revoked_at"])
        self.assertEqual(grants[1]["grant_note"], "Owner restored")

    def test_maintenance_command_never_creates_users(self):
        before = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        result = self.run_role_command(
            "grant-owner",
            "--target-username",
            "missing",
            "--actor-username",
            "operator",
            "--reason",
            "Should fail",
        )
        after = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to create a user", result.stderr)
        self.assertEqual(before, after)

    def test_maintenance_command_rejects_inactive_user(self):
        result = self.run_role_command(
            "grant-owner",
            "--target-username",
            "disabled_teacher",
            "--actor-username",
            "operator",
            "--reason",
            "Should fail",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inactive", result.stderr)

    def test_migration_upgrades_phase_one_table_and_is_idempotent(self):
        migration_db = Path(TEST_DIR.name) / f"migration_{uuid.uuid4().hex}.db"
        with sqlite3.connect(migration_db) as conn:
            conn.execute(
                """
                CREATE TABLE users (
                  id INTEGER PRIMARY KEY,
                  username TEXT NOT NULL,
                  role TEXT NOT NULL,
                  is_active INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE user_platform_roles (
                  user_id INTEGER NOT NULL,
                  platform_role TEXT NOT NULL,
                  granted_at INTEGER NOT NULL,
                  PRIMARY KEY (user_id, platform_role)
                )
                """
            )
            conn.execute(
                "INSERT INTO users VALUES (1, 'teacher1', 'teacher', 1)"
            )
            conn.execute(
                "INSERT INTO user_platform_roles VALUES (1, 'owner', 123456)"
            )
            conn.commit()

        from migrations.add_user_platform_roles import run

        run(migration_db)
        run(migration_db)
        with sqlite3.connect(migration_db) as conn:
            conn.row_factory = sqlite3.Row
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(user_platform_roles)")
            }
            grants = conn.execute("SELECT * FROM user_platform_roles").fetchall()
        self.assertIn("grant_id", columns)
        self.assertIn("revoked_at", columns)
        self.assertEqual(len(grants), 1)
        self.assertIsNone(grants[0]["revoked_at"])
        self.assertEqual(
            grants[0]["grant_note"],
            "Migrated from Owner Role Phase 1",
        )


if __name__ == "__main__":
    unittest.main()
