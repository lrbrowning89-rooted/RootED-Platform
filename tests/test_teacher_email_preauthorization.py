import gc
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
TEST_DB = Path(TEST_DIR.name) / f"teacher_email_{uuid.uuid4().hex}.db"
os.environ["NGSS_DB"] = str(TEST_DB)
os.environ["SECRET_KEY"] = "test-secret"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
dash = importlib.import_module("app_core.dashboard_v5")


class TeacherEmailPreauthorizationTests(unittest.TestCase):
    def setUp(self):
        dash.DB = str(TEST_DB)
        dash.ae.DB_PATH = str(TEST_DB)
        self.conn = sqlite3.connect(TEST_DB)
        self.conn.row_factory = sqlite3.Row
        dash.ensure_schema(self.conn)
        for table in (
            "teacher_email_authorizations", "user_instructional_authorizations",
            "user_auth_identities", "user_platform_roles", "users",
        ):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.execute(
            "INSERT INTO users(id,username,password_hash,role,account_role,is_active) VALUES (1,'owner','x','student','pending',1)"
        )
        self.conn.execute(
            "INSERT INTO user_platform_roles(user_id,platform_role,granted_at) VALUES (1,'owner',1)"
        )
        self.conn.commit()
        dash.app.config.update(TESTING=True)
        self.client = dash.app.test_client()

    def tearDown(self):
        self.conn.close()

    @classmethod
    def tearDownClass(cls):
        boot = getattr(dash, "_conn_boot", None)
        if boot:
            try:
                boot.close()
            except sqlite3.Error:
                pass
        gc.collect()
        TEST_DIR.cleanup()

    def owner_login(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["username"] = "owner"
        self.client.get("/owner/people-access")
        with self.client.session_transaction() as session:
            return session["owner_csrf_token"]

    def invite(self, email=" Teacher@Example.org "):
        token = self.owner_login()
        return self.client.post(
            "/owner/people-access/teacher-authorizations",
            data={"csrf_token": token, "email": email, "display_name": "Teacher"},
        )

    def test_owner_pre_authorizes_without_user_and_duplicate_is_reused(self):
        self.invite()
        self.invite("teacher@example.org")
        row = self.conn.execute("SELECT * FROM teacher_email_authorizations").fetchone()
        self.assertEqual(row["normalized_email"], "teacher@example.org")
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["user_id"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM teacher_email_authorizations").fetchone()[0], 1
        )

    def test_google_and_microsoft_claim_verified_email_transactionally(self):
        for provider in ("google", "microsoft"):
            with self.subTest(provider=provider):
                email = f"{provider}@example.org"
                self.invite(f"  {email.upper()} ")
                user = dash.resolve_sso_user(
                    self.conn, provider, f"{provider}-subject", email,
                    email_verified=True, display_name="New Teacher", allow_create=True,
                )
                self.assertIsNotNone(user)
                self.assertEqual(user["account_role"], "teacher")
                auth = self.conn.execute(
                    "SELECT * FROM teacher_email_authorizations WHERE normalized_email=?",
                    (email,),
                ).fetchone()
                self.assertEqual(auth["status"], "active")
                self.assertEqual(auth["user_id"], user["id"])
                self.assertTrue(dash.has_instructional_authorization("teacher", user))
                again = dash.resolve_sso_user(
                    self.conn, provider, f"{provider}-subject", email,
                    email_verified=True, allow_create=True,
                )
                self.assertEqual(again["id"], user["id"])
                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM user_instructional_authorizations WHERE user_id=?",
                        (user["id"],),
                    ).fetchone()[0], 1
                )

    def test_unverified_revoked_and_deactivated_cannot_claim(self):
        self.invite("blocked@example.org")
        unverified = dash.resolve_sso_user(
            self.conn, "google", "unverified", "blocked@example.org",
            email_verified=False, allow_create=True,
        )
        self.assertEqual(unverified["account_role"], "pending")
        self.assertEqual(
            self.conn.execute("SELECT status FROM teacher_email_authorizations").fetchone()[0],
            "pending",
        )
        token = self.owner_login()
        item_id = self.conn.execute(
            "SELECT email_authorization_id FROM teacher_email_authorizations"
        ).fetchone()[0]
        self.client.post(
            f"/owner/people-access/teacher-authorizations/{item_id}/deactivate",
            data={"csrf_token": token},
        )
        user = dash.resolve_sso_user(
            self.conn, "microsoft", "deactivated", " BLOCKED@example.org ",
            email_verified=True, allow_create=True,
        )
        self.assertEqual(user["account_role"], "pending")

    def test_existing_student_history_is_not_silently_converted(self):
        self.conn.execute(
            "INSERT INTO users(id,username,password_hash,role,account_role,linked_student_id,is_active,sso_email) VALUES (2,'learner','x','student','student','S-2',1,'same@example.org')"
        )
        self.conn.commit()
        self.invite("same@example.org")
        self.assertIsNone(dash.resolve_sso_user(
            self.conn, "google", "student-conflict", "same@example.org",
            email_verified=True, allow_create=True,
        ))
        self.assertEqual(
            self.conn.execute("SELECT account_role FROM users WHERE id=2").fetchone()[0],
            "student",
        )

    def test_non_owner_cannot_manage_and_deauthorized_teacher_sees_message(self):
        self.invite("teacher@example.org")
        user = dash.resolve_sso_user(
            self.conn, "google", "teacher", "teacher@example.org",
            email_verified=True, allow_create=True,
        )
        item_id = self.conn.execute(
            "SELECT email_authorization_id FROM teacher_email_authorizations"
        ).fetchone()[0]
        with self.client.session_transaction() as session:
            session["user_id"] = user["id"]
        self.assertEqual(
            self.client.post(
                "/owner/people-access/teacher-authorizations",
                data={"csrf_token": "bad", "email": "x@example.org"},
            ).status_code, 403
        )
        token = self.owner_login()
        self.client.post(
            f"/owner/people-access/teacher-authorizations/{item_id}/deactivate",
            data={"csrf_token": token},
        )
        with self.client.session_transaction() as session:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
        page = self.client.get("/restricted").get_data(as_text=True)
        self.assertIn("Teacher access unavailable", page)
        self.assertNotIn("Join class</button>", page)

    def microsoft_callback(self, claims):
        class FakeClient:
            def authorize_access_token(self, **_kwargs):
                return {"userinfo": claims}

        old_enabled = dash.ENABLE_MICROSOFT_AUTH
        old_create = dash.ALLOW_SSO_AUTO_CREATE
        dash.ENABLE_MICROSOFT_AUTH = True
        dash.ALLOW_SSO_AUTO_CREATE = True
        try:
            with patch.object(dash.oauth, "create_client", return_value=FakeClient()):
                return self.client.get("/auth/callback/microsoft")
        finally:
            dash.ENABLE_MICROSOFT_AUTH = old_enabled
            dash.ALLOW_SSO_AUTO_CREATE = old_create

    def test_microsoft_callback_claims_teacher_from_email_or_preferred_username(self):
        tenant = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        for claim_name in ("email", "preferred_username"):
            with self.subTest(claim_name=claim_name):
                email = f"{claim_name}@example.org"
                self.invite(f"  {email.upper()} ")
                subject = f"opaque-{claim_name}-subject"
                response = self.microsoft_callback({
                    "sub": subject, "tid": tenant, claim_name: email,
                    "name": "Human Teacher",
                })
                self.assertTrue(response.location.endswith("/teacher"))
                identity = self.conn.execute(
                    """
                    SELECT i.*,u.account_role FROM user_auth_identities i
                    JOIN users u ON u.id=i.user_id
                    WHERE i.provider='microsoft' AND i.provider_subject=?
                    """,
                    (f"{tenant}:{subject}",),
                ).fetchone()
                self.assertEqual(identity["verified_email"], email)
                self.assertEqual(identity["display_name"], "Human Teacher")
                self.assertEqual(identity["account_role"], "teacher")
                self.assertNotEqual(identity["user_id"], subject)
                with self.client.session_transaction() as session:
                    flashes = " ".join(message for _, message in session.get("_flashes", []))
                self.assertNotIn(subject, flashes)

    def test_microsoft_no_trusted_email_stops_without_partial_records(self):
        self.invite("teacher@example.org")
        before_users = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        subject = "opaque-no-email"
        response = self.microsoft_callback({
            "sub": subject,
            "tid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "name": "No Email",
        })
        self.assertTrue(response.location.endswith("/not-authorized"))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            before_users,
        )
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM user_auth_identities WHERE provider_subject LIKE ?",
            (f"%{subject}",),
        ).fetchone())
        self.assertEqual(
            self.conn.execute("SELECT status FROM teacher_email_authorizations").fetchone()[0],
            "pending",
        )

    def test_microsoft_repeat_callback_is_idempotent_and_flash_is_generic(self):
        tenant = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        subject = "opaque-repeat-subject"
        email = "repeat@example.org"
        self.invite(email)
        claims = {
            "sub": subject, "tid": tenant, "preferred_username": email,
            "name": "Repeat Teacher",
        }
        first = self.microsoft_callback(claims)
        second = self.microsoft_callback(claims)
        self.assertTrue(first.location.endswith("/teacher"))
        self.assertTrue(second.location.endswith("/teacher"))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_auth_identities WHERE provider='microsoft' AND provider_subject=?",
                (f"{tenant}:{subject}",),
            ).fetchone()[0], 1
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_instructional_authorizations WHERE revoked_at IS NULL"
            ).fetchone()[0], 1
        )
        with self.client.session_transaction() as session:
            flashes = [message for _, message in session.get("_flashes", [])]
        self.assertIn("Welcome to RootED.", flashes)
        self.assertTrue(all(subject not in message for message in flashes))

    def test_ordinary_microsoft_callback_still_reaches_join_class(self):
        response = self.microsoft_callback({
            "sub": "ordinary-subject",
            "tid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "preferred_username": "ordinary@example.org",
            "name": "Ordinary User",
        })
        self.assertTrue(response.location.endswith("/restricted"))

    def test_subject_is_never_an_email_match_or_display_fallback(self):
        subject = "teacher@example.org"
        self.invite(subject)
        response = self.microsoft_callback({
            "sub": subject,
            "tid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "name": "Subject Only",
        })
        self.assertTrue(response.location.endswith("/not-authorized"))
        invitation = self.conn.execute(
            "SELECT * FROM teacher_email_authorizations"
        ).fetchone()
        self.assertEqual(invitation["status"], "pending")
        self.assertIsNone(invitation["user_id"])


if __name__ == "__main__":
    unittest.main()
