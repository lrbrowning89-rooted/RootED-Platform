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
from unittest.mock import patch

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
        self.conn.execute("DELETE FROM access_authorization_audit_log")
        self.conn.execute("DELETE FROM user_instructional_authorizations")
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
        self.conn.executemany(
            """
            INSERT INTO user_instructional_authorizations
              (user_id, instructional_role, granted_at, grant_note)
            VALUES (?, 'teacher', 123456, 'Test teacher authorization')
            """,
            [(1,), (3,), (4,)],
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

    def test_microsoft_common_validates_concrete_tenant_issuer(self):
        tenant_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"

        class FakeClient:
            def load_server_metadata(self):
                return {
                    "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0"
                }

            def fetch_jwk_set(self):
                return {
                    "keys": [
                        {
                            "kid": "microsoft-key",
                            "issuer": (
                                "https://login.microsoftonline.com/"
                                "{tenantid}/v2.0"
                            ),
                        }
                    ]
                }

        class Claims(dict):
            header = {"kid": "microsoft-key"}

        claims = Claims(tid=tenant_id)
        validator = dash.microsoft_multitenant_claims_options(FakeClient())[
            "iss"
        ]["validate"]
        self.assertTrue(validator(claims, issuer))
        self.assertFalse(
            validator(
                claims,
                "https://login.microsoftonline.com/"
                "ffffffff-1111-2222-3333-444444444444/v2.0",
            )
        )

    def test_microsoft_common_rejects_invalid_tid_and_wrong_key_issuer(self):
        class Claims(dict):
            header = {"kid": "microsoft-key"}

        class FakeClient:
            def load_server_metadata(self):
                return {
                    "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0"
                }

            def fetch_jwk_set(self):
                return {
                    "keys": [
                        {
                            "kid": "microsoft-key",
                            "issuer": (
                                "https://login.microsoftonline.com/"
                                "ffffffff-1111-2222-3333-444444444444/v2.0"
                            ),
                        }
                    ]
                }

        validator = dash.microsoft_multitenant_claims_options(FakeClient())[
            "iss"
        ]["validate"]
        self.assertFalse(
            validator(
                Claims(tid="not-a-guid"),
                "https://login.microsoftonline.com/not-a-guid/v2.0",
            )
        )
        tenant_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.assertFalse(
            validator(
                Claims(tid=tenant_id),
                f"https://login.microsoftonline.com/{tenant_id}/v2.0",
            )
        )

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
        identity_sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_auth_identities'"
        ).fetchone()[0]
        self.assertIn("UNIQUE(provider, provider_subject)", identity_sql)

    def test_login_offers_google_and_microsoft(self):
        old_google = dash.ENABLE_GOOGLE_AUTH
        old_microsoft = dash.ENABLE_MICROSOFT_AUTH
        try:
            dash.ENABLE_GOOGLE_AUTH = True
            dash.ENABLE_MICROSOFT_AUTH = True
            response = dash.app.test_client().get("/login")
            self.assertIn(b"Continue with Google", response.data)
            self.assertIn(b"Continue with Microsoft", response.data)
            self.assertIn(b"sso-icon-google", response.data)
            self.assertIn(b"sso-icon-microsoft", response.data)
            self.assertEqual(response.data.count(b'aria-hidden="true"'), 2)
            self.assertEqual(response.data.count(b'focusable="false"'), 2)
            self.assertNotIn(b"http://www.google.com", response.data)
            self.assertNotIn(b"https://www.microsoft.com", response.data)
        finally:
            dash.ENABLE_GOOGLE_AUTH = old_google
            dash.ENABLE_MICROSOFT_AUTH = old_microsoft

    def test_restricted_onboarding_uses_student_enrollment_wording(self):
        client = dash.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "teacher1"
            sess["role"] = "teacher"
        response = client.get("/restricted")
        self.assertEqual(response.status_code, 200)
        page = response.data.decode("utf-8")
        self.assertIn(
            "Your account is ready. Enter the class code provided by your "
            "teacher to begin learning.",
            page,
        )
        self.assertNotIn("Class-code entry coming soon", page)

    def test_post_login_redirect_rejects_external_destinations(self):
        self.assertEqual(dash.safe_local_redirect("/student?from=login"), "/student?from=login")
        self.assertIsNone(dash.safe_local_redirect("https://evil.example/student"))
        self.assertIsNone(dash.safe_local_redirect("//evil.example/student"))

    def test_sso_accounts_are_restricted_until_membership_exists(self):
        user = dash.resolve_sso_user(
            self.conn, "microsoft", "subject-1", "new@example.org",
            email_verified=True, display_name="New User", allow_create=True,
        )
        self.assertEqual(user["role"], "student")  # deprecated compatibility value
        self.assertEqual(user["account_role"], "pending")
        self.assertFalse(dash.is_student(user))
        self.assertIsNone(user["linked_student_id"])
        with dash.app.test_request_context("/"):
            dash.session["user_id"] = user["id"]
            self.assertTrue(dash.get_post_login_destination().endswith("/restricted"))

    def test_google_and_microsoft_callbacks_create_pending_accounts(self):
        tenant_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        provider_claims = {
            "google": {
                "sub": "google-callback-subject",
                "email": "google-callback@example.org",
                "email_verified": True,
            },
            "microsoft": {
                "sub": "microsoft-callback-subject",
                "tid": tenant_id,
                "preferred_username": "microsoft-callback@example.org",
                "xms_edov": True,
            },
        }

        class FakeClient:
            def __init__(self, claims):
                self.claims = claims

            def authorize_access_token(self, **_kwargs):
                return {"userinfo": self.claims}

        old_allow_create = dash.ALLOW_SSO_AUTO_CREATE
        old_google = dash.ENABLE_GOOGLE_AUTH
        old_microsoft = dash.ENABLE_MICROSOFT_AUTH
        dash.ALLOW_SSO_AUTO_CREATE = True
        dash.ENABLE_GOOGLE_AUTH = True
        dash.ENABLE_MICROSOFT_AUTH = True
        try:
            for provider, claims in provider_claims.items():
                with self.subTest(provider=provider), patch.object(
                    dash.oauth,
                    "create_client",
                    return_value=FakeClient(claims),
                ):
                    response = dash.app.test_client().get(
                        f"/auth/callback/{provider}"
                    )
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response.location.endswith("/restricted"))
                    subject = claims["sub"]
                    if provider == "microsoft":
                        subject = f"{tenant_id}:{subject}"
                    identity = self.conn.execute(
                        """
                        SELECT u.account_role, u.linked_student_id
                        FROM user_auth_identities i
                        JOIN users u ON u.id=i.user_id
                        WHERE i.provider=? AND i.provider_subject=?
                        """,
                        (provider, subject),
                    ).fetchone()
                    self.assertIsNotNone(identity)
                    self.assertEqual(identity["account_role"], "pending")
                    self.assertIsNone(identity["linked_student_id"])
        finally:
            dash.ALLOW_SSO_AUTO_CREATE = old_allow_create
            dash.ENABLE_GOOGLE_AUTH = old_google
            dash.ENABLE_MICROSOFT_AUTH = old_microsoft

    def test_pending_account_cannot_bypass_instructional_routes(self):
        user = dash.resolve_sso_user(
            self.conn, "google", "pending-direct", "pending@example.org",
            email_verified=True, allow_create=True,
        )
        client = dash.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user["id"]
            sess["username"] = user["username"]
            sess["role"] = "pending"
        for path in ("/student", "/dashboard", "/teacher/question_preview"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.endswith("/restricted"))
        question_post = client.post("/question_flags", data={"question_id": "Q1"})
        self.assertEqual(question_post.status_code, 302)
        self.assertTrue(question_post.location.endswith("/restricted"))
        teacher_home = client.get("/teacher")
        self.assertEqual(teacher_home.status_code, 302)
        self.assertTrue(teacher_home.location.endswith("/restricted"))
        for nonexistent_path in ("/questions", "/adaptive"):
            with self.subTest(path=nonexistent_path):
                self.assertEqual(client.get(nonexistent_path).status_code, 404)

    def test_verified_email_links_one_account_but_rejects_ambiguity(self):
        self.conn.execute(
            "UPDATE users SET sso_email='one@example.org' WHERE id=2"
        )
        self.conn.commit()
        linked = dash.resolve_sso_user(
            self.conn, "google", "g-one", "one@example.org",
            email_verified=True, allow_create=True,
        )
        self.assertEqual(linked["id"], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO user_auth_identities
                  (user_id, provider, provider_subject, created_at, last_login_at)
                VALUES (1, 'google', 'g-one', 1, 1)
                """
            )

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
            self.assertTrue(response.location.endswith("/restricted"))

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

    def test_auth_identity_migration_downgrade_is_guarded_and_reversible(self):
        from migrations.add_user_auth_identities import downgrade, migrate

        migration_db = Path(TEST_DIR.name) / f"auth_migration_{uuid.uuid4().hex}.db"
        with sqlite3.connect(migration_db) as conn:
            conn.execute(
                """
                CREATE TABLE users (
                  id INTEGER PRIMARY KEY, username TEXT, role TEXT,
                  sso_provider TEXT, sso_subject TEXT, sso_email TEXT,
                  last_login_ts INTEGER
                )
                """
            )
            conn.execute(
                """
                INSERT INTO users VALUES
                  (1, 'legacy', 'student', 'google', 'legacy-sub',
                   'legacy@example.org', 123)
                """
            )
            migrate(conn)
            identity = conn.execute(
                "SELECT provider, provider_subject FROM user_auth_identities"
            ).fetchone()
            self.assertEqual(identity, ("google", "legacy-sub"))
            downgrade(conn)
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            self.assertNotIn("user_auth_identities", tables)
            self.assertNotIn("account_role", columns)


if __name__ == "__main__":
    unittest.main()
