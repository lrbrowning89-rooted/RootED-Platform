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
TEST_DB = Path(TEST_DIR.name) / f"test_question_flags_{uuid.uuid4().hex}.db"
os.environ["NGSS_DB"] = str(TEST_DB)
os.environ["SECRET_KEY"] = "test-secret"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

dash = importlib.import_module("app_core.dashboard_v5")


class QuestionFlaggingTests(unittest.TestCase):
    def setUp(self):
        dash.DB = str(TEST_DB)
        dash.ae.DB_PATH = str(TEST_DB)
        self.conn = sqlite3.connect(TEST_DB)
        self.conn.row_factory = sqlite3.Row
        dash.ensure_schema(self.conn)
        self.clear_tables()
        self.seed_content()
        dash.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
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

    def clear_tables(self):
        for table in [
            "question_flags",
            "student_objective_state",
            "progress_state",
            "responses",
            "attempts",
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
            "INSERT INTO standards (standard_id, core_idea, grade_band) VALUES (?, 'LS1', 'MS')",
            [("MS-LS1-1",), ("MS-LS1-2",), ("MS-LS1-3",), ("MS-LS1-4",)],
        )
        self.conn.executemany(
            """
            INSERT INTO objectives (objective_id, standard_id, objective_text, order_in_band)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("MS-LS1-1A", "MS-LS1-1", "Cells are living units.", 1),
                ("MS-LS1-1B", "MS-LS1-1", "Cells form tissues.", 2),
                ("MS-LS1-4A", "MS-LS1-4", "Non-launch objective.", 3),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO questions
              (question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key)
            VALUES (?, ?, ?, 'A', 'B', 'C', 'D', 'A')
            """,
            [
                ("Q1A", "MS-LS1-1A", "Launch question 1A"),
                ("Q1B", "MS-LS1-1B", "Launch question 1B"),
                ("Q4A", "MS-LS1-4A", "Non-launch question"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO students (student_id, first_name, last_name, grade, class_period) VALUES (?, ?, ?, 6, ?)",
            [
                ("S1", "Ava", "One", "1"),
                ("S2", "Ben", "Two", "2"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO users
              (id, username, password_hash, role, linked_student_id, is_active)
            VALUES (?, ?, 'unused', ?, ?, 1)
            """,
            [
                (1, "teacher1", "teacher", None),
                (2, "teacher2", "teacher", None),
                (3, "student1", "student", "S1"),
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
        self.conn.execute(
            """
            INSERT INTO progress_state
              (student_id, standard_id, current_level, status, rolling_avg, locked, locked_reason, last_update)
            VALUES ('S1', 'MS-LS1-1', 1, 'practicing', 0.0, 0, NULL, ?)
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO student_objective_state
              (student_id, standard_id, current_objective_id, status, last_update)
            VALUES ('S1', 'MS-LS1-1', 'MS-LS1-1A', 'active', ?)
            """,
            (now,),
        )
        self.conn.commit()

    def login_as(self, user_id, username, role):
        with self.client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["username"] = username
            sess["role"] = role
            sess["current_mode"] = "question"

    def flag_rows(self):
        return self.conn.execute(
            "SELECT * FROM question_flags ORDER BY created_ts, flag_id"
        ).fetchall()

    def table_count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_student_flag_submission_records_context(self):
        self.login_as(3, "student1", "student")

        response = self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1A",
                "category": "confusing_question",
                "comment": "The wording is unclear.",
                "page_context": "student_question_view",
            },
        )

        self.assertEqual(response.status_code, 302)
        row = self.flag_rows()[0]
        self.assertEqual(row["question_id"], "Q1A")
        self.assertEqual(row["objective_id"], "MS-LS1-1A")
        self.assertEqual(row["standard_id"], "MS-LS1-1")
        self.assertEqual(row["reporter_user_id"], 3)
        self.assertEqual(row["reporter_role"], "student")
        self.assertEqual(row["class_id"], "C1")
        self.assertEqual(row["student_id"], "S1")
        self.assertEqual(row["category"], "confusing_question")
        self.assertEqual(row["status"], "open")

    def test_teacher_preview_flag_submission_records_question_context(self):
        self.login_as(1, "teacher1", "teacher")

        response = self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1B",
                "category": "visual_problem",
                "comment": "Diagram did not load.",
                "page_context": "teacher_question_preview",
            },
        )

        self.assertEqual(response.status_code, 302)
        row = self.flag_rows()[0]
        self.assertEqual(row["question_id"], "Q1B")
        self.assertEqual(row["objective_id"], "MS-LS1-1B")
        self.assertEqual(row["standard_id"], "MS-LS1-1")
        self.assertEqual(row["reporter_user_id"], 1)
        self.assertEqual(row["reporter_role"], "teacher")
        self.assertIsNone(row["student_id"])
        self.assertIsNone(row["class_id"])

        page = self.client.get("/teacher/question_flags")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Q1B", page.data)
        self.assertIn(b"teacher_question_preview", page.data)
        self.assertIn(b"The picture or model has a problem", page.data)

    def test_flag_question_link_opens_exact_teacher_preview_question(self):
        self.login_as(1, "teacher1", "teacher")
        self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1B",
                "category": "visual_problem",
                "comment": "Diagram did not load.",
                "page_context": "teacher_question_preview",
            },
        )

        flags_page = self.client.get("/teacher/question_flags")
        self.assertIn(
            b"/teacher/question_preview?question_id=Q1B&amp;return_to=question_flags",
            flags_page.data,
        )

        preview = self.client.get(
            "/teacher/question_preview?question_id=Q1B&return_to=question_flags"
        )

        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"Launch question 1B", preview.data)
        self.assertNotIn(b"Launch question 1A</p>", preview.data)
        self.assertIn(b'<option value="MS-LS1-1B" selected>', preview.data)
        self.assertIn(b'<option value="Q1B" selected>', preview.data)
        self.assertIn(b"Back to Question Flags", preview.data)
        self.assertIn(b'href="/teacher/question_flags"', preview.data)

    def test_flag_review_shows_missing_question_without_fallback_link(self):
        self.conn.execute(
            """
            INSERT INTO question_flags
              (flag_id, question_id, objective_id, standard_id, reporter_user_id,
               reporter_role, category, comment, page_context, created_ts, status)
            VALUES ('QF-MISSING', 'Q_RETIRED', 'MS-LS1-1A', 'MS-LS1-1', 1,
                    'teacher', 'other', 'Historical flag.', 'teacher_question_preview',
                    123457, 'open')
            """
        )
        self.conn.commit()
        self.login_as(1, "teacher1", "teacher")

        flags_page = self.client.get("/teacher/question_flags")
        invalid_preview = self.client.get(
            "/teacher/question_preview?question_id=Q_RETIRED&return_to=question_flags"
        )

        self.assertEqual(flags_page.status_code, 200)
        self.assertIn(b"Q_RETIRED", flags_page.data)
        self.assertIn(b"Question no longer available", flags_page.data)
        self.assertNotIn(
            b"/teacher/question_preview?question_id=Q_RETIRED",
            flags_page.data,
        )
        self.assertEqual(invalid_preview.status_code, 200)
        self.assertIn(b"No questions are available to preview yet.", invalid_preview.data)
        self.assertNotIn(b"Launch question 1A</p>", invalid_preview.data)

    def test_teacher_preview_rejects_oversized_comment(self):
        self.login_as(1, "teacher1", "teacher")
        page = self.client.get("/teacher/question_preview?question_id=Q1A")
        self.assertIn(b'maxlength="500"', page.data)

        response = self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1A",
                "category": "other",
                "comment": "x" * 501,
                "page_context": "teacher_question_preview",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Flag comments must be 500 characters or fewer.", response.data)
        self.assertEqual(self.table_count("question_flags"), 0)

    def test_student_friendly_category_labels_appear_and_form_is_hidden(self):
        self.login_as(3, "student1", "student")

        student_page = self.client.get("/student")
        self.login_as(1, "teacher1", "teacher")
        teacher_page = self.client.get("/teacher/question_preview?question_id=Q1A")

        for page in [student_page, teacher_page]:
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"Report a problem", page.data)
            self.assertIn(b"The answer looks wrong", page.data)
            self.assertIn(b"This question is confusing", page.data)
            self.assertIn(b"The picture or model has a problem", page.data)
            self.assertIn(b"Something does not look right on my screen", page.data)
            self.assertIn(b"This question is hard to read or use", page.data)
            self.assertIn(b"I have already seen this question", page.data)
            self.assertIn(b"Something else", page.data)

        self.assertIn(b'id="student-report-panel" hidden', student_page.data)
        self.assertIn(b'id="teacher-report-panel" hidden', teacher_page.data)
        self.assertIn(b'type="button" aria-expanded="false"', student_page.data)
        self.assertIn(b'type="button" aria-expanded="false"', teacher_page.data)

    def test_opening_report_form_does_not_affect_response_state(self):
        before = {
            "attempts": self.table_count("attempts"),
            "responses": self.table_count("responses"),
            "progress_state": [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM progress_state ORDER BY student_id, standard_id"
                ).fetchall()
            ],
            "student_objective_state": [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM student_objective_state ORDER BY student_id, standard_id"
                ).fetchall()
            ],
        }
        self.login_as(3, "student1", "student")

        page = self.client.get("/student")

        after = {
            "attempts": self.table_count("attempts"),
            "responses": self.table_count("responses"),
            "progress_state": [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM progress_state ORDER BY student_id, standard_id"
                ).fetchall()
            ],
            "student_objective_state": [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM student_objective_state ORDER BY student_id, standard_id"
                ).fetchall()
            ],
        }

        self.assertIn(b"toggleReportPanel", page.data)
        self.assertEqual(after, before)

    def test_classless_teacher_preview_flags_are_limited_to_reporter(self):
        self.login_as(1, "teacher1", "teacher")
        self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1B",
                "category": "display_problem",
                "comment": "Preview report from teacher one.",
                "page_context": "teacher_question_preview",
            },
        )

        self.login_as(2, "teacher2", "teacher")
        page = self.client.get("/teacher/question_flags")

        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"Q1B", page.data)
        self.assertNotIn(
            b"/teacher/question_preview?question_id=Q1B&amp;return_to=question_flags",
            page.data,
        )

    def test_invalid_question_rejected(self):
        self.login_as(3, "student1", "student")

        response = self.client.post(
            "/question_flags",
            data={
                "question_id": "Q4A",
                "category": "other",
                "comment": "Not launch content.",
                "page_context": "student_question_view",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.table_count("question_flags"), 0)

    def test_authorization_limits_student_context_and_resolution(self):
        self.login_as(3, "student1", "student")
        self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1A",
                "category": "display_problem",
                "comment": "Text overlaps.",
                "page_context": "student_question_view",
            },
        )
        flag_id = self.flag_rows()[0]["flag_id"]

        self.login_as(2, "teacher2", "teacher")
        page = self.client.get("/teacher/question_flags")
        blocked = self.client.post(
            f"/teacher/question_flags/{flag_id}/resolve",
            data={"resolution_note": "Cannot resolve another class."},
        )

        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"Q1A", page.data)
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(self.flag_rows()[0]["status"], "open")

    def test_authorized_teacher_can_escalate_class_flag(self):
        self.login_as(3, "student1", "student")
        self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1A",
                "category": "display_problem",
                "comment": "Text overlaps.",
                "page_context": "student_question_view",
            },
        )
        flag_id = self.flag_rows()[0]["flag_id"]

        self.login_as(1, "teacher1", "teacher")
        response = self.client.post(
            f"/teacher/question_flags/{flag_id}/escalate",
            data={"escalation_note": "Please review this display issue."},
        )

        self.assertEqual(response.status_code, 302)
        row = self.flag_rows()[0]
        self.assertEqual(row["status"], "escalated")
        self.assertEqual(row["escalated_by_user_id"], 1)
        self.assertEqual(row["escalation_note"], "Please review this display issue.")
        self.assertIsNotNone(row["escalated_at"])

    def test_submitting_teacher_can_escalate_own_classless_preview_flag(self):
        self.login_as(1, "teacher1", "teacher")
        self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1B",
                "category": "visual_problem",
                "comment": "Image problem in preview.",
                "page_context": "teacher_question_preview",
            },
        )
        flag_id = self.flag_rows()[0]["flag_id"]

        response = self.client.post(
            f"/teacher/question_flags/{flag_id}/escalate",
            data={"escalation_note": "Teacher preview issue."},
        )

        self.assertEqual(response.status_code, 302)
        row = self.flag_rows()[0]
        self.assertEqual(row["status"], "escalated")
        self.assertEqual(row["escalated_by_user_id"], 1)
        self.assertEqual(row["escalation_note"], "Teacher preview issue.")

    def test_unrelated_teacher_cannot_escalate_flag(self):
        self.login_as(1, "teacher1", "teacher")
        self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1B",
                "category": "visual_problem",
                "comment": "Image problem in preview.",
                "page_context": "teacher_question_preview",
            },
        )
        flag_id = self.flag_rows()[0]["flag_id"]

        self.login_as(2, "teacher2", "teacher")
        response = self.client.post(
            f"/teacher/question_flags/{flag_id}/escalate",
            data={"escalation_note": "Not authorized."},
        )

        self.assertEqual(response.status_code, 403)
        row = self.flag_rows()[0]
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["escalated_by_user_id"])

    def test_normal_teacher_cannot_view_another_teachers_escalated_queue(self):
        self.login_as(1, "teacher1", "teacher")
        self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1B",
                "category": "visual_problem",
                "comment": "Image problem in preview.",
                "page_context": "teacher_question_preview",
            },
        )
        flag_id = self.flag_rows()[0]["flag_id"]
        self.client.post(
            f"/teacher/question_flags/{flag_id}/escalate",
            data={"escalation_note": "Teacher preview issue."},
        )

        self.login_as(2, "teacher2", "teacher")
        page = self.client.get("/teacher/question_flags")

        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"Q1B", page.data)
        self.assertNotIn(b"Teacher preview issue.", page.data)

    def test_resolution_workflow(self):
        self.login_as(3, "student1", "student")
        self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1A",
                "category": "accessibility_problem",
                "comment": "Alt text needs review.",
                "page_context": "student_question_view",
            },
        )
        flag_id = self.flag_rows()[0]["flag_id"]

        self.login_as(1, "teacher1", "teacher")
        response = self.client.post(
            f"/teacher/question_flags/{flag_id}/resolve",
            data={"resolution_note": "Added a clearer image description."},
        )

        self.assertEqual(response.status_code, 302)
        row = self.flag_rows()[0]
        self.assertEqual(row["status"], "teacher_resolved")
        self.assertEqual(row["resolution_note"], "Added a clearer image description.")
        self.assertEqual(row["resolved_by_user_id"], 1)
        self.assertIsNotNone(row["resolved_ts"])

    def test_escalation_does_not_change_adaptive_or_progress_state(self):
        self.login_as(3, "student1", "student")
        self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1A",
                "category": "incorrect_answer",
                "comment": "I think another answer may work.",
                "page_context": "student_question_view",
            },
        )
        flag_id = self.flag_rows()[0]["flag_id"]
        before = {
            "attempts": self.table_count("attempts"),
            "responses": self.table_count("responses"),
            "progress_state": [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM progress_state ORDER BY student_id, standard_id"
                ).fetchall()
            ],
            "student_objective_state": [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM student_objective_state ORDER BY student_id, standard_id"
                ).fetchall()
            ],
        }

        self.login_as(1, "teacher1", "teacher")
        self.client.post(
            f"/teacher/question_flags/{flag_id}/escalate",
            data={"escalation_note": "Needs RootED Support."},
        )

        after = {
            "attempts": self.table_count("attempts"),
            "responses": self.table_count("responses"),
            "progress_state": [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM progress_state ORDER BY student_id, standard_id"
                ).fetchall()
            ],
            "student_objective_state": [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM student_objective_state ORDER BY student_id, standard_id"
                ).fetchall()
            ],
        }

        self.assertEqual(after["attempts"], before["attempts"])
        self.assertEqual(after["responses"], before["responses"])
        self.assertEqual(after["progress_state"], before["progress_state"])
        self.assertEqual(after["student_objective_state"], before["student_objective_state"])
        self.assertEqual(self.flag_rows()[0]["status"], "escalated")

    def test_flagging_does_not_change_adaptive_or_progress_state(self):
        before = {
            "attempts": self.table_count("attempts"),
            "responses": self.table_count("responses"),
            "progress_state": [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM progress_state ORDER BY student_id, standard_id"
                ).fetchall()
            ],
            "student_objective_state": [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM student_objective_state ORDER BY student_id, standard_id"
                ).fetchall()
            ],
        }
        self.login_as(3, "student1", "student")

        self.client.post(
            "/question_flags",
            data={
                "question_id": "Q1A",
                "category": "incorrect_answer",
                "comment": "I think another answer may work.",
                "page_context": "student_question_view",
            },
        )

        after = {
            "attempts": self.table_count("attempts"),
            "responses": self.table_count("responses"),
            "progress_state": [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM progress_state ORDER BY student_id, standard_id"
                ).fetchall()
            ],
            "student_objective_state": [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM student_objective_state ORDER BY student_id, standard_id"
                ).fetchall()
            ],
        }

        self.assertEqual(after["attempts"], before["attempts"])
        self.assertEqual(after["responses"], before["responses"])
        self.assertEqual(after["progress_state"], before["progress_state"])
        self.assertEqual(after["student_objective_state"], before["student_objective_state"])
        self.assertEqual(self.table_count("question_flags"), 1)

    def test_duplicate_reports_are_allowed_and_summarized(self):
        self.login_as(3, "student1", "student")
        for comment in ["First report", "Second report"]:
            self.client.post(
                "/question_flags",
                data={
                    "question_id": "Q1A",
                    "category": "duplicate_question",
                    "comment": comment,
                    "page_context": "student_question_view",
                },
            )

        self.assertEqual(self.table_count("question_flags"), 2)
        self.login_as(1, "teacher1", "teacher")
        page = self.client.get("/teacher/question_flags")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"2 total flag(s), 2 open", page.data)


if __name__ == "__main__":
    unittest.main()
