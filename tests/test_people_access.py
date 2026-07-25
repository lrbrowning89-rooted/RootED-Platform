import gc
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
TEST_DB = Path(TEST_DIR.name) / f"test_people_access_{uuid.uuid4().hex}.db"
os.environ["NGSS_DB"] = str(TEST_DB)
os.environ["SECRET_KEY"] = "test-secret"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

dash = importlib.import_module("app_core.dashboard_v5")


class PeopleAccessTests(unittest.TestCase):
    def setUp(self):
        dash.DB = str(TEST_DB)
        dash.ae.DB_PATH = str(TEST_DB)
        self.conn = sqlite3.connect(TEST_DB)
        self.conn.row_factory = sqlite3.Row
        dash.ensure_schema(self.conn)
        for table in [
            "access_authorization_audit_log",
            "user_instructional_authorizations",
            "user_auth_identities",
            "user_platform_roles",
            "class_enrollments",
            "class_sections",
            "users",
            "students",
        ]:
            self.conn.execute(f"DELETE FROM {table}")
        self.seed_data()
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

    def seed_data(self):
        now = 123456
        self.conn.execute(
            """
            INSERT INTO students
              (student_id, first_name, last_name, grade, class_period)
            VALUES ('STUDENT-1', 'Private', 'Learner', 6, '1')
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users
              (id, username, password_hash, role, account_role,
               linked_student_id, is_active, last_login_ts)
            VALUES (?, ?, 'unused', ?, ?, ?, 1, ?)
            """,
            [
                (1, "owner", "student", "pending", None, now),
                (2, "pending-teacher", "student", "pending", None, None),
                (3, "teacher", "teacher", "teacher", None, now),
                (4, "student", "student", "student", "STUDENT-1", now),
            ],
        )
        self.conn.execute(
            """
            INSERT INTO user_platform_roles
              (user_id, platform_role, granted_at, granted_by, grant_note)
            VALUES (1, 'owner', ?, 1, 'Test owner')
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO user_instructional_authorizations
              (user_id, instructional_role, granted_at, granted_by, grant_note)
            VALUES (3, 'teacher', ?, 1, 'Existing teacher')
            """,
            (now,),
        )
        self.conn.executemany(
            """
            INSERT INTO user_auth_identities
              (user_id, provider, provider_subject, verified_email,
               display_name, created_at, last_login_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "google", "owner-sub", "owner@example.org", "Owner Name", now, now),
                (2, "microsoft", "pending-sub", "pending@example.org", "Pending Name", now, now),
                (3, "google", "teacher-sub", "teacher@example.org", "Teacher Name", now, now),
                (4, "google", "student-sub", "student@example.org", "Student Name", now, now),
            ],
        )
        self.conn.commit()

    def login_as(self, user_id):
        with self.client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["username"] = f"user-{user_id}"

    def csrf_for_pending_teacher(self):
        response = self.client.get(
            "/owner/people-access/2/authorize-teacher"
        )
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as sess:
            return sess["owner_csrf_token"]

    def identity_snapshot(self):
        return [
            tuple(row)
            for row in self.conn.execute(
                "SELECT * FROM user_auth_identities ORDER BY identity_id"
            )
        ]

    def test_owner_can_view_people_access_with_admin_fields_only(self):
        self.login_as(1)
        response = self.client.get("/owner/people-access")
        page = response.data.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Pending Name", page)
        self.assertIn("pending@example.org", page)
        self.assertIn("Authentication provider", page)
        self.assertIn("Expected destination", page)
        for educational_field in [
            "Grades", "Scores", "Adaptive history", "Mastery",
            "Question responses", "Teacher notes", "Accommodations",
            "Progress history",
        ]:
            self.assertNotIn(educational_field, page)

    def test_teacher_student_and_anonymous_cannot_view_or_query_directory(self):
        for user_id in (3, 4):
            with self.subTest(user_id=user_id):
                self.login_as(user_id)
                response = self.client.get("/owner/people-access")
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(b"Pending Name", response.data)
        with self.client.session_transaction() as sess:
            sess.clear()
        response = self.client.get("/owner/people-access")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(b"Pending Name", response.data)

    def test_teacher_authorization_requires_owner_confirmation_and_csrf(self):
        self.login_as(1)
        confirmation = self.client.get(
            "/owner/people-access/2/authorize-teacher"
        )
        self.assertIn(b"Confirm Teacher Authorization", confirmation.data)
        missing_csrf = self.client.post(
            "/owner/people-access/2/authorize-teacher"
        )
        self.assertEqual(missing_csrf.status_code, 400)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_instructional_authorizations WHERE user_id=2"
            ).fetchone()[0],
            0,
        )

    def test_owner_authorizes_pending_account_idempotently_without_side_effects(self):
        self.login_as(1)
        csrf_token = self.csrf_for_pending_teacher()
        identities_before = self.identity_snapshot()
        users_before = tuple(
            self.conn.execute("SELECT * FROM users WHERE id=2").fetchone()
        )

        first = self.client.post(
            "/owner/people-access/2/authorize-teacher",
            data={"csrf_token": csrf_token},
        )
        second = self.client.post(
            "/owner/people-access/2/authorize-teacher",
            data={"csrf_token": csrf_token},
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        grants = self.conn.execute(
            "SELECT * FROM user_instructional_authorizations WHERE user_id=2"
        ).fetchall()
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["granted_by"], 1)
        self.assertEqual(users_before, tuple(
            self.conn.execute("SELECT * FROM users WHERE id=2").fetchone()
        ))
        self.assertEqual(identities_before, self.identity_snapshot())
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM class_sections").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM class_enrollments").fetchone()[0],
            0,
        )
        audits = self.conn.execute(
            """
            SELECT actor_user_id, target_user_id, action, outcome
            FROM access_authorization_audit_log
            WHERE target_user_id=2 ORDER BY audit_id
            """
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in audits],
            [
                (1, 2, "teacher_authorized", "granted"),
                (1, 2, "teacher_authorized", "already_granted"),
            ],
        )

    def test_authorized_teacher_workspace_and_owner_authority_both_remain_available(self):
        self.login_as(1)
        token = self.csrf_for_pending_teacher()
        self.client.post(
            "/owner/people-access/2/authorize-teacher",
            data={"csrf_token": token},
        )

        self.login_as(2)
        teacher = self.client.get("/teacher", follow_redirects=True)
        self.assertEqual(teacher.status_code, 200)
        self.assertIn(b"Create Class Code", teacher.data)

        # A single account can hold both grants without either replacing the other.
        self.conn.execute(
            """
            INSERT INTO user_platform_roles
              (user_id, platform_role, granted_at, granted_by, grant_note)
            VALUES (2, 'owner', 123457, 1, 'Dual authority test')
            """
        )
        self.conn.commit()
        self.login_as(2)
        self.assertEqual(self.client.get("/owner").status_code, 200)
        self.assertEqual(self.client.get("/teacher").status_code, 302)
        with dash.app.test_request_context("/"):
            dash.session["user_id"] = 2
            self.assertEqual(dash.get_post_login_destination(), "/owner")

    def test_non_owner_cannot_invoke_teacher_authorization(self):
        self.login_as(3)
        response = self.client.post(
            "/owner/people-access/2/authorize-teacher",
            data={"csrf_token": "anything"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_instructional_authorizations WHERE user_id=2"
            ).fetchone()[0],
            0,
        )

    def test_post_login_routing_keeps_authorities_and_membership_separate(self):
        now = 123458
        self.conn.execute(
            """
            INSERT INTO class_sections
              (class_id, teacher_user_id, name, join_code, is_active,
               created_at, updated_at)
            VALUES ('ROUTING-CLASS', 3, 'Routing Class', 'ROUTE1', 1, ?, ?)
            """,
            (now, now),
        )
        self.conn.execute(
            """
            INSERT INTO class_enrollments
              (class_id, student_id, enrolled_at, enrolled_by)
            VALUES ('ROUTING-CLASS', 'STUDENT-1', ?, 'self')
            """,
            (now,),
        )
        self.conn.commit()

        expected = {
            1: "/owner",
            2: "/restricted",
            3: "/teacher",
            4: "/student",
        }
        for user_id, destination in expected.items():
            with self.subTest(user_id=user_id):
                with dash.app.test_request_context("/"):
                    dash.session["user_id"] = user_id
                    self.assertEqual(
                        dash.get_post_login_destination(),
                        destination,
                    )

    def test_instructional_authorization_migration_backfills_idempotently(self):
        from migrations.add_user_instructional_authorizations import run

        migration_db = Path(TEST_DIR.name) / f"migration_{uuid.uuid4().hex}.db"
        with sqlite3.connect(migration_db) as conn:
            conn.execute(
                """
                CREATE TABLE users (
                  id INTEGER PRIMARY KEY,
                  role TEXT NOT NULL,
                  account_role TEXT,
                  last_login_ts INTEGER
                )
                """
            )
            conn.executemany(
                "INSERT INTO users VALUES (?, ?, ?, ?)",
                [
                    (1, "teacher", "teacher", 100),
                    (2, "student", "pending", None),
                ],
            )
            conn.commit()

        run(migration_db)
        run(migration_db)

        with sqlite3.connect(migration_db) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM user_instructional_authorizations"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT user_id, instructional_role
                    FROM user_instructional_authorizations
                    """
                ).fetchone(),
                (1, "teacher"),
            )
            self.assertIsNotNone(
                conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table'
                      AND name='access_authorization_audit_log'
                    """
                ).fetchone()
            )


if __name__ == "__main__":
    unittest.main()
