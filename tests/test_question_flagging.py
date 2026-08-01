import gc
import importlib
import os
import re
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
            "impersonated_action_audit",
            "impersonation_audit",
            "access_authorization_audit_log",
            "question_flags",
            "user_instructional_authorizations",
            "user_platform_roles",
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
            """
            INSERT INTO user_instructional_authorizations
              (user_id, instructional_role, granted_at, grant_note)
            VALUES (?, 'teacher', ?, 'Test teacher authorization')
            """,
            [(1, now), (2, now)],
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

    def grant_owner(self, user_id=1):
        self.conn.execute(
            """
            INSERT INTO user_platform_roles
              (user_id, platform_role, granted_at, grant_note)
            VALUES (?, 'owner', 123456, 'Manual-attempt diagnostic test')
            """,
            (user_id,),
        )
        self.conn.commit()

    def flag_rows(self):
        return self.conn.execute(
            "SELECT * FROM question_flags ORDER BY created_ts, flag_id"
        ).fetchall()

    def table_count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def insert_flag(
        self,
        flag_id,
        *,
        question_id="Q1A",
        objective_id="MS-LS1-1A",
        standard_id="MS-LS1-1",
        reporter_user_id=1,
        reporter_role="teacher",
        class_id=None,
        student_id=None,
        category="other",
        comment="Seeded flag.",
        status="open",
        created_ts=123457,
    ):
        self.conn.execute(
            """
            INSERT INTO question_flags
              (flag_id, question_id, objective_id, standard_id, reporter_user_id,
               reporter_role, class_id, student_id, category, comment, page_context,
               created_ts, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test_seed', ?, ?)
            """,
            (
                flag_id,
                question_id,
                objective_id,
                standard_id,
                reporter_user_id,
                reporter_role,
                class_id,
                student_id,
                category,
                comment,
                created_ts,
                status,
            ),
        )
        self.conn.commit()

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
        preview_url = (
            b"/teacher/question_preview?question_id=Q1B&amp;return_to=question_flags"
            b"&amp;filter=current&amp;category=all"
        )
        self.assertIn(
            preview_url,
            flags_page.data,
        )
        self.assertEqual(flags_page.data.count(preview_url), 2)
        self.assertIn(b'<article class="question-reference"', flags_page.data)
        self.assertIn(b'<div class="question-reference-label">Question</div>', flags_page.data)
        self.assertIn(
            b'<a class="question-reference-stem question-reference-stem-link"',
            flags_page.data,
        )
        self.assertIn(b"Launch question 1B", flags_page.data)
        self.assertIn(b"MS-LS1-1B", flags_page.data)
        self.assertIn(b"Cells form tissues.", flags_page.data)
        self.assertIn(b"Q1B", flags_page.data)
        self.assertEqual(flags_page.data.count(b"Launch question 1B"), 1)

        preview = self.client.get(
            "/teacher/question_preview?question_id=Q1B&return_to=question_flags"
        )

        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"Launch question 1B", preview.data)
        self.assertIn(b"Cells form tissues.", preview.data)
        self.assertIn(b"Q1B", preview.data)
        self.assertNotIn(b"Launch question 1A</p>", preview.data)
        self.assertIn(b'<option value="MS-LS1-1B" selected>', preview.data)
        self.assertIn(b'<option value="Q1B" selected>', preview.data)
        self.assertIn(b"Back to Question Flags", preview.data)
        self.assertIn(b'href="/teacher/question_flags?filter=current&amp;category=all"', preview.data)

    def test_question_reference_uses_fallback_when_objective_title_missing(self):
        self.conn.execute(
            "UPDATE objectives SET objective_text = '' WHERE objective_id = 'MS-LS1-1B'"
        )
        self.conn.commit()
        self.insert_flag(
            "QF-NO-TITLE",
            question_id="Q1B",
            objective_id="MS-LS1-1B",
            reporter_user_id=1,
            comment="Missing title check.",
        )
        self.login_as(1, "teacher1", "teacher")

        page = self.client.get("/teacher/question_flags")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Launch question 1B", page.data)
        self.assertIn(b"MS-LS1-1B", page.data)
        self.assertIn(b"Q1B", page.data)
        self.assertNotIn(b"No objective text", page.data)
        self.assertNotIn(b"Cells form tissues.", page.data)

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
        self.assertIn(b'<article class="question-reference question-reference-missing"', flags_page.data)
        self.assertIn(b"MS-LS1-1A", flags_page.data)
        self.assertNotIn(
            b"/teacher/question_preview?question_id=Q_RETIRED",
            flags_page.data,
        )
        self.assertEqual(invalid_preview.status_code, 200)
        self.assertIn(b"No questions are available to preview yet.", invalid_preview.data)
        self.assertNotIn(b"Launch question 1A</p>", invalid_preview.data)

    def test_question_reference_escapes_long_text_without_malformed_markup(self):
        long_stem = "This is a very long question stem " * 20 + "<script>alert('x')</script>"
        long_title = "Long objective title " * 20 + "<b>not bold</b>"
        self.conn.execute(
            "UPDATE questions SET stem = ? WHERE question_id = 'Q1A'",
            (long_stem,),
        )
        self.conn.execute(
            "UPDATE objectives SET objective_text = ? WHERE objective_id = 'MS-LS1-1A'",
            (long_title,),
        )
        self.conn.commit()
        self.insert_flag("QF-LONG", reporter_user_id=1, comment="Long text check.")
        self.login_as(1, "teacher1", "teacher")

        page = self.client.get("/teacher/question_flags")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"This is a very long question stem", page.data)
        self.assertIn(b"Long objective title", page.data)
        self.assertIn(b"&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;", page.data)
        self.assertIn(b"&lt;b&gt;not bold&lt;/b&gt;", page.data)
        self.assertNotIn(b"<script>alert", page.data)
        self.assertNotIn(b"<b>not bold</b>", page.data)

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

    def test_dashboard_badge_shows_authorized_open_flag_count_only(self):
        self.insert_flag("QF-OPEN-OWN", reporter_user_id=1, status="open")
        self.insert_flag(
            "QF-OPEN-CLASS",
            reporter_user_id=3,
            reporter_role="student",
            class_id="C1",
            student_id="S1",
            status="open",
        )
        self.insert_flag("QF-RESOLVED", reporter_user_id=1, status="teacher_resolved")
        self.insert_flag("QF-ESCALATED", reporter_user_id=1, status="escalated")
        self.insert_flag("QF-OTHER-TEACHER", reporter_user_id=2, status="open")
        self.insert_flag(
            "QF-OTHER-CLASS",
            reporter_user_id=3,
            reporter_role="student",
            class_id="C2",
            student_id="S2",
            status="open",
        )
        self.login_as(1, "teacher1", "teacher")

        page = self.client.get("/dashboard")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Question Flags", page.data)
        self.assertIn(b"2 open question reports requiring action", page.data)
        self.assertIn(b'<span class="action-badge"', page.data)

    def test_dashboard_badge_absent_when_no_authorized_open_flags(self):
        self.insert_flag("QF-RESOLVED", reporter_user_id=1, status="teacher_resolved")
        self.insert_flag("QF-ESCALATED", reporter_user_id=1, status="escalated")
        self.insert_flag("QF-OTHER-TEACHER", reporter_user_id=2, status="open")
        self.login_as(1, "teacher1", "teacher")

        page = self.client.get("/dashboard")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Question Flags", page.data)
        self.assertNotIn(b'<span class="action-badge"', page.data)

    def test_question_flags_default_current_filter_shows_only_open(self):
        self.insert_flag("QF-OPEN", reporter_user_id=1, status="open", comment="Open flag.")
        self.insert_flag("QF-RESOLVED", reporter_user_id=1, status="teacher_resolved", comment="Resolved flag.")
        self.insert_flag("QF-SENT", reporter_user_id=1, status="escalated", comment="Sent flag.")
        self.login_as(1, "teacher1", "teacher")

        page = self.client.get("/teacher/question_flags")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b'aria-current="page"', page.data)
        self.assertIn(b"Open flag.", page.data)
        self.assertNotIn(b"Resolved flag.", page.data)
        self.assertNotIn(b"Sent flag.", page.data)

    def test_status_filters_show_expected_status_groups_and_fallback(self):
        for flag_id, status, comment in [
            ("QF-OPEN", "open", "Open flag."),
            ("QF-TEACHER-RESOLVED", "teacher_resolved", "Teacher resolved flag."),
            ("QF-FIXED", "fixed", "Fixed flag."),
            ("QF-CLOSED", "closed", "Closed flag."),
            ("QF-ESCALATED", "escalated", "Escalated flag."),
            ("QF-OWNER", "owner_reviewing", "Owner reviewing flag."),
        ]:
            self.insert_flag(flag_id, reporter_user_id=1, status=status, comment=comment)
        self.login_as(1, "teacher1", "teacher")

        resolved_page = self.client.get("/teacher/question_flags?filter=resolved")
        sent_page = self.client.get("/teacher/question_flags?filter=sent")
        fallback_page = self.client.get("/teacher/question_flags?filter=definitely_wrong")

        self.assertIn(b"Teacher resolved flag.", resolved_page.data)
        self.assertIn(b"Fixed flag.", resolved_page.data)
        self.assertIn(b"Closed flag.", resolved_page.data)
        self.assertNotIn(b"Open flag.", resolved_page.data)
        self.assertIn(b"Escalated flag.", sent_page.data)
        self.assertIn(b"Owner reviewing flag.", sent_page.data)
        self.assertNotIn(b"Open flag.", sent_page.data)
        self.assertIn(b"Open flag.", fallback_page.data)
        self.assertNotIn(b"Escalated flag.", fallback_page.data)

    def test_tab_counts_honor_authorization_and_category_filter(self):
        self.insert_flag("QF-OPEN", reporter_user_id=1, status="open", category="visual_problem")
        self.insert_flag("QF-SENT", reporter_user_id=1, status="escalated", category="visual_problem")
        self.insert_flag("QF-OTHER-CATEGORY", reporter_user_id=1, status="open", category="other")
        self.insert_flag("QF-OTHER-TEACHER", reporter_user_id=2, status="open", category="visual_problem")
        self.login_as(1, "teacher1", "teacher")

        page = self.client.get("/teacher/question_flags?filter=current&category=visual_problem")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Current", page.data)
        self.assertIn(b"Sent to RootED", page.data)
        self.assertIn(b"Seeded flag.", page.data)
        self.assertNotIn(b"QF-OTHER-CATEGORY", page.data)
        self.assertNotIn(b"QF-OTHER-TEACHER", page.data)
        self.assertIn(b"filter=sent&amp;category=visual_problem", page.data)

    def test_category_filters_use_codes_and_show_friendly_labels(self):
        categories = [
            ("incorrect_answer", "The answer looks wrong"),
            ("confusing_question", "This question is confusing"),
            ("visual_problem", "The picture or model has a problem"),
            ("display_problem", "Something does not look right on my screen"),
            ("accessibility_problem", "This question is hard to read or use"),
            ("duplicate_question", "I have already seen this question"),
            ("other", "Something else"),
        ]
        for index, (code, _label) in enumerate(categories):
            self.insert_flag(
                f"QF-CAT-{index}",
                reporter_user_id=1,
                status="open",
                category=code,
                comment=f"Category {code}",
                created_ts=123457 + index,
            )
        self.login_as(1, "teacher1", "teacher")

        for code, label in categories:
            page = self.client.get(f"/teacher/question_flags?category={code}")
            self.assertEqual(page.status_code, 200)
            self.assertIn(label.encode(), page.data)
            self.assertIn(f"Category {code}".encode(), page.data)

        invalid_page = self.client.get("/teacher/question_flags?category=bad_code")
        self.assertIn(b"All categories", invalid_page.data)
        self.assertIn(b"Category incorrect_answer", invalid_page.data)
        self.assertIn(b"Category other", invalid_page.data)

    def test_status_and_category_filters_work_together_with_empty_states(self):
        self.insert_flag("QF-SENT-VISUAL", reporter_user_id=1, status="escalated", category="visual_problem")
        self.insert_flag("QF-OPEN-OTHER", reporter_user_id=1, status="open", category="other")
        self.login_as(1, "teacher1", "teacher")

        sent_visual = self.client.get("/teacher/question_flags?filter=sent&category=visual_problem")
        current_visual = self.client.get("/teacher/question_flags?filter=current&category=visual_problem")
        resolved = self.client.get("/teacher/question_flags?filter=resolved")

        self.assertIn(b"Seeded flag.", sent_visual.data)
        self.assertNotIn(b"QF-OPEN-OTHER", sent_visual.data)
        self.assertIn(b"No current reports match The picture or model has a problem.", current_visual.data)
        self.assertIn(b"No resolved reports.", resolved.data)

    def test_record_attempt_uses_shared_question_reference_and_content_first_labels(self):
        self.grant_owner()
        self.login_as(1, "teacher1", "teacher")

        page = self.client.get("/teacher/question_preview?question_id=Q1B")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Teacher Question Preview", page.data)
        self.assertIn(b"Launch question 1B", page.data)
        self.assertIn(b"MS-LS1-1B", page.data)
        self.assertIn(b"Cells form tissues.", page.data)
        self.assertIn(b"Q1B", page.data)
        self.assertIn(
            b"Launch question 1B \xe2\x80\x94 MS-LS1-1B \xe2\x80\xa2 Q1B",
            page.data,
        )
        self.assertIn(
            b"/teacher/question_preview?question_id=Q1B",
            page.data,
        )
        self.assertNotIn(b"Q1B - Launch question 1B", page.data)
        self.assertIn(b"Read-only student rendering for teacher review.", page.data)

    def test_teacher_preview_objective_is_authoritative_over_stale_question(self):
        self.login_as(1, "teacher1", "teacher")
        before = {
            table: self.table_count(table)
            for table in (
                "attempts",
                "responses",
                "progress_state",
                "student_objective_state",
            )
        }

        matching = self.client.get(
            "/teacher/question_preview",
            query_string={"objective_id": "MS-LS1-1B", "question_id": "Q1B"},
        )
        self.assertEqual(matching.status_code, 200)
        self.assertIn(b'<option value="MS-LS1-1B" selected>', matching.data)
        self.assertIn(b'<option value="Q1B" selected>', matching.data)

        switched = self.client.get(
            "/teacher/question_preview",
            query_string={"objective_id": "MS-LS1-1A", "question_id": "Q1B"},
        )
        self.assertEqual(switched.status_code, 200)
        self.assertIn(b'<option value="MS-LS1-1A" selected>', switched.data)
        self.assertIn(b'<option value="Q1A" selected>', switched.data)
        self.assertNotIn(b'<option value="Q1B"', switched.data)
        self.assertIn(
            b"this.form.elements.question_id.disabled=true",
            switched.data,
        )

        refreshed = self.client.get("/teacher/question_preview")
        self.assertIn(b'<option value="MS-LS1-1A" selected>', refreshed.data)
        self.assertIn(b'<option value="Q1A" selected>', refreshed.data)
        self.assertNotIn(b'<option value="Q1B"', refreshed.data)
        self.assertEqual(
            {table: self.table_count(table) for table in before},
            before,
        )

    def test_teacher_preview_invalid_ids_fall_back_safely(self):
        self.login_as(1, "teacher1", "teacher")

        invalid_objective = self.client.get(
            "/teacher/question_preview",
            query_string={"objective_id": "NOT-AN-OBJECTIVE", "question_id": "Q1B"},
        )
        self.assertEqual(invalid_objective.status_code, 200)
        self.assertIn(b'<option value="MS-LS1-1A" selected>', invalid_objective.data)
        self.assertIn(b'<option value="Q1A" selected>', invalid_objective.data)
        self.assertNotIn(b'<option value="Q1B"', invalid_objective.data)

        invalid_question = self.client.get(
            "/teacher/question_preview",
            query_string={"objective_id": "MS-LS1-1B", "question_id": "NOT-A-QUESTION"},
        )
        self.assertEqual(invalid_question.status_code, 200)
        self.assertIn(b'<option value="MS-LS1-1B" selected>', invalid_question.data)
        self.assertIn(b'<option value="Q1B" selected>', invalid_question.data)

        question_only = self.client.get(
            "/teacher/question_preview",
            query_string={"question_id": "Q1A"},
        )
        self.assertIn(b'<option value="MS-LS1-1A" selected>', question_only.data)
        self.assertIn(b'<option value="Q1A" selected>', question_only.data)

    def test_record_attempt_question_reference_falls_back_without_objective_title(self):
        self.conn.execute(
            "UPDATE objectives SET objective_text = '' WHERE objective_id = 'MS-LS1-1B'"
        )
        self.conn.commit()
        self.grant_owner()
        self.login_as(1, "teacher1", "teacher")

        page = self.client.get("/teacher/question_preview?question_id=Q1B")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Launch question 1B", page.data)
        self.assertIn(b"MS-LS1-1B", page.data)
        self.assertIn(b"Q1B", page.data)
        self.assertNotIn(b"Cells form tissues.", page.data)
        self.assertNotIn(b"No objective text", page.data)

    def test_record_attempt_missing_question_rejected_and_state_preserved(self):
        self.grant_owner()
        self.login_as(1, "teacher1", "teacher")
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

        response = self.client.post(
            "/dashboard",
            data={
                "action": "save_attempt",
                "student_id": "S1",
                "objective_id": "MS-LS1-1A",
                "class_objective": "MS-LS1-1A",
                "period": "ALL",
                "question_id": "Q_RETIRED",
                "response": "B",
            },
            follow_redirects=True,
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

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Selected question is no longer available. Choose an available launch question before saving an attempt.",
            response.data,
        )
        self.assertNotIn(b"Q_RETIRED", response.data)
        self.assertEqual(after, before)

    def test_record_attempt_success_still_records_selected_question(self):
        self.grant_owner()
        self.login_as(1, "teacher1", "teacher")

        response = self.client.post(
            "/dashboard",
            data={
                "action": "save_attempt",
                "student_id": "S1",
                "objective_id": "MS-LS1-1B",
                "class_objective": "MS-LS1-1B",
                "period": "ALL",
                "question_id": "Q1B",
                "response": "A",
            },
            follow_redirects=False,
        )
        attempt = self.conn.execute(
            "SELECT * FROM attempts WHERE question_id = 'Q1B'"
        ).fetchone()
        response_row = self.conn.execute(
            "SELECT * FROM responses WHERE question_id = 'Q1B'"
        ).fetchone()

        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(attempt)
        self.assertIsNotNone(response_row)
        self.assertEqual(attempt["student_id"], "S1")
        self.assertEqual(response_row["correct"], 1)

    def test_teacher_student_mode_advances_in_session_and_clears_on_exit(self):
        self.conn.execute(
            """
            INSERT INTO questions
              (question_id, objective_id, stem, choice_a, choice_b, choice_c,
               choice_d, answer_key)
            VALUES ('Q1A-SECOND', 'MS-LS1-1A', 'Launch question 1A second',
                    'A', 'B', 'C', 'D', 'B')
            """
        )
        self.conn.commit()
        self.login_as(1, "teacher1", "teacher")
        before = {
            table: self.table_count(table)
            for table in (
                "attempts",
                "responses",
                "progress_state",
                "student_objective_state",
                "student_growth_progress",
                "student_question_deliveries",
                "routing_level_attempts",
            )
        }
        page = self.client.get("/student?student_id=S1&objective_id=MS-LS1-1A")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Student Mode Preview", page.data)
        self.assertIn(b"Launch question 1A", page.data)
        answer = self.client.post(
            "/student",
            data={
                "action": "answer",
                "student_id": "S1",
                "objective_id": "MS-LS1-1A",
                "question_id": "Q1A",
                "response": "A",
            },
        )
        self.assertEqual(answer.status_code, 200)
        self.assertNotIn(b"Preview only", answer.data)
        self.assertNotIn(b"Engine (standard-level)", answer.data)
        self.assertNotIn(b"Debug target", answer.data)
        self.assertIn(b"Launch question 1A second", answer.data)
        self.assertNotIn(
            b'name="question_id" value="Q1A"',
            answer.data,
        )
        self.assertIn(b"Leave Student Mode", answer.data)
        after = {table: self.table_count(table) for table in before}
        self.assertEqual(after, before)
        with self.client.session_transaction() as preview_session:
            preview_state = preview_session["student_mode_preview"]
            self.assertEqual(
                [item["question_id"] for item in preview_state["responses"]],
                ["Q1A"],
            )
            self.assertEqual(
                preview_state["current_question_id"],
                "Q1A-SECOND",
            )
            exit_token = preview_session["owner_csrf_token"]
        dashboard = self.client.post(
            "/student",
            data={"action": "exit_preview", "csrf_token": exit_token},
            follow_redirects=True,
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"RootED Teacher Dashboard", dashboard.data)
        self.assertNotIn(b"Developer / Debug", dashboard.data)
        with self.client.session_transaction() as ended_session:
            self.assertNotIn("student_mode_preview", ended_session)

    def test_teacher_student_mode_adaptive_routing_remains_session_only(self):
        self.conn.executemany(
            """
            INSERT INTO questions
              (question_id, objective_id, stem, choice_a, choice_b, choice_c,
               choice_d, answer_key)
            VALUES (?, 'MS-LS1-1A', ?, 'A', 'B', 'C', 'D', 'A')
            """,
            [
                (f"Q1A-{number}", f"Preview adaptive question {number}")
                for number in range(2, 9)
            ],
        )
        self.conn.commit()
        self.login_as(1, "teacher1", "teacher")
        protected_tables = (
            "attempts",
            "responses",
            "progress_state",
            "student_objective_state",
            "student_growth_progress",
            "student_question_deliveries",
            "routing_level_attempts",
        )
        before = {table: self.table_count(table) for table in protected_tables}
        page = self.client.get("/student?student_id=S1&objective_id=MS-LS1-1A")

        delivered = []
        for _ in range(7):
            match = re.search(
                rb'name="question_id" value="([^"]+)"',
                page.data,
            )
            self.assertIsNotNone(match)
            question_id = match.group(1).decode()
            if delivered:
                self.assertNotEqual(question_id, delivered[-1])
            delivered.append(question_id)
            page = self.client.post(
                "/student",
                data={
                    "action": "answer",
                    "student_id": "S1",
                    "objective_id": "MS-LS1-1A",
                    "question_id": question_id,
                    "response": "A",
                },
            )
            self.assertEqual(page.status_code, 200)

        with self.client.session_transaction() as preview_session:
            preview_state = preview_session["student_mode_preview"]
            self.assertEqual(preview_state["level"], 2)
            self.assertEqual(
                preview_state["routing_history"][-1]["action"],
                "advance_level",
            )
        self.assertEqual(
            {table: self.table_count(table) for table in protected_tables},
            before,
        )

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

    def test_resolve_and_escalate_move_flags_between_filters_and_update_badge(self):
        self.insert_flag(
            "QF-TO-RESOLVE",
            reporter_user_id=3,
            reporter_role="student",
            class_id="C1",
            student_id="S1",
            category="confusing_question",
            comment="Resolve me.",
            status="open",
        )
        self.insert_flag(
            "QF-TO-SEND",
            reporter_user_id=1,
            reporter_role="teacher",
            category="visual_problem",
            comment="Send me.",
            status="open",
            created_ts=123458,
        )
        self.login_as(1, "teacher1", "teacher")

        before_dashboard = self.client.get("/dashboard")
        self.client.post(
            "/teacher/question_flags/QF-TO-RESOLVE/resolve",
            data={
                "filter": "current",
                "category": "all",
                "resolution_note": "Handled in class.",
            },
        )
        self.client.post(
            "/teacher/question_flags/QF-TO-SEND/escalate",
            data={
                "filter": "current",
                "category": "all",
                "escalation_note": "Needs RootED Support.",
            },
        )
        current = self.client.get("/teacher/question_flags?filter=current")
        resolved = self.client.get("/teacher/question_flags?filter=resolved")
        sent = self.client.get("/teacher/question_flags?filter=sent")
        after_dashboard = self.client.get("/dashboard")

        self.assertIn(b"2 open question reports requiring action", before_dashboard.data)
        self.assertIn(b"No open question reports.", current.data)
        self.assertIn(b"Resolve me.", resolved.data)
        self.assertIn(b"Send me.", sent.data)
        self.assertNotIn(b'<span class="action-badge"', after_dashboard.data)

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
