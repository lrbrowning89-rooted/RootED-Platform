import importlib
import os
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_DB = ROOT / "data" / f".test_placement_{uuid.uuid4().hex}.db"
os.environ["NGSS_DB"] = str(TEST_DB)
os.environ["SECRET_KEY"] = "test-secret"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

dash = importlib.import_module("app_core.dashboard_v5")


class PlacementBridgeTests(unittest.TestCase):
    def setUp(self):
        dash.DB = str(TEST_DB)
        dash.ae.DB_PATH = str(TEST_DB)
        self.conn = sqlite3.connect(TEST_DB)
        self.conn.row_factory = sqlite3.Row
        dash.ensure_schema(self.conn)
        self.clear_seed_tables()
        self.seed_content()
        dash.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = dash.app.test_client()

    def tearDown(self):
        self.conn.close()

    @classmethod
    def tearDownClass(cls):
        if TEST_DB.exists():
            try:
                TEST_DB.unlink()
            except PermissionError:
                pass

    def clear_seed_tables(self):
        for table in [
            "student_objective_state",
            "progress_state",
            "class_enrollments",
            "class_sections",
            "users",
            "students",
            "questions",
            "objectives",
            "standards",
        ]:
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    def seed_content(self):
        now = 123456
        self.conn.executemany(
            "INSERT INTO standards (standard_id, core_idea, grade_band) VALUES (?, ?, ?)",
            [
                ("MS-LS1-1", "LS1", "MS"),
                ("MS-LS1-2", "LS1", "MS"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO objectives (objective_id, standard_id, objective_text, order_in_band)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("MS-LS1-1A", "MS-LS1-1", "Cells are living units.", 1),
                ("MS-LS1-1B", "MS-LS1-1", "Cells form tissues.", 2),
                ("MS-LS1-2A", "MS-LS1-2", "Cell systems.", 1),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO questions
              (question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key)
            VALUES (?, ?, ?, 'A', 'B', 'C', 'D', 'A')
            """,
            [
                ("Q1A", "MS-LS1-1A", "Question 1A"),
                ("Q1B", "MS-LS1-1B", "Question 1B"),
                ("Q2A", "MS-LS1-2A", "Question 2A"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO students (student_id, first_name, last_name, grade, class_period) VALUES (?, ?, ?, ?, ?)",
            [
                ("S1", "Ava", "One", 6, "1"),
                ("S2", "Ben", "Two", 6, "2"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO users
              (id, username, password_hash, role, linked_student_id, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            [
                (1, "teacher1", "unused", "teacher", None),
                (2, "teacher2", "unused", "teacher", None),
                (3, "student1", "unused", "student", "S1"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO class_sections
              (class_id, teacher_user_id, name, class_period, join_code, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            [
                ("C1", 1, "Period 1", "1", "AAA111", now, now),
                ("C2", 2, "Period 2", "2", "BBB222", now, now),
            ],
        )
        self.conn.executemany(
            "INSERT INTO class_enrollments (class_id, student_id, enrolled_at, enrolled_by) VALUES (?, ?, ?, 'self')",
            [
                ("C1", "S1", now),
                ("C2", "S2", now),
            ],
        )
        self.conn.commit()

    def login_as(self, user_id, username, role):
        with self.client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["username"] = username
            sess["role"] = role
            sess["current_mode"] = "home" if role == "student" else "question"

    def progress_row(self, student_id):
        return self.conn.execute(
            """
            SELECT *
            FROM progress_state
            WHERE student_id = ?
              AND status <> 'inactive'
            ORDER BY last_update DESC
            LIMIT 1
            """,
            (student_id,),
        ).fetchone()

    def objective_row(self, student_id, standard_id):
        return self.conn.execute(
            "SELECT * FROM student_objective_state WHERE student_id = ? AND standard_id = ?",
            (student_id, standard_id),
        ).fetchone()

    def test_place_student_with_no_prior_progress(self):
        ok, message = dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1A",
            current_level=1,
            placed_by_user_id=1,
        )

        self.assertTrue(ok, message)
        self.assertEqual(self.progress_row("S1")["standard_id"], "MS-LS1-1")
        self.assertEqual(self.progress_row("S1")["current_level"], 1)
        self.assertEqual(
            self.objective_row("S1", "MS-LS1-1")["current_objective_id"],
            "MS-LS1-1A",
        )

    def test_student_route_resumes_assigned_placement(self):
        dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1B",
            current_level=2,
            placed_by_user_id=1,
        )
        self.login_as(3, "student1", "student")

        response = self.client.post("/student", data={"action": "continue_learning"})

        self.assertEqual(response.status_code, 302)
        page = self.client.get("/student")
        self.assertIn(b"MS-LS1-1B", page.data)
        self.assertIn(b"level 2", page.data)

    def test_logout_login_does_not_lose_placement(self):
        dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1B",
            current_level=3,
            placed_by_user_id=1,
        )
        self.login_as(3, "student1", "student")
        self.client.get("/logout")
        self.login_as(3, "student1", "student")
        self.client.post("/student", data={"action": "continue_learning"})

        page = self.client.get("/student")

        self.assertIn(b"MS-LS1-1B", page.data)
        self.assertIn(b"level 3", page.data)

    def test_opening_student_does_not_overwrite_existing_placement(self):
        dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-2",
            objective_id="MS-LS1-2A",
            current_level=2,
            placed_by_user_id=1,
        )
        before = dict(self.progress_row("S1"))
        self.login_as(3, "student1", "student")

        self.client.get("/student")
        after = dict(self.progress_row("S1"))

        self.assertEqual(after["standard_id"], before["standard_id"])
        self.assertEqual(after["current_level"], before["current_level"])
        self.assertEqual(
            self.objective_row("S1", "MS-LS1-2")["current_objective_id"],
            "MS-LS1-2A",
        )

    def test_explicit_teacher_repositioning(self):
        dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1A",
            current_level=1,
            placed_by_user_id=1,
        )

        ok, message = dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-2",
            objective_id="MS-LS1-2A",
            current_level=3,
            placed_by_user_id=1,
        )

        self.assertTrue(ok, message)
        self.assertEqual(self.progress_row("S1")["standard_id"], "MS-LS1-2")
        self.assertEqual(self.progress_row("S1")["current_level"], 3)
        self.assertEqual(
            self.objective_row("S1", "MS-LS1-2")["current_objective_id"],
            "MS-LS1-2A",
        )

    def test_student_overview_uses_current_durable_placement(self):
        dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1A",
            current_level=1,
            placed_by_user_id=1,
        )
        dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-2",
            objective_id="MS-LS1-2A",
            current_level=2,
            placed_by_user_id=1,
        )
        self.conn.execute(
            """
            UPDATE progress_state
            SET last_update = last_update + 1000
            WHERE student_id = 'S1'
              AND standard_id = 'MS-LS1-1'
              AND status = 'inactive'
            """
        )
        self.conn.commit()

        with dash.app.test_request_context("/teacher/student/S1"):
            dash.session["user_id"] = 1
            dash.session["username"] = "teacher1"
            dash.session["role"] = "teacher"
            overview = dash.get_student_overview(self.conn, "S1")
        placement = dash.get_current_student_placement(self.conn, "S1")

        self.assertEqual(placement["standard_id"], "MS-LS1-2")
        self.assertEqual(placement["objective_id"], "MS-LS1-2A")
        self.assertEqual(overview["latest_progress"]["standard_id"], "MS-LS1-2")
        self.assertEqual(overview["objective"]["objective_id"], "MS-LS1-2A")

    def test_placed_student_with_no_responses_is_not_started_not_needs_attention(self):
        dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1A",
            current_level=1,
            placed_by_user_id=1,
        )
        student = self.conn.execute(
            "SELECT * FROM students WHERE student_id = 'S1'"
        ).fetchone()

        with dash.app.test_request_context("/dashboard"):
            dash.session["user_id"] = 1
            dash.session["username"] = "teacher1"
            dash.session["role"] = "teacher"
            rows = dash.get_dashboard_student_rows(self.conn, [student], 0.9, 0.7)
        overview = None
        with dash.app.test_request_context("/teacher/student/S1"):
            dash.session["user_id"] = 1
            dash.session["username"] = "teacher1"
            dash.session["role"] = "teacher"
            overview = dash.get_student_overview(self.conn, "S1")

        self.assertEqual(rows[0]["category"], "not_started")
        self.assertEqual(rows[0]["status_label"], "Not Started")
        self.assertIsNone(rows[0]["rolling_avg"])
        self.assertEqual(rows[0]["response_count"], 0)
        self.assertIsNone(overview["rolling_avg"])
        self.assertEqual(overview["rolling_count"], 0)

    def test_student_with_response_evidence_can_be_low_performance(self):
        dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1A",
            current_level=1,
            placed_by_user_id=1,
        )
        self.conn.execute(
            """
            INSERT INTO responses (student_id, standard_id, level, question_id, correct, ts)
            VALUES ('S1', 'MS-LS1-1', 1, 'Q1A', 0, 123457)
            """
        )
        self.conn.execute(
            """
            UPDATE progress_state
            SET rolling_avg = 0.0
            WHERE student_id = 'S1'
              AND standard_id = 'MS-LS1-1'
            """
        )
        self.conn.commit()
        student = self.conn.execute(
            "SELECT * FROM students WHERE student_id = 'S1'"
        ).fetchone()

        with dash.app.test_request_context("/dashboard"):
            dash.session["user_id"] = 1
            dash.session["username"] = "teacher1"
            dash.session["role"] = "teacher"
            rows = dash.get_dashboard_student_rows(self.conn, [student], 0.9, 0.7)

        self.assertEqual(rows[0]["category"], "needs_help")
        self.assertEqual(rows[0]["status_label"], "Needs Attention")
        self.assertEqual(rows[0]["rolling_avg"], 0.0)
        self.assertEqual(rows[0]["response_count"], 1)

    def test_adaptive_engine_zero_responses_is_insufficient_data_not_poor_performance(self):
        dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1A",
            current_level=1,
            placed_by_user_id=1,
        )

        decision = dash.ae.process_after_response("S1", "MS-LS1-1")
        state = self.conn.execute(
            """
            SELECT status, rolling_avg, locked, locked_reason
            FROM progress_state
            WHERE student_id = 'S1'
              AND standard_id = 'MS-LS1-1'
            """
        ).fetchone()

        self.assertEqual(decision["status"], "question")
        self.assertEqual(decision["reason"], "insufficient_rolling7_data")
        self.assertEqual(decision["count"], 0)
        self.assertEqual(decision["action"], "collecting_rolling7")
        self.assertEqual(state["status"], "practicing")
        self.assertEqual(state["locked"], 0)
        self.assertIsNone(state["locked_reason"])

    def test_invalid_standard_objective_combination_rejected(self):
        ok, message = dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-2A",
            current_level=1,
            placed_by_user_id=1,
        )

        self.assertFalse(ok)
        self.assertIn("does not belong", message)
        self.assertIsNone(self.progress_row("S1"))

    def test_teacher_cannot_place_student_they_do_not_control(self):
        ok, message = dash.place_student_learning_node(
            self.conn,
            student_id="S2",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1A",
            current_level=1,
            placed_by_user_id=1,
        )

        self.assertFalse(ok)
        self.assertIn("one of your active classes", message)
        self.assertIsNone(self.progress_row("S2"))

    def test_placement_remains_associated_with_correct_student_and_class(self):
        dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1A",
            current_level=1,
            placed_by_user_id=1,
        )

        enrollment = self.conn.execute(
            """
            SELECT cs.class_id, cs.teacher_user_id
            FROM class_enrollments ce
            JOIN class_sections cs ON cs.class_id = ce.class_id
            WHERE ce.student_id = ?
            """,
            ("S1",),
        ).fetchone()

        self.assertEqual(enrollment["class_id"], "C1")
        self.assertEqual(enrollment["teacher_user_id"], 1)
        self.assertEqual(self.progress_row("S1")["student_id"], "S1")
        self.assertIsNone(self.progress_row("S2"))


if __name__ == "__main__":
    unittest.main()
