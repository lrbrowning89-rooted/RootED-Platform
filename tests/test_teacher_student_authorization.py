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
TEST_DB = Path(TEST_DIR.name) / f"teacher_scope_{uuid.uuid4().hex}.db"
os.environ["NGSS_DB"] = str(TEST_DB)
os.environ["SECRET_KEY"] = "test-secret"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
dash = importlib.import_module("app_core.dashboard_v5")


class TeacherStudentAuthorizationTests(unittest.TestCase):
    def setUp(self):
        dash.DB = str(TEST_DB)
        dash.ae.DB_PATH = str(TEST_DB)
        self.conn = sqlite3.connect(TEST_DB)
        self.conn.row_factory = sqlite3.Row
        dash.ensure_schema(self.conn)
        for table in (
            "diagnostic_responses", "diagnostic_sessions", "class_enrollments",
            "class_sections", "user_instructional_authorizations",
            "user_platform_roles", "user_auth_identities", "users", "students",
        ):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.executemany(
            """
            INSERT INTO users(id,username,password_hash,role,account_role,is_active)
            VALUES (?,?,'x','teacher','teacher',1)
            """,
            [(1, "teacher-a"), (2, "teacher-b"), (3, "new-teacher")],
        )
        self.conn.execute(
            "INSERT INTO users(id,username,password_hash,role,account_role,is_active) VALUES (4,'owner','x','student','pending',1)"
        )
        self.conn.executemany(
            "INSERT INTO user_instructional_authorizations(user_id,instructional_role,granted_at) VALUES (?,'teacher',1)",
            [(1,), (2,), (3,)],
        )
        self.conn.execute(
            "INSERT INTO user_platform_roles(user_id,platform_role,granted_at) VALUES (4,'owner',1)"
        )
        self.conn.executemany(
            "INSERT INTO students(student_id,first_name,last_name,grade,class_period) VALUES (?,?,?,?,?)",
            [
                ("A-STUDENT", "Alice", "Allowed", 6, "1"),
                ("B-STUDENT", "Bob", "Blocked", 6, "2"),
                ("INACTIVE-STUDENT", "Ivy", "Inactive", 6, "1"),
                ("ARCHIVED-STUDENT", "Aria", "Archived", 6, "1"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO class_sections
              (class_id,teacher_user_id,name,join_code,is_active,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            [
                ("CLASS-A", 1, "A Class", "AAA234", 1, 1, 1),
                ("CLASS-B", 2, "B Class", "BBB234", 1, 1, 1),
                ("ARCHIVED", 1, "Old Class", "CCC234", 0, 1, 1),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO class_enrollments
              (class_id,student_id,enrolled_at,enrolled_by,is_active)
            VALUES (?,?,1,'teacher',?)
            """,
            [
                ("CLASS-A", "A-STUDENT", 1),
                ("CLASS-B", "B-STUDENT", 1),
                ("CLASS-A", "INACTIVE-STUDENT", 0),
                ("ARCHIVED", "ARCHIVED-STUDENT", 1),
            ],
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

    def login(self, user_id):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
            session["username"] = f"user-{user_id}"

    def test_new_teacher_with_no_classes_sees_no_students_or_counts(self):
        self.login(3)
        page = self.client.get("/dashboard").get_data(as_text=True)
        for student_id in ("A-STUDENT", "B-STUDENT", "INACTIVE-STUDENT", "ARCHIVED-STUDENT"):
            self.assertNotIn(student_id, page)
        self.assertEqual(dash.authorized_student_ids_for_teacher(self.conn, 3), [])

    def test_dashboard_and_export_include_only_active_own_class_students(self):
        self.login(1)
        page = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("A-STUDENT", page)
        self.assertNotIn("B-STUDENT", page)
        self.assertNotIn("INACTIVE-STUDENT", page)
        self.assertNotIn("ARCHIVED-STUDENT", page)
        exported = self.client.get("/export_csv").get_data(as_text=True)
        self.assertNotIn("B-STUDENT", exported)
        self.assertEqual(
            [row["student_id"] for row in dash.get_students(
                self.conn, teacher_user_id=1
            )],
            ["A-STUDENT"],
        )

    def test_student_overview_and_enumeration_deny_unrelated_student(self):
        self.login(1)
        self.assertEqual(self.client.get("/teacher/student/A-STUDENT").status_code, 200)
        self.assertEqual(self.client.get("/teacher/student/B-STUDENT").status_code, 404)
        self.assertEqual(self.client.get("/student?student_id=B-STUDENT").status_code, 404)
        self.assertEqual(self.client.get("/diagnostic?student_id=B-STUDENT").status_code, 403)

    def test_placement_and_diagnostic_deny_unrelated_student(self):
        self.login(1)
        placement = self.client.post(
            "/teacher/student/B-STUDENT/placement",
            data={"learning_node": "MS-LS1-1||MS-LS1-1A", "current_level": "1"},
        )
        self.assertEqual(placement.status_code, 404)
        diagnostic = self.client.post(
            "/diagnostic/start", data={"student_id": "B-STUDENT"}
        )
        self.assertEqual(diagnostic.status_code, 403)

    def test_class_roster_and_helpers_are_scoped(self):
        self.assertTrue(dash.teacher_can_access_class(self.conn, 1, "CLASS-A"))
        self.assertFalse(dash.teacher_can_access_class(self.conn, 1, "CLASS-B"))
        self.assertTrue(dash.teacher_can_access_student(self.conn, 1, "A-STUDENT"))
        self.assertFalse(dash.teacher_can_access_student(self.conn, 1, "B-STUDENT"))
        classes = dash.get_class_sections_for_teacher(self.conn, 1)
        self.assertEqual([row["class_id"] for row in classes], ["CLASS-A"])

    def test_owner_retains_overview_but_student_and_anonymous_are_denied(self):
        self.login(4)
        self.assertEqual(self.client.get("/teacher/student/B-STUDENT").status_code, 200)
        self.conn.execute(
            "INSERT INTO users(id,username,password_hash,role,account_role,is_active) VALUES (5,'student','x','student','student',1)"
        )
        self.conn.commit()
        self.login(5)
        self.assertEqual(self.client.get("/teacher/student/A-STUDENT").status_code, 403)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get("/teacher/student/A-STUDENT").status_code, 302)

    def test_deauthorized_teacher_cannot_access_student_data(self):
        self.conn.execute(
            "UPDATE user_instructional_authorizations SET revoked_at=2 WHERE user_id=1"
        )
        self.conn.commit()
        self.login(1)
        response = self.client.get("/teacher/student/A-STUDENT")
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
