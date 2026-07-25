import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "maintenance" / "configure_sso_authorization.py"


class SsoAuthorizationAdministrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = Path(self.temporary.name) / "authorization.db"
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE users (
                  id INTEGER PRIMARY KEY,
                  username TEXT NOT NULL,
                  role TEXT NOT NULL,
                  account_role TEXT,
                  is_active INTEGER NOT NULL,
                  linked_student_id TEXT
                );
                CREATE TABLE user_auth_identities (
                  identity_id INTEGER PRIMARY KEY,
                  user_id INTEGER NOT NULL,
                  provider TEXT NOT NULL,
                  provider_subject TEXT NOT NULL,
                  verified_email TEXT,
                  display_name TEXT,
                  revoked_at INTEGER
                );
                CREATE TABLE user_platform_roles (
                  grant_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  platform_role TEXT NOT NULL,
                  granted_at INTEGER NOT NULL,
                  granted_by INTEGER,
                  revoked_at INTEGER,
                  grant_note TEXT
                );
                CREATE UNIQUE INDEX uq_active_owner
                  ON user_platform_roles(user_id, platform_role)
                  WHERE revoked_at IS NULL;
                CREATE TABLE class_sections (
                  class_id TEXT PRIMARY KEY,
                  teacher_user_id INTEGER,
                  name TEXT NOT NULL,
                  class_period TEXT,
                  join_code TEXT UNIQUE NOT NULL,
                  is_active INTEGER NOT NULL,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                );
                CREATE TABLE class_enrollments (
                  class_id TEXT NOT NULL,
                  student_id TEXT NOT NULL,
                  enrolled_at INTEGER NOT NULL,
                  enrolled_by TEXT NOT NULL
                );
                INSERT INTO users VALUES
                  (1, 'owner-target', 'student', 'pending', 1, NULL),
                  (2, 'teacher-target', 'student', 'pending', 1, NULL),
                  (3, 'pending-user', 'student', 'pending', 1, NULL);
                INSERT INTO user_auth_identities VALUES
                  (1, 1, 'microsoft', 'tenant:owner', 'owner@example.org',
                   'Owner Target', NULL),
                  (2, 2, 'google', 'teacher', 'teacher@example.org',
                   'Teacher Target', NULL),
                  (3, 3, 'google', 'pending', 'pending@example.org',
                   'Pending Target', NULL);
                """
            )

    def tearDown(self):
        self.temporary.cleanup()

    def run_command(self, *arguments):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db",
                str(self.database),
                *arguments,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_owner_grant_is_identity_based_backed_up_and_idempotent(self):
        arguments = (
            "grant-owner",
            "--email",
            "owner@example.org",
            "--provider",
            "microsoft",
            "--actor-email",
            "owner@example.org",
            "--actor-provider",
            "microsoft",
            "--reason",
            "Approved initial owner",
        )
        first = self.run_command(*arguments)
        second = self.run_command(*arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already-owner", second.stdout)
        self.assertIn("expected_post_login_destination: /owner", second.stdout)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM user_platform_roles"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT account_role FROM users WHERE id=3"
                ).fetchone()[0],
                "pending",
            )
        self.assertEqual(
            len(list((self.database.parent / "backups").glob("*.db"))),
            1,
        )

    def test_teacher_configuration_creates_one_empty_active_class(self):
        arguments = (
            "configure-teacher",
            "--email",
            "teacher@example.org",
            "--provider",
            "google",
            "--class-name",
            "Temporary classroom setup",
        )
        first = self.run_command(*arguments)
        second = self.run_command(*arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already-authorized teacher", second.stdout)
        self.assertIn("expected_post_login_destination: /dashboard", second.stdout)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT account_role FROM users WHERE id=2"
                ).fetchone()[0],
                "teacher",
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM class_sections
                    WHERE teacher_user_id=2 AND is_active=1
                    """
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM class_enrollments"
                ).fetchone()[0],
                0,
            )

    def test_missing_identity_is_rejected_without_creating_user(self):
        result = self.run_command(
            "audit",
            "--email",
            "missing@example.org",
            "--provider",
            "google",
        )
        self.assertNotEqual(result.returncode, 0)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                3,
            )

    def test_exact_microsoft_subject_grants_owner_and_masks_confirmation(self):
        arguments = (
            "grant-owner",
            "--provider-subject",
            "tenant:owner",
            "--provider",
            "microsoft",
            "--actor-provider-subject",
            "tenant:owner",
            "--actor-provider",
            "microsoft",
            "--reason",
            "Immutable subject owner grant",
        )
        first = self.run_command(*arguments)
        second = self.run_command(*arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("matched_display_name: Owner Target", first.stdout)
        self.assertIn("matched_provider_subject: tena...wner", first.stdout)
        self.assertNotIn("matched_provider_subject: tenant:owner", first.stdout)
        self.assertIn("expected_post_login_destination: /owner", second.stdout)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM user_platform_roles"
                ).fetchone()[0],
                1,
            )

    def test_provider_subject_requires_microsoft(self):
        result = self.run_command(
            "audit",
            "--provider-subject",
            "tenant:owner",
            "--provider",
            "google",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "--provider-subject requires --provider microsoft",
            result.stderr,
        )

    def test_missing_and_revoked_subjects_are_rejected(self):
        missing = self.run_command(
            "audit",
            "--provider-subject",
            "tenant:missing",
            "--provider",
            "microsoft",
        )
        self.assertNotEqual(missing.returncode, 0)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE user_auth_identities SET revoked_at=123 WHERE identity_id=1"
            )
            connection.commit()
        revoked = self.run_command(
            "audit",
            "--provider-subject",
            "tenant:owner",
            "--provider",
            "microsoft",
        )
        self.assertNotEqual(revoked.returncode, 0)
        self.assertIn("No active Microsoft SSO identity", revoked.stderr)

    def test_duplicate_subject_is_refused_as_ambiguous(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                INSERT INTO user_auth_identities
                  (identity_id, user_id, provider, provider_subject,
                   verified_email, display_name, revoked_at)
                VALUES (4, 2, 'microsoft', 'tenant:owner',
                        'mutable@example.org', 'Duplicate', NULL)
                """
            )
            connection.commit()
        result = self.run_command(
            "audit",
            "--provider-subject",
            "tenant:owner",
            "--provider",
            "microsoft",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing an ambiguous change", result.stderr)

    def test_subject_lookup_never_falls_back_to_mutable_email(self):
        result = self.run_command(
            "audit",
            "--provider-subject",
            "owner@example.org",
            "--provider",
            "microsoft",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No active Microsoft SSO identity", result.stderr)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM user_platform_roles"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
