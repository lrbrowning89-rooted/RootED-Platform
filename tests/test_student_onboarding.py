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
TEST_DB = Path(TEST_DIR.name) / f"test_student_onboarding_{uuid.uuid4().hex}.db"
os.environ["NGSS_DB"] = str(TEST_DB)
os.environ["SECRET_KEY"] = "test-secret"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

dash = importlib.import_module("app_core.dashboard_v5")


class StudentOnboardingTests(unittest.TestCase):
    def setUp(self):
        dash.DB = str(TEST_DB)
        dash.ae.DB_PATH = str(TEST_DB)
        self.conn = sqlite3.connect(TEST_DB)
        self.conn.row_factory = sqlite3.Row
        dash.ensure_schema(self.conn)
        for table in [
            "class_enrollments",
            "class_sections",
            "user_instructional_authorizations",
            "user_platform_roles",
            "user_auth_identities",
            "users",
            "students",
        ]:
            self.conn.execute(f"DELETE FROM {table}")
        self.seed_accounts_and_classes()
        dash.app.config.update(TESTING=True)
        self.client = dash.app.test_client()

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

    def seed_accounts_and_classes(self):
        self.conn.executemany(
            """
            INSERT INTO users
              (id, username, password_hash, role, account_role,
               linked_student_id, is_active)
            VALUES (?, ?, 'unused', ?, ?, ?, 1)
            """,
            [
                (1, "teacher", "teacher", "teacher", None),
                (2, "owner", "student", "pending", None),
                (3, "google-new", "student", "pending", None),
                (4, "microsoft-new", "student", "pending", None),
                (5, "existing-student", "student", "student", "ST5"),
            ],
        )
        self.conn.execute(
            """
            INSERT INTO students
              (student_id, first_name, last_name, grade, class_period)
            VALUES ('ST5', 'Existing', 'Student', 6, '2')
            """
        )
        self.conn.executemany(
            """
            INSERT INTO user_auth_identities
              (user_id, provider, provider_subject, verified_email,
               display_name, created_at, last_login_at)
            VALUES (?, ?, ?, ?, ?, 123456, 123456)
            """,
            [
                (3, "google", "google-subject", "google@example.org", "Google Student"),
                (
                    4,
                    "microsoft",
                    "tenant:microsoft-subject",
                    None,
                    "Microsoft Student",
                ),
            ],
        )
        self.conn.execute(
            """
            INSERT INTO user_instructional_authorizations
              (user_id, instructional_role, granted_at, grant_note)
            VALUES (1, 'teacher', 123456, 'Test teacher')
            """
        )
        self.conn.execute(
            """
            INSERT INTO user_platform_roles
              (user_id, platform_role, granted_at, grant_note)
            VALUES (2, 'owner', 123456, 'Test owner')
            """
        )
        self.conn.executemany(
            """
            INSERT INTO class_sections
              (class_id, teacher_user_id, name, class_period, join_code,
               is_active, created_at, updated_at)
            VALUES (?, 1, ?, ?, ?, ?, 123456, 123456)
            """,
            [
                ("C1", "Life Science", "1", "ABC234", 1),
                ("C2", "Earth Science", "2", "JKL567", 1),
                ("C3", "Archived Science", "3", "MNP789", 0),
            ],
        )
        self.conn.commit()

    def login(self, user_id):
        user = self.conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
        with self.client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["username"] = user["username"]
            sess["role"] = user["account_role"] or user["role"]

    def csrf_token(self):
        self.client.get("/restricted")
        with self.client.session_transaction() as sess:
            return sess["student_onboarding_csrf_token"]

    def submit_code(self, user_id, code, *, follow_redirects=False):
        self.login(user_id)
        token = self.csrf_token()
        return self.client.post(
            "/restricted",
            data={"csrf_token": token, "join_code": code},
            follow_redirects=follow_redirects,
        )

    def assert_joined_once(self, user_id, provider, class_id="C1"):
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            5,
        )
        user = self.conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
        self.assertEqual(user["account_role"], "student")
        self.assertIsNotNone(user["linked_student_id"])
        membership = self.conn.execute(
            """
            SELECT * FROM class_enrollments
            WHERE class_id=? AND student_id=?
            """,
            (class_id, user["linked_student_id"]),
        ).fetchone()
        self.assertIsNotNone(membership)
        self.assertEqual(membership["is_active"], 1)
        self.assertEqual(membership["enrolled_by"], "self")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM class_enrollments WHERE student_id=?",
                (user["linked_student_id"],),
            ).fetchone()[0],
            1,
        )
        identity = self.conn.execute(
            """
            SELECT provider, provider_subject
            FROM user_auth_identities WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()
        self.assertEqual(identity["provider"], provider)
        self.assertTrue(identity["provider_subject"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_auth_identities WHERE user_id=?",
                (user_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_platform_roles WHERE user_id=?",
                (user_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM user_instructional_authorizations
                WHERE user_id=?
                """,
                (user_id,),
            ).fetchone()[0],
            0,
        )

    def test_google_authenticated_account_joins_and_redirects_to_student(self):
        response = self.submit_code(3, "ABC234")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/student"))
        self.assert_joined_once(3, "google")

    def test_microsoft_authenticated_account_joins_and_redirects_to_student(self):
        response = self.submit_code(4, "ABC234")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/student"))
        self.assert_joined_once(4, "microsoft")

    def test_code_normalization_handles_whitespace_and_capitalization(self):
        response = self.submit_code(3, "  ab c234 \n")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/student"))
        self.assert_joined_once(3, "google")

    def test_blank_code_has_specific_message(self):
        response = self.submit_code(3, "   ", follow_redirects=True)
        self.assertIn(b"Enter a class code to continue.", response.data)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM students").fetchone()[0], 1)

    def test_malformed_and_nonexistent_codes_are_rejected(self):
        malformed = self.submit_code(3, "bad!", follow_redirects=True)
        self.assertIn(b"Enter a valid class code.", malformed.data)
        missing = self.submit_code(3, "QRS234", follow_redirects=True)
        self.assertIn(
            "We couldn’t find an active class with that code.",
            missing.get_data(as_text=True),
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM class_enrollments").fetchone()[0], 0)

    def test_inactive_class_is_rejected(self):
        response = self.submit_code(3, "MNP789", follow_redirects=True)
        self.assertIn(
            "That class is no longer accepting students.",
            response.get_data(as_text=True),
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM class_enrollments").fetchone()[0], 0)

    def test_regenerated_code_invalidates_old_code(self):
        self.conn.execute(
            "UPDATE class_sections SET join_code='QRS567' WHERE class_id='C1'"
        )
        self.conn.commit()
        old_code = self.submit_code(3, "ABC234", follow_redirects=True)
        self.assertIn(
            "We couldn’t find an active class with that code.",
            old_code.get_data(as_text=True),
        )
        current_code = self.submit_code(3, "QRS567")
        self.assertTrue(current_code.location.endswith("/student"))

    def test_duplicate_submission_creates_no_duplicate_membership(self):
        first = self.submit_code(3, "ABC234")
        self.assertTrue(first.location.endswith("/student"))
        second = self.submit_code(3, "ABC234", follow_redirects=True)
        self.assertIn(
            "You’re already connected to this class.",
            second.get_data(as_text=True),
        )
        self.assert_joined_once(3, "google")

    def test_existing_membership_and_other_active_membership_follow_single_class_rule(self):
        self.conn.execute(
            """
            INSERT INTO class_enrollments
              (class_id, student_id, enrolled_at, enrolled_by, is_active)
            VALUES ('C1', 'ST5', 123456, 'self', 1)
            """
        )
        self.conn.commit()
        same = self.submit_code(5, "ABC234", follow_redirects=True)
        self.assertIn(
            "You’re already connected to this class.",
            same.get_data(as_text=True),
        )

        self.conn.execute(
            "UPDATE class_enrollments SET class_id='C2' WHERE student_id='ST5'"
        )
        self.conn.commit()
        other = self.submit_code(5, "ABC234", follow_redirects=True)
        self.assertIn(
            "already connected to another active class",
            other.get_data(as_text=True),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT class_id FROM class_enrollments WHERE student_id='ST5'"
            ).fetchone()["class_id"],
            "C2",
        )

    def test_transaction_rolls_back_all_records_on_failure(self):
        with patch.object(
            dash,
            "_activate_student_account_for_membership",
            side_effect=sqlite3.OperationalError("simulated failure"),
        ):
            response = self.submit_code(3, "ABC234", follow_redirects=True)
        self.assertIn(
            "We couldn’t connect you to that class. Please try again.",
            response.get_data(as_text=True),
        )
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM students WHERE student_id='USR-3'").fetchone()
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM class_enrollments").fetchone()[0], 0)
        user = self.conn.execute("SELECT * FROM users WHERE id=3").fetchone()
        self.assertEqual(user["account_role"], "pending")
        self.assertIsNone(user["linked_student_id"])

    def test_teacher_owner_and_anonymous_cannot_self_enroll(self):
        for user_id in (1, 2):
            with self.subTest(user_id=user_id):
                self.login(user_id)
                token = self.csrf_token()
                response = self.client.post(
                    "/restricted",
                    data={"csrf_token": token, "join_code": "ABC234"},
                )
                self.assertEqual(response.status_code, 403)
        with self.client.session_transaction() as sess:
            sess.clear()
        anonymous = self.client.post("/restricted", data={"join_code": "ABC234"})
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/login", anonymous.location)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM class_enrollments").fetchone()[0], 0)

    def test_post_requires_csrf(self):
        self.login(3)
        response = self.client.post("/restricted", data={"join_code": "ABC234"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM class_enrollments").fetchone()[0], 0)

    def test_onboarding_form_is_enabled_and_old_placeholder_is_removed(self):
        self.login(3)
        response = self.client.get("/restricted")
        page = response.get_data(as_text=True)
        self.assertIn("Your account is ready.", page)
        self.assertIn('placeholder="Enter your class code"', page)
        self.assertIn(">Join class</button>", page)
        self.assertIn("Don’t have a class code?", page)
        self.assertNotIn("Class-code entry coming soon", page)
        self.assertNotIn('id="code" disabled', page)


if __name__ == "__main__":
    unittest.main()
