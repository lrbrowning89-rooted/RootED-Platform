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
TEST_DB = Path(TEST_DIR.name) / f"test_owner_workspace_{uuid.uuid4().hex}.db"
os.environ["NGSS_DB"] = str(TEST_DB)
os.environ["SECRET_KEY"] = "test-secret"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

dash = importlib.import_module("app_core.dashboard_v5")


class OwnerWorkspaceTests(unittest.TestCase):
    def setUp(self):
        dash.DB = str(TEST_DB)
        dash.ae.DB_PATH = str(TEST_DB)
        self.conn = sqlite3.connect(TEST_DB)
        self.conn.row_factory = sqlite3.Row
        dash.ensure_schema(self.conn)
        self.clear_tables()
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

    def clear_tables(self):
        for table in [
            "access_authorization_audit_log",
            "question_flag_events",
            "question_flags",
            "user_instructional_authorizations",
            "user_platform_roles",
            "student_question_deliveries",
            "student_growth_progress",
            "student_objective_state",
            "progress_state",
            "responses",
            "attempts",
            "routing_level_attempts",
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

    def seed_data(self):
        now = 123456
        self.conn.execute(
            """
            INSERT INTO standards (standard_id, core_idea, grade_band)
            VALUES ('MS-LS1-1', 'LS1', 'MS')
            """
        )
        self.conn.execute(
            """
            INSERT INTO objectives
              (objective_id, standard_id, objective_text, order_in_band)
            VALUES ('MS-LS1-1A', 'MS-LS1-1', 'Cells are living units.', 1)
            """
        )
        self.conn.execute(
            """
            INSERT INTO questions
              (question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key)
            VALUES
              ('Q1A', 'MS-LS1-1A', 'Which statement best describes a cell?',
               'Living unit', 'Rock', 'Cloud', 'Planet', 'A')
            """
        )
        self.conn.execute(
            """
            INSERT INTO students
              (student_id, first_name, last_name, grade, class_period)
            VALUES ('S1', 'Ava', 'One', 6, '1')
            """
        )
        self.conn.executemany(
            """
            INSERT INTO users
              (id, username, password_hash, role, linked_student_id, is_active)
            VALUES (?, ?, 'unused', ?, ?, 1)
            """,
            [
                (1, "owner_teacher", "teacher", None),
                (2, "teacher_only", "teacher", None),
                (3, "student_only", "student", "S1"),
                (4, "owner_student", "student", "S1"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO user_platform_roles
              (user_id, platform_role, granted_at, granted_by, grant_note)
            VALUES (?, 'owner', ?, 1, 'Test owner')
            """,
            [(1, now), (4, now)],
        )
        self.conn.executemany(
            """
            INSERT INTO user_instructional_authorizations
              (user_id, instructional_role, granted_at, granted_by, grant_note)
            VALUES (?, 'teacher', ?, 1, 'Test teacher')
            """,
            [(1, now), (2, now)],
        )
        self.conn.execute(
            """
            INSERT INTO class_sections
              (class_id, teacher_user_id, name, class_period, join_code,
               is_active, created_at, updated_at)
            VALUES ('C1', 2, 'Period 1 Science', '1', 'AAA111', 1, ?, ?)
            """,
            (now, now),
        )
        self.conn.execute(
            """
            INSERT INTO class_sections
              (class_id, teacher_user_id, name, class_period, join_code,
               is_active, created_at, updated_at)
            VALUES ('C2', 1, 'Owner Teaching Section', '2', 'BBB222', 1, ?, ?)
            """,
            (now, now),
        )
        self.conn.execute(
            """
            INSERT INTO class_enrollments
              (class_id, student_id, enrolled_at, enrolled_by)
            VALUES ('C1', 'S1', ?, 'self')
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO progress_state
              (student_id, standard_id, current_level, status, rolling_avg,
               locked, locked_reason, last_update)
            VALUES ('S1', 'MS-LS1-1', 1, 'practicing', 0.5, 0, NULL, ?)
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
        self.conn.execute(
            """
            INSERT INTO attempts
              (attempt_id, student_id, question_id, timestamp, response,
               is_correct, time_seconds, skills_missed)
            VALUES ('A1', 'S1', 'Q1A', ?, 'A', 1, 8.0, '[]')
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO responses
              (student_id, standard_id, level, question_id, correct, ts)
            VALUES ('S1', 'MS-LS1-1', 1, 'Q1A', 1, ?)
            """,
            (now,),
        )
        self.conn.commit()

    def login_as(self, user_id, username, role):
        with self.client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["username"] = username
            sess["role"] = role
            sess["current_mode"] = "home" if role == "student" else "question"

    def insert_flag(
        self,
        flag_id,
        *,
        status="escalated",
        question_id="Q1A",
        comment=None,
        class_id="C1",
        student_id="S1",
    ):
        self.conn.execute(
            """
            INSERT INTO question_flags
              (flag_id, question_id, objective_id, standard_id,
               reporter_user_id, reporter_role, class_id, student_id,
               category, comment, page_context, created_ts, status,
               escalated_by_user_id, escalated_at, escalation_note)
            VALUES (?, ?, 'MS-LS1-1A', 'MS-LS1-1',
                    3, 'student', ?, ?, 'confusing_question', ?,
                    'student_practice', 123457, ?, 2, 123458,
                    'Please review this content.')
            """,
            (
                flag_id,
                question_id,
                class_id,
                student_id,
                comment or f"{status} report",
                status,
            ),
        )
        self.conn.commit()

    def learning_state(self):
        snapshot = {}
        for table in [
            "attempts",
            "responses",
            "progress_state",
            "student_objective_state",
        ]:
            snapshot[table] = [
                tuple(row)
                for row in self.conn.execute(
                    f"SELECT * FROM {table} ORDER BY 1"
                ).fetchall()
            ]
        return snapshot

    def test_owner_homepage_badge_account_and_navigation(self):
        self.insert_flag("QF-ESC-1", status="escalated")
        self.insert_flag("QF-ESC-2", status="escalated")
        self.insert_flag("QF-REVIEW", status="owner_reviewing")
        self.login_as(1, "owner_teacher", "teacher")

        response = self.client.get("/owner")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"RootED Owner Workspace", response.data)
        self.assertIn(b"Escalated Question Flags", response.data)
        self.assertIn(b"owner_teacher", response.data)
        self.assertIn(b"Teacher Workspace", response.data)
        self.assertIn(b"Adaptive Engine Debug", response.data)
        self.assertIn(b">2</span>", response.data)
        self.assertNotIn(b"Subscriptions", response.data)
        self.assertNotIn(b"Organizations", response.data)

    def test_adaptive_debug_is_owner_only(self):
        self.login_as(2, "teacher_only", "teacher")
        teacher_index = self.client.get("/owner/adaptive-debug")
        teacher_detail = self.client.get("/engine_debug/S1/MS-LS1-1")
        self.assertEqual(teacher_index.status_code, 403)
        self.assertEqual(teacher_detail.status_code, 403)

        self.login_as(3, "student_only", "student")
        student_detail = self.client.get("/engine_debug/S1/MS-LS1-1")
        self.assertEqual(student_detail.status_code, 403)

        with self.client.session_transaction() as sess:
            sess.clear()
        anonymous_detail = self.client.get("/engine_debug/S1/MS-LS1-1")
        self.assertEqual(anonymous_detail.status_code, 302)
        self.assertIn("/login", anonymous_detail.headers["Location"])

    def test_adaptive_debug_explains_active_attempt_without_mutating_state(self):
        self.conn.execute(
            """
            INSERT INTO routing_level_attempts
              (attempt_id, student_id, standard_id, objective_id, level,
               status, started_at)
            VALUES ('RLA-DEBUG', 'S1', 'MS-LS1-1', 'MS-LS1-1A', 1,
                    'active', 123450)
            """
        )
        self.conn.execute(
            """
            UPDATE responses
            SET routing_level_attempt_id='RLA-DEBUG'
            WHERE student_id='S1'
            """
        )
        for offset, correct in enumerate([1, 1, 0, 1, 1, 1, 0], 1):
            if offset == 1:
                continue
            self.conn.execute(
                """
                INSERT INTO responses
                  (student_id, standard_id, level, question_id, correct, ts,
                   routing_level_attempt_id)
                VALUES ('S1', 'MS-LS1-1', 1, 'Q1A', ?, ?, 'RLA-DEBUG')
                """,
                (correct, 123456 + offset),
            )
        self.conn.executemany(
            """
            INSERT INTO student_question_deliveries
              (submission_token, student_id, growth_attempt_id, standard_id,
               objective_id, level, question_id, sequence_number, served_at,
               consumed_at, invalidated_at)
            VALUES (?, 'S1', 'GROWTH-DEBUG', 'MS-LS1-1', 'MS-LS1-1A', 1,
                    'Q1A', ?, ?, ?, ?)
            """,
            [
                ("delivery-consumed", 0, 123460, 123461, None),
                ("delivery-active", 1, 123462, None, None),
                ("delivery-invalid", 2, 123463, None, 123464),
            ],
        )
        self.conn.commit()
        before = self.learning_state()
        self.login_as(1, "owner_teacher", "teacher")

        index = self.client.get("/owner/adaptive-debug")
        detail = self.client.get("/engine_debug/S1/MS-LS1-1")

        self.assertEqual(index.status_code, 200)
        self.assertIn(b"Ava One", index.data)
        self.assertIn(b"RLA-DEBUG", detail.data)
        self.assertIn(b"Cells are living units.", detail.data)
        self.assertIn(b">7</strong>Responses", detail.data)
        self.assertIn(b">5</strong>Correct", detail.data)
        self.assertIn(b">2</strong>Incorrect", detail.data)
        self.assertIn(b"71.4%", detail.data)
        self.assertIn(b">Yes</dd>", detail.data)
        self.assertIn(b">Practice</span>", detail.data)
        self.assertIn(b"middle_band_continue", detail.data)
        self.assertIn(b">1</strong>Active", detail.data)
        self.assertIn(b">1</strong>Consumed", detail.data)
        self.assertIn(b">1</strong>Invalidated", detail.data)
        self.assertEqual(detail.data.count(b'aria-label="Correct"'), 5)
        self.assertEqual(detail.data.count(b'aria-label="Incorrect"'), 2)
        self.assertEqual(self.learning_state(), before)

    def test_teacher_dashboard_links_owner_workspace_only_for_owner(self):
        self.insert_flag("QF-ESC", status="escalated")
        self.login_as(1, "owner_teacher", "teacher")
        owner_dashboard = self.client.get("/dashboard")
        self.assertEqual(owner_dashboard.status_code, 200)
        self.assertIn(b"Owner Workspace", owner_dashboard.data)
        self.assertIn(b"awaiting RootED review", owner_dashboard.data)

        self.login_as(2, "teacher_only", "teacher")
        teacher_dashboard = self.client.get("/dashboard")
        self.assertEqual(teacher_dashboard.status_code, 200)
        self.assertNotIn(b"Owner Workspace", teacher_dashboard.data)

    def test_teacher_student_and_anonymous_are_denied_owner_routes(self):
        for user in [
            (2, "teacher_only", "teacher"),
            (3, "student_only", "student"),
        ]:
            with self.subTest(role=user[2]):
                self.login_as(*user)
                self.assertEqual(self.client.get("/owner").status_code, 403)
                self.assertEqual(
                    self.client.get("/owner/question-flags").status_code,
                    403,
                )
        with self.client.session_transaction() as sess:
            sess.clear()
        self.assertEqual(self.client.get("/owner").status_code, 302)

    def test_owner_without_teacher_role_cannot_access_teacher_dashboard(self):
        self.login_as(4, "owner_student", "student")
        owner_home = self.client.get("/owner")
        teacher_dashboard = self.client.get("/dashboard")

        self.assertEqual(owner_home.status_code, 200)
        self.assertNotIn(b"Teacher Dashboard", owner_home.data)
        self.assertEqual(teacher_dashboard.status_code, 302)
        self.assertTrue(teacher_dashboard.location.endswith("/restricted"))

    def test_owner_queue_filters_and_question_reference(self):
        self.insert_flag("QF-OPEN", status="open", comment="ordinary hidden")
        self.insert_flag("QF-ESC", status="escalated", comment="needs review")
        self.insert_flag(
            "QF-REVIEW", status="owner_reviewing", comment="being reviewed"
        )
        self.insert_flag("QF-FIXED", status="fixed", comment="fixed report")
        self.insert_flag("QF-CLOSED", status="closed", comment="closed report")
        self.login_as(1, "owner_teacher", "teacher")

        needs_review = self.client.get("/owner/question-flags")
        in_review = self.client.get("/owner/question-flags?filter=in-review")
        completed = self.client.get("/owner/question-flags?filter=completed")

        self.assertIn(b"needs review", needs_review.data)
        self.assertNotIn(b"ordinary hidden", needs_review.data)
        self.assertNotIn(b"being reviewed", needs_review.data)
        self.assertIn(b"Which statement best describes a cell?", needs_review.data)
        self.assertIn(b"Cells are living units.", needs_review.data)
        self.assertIn(b"This question is confusing", needs_review.data)
        self.assertIn(b"Please review this content.", needs_review.data)
        self.assertIn(b"Period 1 Science", needs_review.data)
        self.assertIn(b"student_practice", needs_review.data)
        self.assertIn(b"being reviewed", in_review.data)
        self.assertNotIn(b"needs review", in_review.data)
        self.assertIn(b"fixed report", completed.data)
        self.assertIn(b"closed report", completed.data)

    def test_missing_question_is_non_clickable(self):
        self.insert_flag(
            "QF-MISSING",
            status="escalated",
            question_id="Q-RETIRED",
            comment="retired question",
        )
        self.login_as(1, "owner_teacher", "teacher")

        response = self.client.get("/owner/question-flags")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Question no longer available", response.data)
        self.assertNotIn(
            b"/owner/question-preview?question_id=Q-RETIRED",
            response.data,
        )

    def test_owner_preview_is_read_only_and_preserves_learning_state(self):
        before = self.learning_state()
        self.login_as(1, "owner_teacher", "teacher")

        response = self.client.get("/owner/question-preview?question_id=Q1A")
        after = self.learning_state()

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Read-only owner review", response.data)
        self.assertIn(b"Which statement best describes a cell?", response.data)
        self.assertNotIn(b"<form", response.data)
        self.assertNotIn(b"submit_question_flag", response.data)
        self.assertNotIn(b"record_attempt", response.data)
        self.assertEqual(before, after)

    def test_valid_owner_transitions_persist_history_and_leave_learning_unchanged(self):
        self.insert_flag("QF-WORKFLOW", status="escalated")
        before = self.learning_state()
        self.login_as(1, "owner_teacher", "teacher")

        review = self.client.post(
            "/owner/question-flags/QF-WORKFLOW/review",
            data={"note": "Investigating answer key."},
        )
        fixed = self.client.post(
            "/owner/question-flags/QF-WORKFLOW/fixed",
            data={"resolution_note": "Corrected the answer key and verified preview."},
        )

        self.assertEqual(review.status_code, 302)
        self.assertEqual(fixed.status_code, 302)
        flag = self.conn.execute(
            "SELECT * FROM question_flags WHERE flag_id = 'QF-WORKFLOW'"
        ).fetchone()
        events = self.conn.execute(
            """
            SELECT * FROM question_flag_events
            WHERE flag_id = 'QF-WORKFLOW'
            ORDER BY event_id
            """
        ).fetchall()
        self.assertEqual(flag["status"], "fixed")
        self.assertEqual(flag["owner_reviewing_by_user_id"], 1)
        self.assertIsNotNone(flag["owner_reviewing_at"])
        self.assertEqual(flag["resolved_by_user_id"], 1)
        self.assertIsNotNone(flag["resolved_ts"])
        self.assertEqual(
            flag["resolution_note"],
            "Corrected the answer key and verified preview.",
        )
        self.assertEqual(
            [(event["from_status"], event["to_status"]) for event in events],
            [
                ("escalated", "owner_reviewing"),
                ("owner_reviewing", "fixed"),
            ],
        )
        self.assertEqual(events[0]["actor_user_id"], 1)
        self.assertEqual(events[0]["actor_authority"], "owner")
        self.assertEqual(events[0]["note"], "Investigating answer key.")
        self.assertEqual(before, self.learning_state())

    def test_close_transition_requires_note(self):
        self.insert_flag("QF-CLOSE", status="owner_reviewing")
        self.login_as(1, "owner_teacher", "teacher")

        missing_note = self.client.post(
            "/owner/question-flags/QF-CLOSE/closed",
            data={"resolution_note": "   "},
        )
        valid = self.client.post(
            "/owner/question-flags/QF-CLOSE/closed",
            data={"resolution_note": "Duplicate of another resolved report."},
        )

        self.assertEqual(missing_note.status_code, 400)
        self.assertEqual(valid.status_code, 302)
        row = self.conn.execute(
            "SELECT * FROM question_flags WHERE flag_id = 'QF-CLOSE'"
        ).fetchone()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(
            row["resolution_note"],
            "Duplicate of another resolved report.",
        )

    def test_fixed_transition_requires_note(self):
        self.insert_flag("QF-FIX-NOTE", status="owner_reviewing")
        self.login_as(1, "owner_teacher", "teacher")

        response = self.client.post(
            "/owner/question-flags/QF-FIX-NOTE/fixed",
            data={"resolution_note": ""},
        )

        self.assertEqual(response.status_code, 400)
        row = self.conn.execute(
            "SELECT status FROM question_flags WHERE flag_id = 'QF-FIX-NOTE'"
        ).fetchone()
        self.assertEqual(row["status"], "owner_reviewing")

    def test_invalid_and_unauthorized_transitions_are_rejected(self):
        self.insert_flag("QF-OPEN", status="open")
        self.insert_flag("QF-ESC", status="escalated")
        self.login_as(1, "owner_teacher", "teacher")

        invalid = self.client.post(
            "/owner/question-flags/QF-OPEN/review",
            data={"note": "Invalid"},
        )
        skip_review = self.client.post(
            "/owner/question-flags/QF-ESC/fixed",
            data={"resolution_note": "Cannot skip review."},
        )
        self.login_as(2, "teacher_only", "teacher")
        unauthorized = self.client.post(
            "/owner/question-flags/QF-ESC/review",
            data={"note": "Not an owner"},
        )

        self.assertEqual(invalid.status_code, 409)
        self.assertEqual(skip_review.status_code, 409)
        self.assertEqual(unauthorized.status_code, 403)
        statuses = {
            row["flag_id"]: row["status"]
            for row in self.conn.execute(
                "SELECT flag_id, status FROM question_flags"
            )
        }
        self.assertEqual(statuses["QF-OPEN"], "open")
        self.assertEqual(statuses["QF-ESC"], "escalated")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM question_flag_events"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
