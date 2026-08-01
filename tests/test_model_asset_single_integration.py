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
IMPORT_DB_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
IMPORT_DB = Path(IMPORT_DB_DIR.name) / f"test_model_asset_import_{uuid.uuid4().hex}.db"
os.environ["NGSS_DB"] = str(IMPORT_DB)
os.environ["SECRET_KEY"] = "test-secret"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

dash = importlib.import_module("app_core.dashboard_v5")


MODEL_ID = "MS-LS1-1B_model_cell_theory_basic_01"
QUESTION_ID = "Q_MSLS1_1_B_006"
MODEL_FILENAME = "ms-ls1-1b-model-cell-theory-basic-01.png"
MODEL_SRC = f"model_assets/{MODEL_FILENAME}"
MODEL_ALT = "A model showing living things made of cells to support basic cell theory."
MODEL_TITLE = "Basic Cell Theory Model"
MODEL_DISPLAY_TITLE = "Animal Cell"
MODEL_CAPTION = (
    "Supports questions about evidence for the idea that organisms are made of cells."
)
INSTRUCTIONAL_CAPTION = "Not to scale; colors show different cell parts."
QUESTION_STEM = (
    "In a cell model, what should the student focus on when deciding whether "
    "the model supports cell theory?"
)


class SingleModelAssetIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / f"test_model_asset_{uuid.uuid4().hex}.db"
        dash.DB = str(self.db_path)
        dash.ae.DB_PATH = str(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        dash.ensure_schema(self.conn)
        self.ensure_model_asset_tables()
        self.clear_tables()
        self.seed_data()
        dash.app.config.update(TESTING=True)
        self.client = dash.app.test_client()

    def tearDown(self):
        self.conn.close()
        gc.collect()
        self.temp_dir.cleanup()

    @classmethod
    def tearDownClass(cls):
        boot_conn = getattr(dash, "_conn_boot", None)
        if boot_conn is not None:
            try:
                boot_conn.close()
            except sqlite3.Error:
                pass
        gc.collect()
        IMPORT_DB_DIR.cleanup()

    def ensure_model_asset_tables(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS question_metadata (
              question_id       TEXT PRIMARY KEY,
              difficulty        TEXT,
              question_type     TEXT,
              explanation       TEXT,
              identifier_tags   TEXT,
              remediation_tags  TEXT,
              model_id          TEXT,
              source_artifact   TEXT,
              imported_at       INTEGER
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_assets (
              model_id   TEXT PRIMARY KEY CHECK (TRIM(model_id) <> ''),
              asset_type TEXT NOT NULL CHECK (asset_type IN ('image')),
              src        TEXT NOT NULL CHECK (TRIM(src) <> ''),
              alt_text   TEXT NOT NULL,
              title      TEXT,
              caption    TEXT,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            )
            """
        )
        self.conn.commit()

    def clear_tables(self):
        for table in [
            "student_question_deliveries",
            "student_growth_progress",
            "student_objective_state",
            "progress_state",
            "responses",
            "attempts",
            "routing_level_attempts",
            "class_enrollments",
            "class_sections",
            "user_instructional_authorizations",
            "user_platform_roles",
            "users",
            "students",
            "question_metadata",
            "model_assets",
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
        self.conn.executemany(
            """
            INSERT INTO objectives
              (objective_id, standard_id, objective_text, order_in_band)
            VALUES (?, 'MS-LS1-1', ?, ?)
            """,
            [
                ("MS-LS1-1A", "Cells are living units.", 1),
                ("MS-LS1-1B", "Cells support cell theory.", 2),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO questions
              (question_id, objective_id, stem, choice_a, choice_b, choice_c,
               choice_d, answer_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    QUESTION_ID,
                    "MS-LS1-1B",
                    QUESTION_STEM,
                    "Whether the model is colorful",
                    "Whether the model shows that living things are made of cells",
                    "Whether the model includes every atom in the organism",
                    "Whether the model is larger than the real organism",
                    "B",
                ),
                (
                    "Q_UNRELATED_NO_MODEL",
                    "MS-LS1-1A",
                    "Which statement best describes a cell?",
                    "A living unit",
                    "A rock",
                    "A cloud",
                    "A planet",
                    "A",
                ),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO question_metadata
              (question_id, difficulty, question_type, explanation,
               identifier_tags, remediation_tags, model_id, source_artifact,
               imported_at)
            VALUES (?, 'Application', 'multiple_choice', ?, '[]', '[]', ?, 'test', ?)
            """,
            [
                (
                    QUESTION_ID,
                    "Cell theory models should show that living things are made of cells.",
                    MODEL_ID,
                    now,
                ),
                (
                    "Q_UNRELATED_NO_MODEL",
                    "Cells are basic living units.",
                    None,
                    now,
                ),
            ],
        )
        self.conn.execute(
            """
            INSERT INTO model_assets
              (model_id, asset_type, src, alt_text, title, caption, created_at, updated_at)
            VALUES (?, 'image', ?, ?, ?, ?, ?, ?)
            """,
            (
                MODEL_ID,
                MODEL_SRC,
                MODEL_ALT,
                MODEL_TITLE,
                MODEL_CAPTION,
                now,
                now,
            ),
        )
        self.conn.executemany(
            """
            INSERT INTO students
              (student_id, first_name, last_name, grade, class_period)
            VALUES (?, ?, ?, 6, '1')
            """,
            [
                ("S1", "Ava", "Student"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO users
              (id, username, password_hash, role, linked_student_id, is_active)
            VALUES (?, ?, 'unused', ?, ?, 1)
            """,
            [
                (1, "teacher", "teacher", None),
                (2, "student", "student", "S1"),
            ],
        )
        self.conn.execute(
            """
            INSERT INTO user_instructional_authorizations
              (user_id, instructional_role, granted_at, grant_note)
            VALUES (1, 'teacher', ?, 'Test teacher')
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO user_platform_roles
              (user_id, platform_role, granted_at, grant_note)
            VALUES (1, 'owner', ?, 'Test owner')
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO class_sections
              (class_id, teacher_user_id, name, class_period, join_code,
               is_active, created_at, updated_at)
            VALUES ('C1', 1, 'Period 1', '1', 'AAA111', 1, ?, ?)
            """,
            (now, now),
        )
        self.conn.execute(
            """
            INSERT INTO class_enrollments
              (class_id, student_id, enrolled_at, enrolled_by, is_active)
            VALUES ('C1', 'S1', ?, 'teacher', 1)
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO progress_state
              (student_id, standard_id, current_level, status, rolling_avg,
               locked, locked_reason, last_update)
            VALUES ('S1', 'MS-LS1-1', 1, 'practicing', 0.0, 0, NULL, ?)
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO student_objective_state
              (student_id, standard_id, current_objective_id, status, last_update)
            VALUES ('S1', 'MS-LS1-1', 'MS-LS1-1B', 'active', ?)
            """,
            (now,),
        )
        self.conn.commit()

    def login_as_teacher(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["username"] = "teacher"
            session["role"] = "teacher"
            session["current_mode"] = "question"

    def login_as_student(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 2
            session["username"] = "student"
            session["role"] = "student"
            session["current_mode"] = "question"
            session["locked_payload"] = None

    def assert_model_asset_rendered(self, html: bytes):
        self.assertIn(b'<div class="assessment-body assessment-body--with-model">', html)
        self.assertIn(b'<figure class="model-asset">', html)
        self.assertIn(MODEL_DISPLAY_TITLE.encode(), html)
        self.assertNotIn(MODEL_TITLE.encode(), html)
        self.assertNotIn(MODEL_CAPTION.encode(), html)
        self.assertNotIn(b'<p class="model-asset-caption">', html)
        self.assertIn(MODEL_ALT.encode(), html)
        self.assertIn(f'src="/static/{MODEL_SRC}"'.encode(), html)
        self.assertIn(f'alt="{MODEL_ALT}"'.encode(), html)
        self.assertIn(f"Image description: {MODEL_ALT}".encode(), html)
        figure_index = html.index(b'<figure class="model-asset">')
        choice_markers = [
            marker
            for marker in (
                b'name="response"',
                b'name="preview_response"',
                b'name="owner_preview_response"',
            )
            if marker in html
        ]
        self.assertTrue(choice_markers)
        self.assertLess(html.index(QUESTION_STEM.encode()), figure_index)
        self.assertLess(figure_index, min(html.index(marker) for marker in choice_markers))

    def assert_shared_assessment_presentation(self, html: bytes):
        self.assertIn(b"<section class=\"assessment\" aria-labelledby=\"question-prompt\">", html)
        self.assertIn(
            b".assessment{width:100%;margin:0;background:#fff;border:1px solid #e5e7eb",
            html,
        )
        self.assertIn(b".assessment-question{max-width:85ch", html)
        self.assertIn(b".assessment-body{width:100%}", html)
        self.assertIn(
            b".assessment-body--with-model{display:grid;grid-template-columns:minmax(260px,42%) minmax(0,58%)",
            html,
        )
        self.assertIn(b".answer-form{max-width:920px;margin:0 auto}", html)
        self.assertIn(b".assessment-body--with-model .answer-form{max-width:none;margin:0}", html)
        self.assertIn(b".choice{margin:8px 0;padding:8px;border:1px solid #e5e7eb", html)
        self.assertIn(
            b".model-asset-title{margin:0 0 8px 0;font-weight:700;color:#1f2937;text-align:center}",
            html,
        )
        self.assertIn(
            b".model-asset{box-sizing:border-box;width:100%;max-width:500px;margin:12px auto",
            html,
        )
        self.assertIn(
            b".model-asset img{display:block;width:100%;max-width:500px;height:auto",
            html,
        )
        self.assertIn(
            b"@media(max-width:900px){.assessment-body--with-model{display:block}",
            html,
        )
        self.assertIn(b".assessment-question{max-width:none}.answer-form{max-width:none}", html)

    def assert_student_question_layout(self, html: bytes):
        self.assertIn(b"<main class=\"learning-shell\">", html)
        self.assertIn(b"<header class=\"learning-header\">", html)
        self.assertIn(b"<section class=\"growth ", html)
        self.assert_shared_assessment_presentation(html)
        self.assertIn(
            b".learning-shell{max-width:1120px;margin:0 auto 24px auto",
            html,
        )
        self.assertIn(
            b"@media(max-width:800px){body{margin:16px}.learning-header{flex-direction:column}",
            html,
        )
        self.assertIn(
            b"@media(max-width:600px){body{margin:12px}.learning-shell{max-width:none}",
            html,
        )
        self.assertNotIn(b".card{max-width:700px", html)
        self.assertNotIn(b"<div class=\"card\">", html)
        self.assertNotIn(b".assessment{max-width:760px", html)

    def test_repository_runtime_and_metadata_mapping_for_single_asset(self):
        canonical = (
            ROOT
            / "visual_library"
            / "model_assets"
            / "ms-ls1-1b"
            / "05_approved"
            / MODEL_FILENAME
        )
        export = (
            ROOT
            / "visual_library"
            / "model_assets"
            / "ms-ls1-1b"
            / "06_exports"
            / MODEL_FILENAME
        )
        runtime = ROOT / "app_core" / "static" / "model_assets" / MODEL_FILENAME

        self.assertTrue(canonical.is_file())
        self.assertTrue(export.is_file())
        self.assertTrue(runtime.is_file())
        self.assertEqual(canonical.read_bytes(), export.read_bytes())
        self.assertEqual(export.read_bytes(), runtime.read_bytes())

        metadata = self.conn.execute(
            "SELECT * FROM model_assets WHERE model_id = ?",
            (MODEL_ID,),
        ).fetchone()
        self.assertEqual(metadata["asset_type"], "image")
        self.assertEqual(metadata["src"], MODEL_SRC)
        self.assertEqual(metadata["title"], MODEL_TITLE)
        self.assertEqual(metadata["alt_text"], MODEL_ALT)
        self.assertEqual(metadata["caption"], MODEL_CAPTION)

        resolved = dash.resolve_model_asset_for_question(self.conn, QUESTION_ID)
        render_asset = dash.static_image_asset_for_render(resolved)
        self.assertEqual(render_asset["filename"], MODEL_SRC)
        self.assertEqual(render_asset["alt_text"], MODEL_ALT)
        self.assertEqual(render_asset["title"], MODEL_DISPLAY_TITLE)
        self.assertEqual(render_asset["caption"], "")

    def test_teacher_question_preview_renders_asset_accessibly_and_responsively(self):
        self.login_as_teacher()
        before = self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        response = self.client.get(f"/teacher/question_preview?question_id={QUESTION_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Question Preview", response.data)
        self.assertIn(b"Teacher Preview", response.data)
        self.assertIn(b"Responses are disabled.", response.data)
        self.assertIn(b'name="objective_id"', response.data)
        self.assertIn(b'name="question_id"', response.data)
        self.assertIn(b"Back to Dashboard", response.data)
        self.assertIn(b".card{max-width:1120px", response.data)
        self.assertNotIn(b".card{max-width:760px", response.data)
        self.assert_shared_assessment_presentation(response.data)
        self.assertIn(b'name="preview_response" value="A" disabled', response.data)
        self.assertNotIn(b"Submit Answer", response.data)
        self.assert_model_asset_rendered(response.data)
        after = self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        self.assertEqual(after, before)

    def test_owner_question_preview_uses_shared_assessment_presentation(self):
        self.login_as_teacher()
        before = self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]

        response = self.client.get(f"/owner/question-preview?question_id={QUESTION_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Owner Question Preview", response.data)
        self.assertIn(b"Responses are disabled.", response.data)
        self.assertIn(b".card{max-width:1120px", response.data)
        self.assert_shared_assessment_presentation(response.data)
        self.assertIn(b'name="owner_preview_response" value="A" disabled', response.data)
        self.assertNotIn(b"Submit Answer", response.data)
        self.assert_model_asset_rendered(response.data)
        after = self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        self.assertEqual(after, before)

    def test_teacher_student_mode_renders_asset_without_recording_real_attempts(self):
        self.login_as_teacher()
        before = self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]

        response = self.client.get("/student?student_id=S1&objective_id=MS-LS1-1B")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Student Mode Preview", response.data)
        self.assert_student_question_layout(response.data)
        self.assert_model_asset_rendered(response.data)
        after = self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        self.assertEqual(after, before)

    def test_authenticated_student_flow_renders_asset_and_keeps_submission_working(self):
        self.login_as_student()
        page = self.client.get("/student")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"RootED Learning", page.data)
        self.assert_student_question_layout(page.data)
        self.assert_model_asset_rendered(page.data)

        token_match = re.search(
            rb'name="submission_token" value="([^"]+)"',
            page.data,
        )
        self.assertIsNotNone(token_match)
        submission = self.client.post(
            "/student",
            data={
                "action": "answer",
                "question_id": QUESTION_ID,
                "submission_token": token_match.group(1).decode(),
                "response": "B",
            },
            follow_redirects=True,
        )

        self.assertEqual(submission.status_code, 200)
        recorded = self.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM attempts
            WHERE student_id = 'S1'
              AND question_id = ?
              AND response = 'B'
              AND is_correct = 1
            """,
            (QUESTION_ID,),
        ).fetchone()["n"]
        self.assertEqual(recorded, 1)

    def test_authenticated_student_layout_without_model_asset(self):
        self.conn.execute(
            """
            UPDATE student_objective_state
            SET current_objective_id = 'MS-LS1-1A'
            WHERE student_id = 'S1'
              AND standard_id = 'MS-LS1-1'
            """
        )
        self.conn.commit()
        self.login_as_student()

        page = self.client.get("/student")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Which statement best describes a cell?", page.data)
        self.assert_student_question_layout(page.data)
        self.assertNotIn(b'<figure class="model-asset">', page.data)
        self.assertIn(b'<div class="assessment-body">', page.data)
        self.assertNotIn(b'assessment-body assessment-body--with-model', page.data)
        self.assertIn(b'name="response"', page.data)

    def test_missing_static_file_falls_back_to_plain_question_rendering(self):
        self.conn.execute(
            "UPDATE model_assets SET src = ? WHERE model_id = ?",
            ("model_assets/does-not-exist.png", MODEL_ID),
        )
        self.conn.commit()
        self.login_as_teacher()

        response = self.client.get(f"/teacher/question_preview?question_id={QUESTION_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(QUESTION_STEM.encode(), response.data)
        self.assertIn(b'name="preview_response"', response.data)
        self.assertNotIn(b'<figure class="model-asset">', response.data)
        self.assertNotIn(b"does-not-exist.png", response.data)

    def test_empty_or_missing_caption_is_not_rendered(self):
        self.login_as_teacher()
        for caption in ("", None):
            self.conn.execute(
                "UPDATE model_assets SET caption = ? WHERE model_id = ?",
                (caption, MODEL_ID),
            )
            self.conn.commit()

            response = self.client.get(f"/teacher/question_preview?question_id={QUESTION_ID}")

            self.assertEqual(response.status_code, 200)
            self.assertIn(MODEL_DISPLAY_TITLE.encode(), response.data)
            self.assertNotIn(b'<p class="model-asset-caption">', response.data)

    def test_instructionally_necessary_caption_is_rendered(self):
        self.conn.execute(
            "UPDATE model_assets SET caption = ? WHERE model_id = ?",
            (INSTRUCTIONAL_CAPTION, MODEL_ID),
        )
        self.conn.commit()
        self.login_as_teacher()

        response = self.client.get(f"/teacher/question_preview?question_id={QUESTION_ID}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(MODEL_DISPLAY_TITLE.encode(), response.data)
        self.assertIn(b'class="model-asset-caption"', response.data)
        self.assertIn(INSTRUCTIONAL_CAPTION.encode(), response.data)

    def test_unrelated_question_does_not_reuse_the_single_asset(self):
        self.login_as_teacher()

        response = self.client.get(
            "/teacher/question_preview?question_id=Q_UNRELATED_NO_MODEL"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Which statement best describes a cell?", response.data)
        self.assertNotIn(b'<figure class="model-asset">', response.data)
        self.assertNotIn(MODEL_FILENAME.encode(), response.data)
        resolved = dash.resolve_model_asset_for_question(
            self.conn,
            "Q_UNRELATED_NO_MODEL",
        )
        self.assertEqual(resolved["missing_reason"], "no_model_id")


if __name__ == "__main__":
    unittest.main()
