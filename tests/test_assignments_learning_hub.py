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
TEST_DB = Path(TEST_DIR.name) / f"assignments_{uuid.uuid4().hex}.db"
os.environ["NGSS_DB"] = str(TEST_DB)
os.environ["SECRET_KEY"] = "test-secret"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
dash = importlib.import_module("app_core.dashboard_v5")


class AssignmentLearningHubTests(unittest.TestCase):
    def setUp(self):
        dash.DB = str(TEST_DB)
        dash.ae.DB_PATH = str(TEST_DB)
        self.conn = sqlite3.connect(TEST_DB)
        self.conn.row_factory = sqlite3.Row
        dash.ensure_schema(self.conn)
        for table in (
            "assignment_recipients", "assignments", "routing_level_attempts",
            "student_objective_state", "progress_state", "responses", "attempts",
            "class_enrollments", "class_sections",
            "user_instructional_authorizations", "user_platform_roles",
            "user_auth_identities", "users", "students", "questions",
            "objective_levels", "objectives", "standards",
        ):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.executemany(
            "INSERT INTO standards(standard_id,standard_name,core_idea,grade_band) VALUES (?,?,?,?)",
            [
                ("MS-LS1-1", "Cells and Living Things", "Life Science", "Middle School"),
                ("MS-LS1-2", "Body Systems", "Life Science", "Middle School"),
                ("MS-LS1-3", "Interacting Body Systems", "Life Science", "Middle School"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO objectives
              (objective_id,standard_id,objective_text,display_name,student_description,order_in_band)
            VALUES (?,?,?,?,?,1)
            """,
            [
                ("MS-LS1-1A", "MS-LS1-1", "Explain cell evidence", "Cell Evidence", "Explore how cells support life."),
                ("MS-LS1-2A", "MS-LS1-2", "Explain body systems", "Body Systems", "Explore connected body systems."),
                ("MS-LS1-3A", "MS-LS1-3", "Explain interacting systems", "Interacting Systems", "Explore interacting body systems."),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO questions
              (question_id,objective_id,stem,choice_a,choice_b,choice_c,choice_d,answer_key)
            VALUES (?,?,'Question?','A','B','C','D','A')
            """,
            [("Q-A", "MS-LS1-1A"), ("Q-B", "MS-LS1-2A"),
             ("Q-C", "MS-LS1-3A")],
        )
        self.conn.executemany(
            """
            INSERT INTO users(id,username,password_hash,role,account_role,linked_student_id,is_active)
            VALUES (?,?, 'x',?,?,?,1)
            """,
            [
                (1, "teacher-a", "teacher", "teacher", None),
                (2, "teacher-b", "teacher", "teacher", None),
                (3, "student-a", "student", "student", "S-A"),
                (4, "student-b", "student", "student", "S-B"),
                (5, "outsider", "student", "student", "S-X"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO user_instructional_authorizations(user_id,instructional_role,granted_at) VALUES (?,'teacher',1)",
            [(1,), (2,)],
        )
        self.conn.executemany(
            "INSERT INTO students(student_id,first_name,last_name,grade,class_period) VALUES (?,?,?,6,'1')",
            [("S-A", "Alice", "A"), ("S-B", "Ben", "B"), ("S-X", "Xena", "X")],
        )
        self.conn.executemany(
            """
            INSERT INTO class_sections
              (class_id,teacher_user_id,name,join_code,is_active,created_at,updated_at)
            VALUES (?,?,?,?,?,1,1)
            """,
            [("C-A", 1, "Class A", "AAA234", 1), ("C-B", 2, "Class B", "BBB234", 1),
             ("C-OLD", 1, "Archived", "CCC234", 0)],
        )
        self.conn.executemany(
            "INSERT INTO class_enrollments(class_id,student_id,enrolled_at,enrolled_by,is_active) VALUES (?,?,1,'self',?)",
            [("C-A", "S-A", 1), ("C-A", "S-B", 1), ("C-A", "S-X", 0), ("C-B", "S-X", 1)],
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

    def csrf(self):
        with self.client.session_transaction() as session:
            return session.get("owner_csrf_token") or dash.secrets.token_urlsafe(32)

    def create(self, **overrides):
        follow_redirects = overrides.pop("follow_redirects", False)
        self.login(1)
        self.client.get("/dashboard")
        with self.client.session_transaction() as session:
            token = session["owner_csrf_token"]
        data = {
            "csrf_token": token, "class_id": "C-A",
            "standard_id": "MS-LS1-1", "recipient_scope": "class",
            "directions": "Focus on evidence.", "assign_date": "2020-01-01",
            "due_date": "2030-05-10",
        }
        data.update(overrides)
        return self.client.post(
            "/teacher/assignments", data=data, follow_redirects=follow_redirects
        )

    def assignment_id(self):
        return self.conn.execute("SELECT assignment_id FROM assignments").fetchone()[0]

    def test_whole_class_assignment_snapshots_current_active_members(self):
        response = self.create()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            [row[0] for row in self.conn.execute(
                "SELECT student_id FROM assignment_recipients ORDER BY student_id"
            )], ["S-A", "S-B"]
        )
        self.assertNotIn("S-X", [row[0] for row in self.conn.execute(
            "SELECT student_id FROM assignment_recipients"
        )])
        self.conn.execute(
            "INSERT INTO students(student_id,first_name,last_name) VALUES ('S-LATE','Late','Learner')"
        )
        self.conn.execute(
            "INSERT INTO class_enrollments(class_id,student_id,enrolled_at,enrolled_by,is_active) VALUES ('C-A','S-LATE',2,'self',1)"
        )
        self.conn.commit()
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM assignment_recipients WHERE student_id='S-LATE'"
        ).fetchone())

    def test_selected_assignment_and_cross_class_security(self):
        self.create(recipient_scope="selected", student_id="S-A")
        self.assertEqual(
            self.conn.execute("SELECT student_id FROM assignment_recipients").fetchone()[0],
            "S-A",
        )
        self.conn.execute("DELETE FROM assignment_recipients")
        self.conn.execute("DELETE FROM assignments")
        self.conn.commit()
        self.assertEqual(self.create(class_id="C-B").status_code, 404)
        self.assertEqual(
            self.create(recipient_scope="selected", student_id="S-X").status_code, 404
        )
        self.assertEqual(self.create(class_id="C-OLD").status_code, 404)

    def test_invalid_standard_csrf_and_non_teacher_are_denied(self):
        self.assertEqual(self.create(standard_id="FAKE").status_code, 400)
        self.login(1)
        self.assertEqual(self.client.post("/teacher/assignments", data={}).status_code, 400)
        self.login(3)
        self.assertEqual(self.client.post("/teacher/assignments", data={}).status_code, 302)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.post("/teacher/assignments", data={}).status_code, 302)

    def test_student_hub_assignment_visibility_due_date_and_archive(self):
        self.create(recipient_scope="selected", student_id="S-A")
        self.login(3)
        page = self.client.get("/student?home=1").get_data(as_text=True)
        self.assertIn("Assigned by Your Teacher", page)
        self.assertIn("Cells and Living Things", page)
        self.assertIn("May 10, 2030", page)
        self.login(4)
        no_assignment_page = self.client.get("/student?home=1").get_data(as_text=True)
        self.assertNotIn("Focus on evidence.", no_assignment_page)
        self.assertIn("Explore Learning", no_assignment_page)
        self.login(1)
        self.client.get(f"/teacher/assignments/{self.assignment_id()}")
        with self.client.session_transaction() as session:
            token = session["owner_csrf_token"]
        self.client.post(
            f"/teacher/assignments/{self.assignment_id()}/archive",
            data={"csrf_token": token},
        )
        self.login(3)
        self.assertNotIn("Focus on evidence.", self.client.get("/student?home=1").get_data(as_text=True))

    def test_archiving_preserves_evidence_and_mastery(self):
        self.conn.execute(
            "INSERT INTO progress_state(student_id,standard_id,current_level,status,rolling_avg,locked,last_update) VALUES ('S-A','MS-LS1-1',3,'completed',1,0,1)"
        )
        self.conn.execute(
            "INSERT INTO responses(student_id,standard_id,level,question_id,correct,ts) VALUES ('S-A','MS-LS1-1',3,'Q-A',1,1)"
        )
        self.conn.commit()
        self.create(recipient_scope="selected", student_id="S-A")
        before = tuple(self.conn.execute(
            "SELECT status,rolling_avg FROM progress_state WHERE student_id='S-A' AND standard_id='MS-LS1-1'"
        ).fetchone())
        self.login(1)
        self.client.get(f"/teacher/assignments/{self.assignment_id()}")
        with self.client.session_transaction() as session:
            token = session["owner_csrf_token"]
        self.client.post(
            f"/teacher/assignments/{self.assignment_id()}/archive",
            data={"csrf_token": token},
        )
        after = tuple(self.conn.execute(
            "SELECT status,rolling_avg FROM progress_state WHERE student_id='S-A' AND standard_id='MS-LS1-1'"
        ).fetchone())
        self.assertEqual(after, before)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM responses WHERE student_id='S-A'"
        ).fetchone()[0], 1)

    def test_mastered_before_assignment_receives_completion_credit(self):
        self.conn.execute(
            "INSERT INTO progress_state(student_id,standard_id,current_level,status,rolling_avg,locked,last_update) VALUES ('S-A','MS-LS1-1',3,'completed',1.0,0,1)"
        )
        self.conn.commit()
        self.create(recipient_scope="selected", student_id="S-A")
        assignments = dash.get_student_assignments(self.conn, "S-A")
        self.assertEqual(assignments[0]["progress_status"], "completed")
        self.assertEqual(dash.assignment_summary(
            self.conn, self.assignment_id()
        )["completed"], 1)

    def test_open_learning_start_resume_and_switch_preserves_evidence(self):
        self.login(3)
        self.client.get("/student?home=1")
        with self.client.session_transaction() as session:
            token = session["owner_csrf_token"]
        first = self.client.post(
            "/student/learning/MS-LS1-1/start", data={"csrf_token": token}
        )
        self.assertTrue(first.location.endswith("/student"))
        self.conn.execute(
            "UPDATE progress_state SET rolling_avg=.75,current_level=2 WHERE student_id='S-A' AND standard_id='MS-LS1-1'"
        )
        self.conn.execute(
            "INSERT INTO responses(student_id,standard_id,level,question_id,correct,ts) VALUES ('S-A','MS-LS1-1',2,'Q-A',1,2)"
        )
        self.conn.commit()
        self.client.post("/student/learning/MS-LS1-2/start", data={"csrf_token": token})
        saved = self.conn.execute(
            "SELECT rolling_avg,current_level,status FROM progress_state WHERE student_id='S-A' AND standard_id='MS-LS1-1'"
        ).fetchone()
        self.assertEqual(saved["rolling_avg"], .75)
        self.assertEqual(saved["current_level"], 2)
        self.assertEqual(saved["status"], "inactive")
        self.client.post("/student/learning/MS-LS1-1/start", data={"csrf_token": token})
        resumed = self.conn.execute(
            "SELECT rolling_avg,current_level FROM progress_state WHERE student_id='S-A' AND standard_id='MS-LS1-1'"
        ).fetchone()
        self.assertEqual(tuple(resumed), (.75, 2))

    def test_recommendation_prioritizes_active_then_assignment(self):
        self.create(recipient_scope="selected", student_id="S-A")
        standards = dash.get_available_standards(self.conn)
        assignments = dash.get_student_assignments(self.conn, "S-A")
        recommendation = dash.student_recommendation(
            self.conn, "S-A", assignments, standards
        )
        self.assertEqual(recommendation["standard_id"], "MS-LS1-1")
        self.assertEqual(recommendation["reason"], "Your teacher assigned this learning.")
        dash.start_or_resume_student_standard(self.conn, "S-A", "MS-LS1-2")
        recommendation = dash.student_recommendation(
            self.conn, "S-A", assignments, standards
        )
        self.assertEqual(recommendation["standard_id"], "MS-LS1-2")
        self.assertEqual(recommendation["reason"], "Continue where you left off.")

    def test_teacher_progress_and_direct_assignment_access_are_scoped(self):
        self.create()
        self.conn.execute(
            "INSERT INTO progress_state(student_id,standard_id,current_level,status,rolling_avg,locked,last_update) VALUES ('S-A','MS-LS1-1',1,'practicing',.5,0,1)"
        )
        self.conn.execute(
            "INSERT INTO progress_state(student_id,standard_id,current_level,status,rolling_avg,locked,last_update) VALUES ('S-B','MS-LS1-1',3,'completed',1,0,1)"
        )
        self.conn.commit()
        summary = dash.assignment_summary(self.conn, self.assignment_id())
        self.assertEqual(summary, {
            "not_started": 0, "in_progress": 1, "completed": 1, "total": 2
        })
        self.login(2)
        self.assertEqual(
            self.client.get(f"/teacher/assignments/{self.assignment_id()}").status_code,
            404,
        )

    def test_assign_date_hides_future_work_until_release(self):
        self.create(
            recipient_scope="selected", student_id="S-A",
            assign_date="2099-01-01", due_date="2099-01-02",
        )
        self.assertEqual(dash.get_student_assignments(self.conn, "S-A"), [])
        assignment = dash.get_student_assignments(
            self.conn, "S-A", as_of=4070995200
        )
        self.assertEqual(len(assignment), 1)
        self.assertFalse(assignment[0]["is_past_due"])
        due_at = self.conn.execute(
            "SELECT due_at FROM assignments"
        ).fetchone()[0]
        overdue = dash.get_student_assignments(
            self.conn, "S-A", as_of=due_at + 1
        )
        self.assertTrue(overdue[0]["is_past_due"])

    def test_due_date_cannot_precede_assign_date(self):
        response = self.create(assign_date="2030-05-11", due_date="2030-05-10")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0], 0
        )

    def test_authoring_page_uses_scalable_picker_and_conditional_recipients(self):
        self.login(1)
        page = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Assign Learning", page)
        self.assertIn("Course / Grade", page)
        self.assertIn("Search standards", page)
        self.assertIn('id="selected-students" hidden', page)
        self.assertIn("Assign Date", page)
        self.assertNotIn("I reviewed the class", page)

        targets = self.client.get(
            "/teacher/assignment-targets",
            query_string={
                "subject": "Science",
                "course": "Middle School",
                "unit": "Cells: Structure and Function",
                "q": "living",
            },
        ).get_json()["targets"]
        self.assertEqual([target["standard_id"] for target in targets], ["MS-LS1-1"])
        self.assertEqual(targets[0]["title"], "Cells and Living Things")

        cells_unit = self.client.get(
            "/teacher/assignment-targets",
            query_string={"unit": "Cells: Structure and Function"},
        ).get_json()["targets"]
        self.assertEqual(
            [target["standard_id"] for target in cells_unit],
            ["MS-LS1-1", "MS-LS1-2"],
        )
        body_unit = self.client.get(
            "/teacher/assignment-targets",
            query_string={"unit": "Body Systems"},
        ).get_json()["targets"]
        self.assertEqual(
            [target["standard_id"] for target in body_unit],
            ["MS-LS1-3"],
        )

    def test_success_feedback_names_learning_recipients_and_class(self):
        response = self.create(
            recipient_scope="selected", student_id="S-A",
            follow_redirects=True,
        )
        page = response.get_data(as_text=True)
        self.assertIn("Assignment created successfully", page)
        self.assertIn("Cells and Living Things", page)
        self.assertIn("for 1 student in Class A", page)

    def test_multiple_standards_create_independent_assignments(self):
        response = self.create(
            standard_id=["MS-LS1-3", "MS-LS1-1", "MS-LS1-2"],
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        assignments = self.conn.execute(
            "SELECT target_id FROM assignments"
        ).fetchall()
        self.assertEqual(
            sorted((row[0] for row in assignments), key=dash.natural_sort_key),
            ["MS-LS1-1", "MS-LS1-2", "MS-LS1-3"],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM assignment_recipients"
            ).fetchone()[0],
            6,
        )
        self.assertEqual(len(dash.get_student_assignments(self.conn, "S-A")), 3)
        self.assertIn("3 assignments created successfully", response.get_data(as_text=True))

    def test_natural_standard_sorting_and_simplified_history(self):
        ids = ["MS-LS1-10", "MS-LS1-2", "MS-LS1-1"]
        self.assertEqual(
            sorted(ids, key=dash.natural_sort_key),
            ["MS-LS1-1", "MS-LS1-2", "MS-LS1-10"],
        )
        self.create(standard_id=["MS-LS1-3", "MS-LS1-1", "MS-LS1-2"])
        ordered = [
            row["target_id"] for row in dash.teacher_assignments(self.conn, 1)
        ]
        self.assertEqual(ordered, ["MS-LS1-1", "MS-LS1-2", "MS-LS1-3"])
        page = self.client.get("/dashboard").get_data(as_text=True)
        history = page.split("<h3>Assignment History</h3>", 1)[1]
        self.assertNotIn("<th>Scope</th>", history)
        self.assertNotIn("<th>Recipients</th>", history)
        self.assertIn("<th>Assign Date</th>", history)
        self.assertIn("<th>Due Date</th>", history)
        self.assertLess(
            history.index("MS-LS1-1"),
            history.index("MS-LS1-2"),
        )


if __name__ == "__main__":
    unittest.main()
