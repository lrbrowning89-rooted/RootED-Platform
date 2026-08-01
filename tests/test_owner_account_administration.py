import gc
import importlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import check_password_hash


ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
TEST_DB = Path(TEST_DIR.name) / "owner-account-administration.db"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
dash = importlib.import_module("app_core.dashboard_v5")


class OwnerAccountAdministrationTests(unittest.TestCase):
    def setUp(self):
        dash.DB = str(TEST_DB)
        dash.ae.DB_PATH = str(TEST_DB)
        self.conn = sqlite3.connect(TEST_DB)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        dash.ensure_schema(self.conn)
        for table in (
            "impersonated_action_audit",
            "impersonation_audit",
            "access_authorization_audit_log",
            "class_enrollments",
            "class_sections",
            "user_instructional_authorizations",
            "user_platform_roles",
            "user_auth_identities",
            "students",
            "users",
        ):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.executemany(
            """
            INSERT INTO users
              (id, username, password_hash, role, account_role, is_active)
            VALUES (?, ?, 'unused', ?, ?, 1)
            """,
            [
                (1, "owner", "student", "pending"),
                (2, "teacher", "teacher", "teacher"),
                (3, "student", "student", "student"),
                (4, "other-owner", "teacher", "teacher"),
            ],
        )
        self.conn.execute(
            """
            INSERT INTO user_platform_roles
              (user_id, platform_role, granted_at, grant_note)
            VALUES (1, 'owner', 1, 'Test owner')
            """
        )
        self.conn.execute(
            """
            INSERT INTO user_platform_roles
              (user_id, platform_role, granted_at, grant_note)
            VALUES (4, 'owner', 1, 'Second test owner')
            """
        )
        self.conn.execute(
            """
            INSERT INTO user_instructional_authorizations
              (user_id, instructional_role, granted_at, grant_note)
            VALUES (2, 'teacher', 1, 'Test teacher')
            """
        )
        self.conn.execute(
            """
            INSERT INTO class_sections
              (class_id, teacher_user_id, name, class_period, join_code,
               is_active, created_at, updated_at)
            VALUES ('CLASS-1', 2, 'Period 3 Science', '3', 'ABC234', 1, 1, 1)
            """
        )
        self.conn.commit()
        dash.app.config.update(TESTING=True)
        self.client = dash.app.test_client()

    def tearDown(self):
        self.conn.close()

    @classmethod
    def tearDownClass(cls):
        boot = getattr(dash, "_conn_boot", None)
        if boot is not None:
            try:
                boot.close()
            except sqlite3.Error:
                pass
        gc.collect()
        TEST_DIR.cleanup()

    def login(self, user_id):
        user = self.conn.execute(
            "SELECT username, account_role, role FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
            session["username"] = user["username"]
            session["role"] = user["account_role"] or user["role"]

    def owner_token(self):
        self.login(1)
        response = self.client.get("/owner/account-administration")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            return session["owner_csrf_token"]

    def create_account(self, **overrides):
        data = {
            "csrf_token": self.owner_token(),
            "username": "support.student",
            "password": "Strong-Test9!",
            "platform_role": "none",
            "instructional_access": "",
            "class_id": "",
        }
        data.update(overrides)
        return self.client.post(
            "/owner/account-administration",
            data=data,
            follow_redirects=True,
        )

    def test_teacher_dashboard_hides_legacy_form_and_explains_sso_join_code(self):
        self.login(2)
        response = self.client.get("/dashboard")
        text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Add / Update Student", text)
        self.assertNotIn('value="save_student"', text)
        self.assertNotIn("Create login accounts", text)
        for owner_only_label in (
            "Configuration",
            "Import Data from CSV",
            "Maintenance / Data Tools",
            "Adaptive Engine Status",
            "Add / Update Student",
            "Record an Attempt",
            "Class Aggregate",
            "View Error Log",
            "Diagnostic Arena",
        ):
            self.assertNotIn(owner_only_label, text)
        for teacher_label in (
            "Classroom Overview",
            "Class Snapshot",
            "Needs Attention",
            "Active Standards",
            "Assignments",
            "Question Preview",
            "Class Enrollment",
            "Question Flags",
        ):
            self.assertIn(teacher_label, text)
        self.assertIn(
            "Students join RootED by signing in with Google or Microsoft "
            "and entering this class's join code.",
            text,
        )

    def test_teacher_direct_posts_are_forbidden(self):
        self.login(2)
        before_students = self.conn.execute(
            "SELECT COUNT(*) FROM students"
        ).fetchone()[0]
        legacy = self.client.post(
            "/dashboard",
            data={"action": "save_student", "student_id": "FORBIDDEN"},
        )
        account = self.client.post(
            "/owner/account-administration",
            data={
                "username": "forbidden",
                "password": "Strong-Test9!",
                "platform_role": "none",
            },
        )
        self.assertEqual(legacy.status_code, 403)
        self.assertEqual(account.status_code, 403)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM students").fetchone()[0],
            before_students,
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM users WHERE username='forbidden'"
            ).fetchone()
        )

    def test_owner_ui_and_csrf_protection(self):
        token = self.owner_token()
        page = self.client.get("/owner")
        admin = self.client.get("/owner/account-administration")
        self.assertIn(b"Account Administration", page.data)
        self.assertIn(b"Account Details", admin.data)
        self.assertIn(b"Platform Role", admin.data)
        self.assertIn(b"Instructional Access", admin.data)
        self.assertIn(b"Optional Class Membership", admin.data)
        self.assertIn(token.encode(), admin.data)
        missing = self.client.post(
            "/owner/account-administration",
            data={
                "username": "no-csrf",
                "password": "Strong-Test9!",
                "platform_role": "none",
            },
        )
        self.assertEqual(missing.status_code, 400)

    def test_owner_creates_least_privileged_local_account_with_hashed_password(self):
        response = self.create_account()
        self.assertIn(b"Account created.", response.data)
        user = self.conn.execute(
            "SELECT * FROM users WHERE username='support.student'"
        ).fetchone()
        self.assertIsNotNone(user)
        self.assertEqual(user["account_role"], "pending")
        self.assertEqual(user["role"], "student")
        self.assertIsNone(user["linked_student_id"])
        self.assertNotEqual(user["password_hash"], "Strong-Test9!")
        self.assertTrue(check_password_hash(user["password_hash"], "Strong-Test9!"))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_platform_roles WHERE user_id=?",
                (user["id"],),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_instructional_authorizations WHERE user_id=?",
                (user["id"],),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_auth_identities WHERE user_id=?",
                (user["id"],),
            ).fetchone()[0],
            0,
        )

    def test_teacher_authorization_is_separate_idempotent_and_never_grants_owner(self):
        self.create_account(
            username="local.teacher",
            instructional_access="teacher",
        )
        user = self.conn.execute(
            "SELECT * FROM users WHERE username='local.teacher'"
        ).fetchone()
        self.assertEqual(user["account_role"], "teacher")
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM user_instructional_authorizations
                WHERE user_id=? AND instructional_role='teacher' AND revoked_at IS NULL
                """,
                (user["id"],),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_platform_roles WHERE user_id=?",
                (user["id"],),
            ).fetchone()[0],
            0,
        )

        token = self.owner_token()
        first = self.client.post(
            f"/owner/people-access/{user['id']}/authorize-teacher",
            data={"csrf_token": token},
        )
        second = self.client.post(
            f"/owner/people-access/{user['id']}/authorize-teacher",
            data={"csrf_token": token},
        )
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM user_instructional_authorizations
                WHERE user_id=? AND instructional_role='teacher' AND revoked_at IS NULL
                """,
                (user["id"],),
            ).fetchone()[0],
            1,
        )

    def test_optional_membership_uses_canonical_student_and_enrollment(self):
        response = self.create_account(
            username="class.student",
            class_id="CLASS-1",
        )
        self.assertIn(b"Class membership: Period 3 Science.", response.data)
        user = self.conn.execute(
            "SELECT * FROM users WHERE username='class.student'"
        ).fetchone()
        self.assertEqual(user["account_role"], "student")
        self.assertIsNotNone(user["linked_student_id"])
        student = self.conn.execute(
            "SELECT * FROM students WHERE student_id=?",
            (user["linked_student_id"],),
        ).fetchone()
        enrollment = self.conn.execute(
            """
            SELECT * FROM class_enrollments
            WHERE class_id='CLASS-1' AND student_id=?
            """,
            (user["linked_student_id"],),
        ).fetchone()
        self.assertIsNotNone(student)
        self.assertIsNotNone(enrollment)
        self.assertEqual(enrollment["is_active"], 1)
        self.assertEqual(enrollment["enrolled_by"], "owner")
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM class_enrollments
                WHERE class_id='CLASS-1' AND student_id=? AND is_active=1
                """,
                (user["linked_student_id"],),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM user_platform_roles WHERE user_id=?",
                (user["id"],),
            ).fetchone()[0],
            0,
        )

    def test_invalid_class_and_duplicate_identity_fail_without_partial_writes(self):
        invalid = self.create_account(
            username="rollback.student",
            class_id="MISSING",
        )
        self.assertIn(b"Select an active class.", invalid.data)
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM users WHERE username='rollback.student'"
            ).fetchone()
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM students"
            ).fetchone()[0],
            0,
        )

        self.create_account(username="duplicate.student")
        duplicate = self.create_account(username="DUPLICATE.STUDENT")
        self.assertIn(b"already exists", duplicate.data)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM users WHERE LOWER(username)='duplicate.student'"
            ).fetchone()[0],
            1,
        )

    def test_owner_role_input_and_weak_password_are_rejected(self):
        role = self.create_account(
            username="accidental.owner",
            platform_role="owner",
        )
        self.assertEqual(role.status_code, 400)
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM users WHERE username='accidental.owner'"
            ).fetchone()
        )

    def test_owner_navigation_and_moved_tool_pages(self):
        self.login(1)
        home = self.client.get("/owner")
        for label in (
            "Teacher View",
            "Account Administration",
            "Content Administration",
            "Platform Configuration",
            "Data and Maintenance",
            "Developer and Diagnostics",
            "Impersonate User",
        ):
            self.assertIn(label.encode(), home.data)
        self.assertEqual(home.get_data(as_text=True).count("Impersonate User"), 1)
        self.assertIn(b"CSV Content Import", self.client.get("/owner/content-administration").data)
        self.assertIn(b"Adaptive Thresholds", self.client.get("/owner/platform-configuration").data)
        self.assertIn(b"Attempts Backup", self.client.get("/owner/data-maintenance").data)
        self.assertIn(b"Adaptive Assessment Simulator", self.client.get("/owner/developer-diagnostics").data)

    def test_root_routes_anonymous_and_authenticated_effective_roles(self):
        anonymous = self.client.get("/")
        self.assertEqual(anonymous.status_code, 200)
        self.assertIn(b"Science Learning. Built differently.", anonymous.data)

        expected_by_user = {
            1: "/owner",
            2: "/teacher",
            3: "/student",
        }
        for user_id, expected in expected_by_user.items():
            self.login(user_id)
            with self.subTest(user_id=user_id):
                response = self.client.get("/")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], expected)

        self.login(2)
        with self.client.session_transaction() as teacher_session:
            teacher_session["student_mode_preview"] = {
                "teacher_user_id": 2,
                "student_id": "TEST-STUDENT",
                "objective_id": "MS-LS1-1A",
            }
        student_mode = self.client.get("/")
        self.assertEqual(student_mode.status_code, 302)
        self.assertEqual(student_mode.headers["Location"], "/student")

    def test_root_routes_by_impersonated_role_and_public_site_stays_available(self):
        token = self.owner_token()
        self.client.post(
            "/owner/impersonation/start/2",
            data={"csrf_token": token},
        )
        teacher_root = self.client.get("/")
        self.assertEqual(teacher_root.headers["Location"], "/teacher")
        with self.client.session_transaction() as teacher_session:
            stop_token = teacher_session["impersonation_stop_csrf_token"]
        self.client.post(
            "/owner/impersonation/stop",
            data={"csrf_token": stop_token},
        )

        owner_token = self.owner_token()
        self.client.post(
            "/owner/impersonation/start/3",
            data={"csrf_token": owner_token},
        )
        student_root = self.client.get("/")
        self.assertEqual(student_root.headers["Location"], "/student")

        public_site = self.client.get("/landing")
        self.assertEqual(public_site.status_code, 200)
        self.assertIn(b"Science Learning. Built differently.", public_site.data)

    def test_owner_impersonation_page_searches_teacher_and_student(self):
        self.login(1)
        page = self.client.get("/owner/impersonation")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Owner Workspace", page.data)
        self.assertIn(b"teacher", page.data)
        self.assertIn(b"student", page.data)

        teacher_results = self.client.get("/owner/impersonation?q=teacher")
        self.assertIn(b"/owner/impersonation/start/2", teacher_results.data)
        self.assertNotIn(b"/owner/impersonation/start/3", teacher_results.data)
        student_results = self.client.get("/owner/impersonation?q=student")
        self.assertIn(b"/owner/impersonation/start/3", student_results.data)
        self.assertNotIn(b"/owner/impersonation/start/2", student_results.data)

    def test_impersonation_list_excludes_every_ineligible_identity(self):
        self.conn.executemany(
            """
            INSERT INTO users
              (id, username, password_hash, role, account_role, is_active)
            VALUES (?, ?, 'unused', ?, ?, ?)
            """,
            [
                (5, "deleted-user-5-tombstone", "student", "student", 1),
                (6, "inactive-teacher", "teacher", "teacher", 0),
                (7, "pending-person", "student", "pending", 1),
            ],
        )
        self.conn.execute(
            """
            INSERT INTO user_instructional_authorizations
              (user_id, instructional_role, granted_at, grant_note)
            VALUES (6, 'teacher', 1, 'Inactive test teacher')
            """
        )
        self.conn.commit()
        self.login(1)

        page = self.client.get("/owner/impersonation")
        text = page.get_data(as_text=True)
        self.assertIn("/owner/impersonation/start/2", text)
        self.assertIn("/owner/impersonation/start/3", text)
        for user_id, unavailable in (
            (5, "deleted-user-5-tombstone"),
            (6, "inactive-teacher"),
            (7, "pending-person"),
            (4, "other-owner"),
        ):
            self.assertNotIn(unavailable, text)
            searched = self.client.get(
                "/owner/impersonation",
                query_string={"q": unavailable},
            )
            self.assertNotIn(
                f"/owner/impersonation/start/{user_id}".encode(),
                searched.data,
            )

    def test_provider_identities_use_human_readable_names_and_keep_stable_ids(self):
        google_subject = "4c8e5fff-8339-4444-a111-provider-subject"
        microsoft_subject = "tenant:7d71c891-provider-subject"
        self.conn.execute(
            "UPDATE users SET username=? WHERE id=2",
            (google_subject,),
        )
        self.conn.execute(
            "UPDATE users SET username=? WHERE id=3",
            (microsoft_subject,),
        )
        self.conn.executemany(
            """
            INSERT INTO user_auth_identities
              (user_id, provider, provider_subject, verified_email,
               display_name, created_at, last_login_at)
            VALUES (?, ?, ?, ?, ?, 1, 1)
            """,
            [
                (
                    2,
                    "google",
                    google_subject,
                    "logan@example.org",
                    "Logan Browning",
                ),
                (
                    3,
                    "microsoft",
                    microsoft_subject,
                    "maya@example.org",
                    "Maya Rivera",
                ),
            ],
        )
        self.conn.commit()
        self.login(1)

        page = self.client.get("/owner/impersonation")
        self.assertIn(b"Logan Browning", page.data)
        self.assertIn(b"Maya Rivera", page.data)
        self.assertNotIn(google_subject.encode(), page.data)
        self.assertNotIn(microsoft_subject.encode(), page.data)

        with self.client.session_transaction() as owner_session:
            token = owner_session["owner_csrf_token"]
        started = self.client.post(
            "/owner/impersonation/start/2",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        self.assertIn(b"Impersonating <strong>Logan Browning</strong> as Teacher", started.data)
        self.assertNotIn(google_subject.encode(), started.data)
        audit = self.conn.execute(
            "SELECT * FROM impersonation_audit ORDER BY audit_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(audit["acting_owner_user_id"], 1)
        self.assertEqual(audit["target_user_id"], 2)

    def test_owner_can_impersonate_specific_student_and_restore_owner(self):
        token = self.owner_token()
        started = self.client.post(
            "/owner/impersonation/start/3",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        self.assertIn(b"Impersonating <strong>student</strong> as Student", started.data)
        with self.client.session_transaction() as impersonated_session:
            self.assertEqual(impersonated_session["user_id"], 3)
            stop_token = impersonated_session["impersonation_stop_csrf_token"]
        audit = self.conn.execute(
            "SELECT * FROM impersonation_audit ORDER BY audit_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(audit["acting_owner_user_id"], 1)
        self.assertEqual(audit["target_user_id"], 3)
        self.assertEqual(audit["outcome"], "active")

        stopped = self.client.post(
            "/owner/impersonation/stop",
            data={"csrf_token": stop_token},
            follow_redirects=True,
        )
        self.assertIn(b"RootED Owner Workspace", stopped.data)
        completed = self.conn.execute(
            "SELECT * FROM impersonation_audit WHERE audit_id=?",
            (audit["audit_id"],),
        ).fetchone()
        self.assertEqual(completed["outcome"], "completed")

    def test_owner_only_routes_reject_teacher_and_student(self):
        paths = (
            "/owner/content-administration",
            "/owner/platform-configuration",
            "/owner/data-maintenance",
            "/owner/developer-diagnostics",
            "/owner/impersonation",
            "/diagnostic",
            "/errors",
        )
        for user_id in (2, 3):
            self.login(user_id)
            for path in paths:
                with self.subTest(user_id=user_id, path=path):
                    self.assertEqual(self.client.get(path).status_code, 403)

    def test_impersonation_teacher_permissions_banner_stop_and_audit(self):
        token = self.owner_token()
        started = self.client.post(
            "/owner/impersonation/start/2",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        text = started.get_data(as_text=True)
        self.assertIn("Impersonating <strong>teacher</strong> as Teacher", text)
        self.assertIn("Owner <strong>owner</strong>", text)
        self.assertEqual(self.client.get("/owner").status_code, 403)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(session["user_id"], 2)
            stop_token = session["impersonation_stop_csrf_token"]
            self.assertNotIn("password", session)
        audit = self.conn.execute(
            "SELECT * FROM impersonation_audit ORDER BY audit_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(audit["acting_owner_user_id"], 1)
        self.assertEqual(audit["target_user_id"], 2)
        self.assertEqual(audit["outcome"], "active")
        stopped = self.client.post(
            "/owner/impersonation/stop",
            data={"csrf_token": stop_token},
            follow_redirects=True,
        )
        self.assertIn(b"RootED Owner Workspace", stopped.data)
        with self.client.session_transaction() as session:
            self.assertEqual(session["user_id"], 1)
            self.assertNotIn("impersonation_audit_id", session)
        audit = self.conn.execute(
            "SELECT * FROM impersonation_audit WHERE audit_id=?", (audit["audit_id"],)
        ).fetchone()
        self.assertEqual(audit["outcome"], "completed")
        self.assertIsNotNone(audit["stop_timestamp"])

    def test_impersonation_security_rejections_and_csrf(self):
        self.login(2)
        self.assertEqual(
            self.client.post("/owner/impersonation/start/3", data={"csrf_token": "x"}).status_code,
            403,
        )
        token = self.owner_token()
        self.assertEqual(self.client.post("/owner/impersonation/start/2").status_code, 400)
        self.assertEqual(
            self.client.post(
                "/owner/impersonation/start/4", data={"csrf_token": token}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/owner/impersonation/start/3", data={"csrf_token": token}
            ).status_code,
            302,
        )
        with self.client.session_transaction() as session:
            stop_token = session["impersonation_stop_csrf_token"]
        self.assertEqual(
            self.client.post(
                "/owner/impersonation/start/2", data={"csrf_token": token}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.post("/owner/impersonation/stop").status_code, 400)
        self.assertEqual(
            self.client.post(
                "/owner/impersonation/stop", data={"csrf_token": stop_token}
            ).status_code,
            302,
        )

    def test_shared_header_is_role_aware_and_uses_display_identity(self):
        provider_subject = "provider-subject-must-not-be-shown"
        self.conn.execute(
            """
            INSERT INTO user_auth_identities
              (user_id, provider, provider_subject, verified_email, display_name,
               created_at, last_login_at)
            VALUES (2, 'google', ?, 'teacher@example.org', 'Taylor Teacher', 1, 1)
            """,
            (provider_subject,),
        )
        self.conn.commit()

        self.login(1)
        for path in (
            "/owner",
            "/owner/account-administration",
            "/owner/impersonation",
            "/owner/question-flags",
            "/owner/developer-diagnostics",
        ):
            with self.subTest(role="owner", path=path):
                text = self.client.get(path).get_data(as_text=True)
                self.assertEqual(text.count("data-authenticated-header"), 1)
                self.assertEqual(text.count(">Logout</button>"), 1)
                self.assertIn('href="/owner">RootED</a>', text)

        self.login(2)
        teacher = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Teacher Workspace", teacher)
        self.assertIn("Taylor Teacher", teacher)
        self.assertNotIn(provider_subject, teacher)
        self.assertIn('href="/teacher">RootED</a>', teacher)
        self.assertNotIn("People &amp; Access", teacher)
        self.assertNotIn("Impersonate User", teacher)
        self.assertEqual(teacher.count(">Logout</button>"), 1)

        self.login(3)
        student = self.client.get("/restricted").get_data(as_text=True)
        self.assertIn("Student Learning", student)
        self.assertIn('href="/student?home=1">RootED</a>', student)
        self.assertNotIn("Question Flags", student)
        self.assertNotIn("People &amp; Access", student)
        self.assertEqual(student.count(">Logout</button>"), 1)

        with self.client.session_transaction() as session:
            session.clear()
        public = self.client.get("/login").get_data(as_text=True)
        self.assertNotIn("data-authenticated-header", public)

    def test_impersonated_header_follows_effective_role_and_full_logout(self):
        token = self.owner_token()
        self.client.post(
            "/owner/impersonation/start/2",
            data={"csrf_token": token},
        )
        page = self.client.get("/dashboard")
        text = page.get_data(as_text=True)
        self.assertIn("Teacher Workspace", text)
        self.assertIn("Stop Impersonating", text)
        self.assertNotIn("Return to Owner Workspace", text)
        self.assertNotIn("People &amp; Access", text)
        self.assertNotIn("Impersonate User", text)
        with self.client.session_transaction() as session:
            logout_token = session["logout_csrf_token"]
            self.assertIn("impersonation_audit_id", session)
        response = self.client.post(
            "/logout",
            data={"csrf_token": logout_token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)
            self.assertNotIn("impersonation_audit_id", session)

    def test_logout_is_post_only_csrf_protected_and_clears_preview_state(self):
        self.login(2)
        with self.client.session_transaction() as session:
            session["student_mode_preview"] = {"temporary": True}
            session["locked_payload"] = {"temporary": True}
        page = self.client.get("/student")
        self.assertIn(b"Student Mode Preview", page.data)
        self.assertIn(b"Leave Student Mode", page.data)
        self.assertNotIn(b"Return to Owner Workspace", page.data)
        self.assertEqual(self.client.get("/logout").status_code, 405)
        self.assertEqual(self.client.post("/logout").status_code, 400)
        with self.client.session_transaction() as session:
            logout_token = session["logout_csrf_token"]
        self.client.post("/logout", data={"csrf_token": logout_token})
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)
            self.assertNotIn("student_mode_preview", session)
            self.assertNotIn("locked_payload", session)

    def test_logout_ends_impersonation(self):
        token = self.owner_token()
        self.client.post("/owner/impersonation/start/2", data={"csrf_token": token})
        with self.client.session_transaction() as session:
            session["logout_csrf_token"] = "logout-test-token"
        self.client.post("/logout", data={"csrf_token": "logout-test-token"})
        audit = self.conn.execute(
            "SELECT * FROM impersonation_audit ORDER BY audit_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(audit["termination_reason"], "logout")
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)
        weak = self.create_account(
            username="weak.account",
            password="password",
        )
        self.assertIn(b"at least 12 characters", weak.data)
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM users WHERE username='weak.account'"
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
