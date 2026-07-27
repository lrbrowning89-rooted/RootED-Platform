import importlib
import gc
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
TEST_DB = Path(TEST_DIR.name) / f"test_placement_{uuid.uuid4().hex}.db"
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
        boot_conn = getattr(dash, "_conn_boot", None)
        if boot_conn is not None:
            try:
                boot_conn.close()
            except sqlite3.Error:
                pass
        gc.collect()
        TEST_DIR.cleanup()

    def clear_seed_tables(self):
        for table in [
            "student_question_deliveries",
            "student_growth_progress",
            "student_objective_state",
            "progress_state",
            "attempts",
            "responses",
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

    def seed_content(self):
        now = 123456
        self.conn.executemany(
            "INSERT INTO standards (standard_id, core_idea, grade_band) VALUES (?, ?, ?)",
            [
                ("MS-LS1-1", "LS1", "MS"),
                ("MS-LS1-2", "LS1", "MS"),
                ("MS-LS1-3", "LS1", "MS"),
                ("MS-LS1-4", "LS1", "MS"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO objectives (objective_id, standard_id, objective_text, order_in_band)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("MS-LS1-1A", "MS-LS1-1", "Cells are living units.", 70),
                ("MS-LS1-1B", "MS-LS1-1", "Cells form tissues.", 60),
                ("MS-LS1-2A", "MS-LS1-2", "Cell systems.", 50),
                ("MS-LS1-2B", "MS-LS1-2", "Cells work together.", 40),
                ("MS-LS1-2C", "MS-LS1-2", "Obsolete nucleus objective.", 3),
                ("MS-LS1-2D", "MS-LS1-2", "Obsolete chloroplast objective.", 4),
                ("MS-LS1-2E", "MS-LS1-2", "Obsolete mitochondria objective.", 5),
                ("MS-LS1-2F", "MS-LS1-2", "Obsolete cell wall objective.", 6),
                ("MS-LS1-2G", "MS-LS1-2", "Obsolete cell system objective.", 7),
                ("MS-LS1-3A", "MS-LS1-3", "Tissues work together.", 30),
                ("MS-LS1-3B", "MS-LS1-3", "Organs work together.", 20),
                ("MS-LS1-3C", "MS-LS1-3", "Organ systems interact.", 10),
                ("MS-LS1-3E", "MS-LS1-3", "Obsolete body systems objective.", 5),
                ("MS-LS1-4A", "MS-LS1-4", "Non-launch sensory objective.", 1),
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
                ("Q1A2", "MS-LS1-1A", "Question 1A second"),
                ("Q1A3", "MS-LS1-1A", "Question 1A third"),
                ("Q1B", "MS-LS1-1B", "Question 1B"),
                ("Q2A", "MS-LS1-2A", "Question 2A"),
                ("Q2B", "MS-LS1-2B", "Question 2B"),
                ("Q2C", "MS-LS1-2C", "Obsolete Question 2C"),
                ("Q2D", "MS-LS1-2D", "Obsolete Question 2D"),
                ("Q2E", "MS-LS1-2E", "Obsolete Question 2E"),
                ("Q2F", "MS-LS1-2F", "Obsolete Question 2F"),
                ("Q2G", "MS-LS1-2G", "Obsolete Question 2G"),
                ("Q3A", "MS-LS1-3A", "Question 3A"),
                ("Q3B", "MS-LS1-3B", "Question 3B"),
                ("Q3C", "MS-LS1-3C", "Question 3C"),
                ("Q3E", "MS-LS1-3E", "Obsolete Question 3E"),
                ("Q4A", "MS-LS1-4A", "Non-launch Question 4A"),
            ]
            + [
                (f"Q2A{i:02d}", "MS-LS1-2A", f"Question 2A {i}")
                for i in range(2, 22)
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

    def active_delivery(self, student_id="S1"):
        return self.conn.execute(
            """
            SELECT *
            FROM student_question_deliveries
            WHERE student_id = ?
              AND consumed_at IS NULL
              AND invalidated_at IS NULL
            """,
            (student_id,),
        ).fetchone()

    def open_student_question(self):
        self.login_as(3, "student1", "student")
        with self.client.session_transaction() as sess:
            sess["current_mode"] = "question"
        page = self.client.get("/student")
        return page, self.active_delivery()

    def place_for_matrix(self, standard_id, objective_id, level=1):
        ok, message = dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id=standard_id,
            objective_id=objective_id,
            current_level=level,
            placed_by_user_id=1,
        )
        self.assertTrue(ok, message)
        return self.active_routing_attempt()

    def active_routing_attempt(self):
        return self.conn.execute(
            """
            SELECT *
            FROM routing_level_attempts
            WHERE student_id='S1' AND status='active'
            """
        ).fetchone()

    def routing_metrics(self, attempt_id):
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS response_count,
                   COALESCE(SUM(correct), 0) AS correct_count
            FROM responses
            WHERE routing_level_attempt_id=?
            """,
            (attempt_id,),
        ).fetchone()
        count = int(row["response_count"])
        correct = int(row["correct_count"])
        return correct, count, (correct / count if count else 0.0)

    def submit_matrix_answer(self, correct, delivery=None, follow_redirect=True):
        if delivery is None:
            delivery = self.active_delivery()
        if delivery is None:
            _, delivery = self.open_student_question()
        payload = {
            "action": "answer",
            "question_id": delivery["question_id"],
            "submission_token": delivery["submission_token"],
            "response": "A" if correct else "B",
        }
        response = self.client.post("/student", data=payload)
        self.assertEqual(response.status_code, 302)
        if follow_redirect:
            self.client.get(response.headers["Location"])
        return delivery, payload, response

    def assert_matrix_database_integrity(self):
        orphaned = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM responses r
            LEFT JOIN routing_level_attempts rla
              ON rla.attempt_id = r.routing_level_attempt_id
            WHERE r.student_id='S1'
              AND (r.routing_level_attempt_id IS NULL OR rla.attempt_id IS NULL)
            """
        ).fetchone()[0]
        self.assertEqual(orphaned, 0)

        attempts = self.conn.execute(
            """
            SELECT *
            FROM routing_level_attempts
            WHERE student_id='S1'
            ORDER BY started_at, attempt_id
            """
        ).fetchall()
        for attempt in attempts:
            mismatched = self.conn.execute(
                """
                SELECT COUNT(*)
                FROM responses r
                JOIN questions q ON q.question_id=r.question_id
                JOIN objectives o ON o.objective_id=q.objective_id
                WHERE r.routing_level_attempt_id=?
                  AND (
                    r.student_id<>?
                    OR r.standard_id<>?
                    OR q.objective_id<>?
                    OR o.standard_id<>?
                    OR r.level<>?
                  )
                """,
                (
                    attempt["attempt_id"],
                    attempt["student_id"],
                    attempt["standard_id"],
                    attempt["objective_id"],
                    attempt["standard_id"],
                    attempt["level"],
                ),
            ).fetchone()[0]
            self.assertEqual(mismatched, 0)

            correct, count, score = self.routing_metrics(attempt["attempt_id"])
            if attempt["status"] == "closed":
                self.assertEqual(attempt["final_correct_count"], correct)
                self.assertEqual(attempt["final_response_count"], count)
                self.assertAlmostEqual(float(attempt["final_score"]), score)

        accepted_attempts = self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE student_id='S1'"
        ).fetchone()[0]
        accepted_responses = self.conn.execute(
            "SELECT COUNT(*) FROM responses WHERE student_id='S1'"
        ).fetchone()[0]
        self.assertEqual(accepted_attempts, accepted_responses)

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
        self.assertIn(b"Cells form tissues.", page.data)
        self.assertNotIn(b"MS-LS1-1B", page.data)
        self.assertNotIn(b"level 2", page.data)

    def test_student_question_page_hides_internal_metadata_and_keeps_actions(self):
        self.login_as(3, "student1", "student")
        with self.client.session_transaction() as sess:
            sess["current_mode"] = "question"
        page = self.client.get("/student")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"RootED Learning", page.data)
        self.assertIn(b"Back to Home", page.data)
        self.assertIn(b"Submit Answer", page.data)
        self.assertIn(b"Report a problem", page.data)
        self.assertIn(b"Cells are living units.", page.data)
        for internal_text in [
            b"Student Practice TEST-16",
            b"role =",
            b"engine-selected",
            b"MS-LS1-1B",
            b"join_class",
            b"Class code",
            b"Enrolled:",
        ]:
            self.assertNotIn(internal_text, page.data)

    def test_student_growth_component_replaces_fixed_question_progress(self):
        self.login_as(3, "student1", "student")
        with self.client.session_transaction() as sess:
            sess["current_mode"] = "question"

        page = self.client.get("/student")
        html = page.get_data(as_text=True)

        self.assertIn("Growing Understanding", html)
        self.assertIn('<span class="growth-status">Growing</span>', html)
        self.assertIn("You&#39;re building your understanding.", html)
        self.assertNotIn("Strengthening", html)
        self.assertIn("growth-fill growth-seed", html)
        self.assertIn("Learning Goal", html)
        self.assertNotIn("Current focus", html)
        self.assertIn('class="growth-focus-text">Cells are living units.</span>', html)
        self.assertIn('class="assessment-label">Assessment question</p>', html)
        self.assertIn(
            '<h2 class="assessment-question" id="question-prompt">Question 1A</h2>',
            html,
        )
        self.assertLess(html.index("Learning Goal"), html.index("Assessment question"))
        self.assertEqual(html.count("Cells are living units."), 1)
        self.assertIn('role="progressbar"', html)
        self.assertIn(
            'aria-valuetext="You&#39;re building your understanding."', html
        )
        self.assertNotRegex(html, r"Question\s+\d+\s+of\s+\d+")
        self.assertNotRegex(html, r">\s*\d+\s*%\s*<")
        self.assertNotRegex(html, r">\s*\d+\s*/\s*\d+\s*<")
        self.assertNotIn("Rolling-7", html)
        self.assertNotIn("Responses at this level", html)
        self.assertNotIn("aria-valuenow", html)
        self.assertNotIn("mastery_threshold", html)
        self.assertNotIn("remediation_threshold", html)

    def test_all_student_growth_states_can_render_without_color(self):
        attempt = dash.ensure_student_growth_attempt(
            self.conn, "S1", "MS-LS1-1", "MS-LS1-1A"
        )
        expected = {
            "growing": ("Growing", "You're building your understanding."),
            "strengthening": ("Strengthening", "Let's strengthen this idea."),
            "reviewing": ("Reviewing", "RootED is helping you review this concept."),
        }
        for state, (label, message) in expected.items():
            self.conn.execute(
                """
                UPDATE student_growth_progress
                SET presentation_state = ?
                WHERE attempt_id = ?
                """,
                (state, attempt["attempt_id"]),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM student_growth_progress WHERE attempt_id = ?",
                (attempt["attempt_id"],),
            ).fetchone()
            view = dash.student_growth_view_model(row)
            self.assertEqual(view["label"], label)
            self.assertEqual(view["message"], message)

    def test_strongest_non_complete_growth_stage_uses_connection_copy(self):
        attempt = dash.ensure_student_growth_attempt(
            self.conn, "S1", "MS-LS1-1", "MS-LS1-1A"
        )
        self.conn.execute(
            """
            UPDATE student_growth_progress
            SET visible_stage = 3.85, presentation_state = 'growing'
            WHERE attempt_id = ?
            """,
            (attempt["attempt_id"],),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM student_growth_progress WHERE attempt_id = ?",
            (attempt["attempt_id"],),
        ).fetchone()

        view = dash.student_growth_view_model(row)

        self.assertEqual(view["state"], "growing")
        self.assertEqual(view["message"], "You're connecting these ideas.")

    def test_positive_evidence_advances_in_small_distinct_steps_until_completion(self):
        attempt = dash.ensure_student_growth_attempt(
            self.conn, "S1", "MS-LS1-1", "MS-LS1-1A"
        )
        stages = []
        stage_classes = []
        current = attempt
        initial_class = dash.student_growth_view_model(attempt)["stage_class"]
        for _ in range(9):
            current = dash.update_student_growth_presentation(
                self.conn,
                attempt["attempt_id"],
                correct=True,
                engine_decision={"status": "question", "action": "serve_practice"},
            )
            stages.append(float(current["visible_stage"]))
            stage_classes.append(dash.student_growth_view_model(current)["stage_class"])

        self.assertTrue(all(later > earlier for earlier, later in zip(stages, stages[1:])))
        self.assertEqual(len(stage_classes), len(set(stage_classes)))
        self.assertEqual(len({initial_class, *stage_classes}), 10)
        self.assertLess(stages[-1], 4)
        self.assertNotEqual(stage_classes[-1], "growth-complete")

        completed = dash.update_student_growth_presentation(
            self.conn,
            attempt["attempt_id"],
            correct=True,
            engine_decision={"status": "complete", "action": "standard_complete"},
        )
        completed_view = dash.student_growth_view_model(completed)
        self.assertEqual(float(completed["visible_stage"]), 4)
        self.assertEqual(completed_view["stage_class"], "growth-complete")

    def test_objective_transition_resets_bar_and_preserves_plant_growth(self):
        ok, message = dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1A",
            current_level=3,
            placed_by_user_id=1,
        )
        self.assertTrue(ok, message)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS standard_links (
              id INTEGER PRIMARY KEY,
              from_standard_id TEXT NOT NULL,
              to_standard_id TEXT NOT NULL,
              link_type TEXT NOT NULL,
              priority INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.conn.execute("DELETE FROM standard_links")
        old_attempt = self.conn.execute(
            "SELECT * FROM student_growth_progress WHERE student_id='S1' AND is_active=1"
        ).fetchone()
        level_attempt = self.conn.execute(
            """
            SELECT attempt_id FROM routing_level_attempts
            WHERE student_id='S1' AND status='active'
            """
        ).fetchone()
        for offset in range(6):
            self.conn.execute(
                """
                INSERT INTO responses
                  (student_id, standard_id, level, question_id, correct, ts,
                   routing_level_attempt_id)
                VALUES ('S1', 'MS-LS1-1', 3, 'Q1A', 1, ?, ?)
                """,
                (200000 + offset, level_attempt["attempt_id"]),
            )
        self.conn.commit()
        self.login_as(3, "student1", "student")
        with self.client.session_transaction() as sess:
            sess["current_mode"] = "question"
        self.client.get("/student")
        delivery = self.conn.execute(
            """
            SELECT * FROM student_question_deliveries
            WHERE student_id='S1' AND consumed_at IS NULL AND invalidated_at IS NULL
            """
        ).fetchone()

        transition_page = self.client.post(
            "/student",
            data={
                "action": "answer",
                "question_id": delivery["question_id"],
                "submission_token": delivery["submission_token"],
                "response": "A",
            },
            follow_redirects=True,
        )
        html = transition_page.get_data(as_text=True)
        completed = self.conn.execute(
            "SELECT * FROM student_growth_progress WHERE attempt_id=?",
            (old_attempt["attempt_id"],),
        ).fetchone()
        active = self.conn.execute(
            "SELECT * FROM student_growth_progress WHERE student_id='S1' AND is_active=1"
        ).fetchone()

        self.assertEqual(completed["presentation_state"], "complete")
        self.assertEqual(float(completed["visible_stage"]), 4)
        self.assertEqual(active["objective_id"], "MS-LS1-1B")
        self.assertEqual(float(active["visible_stage"]), 1)
        self.assertIn("Cells form tissues.", html)
        self.assertIn("A new branch of learning is beginning.", html)
        self.assertIn("Your learning plant has grown a new branch.", html)
        self.assertIn("growth-fill growth-seed", html)
        self.assertNotIn("MS-LS1-1B", html)

        refreshed = self.client.get("/student").get_data(as_text=True)
        self.assertNotIn("A new branch of learning is beginning.", refreshed)
        self.assertIn("Your learning plant has grown a new branch.", refreshed)
        refreshed_attempt = self.conn.execute(
            "SELECT * FROM student_growth_progress WHERE student_id='S1' AND is_active=1"
        ).fetchone()
        self.assertEqual(refreshed_attempt["attempt_id"], active["attempt_id"])
        self.assertEqual(refreshed_attempt["visible_stage"], active["visible_stage"])

    def test_cumulative_plant_condenses_multiple_completed_objectives(self):
        objective_ids = [
            "MS-LS1-2A",
            "MS-LS1-2B",
            "MS-LS1-2C",
            "MS-LS1-2D",
            "MS-LS1-2E",
        ]
        expected_classes = [
            "plant-first-branch",
            "plant-second-branch",
            "plant-leafy",
            "plant-leafy",
            "plant-canopy",
        ]
        for objective_id, expected_class in zip(objective_ids, expected_classes):
            attempt = dash.ensure_student_growth_attempt(
                self.conn, "S1", "MS-LS1-2", objective_id
            )
            dash.update_student_growth_presentation(
                self.conn,
                attempt["attempt_id"],
                correct=True,
                engine_decision={"status": "complete", "action": "objective_complete"},
            )
            plant = dash.cumulative_plant_view_model(self.conn, "S1", "MS-LS1-2")
            self.assertEqual(plant["plant_class"], expected_class)
            self.assertNotRegex(plant["visible_text"], r"\d")
            self.assertNotRegex(plant["screen_reader_text"], r"\d")

    def test_incorrect_answer_holds_growth_and_progress_persists(self):
        attempt = dash.ensure_student_growth_attempt(
            self.conn, "S1", "MS-LS1-1", "MS-LS1-1A"
        )
        grown = dash.update_student_growth_presentation(
            self.conn,
            attempt["attempt_id"],
            correct=True,
            engine_decision={"status": "question", "action": "collect_rolling7"},
        )
        held = dash.update_student_growth_presentation(
            self.conn,
            attempt["attempt_id"],
            correct=False,
            engine_decision={"status": "question", "action": "collect_rolling7"},
        )
        self.assertEqual(held["visible_stage"], grown["visible_stage"])
        held_view = dash.student_growth_view_model(held)
        self.assertEqual(held_view["state"], "strengthening")
        self.assertEqual(held_view["message"], "Let's strengthen this idea.")

        same_attempt = dash.ensure_student_growth_attempt(
            self.conn, "S1", "MS-LS1-1", "MS-LS1-1A"
        )
        self.assertEqual(same_attempt["attempt_id"], attempt["attempt_id"])
        self.assertEqual(same_attempt["visible_stage"], grown["visible_stage"])

    def test_teacher_placement_starts_a_new_objective_attempt(self):
        first = dash.ensure_student_growth_attempt(
            self.conn, "S1", "MS-LS1-1", "MS-LS1-1A"
        )
        dash.update_student_growth_presentation(
            self.conn,
            first["attempt_id"],
            correct=True,
            engine_decision={"status": "question", "action": "serve_practice"},
        )

        ok, message = dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1B",
            current_level=1,
            placed_by_user_id=1,
        )
        self.assertTrue(ok, message)
        active = self.conn.execute(
            "SELECT * FROM student_growth_progress WHERE student_id='S1' AND is_active=1"
        ).fetchone()
        self.assertNotEqual(active["attempt_id"], first["attempt_id"])
        self.assertEqual(active["objective_id"], "MS-LS1-1B")
        self.assertEqual(active["visible_stage"], 1)
        self.assertEqual(active["presentation_state"], "growing")

    def test_student_submission_keeps_existing_recording_and_routing_flow(self):
        self.login_as(3, "student1", "student")
        with self.client.session_transaction() as sess:
            sess["current_mode"] = "question"
        self.client.get("/student")
        delivery = self.conn.execute(
            """
            SELECT * FROM student_question_deliveries
            WHERE student_id='S1' AND consumed_at IS NULL AND invalidated_at IS NULL
            """
        ).fetchone()

        response = self.client.post(
            "/student",
            data={
                "action": "answer",
                "question_id": delivery["question_id"],
                "submission_token": delivery["submission_token"],
                "response": "A",
            },
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.headers["Location"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM attempts WHERE student_id='S1'").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM responses WHERE student_id='S1'").fetchone()[0],
            1,
        )
        self.assertIn("Your understanding is growing.", page.get_data(as_text=True))

    def test_authoritative_delivery_refresh_and_post_redirect_get(self):
        first_page, first_delivery = self.open_student_question()
        self.assertEqual(first_delivery["question_id"], "Q1A")
        refreshed = self.client.get("/student")
        refreshed_delivery = self.active_delivery()
        self.assertEqual(
            refreshed_delivery["submission_token"],
            first_delivery["submission_token"],
        )
        self.assertIn(first_delivery["submission_token"], refreshed.get_data(as_text=True))

        response = self.client.post(
            "/student",
            data={
                "action": "answer",
                "question_id": "Q1A",
                "submission_token": first_delivery["submission_token"],
                "response": "A",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM attempts WHERE student_id='S1'").fetchone()[0],
            1,
        )
        next_page = self.client.get(response.headers["Location"])
        next_delivery = self.active_delivery()
        self.assertEqual(next_delivery["question_id"], "Q1A2")
        self.assertIn("Question 1A second", next_page.get_data(as_text=True))

        browser_refresh = self.client.get(response.headers["Location"])
        self.assertEqual(browser_refresh.status_code, 200)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM attempts WHERE student_id='S1'").fetchone()[0],
            1,
        )

    def test_consumed_delivery_replay_is_atomic_and_changes_growth_once(self):
        _, delivery = self.open_student_question()
        growth_before = self.conn.execute(
            "SELECT * FROM student_growth_progress WHERE student_id='S1' AND is_active=1"
        ).fetchone()
        payload = {
            "action": "answer",
            "question_id": delivery["question_id"],
            "submission_token": delivery["submission_token"],
            "response": "A",
        }

        accepted = self.client.post("/student", data=payload)
        replayed = self.client.post("/student", data=payload)

        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(replayed.status_code, 302)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM attempts WHERE student_id='S1'").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM responses WHERE student_id='S1'").fetchone()[0],
            1,
        )
        growth_after = self.conn.execute(
            "SELECT * FROM student_growth_progress WHERE student_id='S1' AND is_active=1"
        ).fetchone()
        self.assertEqual(float(growth_after["visible_stage"]), 1.32)
        self.assertGreater(growth_after["visible_stage"], growth_before["visible_stage"])
        stale_page = self.client.get(replayed.headers["Location"]).get_data(as_text=True)
        self.assertIn("already submitted or is no longer active", stale_page)

    def test_tampered_question_id_creates_no_evidence(self):
        _, delivery = self.open_student_question()
        response = self.client.post(
            "/student",
            data={
                "action": "answer",
                "question_id": "Q1A2",
                "submission_token": delivery["submission_token"],
                "response": "A",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 0)
        self.assertEqual(self.active_delivery()["submission_token"], delivery["submission_token"])

    def test_delivery_from_another_student_is_rejected(self):
        _, s1_delivery = self.open_student_question()
        s2_growth = dash.ensure_student_growth_attempt(
            self.conn, "S2", "MS-LS1-1", "MS-LS1-1A"
        )
        s2_delivery, _ = dash.get_or_create_student_question_delivery(
            self.conn,
            student_id="S2",
            growth_attempt_id=s2_growth["attempt_id"],
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1A",
            level=1,
            eligible_questions=dash.get_questions_for_objective(self.conn, "MS-LS1-1A"),
        )

        response = self.client.post(
            "/student",
            data={
                "action": "answer",
                "question_id": s2_delivery["question_id"],
                "submission_token": s2_delivery["submission_token"],
                "response": "A",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        self.assertEqual(self.active_delivery()["submission_token"], s1_delivery["submission_token"])

    def test_teacher_placement_invalidates_outgoing_delivery_without_evidence(self):
        _, old_delivery = self.open_student_question()
        ok, message = dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1B",
            current_level=1,
            placed_by_user_id=1,
        )
        self.assertTrue(ok, message)
        invalidated = self.conn.execute(
            "SELECT * FROM student_question_deliveries WHERE submission_token=?",
            (old_delivery["submission_token"],),
        ).fetchone()
        self.assertIsNotNone(invalidated["invalidated_at"])
        self.assertIsNone(invalidated["consumed_at"])

        replay = self.client.post(
            "/student",
            data={
                "action": "answer",
                "question_id": old_delivery["question_id"],
                "submission_token": old_delivery["submission_token"],
                "response": "A",
            },
        )
        self.assertEqual(replay.status_code, 302)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 0)
        completed = self.conn.execute(
            """
            SELECT COUNT(*) FROM student_growth_progress
            WHERE student_id='S1' AND presentation_state='complete'
            """
        ).fetchone()[0]
        self.assertEqual(completed, 0)

    def test_routing_level_change_serves_from_new_level_pool(self):
        ok, message = dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-2",
            objective_id="MS-LS1-2A",
            current_level=1,
            placed_by_user_id=1,
        )
        self.assertTrue(ok, message)
        for offset in range(6):
            self.conn.execute(
                """
                INSERT INTO responses
                  (student_id, standard_id, level, question_id, correct, ts)
                VALUES ('S1', 'MS-LS1-2', 1, 'Q2A', 1, ?)
                """,
                (300000 + offset,),
            )
        self.conn.commit()
        page, delivery = self.open_student_question()
        self.assertEqual(delivery["level"], 1)

        submitted = self.client.post(
            "/student",
            data={
                "action": "answer",
                "question_id": delivery["question_id"],
                "submission_token": delivery["submission_token"],
                "response": "A",
            },
        )
        self.assertEqual(submitted.status_code, 302)
        self.client.get(submitted.headers["Location"])
        next_delivery = self.active_delivery()
        self.assertEqual(next_delivery["level"], 2)
        self.assertEqual(next_delivery["question_id"], "Q2A08")

    def test_one_question_objective_cycles_with_fresh_tokens(self):
        ok, message = dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1B",
            current_level=1,
            placed_by_user_id=1,
        )
        self.assertTrue(ok, message)
        _, first = self.open_student_question()
        submitted = self.client.post(
            "/student",
            data={
                "action": "answer",
                "question_id": first["question_id"],
                "submission_token": first["submission_token"],
                "response": "A",
            },
        )
        self.client.get(submitted.headers["Location"])
        second = self.active_delivery()
        self.assertEqual(second["question_id"], first["question_id"])
        self.assertNotEqual(second["submission_token"], first["submission_token"])

    def test_schema_upgrade_backfills_delivery_sequence_number(self):
        growth = dash.ensure_student_growth_attempt(
            self.conn, "S1", "MS-LS1-1", "MS-LS1-1A"
        )
        self.conn.execute("DROP TABLE student_question_deliveries")
        self.conn.execute(
            """
            CREATE TABLE student_question_deliveries (
              submission_token TEXT PRIMARY KEY,
              student_id TEXT NOT NULL,
              growth_attempt_id TEXT NOT NULL,
              standard_id TEXT NOT NULL,
              objective_id TEXT NOT NULL,
              level INTEGER NOT NULL,
              question_id TEXT NOT NULL,
              served_at INTEGER NOT NULL,
              consumed_at INTEGER,
              invalidated_at INTEGER
            )
            """
        )
        self.conn.executemany(
            """
            INSERT INTO student_question_deliveries
              (submission_token, student_id, growth_attempt_id, standard_id,
               objective_id, level, question_id, served_at, consumed_at)
            VALUES (?, 'S1', ?, 'MS-LS1-1', 'MS-LS1-1A', 1, ?, ?, ?)
            """,
            [
                ("older", growth["attempt_id"], "Q1A", 100, 101),
                ("newer", growth["attempt_id"], "Q1A2", 200, None),
            ],
        )
        self.conn.commit()

        dash.ensure_schema(self.conn)

        columns = {
            row[1]
            for row in self.conn.execute(
                "PRAGMA table_info(student_question_deliveries)"
            ).fetchall()
        }
        rows = self.conn.execute(
            """
            SELECT submission_token, sequence_number
            FROM student_question_deliveries
            ORDER BY sequence_number
            """
        ).fetchall()
        self.assertIn("sequence_number", columns)
        self.assertEqual(
            [(row["submission_token"], row["sequence_number"]) for row in rows],
            [("older", 0), ("newer", 1)],
        )

    def test_objective_completion_renders_full_growth_state(self):
        self.login_as(3, "student1", "student")
        with self.client.session_transaction() as sess:
            sess["current_mode"] = "completed"
            sess["terminal_completion_payload"] = {
                "status": "complete",
                "action": "standard_complete",
                "standard": "MS-LS1-1",
                "reason": "no_progression_link_found",
            }

        page = self.client.get("/student")
        html = page.get_data(as_text=True)
        self.assertIn("Growing Understanding", html)
        self.assertIn("Completed", html)
        self.assertIn("This understanding has taken root.", html)
        self.assertIn('aria-valuetext="This understanding has taken root."', html)
        self.assertNotIn("no_progression_link_found", html)

    def test_logout_login_does_not_lose_placement(self):
        dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-1",
            objective_id="MS-LS1-1B",
            current_level=3,
            placed_by_user_id=1,
        )
        before_growth = self.conn.execute(
            "SELECT * FROM student_growth_progress WHERE student_id='S1' AND is_active=1"
        ).fetchone()
        before_growth = dash.update_student_growth_presentation(
            self.conn,
            before_growth["attempt_id"],
            correct=True,
            engine_decision={"status": "question", "action": "serve_practice"},
        )
        self.login_as(3, "student1", "student")
        with self.client.session_transaction() as sess:
            sess["current_mode"] = "question"
        self.client.get("/student")
        delivery_before_logout = self.active_delivery()
        self.client.get("/logout")
        self.login_as(3, "student1", "student")
        self.client.post("/student", data={"action": "continue_learning"})

        page = self.client.get("/student")

        self.assertIn(b"Cells form tissues.", page.data)
        self.assertNotIn(b"MS-LS1-1B", page.data)
        self.assertNotIn(b"level 3", page.data)
        after_growth = self.conn.execute(
            "SELECT * FROM student_growth_progress WHERE student_id='S1' AND is_active=1"
        ).fetchone()
        self.assertEqual(after_growth["attempt_id"], before_growth["attempt_id"])
        self.assertEqual(after_growth["visible_stage"], before_growth["visible_stage"])
        self.assertEqual(
            self.active_delivery()["submission_token"],
            delivery_before_logout["submission_token"],
        )

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
        self.assertEqual(decision["reason"], "insufficient_minimum_evidence")
        self.assertEqual(decision["count"], 0)
        self.assertEqual(decision["action"], "collecting_minimum_evidence")
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

    def test_teacher_placement_rejects_obsolete_launch_objective(self):
        ok, message = dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-2",
            objective_id="MS-LS1-2C",
            current_level=1,
            placed_by_user_id=1,
        )

        self.assertFalse(ok)
        self.assertIn("classroom launch manifest", message)
        self.assertIsNone(self.progress_row("S1"))

    def test_teacher_selection_helpers_expose_only_launch_manifest_objectives(self):
        expected_objectives = list(dash.LAUNCH_OBJECTIVE_IDS)

        objective_ids = [row["objective_id"] for row in dash.get_objectives(self.conn)]
        placeable_ids = [
            row["objective_id"] for row in dash.get_placeable_learning_nodes(self.conn)
        ]
        standard_ids = {row["standard_id"] for row in dash.get_available_standards(self.conn)}

        self.assertEqual(objective_ids, expected_objectives)
        self.assertEqual(placeable_ids, expected_objectives)
        self.assertEqual(standard_ids, set(dash.LAUNCH_STANDARD_IDS))

    def test_shared_canonical_order_ignores_database_order_in_band(self):
        db_order = [
            row["objective_id"]
            for row in self.conn.execute(
                """
                SELECT objective_id
                FROM objectives
                WHERE objective_id IN (?, ?, ?, ?, ?, ?, ?)
                ORDER BY order_in_band, objective_id
                """,
                dash.LAUNCH_OBJECTIVE_IDS,
            ).fetchall()
        ]

        self.assertNotEqual(db_order, list(dash.LAUNCH_OBJECTIVE_IDS))
        self.assertEqual(
            [row["objective_id"] for row in dash.get_objectives(self.conn)],
            list(dash.LAUNCH_OBJECTIVE_IDS),
        )
        self.assertEqual(
            dash.canonical_objective_order("MS-LS1-3C"),
            6,
        )

    def test_student_learning_history_uses_canonical_objective_order(self):
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
            current_level=1,
            placed_by_user_id=1,
        )
        dash.place_student_learning_node(
            self.conn,
            student_id="S1",
            standard_id="MS-LS1-3",
            objective_id="MS-LS1-3C",
            current_level=1,
            placed_by_user_id=1,
        )

        history = dash.get_student_learning_history(self.conn, "S1")
        objective_ids = [
            item["objective_id"]
            for standard in history["standards"]
            for item in standard["objectives"]
        ]

        self.assertEqual(objective_ids, list(dash.LAUNCH_OBJECTIVE_IDS))

    def test_question_selection_ignores_obsolete_and_non_launch_objectives(self):
        self.assertEqual(dash.get_questions_for_objective(self.conn, "MS-LS1-2C"), [])
        self.assertEqual(dash.get_questions_for_objective(self.conn, "MS-LS1-4A"), [])
        self.assertIsNone(dash.get_any_question_id(self.conn, "MS-LS1-2C"))
        self.assertIsNone(dash.get_preview_question(self.conn, "Q2C"))
        self.assertEqual(dash.get_first_preview_question_id(self.conn, "MS-LS1-2C"), None)
        self.assertEqual(dash.get_any_question_id(self.conn, "MS-LS1-2A"), "Q2A")

    def test_teacher_dashboard_does_not_render_obsolete_objective_options(self):
        self.login_as(1, "teacher1", "teacher")

        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"MS-LS1-2A", response.data)
        self.assertIn(b"MS-LS1-3C", response.data)
        self.assertNotIn(b"MS-LS1-2C", response.data)
        self.assertNotIn(b"MS-LS1-3E", response.data)
        self.assertNotIn(b"MS-LS1-4A", response.data)

    def test_retirement_migration_archives_and_removes_obsolete_objectives(self):
        env = os.environ.copy()
        env["NGSS_DB"] = str(TEST_DB)

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "migrations" / "retire_obsolete_classroom_launch_objectives.py"),
            ],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("Retired obsolete classroom launch objectives", result.stdout)
        remaining = self.conn.execute(
            """
            SELECT objective_id
            FROM objectives
            WHERE objective_id IN (?, ?, ?, ?, ?, ?)
            """,
            dash.OBSOLETE_LAUNCH_OBJECTIVE_IDS,
        ).fetchall()
        archived = self.conn.execute(
            """
            SELECT objective_id
            FROM archived_objectives
            WHERE objective_id IN (?, ?, ?, ?, ?, ?)
            """,
            dash.OBSOLETE_LAUNCH_OBJECTIVE_IDS,
        ).fetchall()

        self.assertEqual(remaining, [])
        self.assertEqual(
            {row["objective_id"] for row in archived},
            set(dash.OBSOLETE_LAUNCH_OBJECTIVE_IDS),
        )
        self.assertEqual(
            {row["objective_id"] for row in dash.get_objectives(self.conn)},
            set(dash.LAUNCH_OBJECTIVE_IDS),
        )

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

    def test_matrix_scenario_01_perfect_mastery_advances_on_seventh_response(self):
        initial = self.place_for_matrix("MS-LS1-2", "MS-LS1-2A", 1)
        initial_id = initial["attempt_id"]

        for response_number in range(1, 8):
            self.submit_matrix_answer(True)
            correct, count, score = self.routing_metrics(initial_id)
            self.assertEqual((correct, count, score), (response_number, response_number, 1.0))
            active = self.active_routing_attempt()
            if response_number < 7:
                self.assertEqual(active["attempt_id"], initial_id)
                self.assertEqual(active["level"], 1)
            else:
                self.assertNotEqual(active["attempt_id"], initial_id)
                self.assertEqual(active["level"], 2)

        closed = self.conn.execute(
            "SELECT * FROM routing_level_attempts WHERE attempt_id=?", (initial_id,)
        ).fetchone()
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["end_reason"], "mastery_advance")
        next_delivery = self.active_delivery()
        self.assertEqual(next_delivery["level"], 2)
        self.assertIn(next_delivery["question_id"], {f"Q2A{i:02d}" for i in range(8, 15)})
        self.assert_matrix_database_integrity()

    def test_matrix_scenario_02_single_mistake_recovers_exactly_at_ten(self):
        initial = self.place_for_matrix("MS-LS1-2", "MS-LS1-2A", 1)
        initial_id = initial["attempt_id"]
        answers = [True] * 6 + [False] + [True] * 3
        expected_tail = {
            7: (6, 7, 6 / 7),
            8: (7, 8, 7 / 8),
            9: (8, 9, 8 / 9),
            10: (9, 10, 0.9),
        }

        for response_number, correct_answer in enumerate(answers, 1):
            self.submit_matrix_answer(correct_answer)
            metrics = self.routing_metrics(initial_id)
            if response_number in expected_tail:
                expected = expected_tail[response_number]
                self.assertEqual(metrics[:2], expected[:2])
                self.assertAlmostEqual(metrics[2], expected[2])
            active = self.active_routing_attempt()
            self.assertEqual(
                active["attempt_id"] == initial_id,
                response_number < 10,
            )

        self.assertEqual(self.active_routing_attempt()["level"], 2)
        self.assert_matrix_database_integrity()

    def test_matrix_scenario_03_multiple_mistakes_preserve_every_cumulative_decision(self):
        initial = self.place_for_matrix("MS-LS1-2", "MS-LS1-2A", 1)
        initial_id = initial["attempt_id"]
        answers = [
            True, False, True, True, False, True, True, True, True, True,
            True, True,
        ] + [True] * 8
        running_correct = 0

        for response_number, correct_answer in enumerate(answers, 1):
            self.submit_matrix_answer(correct_answer)
            running_correct += int(correct_answer)
            correct, count, percentage = self.routing_metrics(initial_id)
            self.assertEqual(correct, running_correct)
            self.assertEqual(count, response_number)
            self.assertAlmostEqual(percentage * 100, running_correct / response_number * 100)
            qualifies = response_number >= 7 and percentage >= dash.ae.MASTERY
            active = self.active_routing_attempt()
            self.assertEqual(active["attempt_id"] != initial_id, qualifies)
            if qualifies:
                self.assertEqual(response_number, 20)
                break

        self.assertEqual(self.routing_metrics(initial_id)[:2], (18, 20))
        self.assertEqual(self.active_routing_attempt()["level"], 2)
        self.assert_matrix_database_integrity()

    def test_matrix_scenario_04_low_performance_remediates_and_preserves_history(self):
        initial = self.place_for_matrix("MS-LS1-2", "MS-LS1-2A", 2)
        initial_id = initial["attempt_id"]
        answers = [True, False, False, True, False, True, False]

        for response_number, correct_answer in enumerate(answers, 1):
            self.submit_matrix_answer(correct_answer)
            active = self.active_routing_attempt()
            if response_number < dash.ae.MIN_EVIDENCE:
                self.assertEqual(active["attempt_id"], initial_id)

        closed = self.conn.execute(
            "SELECT * FROM routing_level_attempts WHERE attempt_id=?", (initial_id,)
        ).fetchone()
        active = self.active_routing_attempt()
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["end_reason"], "remediation_level_drop")
        self.assertEqual((closed["final_correct_count"], closed["final_response_count"]), (3, 7))
        self.assertAlmostEqual(closed["final_score"], 3 / 7)
        self.assertNotEqual(active["attempt_id"], initial_id)
        self.assertEqual(active["level"], 1)
        self.assertEqual(self.routing_metrics(active["attempt_id"])[:2], (0, 0))
        self.assertEqual(self.routing_metrics(initial_id)[:2], (3, 7))
        self.assert_matrix_database_integrity()

    def test_matrix_scenario_05_small_pool_wraps_without_reset_or_duplicate_evidence(self):
        initial = self.place_for_matrix("MS-LS1-1", "MS-LS1-1B", 1)
        initial_id = initial["attempt_id"]
        answers = [True] * 6 + [False] + [True] * 3
        tokens = []
        sequences = []
        question_ids = []

        for response_number, correct_answer in enumerate(answers, 1):
            delivery = self.active_delivery()
            if delivery is None:
                _, delivery = self.open_student_question()
            tokens.append(delivery["submission_token"])
            sequences.append(delivery["sequence_number"])
            question_ids.append(delivery["question_id"])
            self.submit_matrix_answer(correct_answer, delivery)
            self.assertEqual(self.routing_metrics(initial_id)[1], response_number)

        self.assertEqual(set(question_ids), {"Q1B"})
        self.assertEqual(sequences, list(range(10)))
        self.assertEqual(len(tokens), len(set(tokens)))
        self.assertEqual(self.routing_metrics(initial_id)[:2], (9, 10))
        self.assertEqual(self.active_routing_attempt()["level"], 2)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM responses WHERE routing_level_attempt_id=?",
                (initial_id,),
            ).fetchone()[0],
            10,
        )
        self.assert_matrix_database_integrity()

    def test_matrix_scenario_06_refresh_keeps_attempt_counts_and_replay_guard(self):
        initial = self.place_for_matrix("MS-LS1-2", "MS-LS1-2A", 1)
        initial_id = initial["attempt_id"]
        answers = [True, True, True, True, True, False, True]
        running_correct = 0

        for response_number, correct_answer in enumerate(answers, 1):
            _, delivery = self.open_student_question()
            refreshed = self.client.get("/student")
            refreshed_again = self.client.get("/student")
            self.assertEqual(refreshed.status_code, 200)
            self.assertEqual(refreshed_again.status_code, 200)
            self.assertEqual(
                self.active_delivery()["submission_token"],
                delivery["submission_token"],
            )
            _, payload, _ = self.submit_matrix_answer(correct_answer, delivery)
            running_correct += int(correct_answer)
            count_before_replay = self.routing_metrics(initial_id)[1]
            replay = self.client.post("/student", data=payload)
            self.assertEqual(replay.status_code, 302)
            self.assertEqual(self.routing_metrics(initial_id)[1], count_before_replay)
            self.assertEqual(
                self.routing_metrics(initial_id)[:2],
                (running_correct, response_number),
            )
            self.assertEqual(self.active_routing_attempt()["attempt_id"], initial_id)

        self.assert_matrix_database_integrity()

    def test_matrix_scenario_07_logout_login_resumes_attempt_and_delivery(self):
        initial = self.place_for_matrix("MS-LS1-2", "MS-LS1-2A", 1)
        initial_id = initial["attempt_id"]

        for response_number in range(1, 5):
            _, delivery = self.open_student_question()
            token = delivery["submission_token"]
            self.client.get("/logout")
            self.login_as(3, "student1", "student")
            with self.client.session_transaction() as sess:
                sess["current_mode"] = "question"
            self.client.get("/student")
            self.assertEqual(self.active_delivery()["submission_token"], token)
            self.submit_matrix_answer(True, self.active_delivery())
            self.assertEqual(self.active_routing_attempt()["attempt_id"], initial_id)
            self.assertEqual(self.routing_metrics(initial_id)[:2], (response_number, response_number))

        duplicate_sequences = self.conn.execute(
            """
            SELECT sequence_number, COUNT(*) AS n
            FROM student_question_deliveries
            WHERE student_id='S1'
            GROUP BY growth_attempt_id, level, sequence_number
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        self.assertEqual(duplicate_sequences, [])
        self.assert_matrix_database_integrity()

    def test_matrix_scenario_08_stale_tab_submission_is_rejected_without_mutation(self):
        initial = self.place_for_matrix("MS-LS1-2", "MS-LS1-2A", 1)
        initial_id = initial["attempt_id"]
        _, tab_a_delivery = self.open_student_question()
        self.client.get("/student")
        tab_b_delivery = self.active_delivery()
        self.assertEqual(
            tab_a_delivery["submission_token"],
            tab_b_delivery["submission_token"],
        )

        _, stale_payload, _ = self.submit_matrix_answer(True, tab_a_delivery)
        before = self.routing_metrics(initial_id)
        response_rows_before = self.conn.execute(
            "SELECT COUNT(*) FROM responses WHERE student_id='S1'"
        ).fetchone()[0]
        replay = self.client.post("/student", data=stale_payload)
        self.assertEqual(replay.status_code, 302)
        self.assertEqual(self.routing_metrics(initial_id), before)
        self.assertEqual(self.active_routing_attempt()["attempt_id"], initial_id)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM responses WHERE student_id='S1'"
            ).fetchone()[0],
            response_rows_before,
        )
        self.assert_matrix_database_integrity()

    def test_matrix_scenario_09_teacher_placement_closes_cleanly_without_migration(self):
        initial = self.place_for_matrix("MS-LS1-2", "MS-LS1-2A", 1)
        initial_id = initial["attempt_id"]
        for answer in [True, True, False]:
            self.submit_matrix_answer(answer)
        old_delivery = self.active_delivery()

        replacement = self.place_for_matrix("MS-LS1-1", "MS-LS1-1B", 2)
        closed = self.conn.execute(
            "SELECT * FROM routing_level_attempts WHERE attempt_id=?", (initial_id,)
        ).fetchone()
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["end_reason"], "teacher_placement")
        self.assertEqual((closed["final_correct_count"], closed["final_response_count"]), (2, 3))
        self.assertAlmostEqual(closed["final_score"], 2 / 3)
        self.assertEqual(replacement["objective_id"], "MS-LS1-1B")
        self.assertEqual(replacement["level"], 2)
        self.assertEqual(self.routing_metrics(replacement["attempt_id"])[:2], (0, 0))
        self.assertEqual(self.routing_metrics(initial_id)[:2], (2, 3))
        invalidated = self.conn.execute(
            "SELECT * FROM student_question_deliveries WHERE submission_token=?",
            (old_delivery["submission_token"],),
        ).fetchone()
        self.assertIsNotNone(invalidated["invalidated_at"])
        self.assert_matrix_database_integrity()

    def test_matrix_scenario_10_objective_transition_resets_only_new_objective(self):
        initial = self.place_for_matrix("MS-LS1-1", "MS-LS1-1A", 3)
        initial_id = initial["attempt_id"]
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS standard_links (
              id INTEGER PRIMARY KEY,
              from_standard_id TEXT NOT NULL,
              to_standard_id TEXT NOT NULL,
              link_type TEXT NOT NULL,
              priority INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.conn.execute("DELETE FROM standard_links")
        self.conn.commit()
        self.open_student_question()
        old_growth = self.conn.execute(
            """
            SELECT *
            FROM student_growth_progress
            WHERE student_id='S1' AND is_active=1
            """
        ).fetchone()

        for response_number in range(1, 8):
            self.submit_matrix_answer(True)
            if response_number < 7:
                self.assertEqual(self.active_routing_attempt()["attempt_id"], initial_id)

        closed = self.conn.execute(
            "SELECT * FROM routing_level_attempts WHERE attempt_id=?", (initial_id,)
        ).fetchone()
        new_attempt = self.active_routing_attempt()
        archived_growth = self.conn.execute(
            "SELECT * FROM student_growth_progress WHERE attempt_id=?",
            (old_growth["attempt_id"],),
        ).fetchone()
        new_growth = self.conn.execute(
            """
            SELECT *
            FROM student_growth_progress
            WHERE student_id='S1' AND is_active=1
            """
        ).fetchone()
        self.assertEqual(closed["status"], "closed")
        self.assertEqual((closed["final_correct_count"], closed["final_response_count"]), (7, 7))
        self.assertEqual(archived_growth["is_active"], 0)
        self.assertEqual(archived_growth["presentation_state"], "complete")
        self.assertNotEqual(new_growth["attempt_id"], old_growth["attempt_id"])
        self.assertEqual(new_growth["objective_id"], "MS-LS1-1B")
        self.assertEqual(new_attempt["objective_id"], "MS-LS1-1B")
        self.assertEqual(new_attempt["level"], 1)
        self.assertEqual(self.routing_metrics(new_attempt["attempt_id"])[:2], (0, 0))
        self.assertEqual(self.routing_metrics(initial_id)[:2], (7, 7))
        next_delivery = self.active_delivery()
        self.assertEqual(next_delivery["objective_id"], "MS-LS1-1B")
        self.assertEqual(next_delivery["question_id"], "Q1B")
        self.assert_matrix_database_integrity()


if __name__ == "__main__":
    unittest.main()
