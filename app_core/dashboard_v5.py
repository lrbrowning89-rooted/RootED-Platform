from flask import (
    Flask,
    request,
    render_template_string,
    make_response,
    session,
    redirect,
    url_for,
    flash,
    abort,
    g,
)
import sqlite3
import time
import csv
import io
import uuid
import os
import json
import logging
from logging.handlers import RotatingFileHandler
import traceback
import secrets
import hmac
from functools import wraps
from urllib.parse import urlsplit

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth
from markupsafe import Markup

from app_core.config import (
    ALLOW_SSO_AUTO_CREATE,
    DATABASE_PATH,
    DEFAULT_PII_MODE,
    ENABLE_GOOGLE_AUTH,
    ENABLE_MICROSOFT_AUTH,
    FERPA_ENFORCED,
    SESSION_COOKIE_SECURE,
    SECRET_KEY,
    TRUST_PROXY_HEADERS,
)
from app_core import adaptive_engine as ae

# ---------- App metadata ----------
APP_VERSION = "v0.5 – Rolling-7 Engine Active"
APP_NAME = "Adaptive NGSS Platform"

# Classroom launch manifest. Adaptive levels 1/2/3 live inside each objective;
# objective letters are the approved NGSS-aligned learning objectives.
LAUNCH_STANDARD_IDS = ("MS-LS1-1", "MS-LS1-2", "MS-LS1-3")
LAUNCH_OBJECTIVE_IDS = (
    "MS-LS1-1A",
    "MS-LS1-1B",
    "MS-LS1-2A",
    "MS-LS1-2B",
    "MS-LS1-3A",
    "MS-LS1-3B",
    "MS-LS1-3C",
)
LAUNCH_OBJECTIVE_ID_SET = set(LAUNCH_OBJECTIVE_IDS)
LAUNCH_STANDARD_ID_SET = set(LAUNCH_STANDARD_IDS)
QUESTION_FLAG_CATEGORIES = (
    ("incorrect_answer", "The answer looks wrong"),
    ("confusing_question", "This question is confusing"),
    ("visual_problem", "The picture or model has a problem"),
    ("display_problem", "Something does not look right on my screen"),
    ("accessibility_problem", "This question is hard to read or use"),
    ("duplicate_question", "I have already seen this question"),
    ("other", "Something else"),
)
QUESTION_FLAG_CATEGORY_LABELS = dict(QUESTION_FLAG_CATEGORIES)
QUESTION_FLAG_CATEGORY_SET = set(QUESTION_FLAG_CATEGORY_LABELS)
LEGACY_QUESTION_FLAG_CATEGORIES = {
    "incorrect answer or scoring": "incorrect_answer",
    "confusing wording": "confusing_question",
    "image/model problem": "visual_problem",
    "formatting/rendering problem": "display_problem",
    "accessibility problem": "accessibility_problem",
    "duplicate question": "duplicate_question",
}
QUESTION_FLAG_COMMENT_MAX_LENGTH = 500
QUESTION_FLAG_STATUSES = (
    "open",
    "teacher_resolved",
    "escalated",
    "owner_reviewing",
    "fixed",
    "closed",
)
QUESTION_FLAG_STATUS_FILTERS = {
    "current": {
        "label": "Current",
        "statuses": ("open",),
        "empty": "No open question reports.",
    },
    "resolved": {
        "label": "Resolved",
        "statuses": ("teacher_resolved", "fixed", "closed"),
        "empty": "No resolved reports.",
    },
    "sent": {
        "label": "Sent to RootED",
        "statuses": ("escalated", "owner_reviewing"),
        "empty": "No reports have been sent to RootED Support.",
    },
}
QUESTION_FLAG_DEFAULT_STATUS_FILTER = "current"
QUESTION_FLAG_DEFAULT_CATEGORY_FILTER = "all"
OWNER_QUESTION_FLAG_FILTERS = {
    "needs-review": {
        "label": "Needs Review",
        "statuses": ("escalated",),
        "empty": "No escalated reports are waiting for review.",
    },
    "in-review": {
        "label": "In Review",
        "statuses": ("owner_reviewing",),
        "empty": "No reports are currently in owner review.",
    },
    "completed": {
        "label": "Completed",
        "statuses": ("fixed", "closed"),
        "empty": "No owner-reviewed reports have been completed.",
    },
}
OWNER_QUESTION_FLAG_DEFAULT_FILTER = "needs-review"
QUESTION_REFERENCE_CSS = """
  .question-reference{display:grid;gap:5px;max-width:520px}
  .question-reference-label{font-size:11px;font-weight:850;color:#6b7280;text-transform:uppercase;letter-spacing:.04em}
  .question-reference-stem{display:block;color:#1f2937;font-weight:850;line-height:1.35;overflow-wrap:anywhere}
  .question-reference-stem-link{text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}
  .question-reference-meta{display:block;color:#6b7280;font-size:12px;line-height:1.35;overflow-wrap:anywhere}
  .question-reference-meta-link{text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}
  .question-reference-stem-link:focus,.question-reference-meta-link:focus{outline:3px solid #93c5fd;outline-offset:2px;border-radius:4px}
  .question-reference-missing .question-reference-stem{color:#4b5563}
"""
OBSOLETE_LAUNCH_OBJECTIVE_IDS = (
    "MS-LS1-2C",
    "MS-LS1-2D",
    "MS-LS1-2E",
    "MS-LS1-2F",
    "MS-LS1-2G",
    "MS-LS1-3E",
)


def sql_placeholders(values) -> str:
    return ",".join("?" for _ in values)


def is_launch_standard_id(standard_id: str | None) -> bool:
    return bool(standard_id and standard_id in LAUNCH_STANDARD_ID_SET)


def is_launch_objective_id(objective_id: str | None) -> bool:
    return bool(objective_id and objective_id in LAUNCH_OBJECTIVE_ID_SET)


def canonical_objective_order(objective_id: str | None) -> int:
    try:
        return LAUNCH_OBJECTIVE_IDS.index(objective_id)
    except ValueError:
        return len(LAUNCH_OBJECTIVE_IDS)


def canonical_objective_sort_key(row) -> tuple[int, str]:
    objective_id = row_get(row, "objective_id", None)
    return canonical_objective_order(objective_id), objective_id or ""


def order_objective_rows(rows):
    return sorted(rows, key=canonical_objective_sort_key)


def next_launch_objective_id(current_objective_id: str | None) -> str | None:
    if not is_launch_objective_id(current_objective_id):
        return None
    index = canonical_objective_order(current_objective_id)
    if index + 1 >= len(LAUNCH_OBJECTIVE_IDS):
        return None
    return LAUNCH_OBJECTIVE_IDS[index + 1]

# ---------- Database setup ----------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = DATABASE_PATH

# ---------- Flask app setup ----------
app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    # OAuth callbacks are top-level GET navigations, so Lax preserves the
    # session state while preventing cross-site subrequests from sending it.
    SESSION_COOKIE_SAMESITE="Lax",
)
if TRUST_PROXY_HEADERS:
    # Render terminates HTTPS before forwarding to Gunicorn. Trust exactly one
    # hosting-proxy hop so Flask generates rooted.school HTTPS callback URLs.
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
    )

# ---------- Logging setup ----------
LOG_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "ngss_errors.log")

logger = logging.getLogger("ngss_app")
logger.setLevel(logging.INFO)

if not logger.handlers:
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

if not any(getattr(handler, "_rooted_render_stream", False) for handler in logger.handlers):
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    stream_handler._rooted_render_stream = True
    logger.addHandler(stream_handler)

PUBLIC_CRAWLER_LOG_PATHS = {"/", "/landing", "/robots.txt", "/static/Logo.png"}
PUBLIC_CRAWLER_UA_MARKERS = ("facebookexternalhit", "facebot")
PUBLIC_CRAWLER_SAFE_HEADERS = (
    "Accept",
    "Accept-Language",
    "CF-Connecting-IP",
    "CF-Ray",
    "Host",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
)


@app.after_request
def log_public_crawler_request(response):
    user_agent = request.headers.get("User-Agent", "")
    user_agent_lower = user_agent.lower()

    if request.path not in PUBLIC_CRAWLER_LOG_PATHS or not any(
        marker in user_agent_lower for marker in PUBLIC_CRAWLER_UA_MARKERS
    ):
        return response

    safe_headers = {
        header: request.headers.get(header)
        for header in PUBLIC_CRAWLER_SAFE_HEADERS
        if request.headers.get(header)
    }
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "path": request.path,
        "method": request.method,
        "status": response.status_code,
        "user_agent": user_agent,
        "remote_addr": request.remote_addr,
        "x_forwarded_for": request.headers.get("X-Forwarded-For"),
        "cf_ray": request.headers.get("CF-Ray"),
        "headers": safe_headers,
    }
    logger.info("PUBLIC_CRAWLER_REQUEST %s", json.dumps(payload, sort_keys=True))
    return response

# ---------- OAuth / SSO setup ----------
oauth = OAuth(app)

# Google SSO (OIDC)
oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# Microsoft SSO (OIDC; common tenant by default)
oauth.register(
    name="microsoft",
    client_id=os.environ.get("MICROSOFT_CLIENT_ID") or os.environ.get("MS_CLIENT_ID"),
    client_secret=os.environ.get("MICROSOFT_CLIENT_SECRET") or os.environ.get("MS_CLIENT_SECRET"),
    server_metadata_url=(
        "https://login.microsoftonline.com/"
        f"{os.environ.get('MICROSOFT_TENANT', 'common')}/v2.0/.well-known/openid-configuration"
    ),
    client_kwargs={"scope": "openid email profile"},
)

# ---------- Authorization helpers / decorators ----------
def is_sso_provider_enabled(provider: str) -> bool:
    return (
        (provider == "google" and ENABLE_GOOGLE_AUTH)
        or (provider == "microsoft" and ENABLE_MICROSOFT_AUTH)
    )


def microsoft_multitenant_claims_options(client):
    """Build Microsoft's required issuer validation for tenant-independent metadata."""

    def validate_issuer(claims, issuer):
        tenant_id = claims.get("tid")
        if not tenant_id or not issuer:
            return False
        try:
            canonical_tenant_id = str(uuid.UUID(str(tenant_id)))
        except (ValueError, TypeError, AttributeError):
            return False

        metadata = client.load_server_metadata()
        issuer_template = metadata.get("issuer")
        if not issuer_template:
            return False
        expected_issuer = issuer_template.replace(
            "{tenantid}", canonical_tenant_id
        ).replace("{tenantId}", canonical_tenant_id)
        if issuer != expected_issuer:
            return False

        # Microsoft tenant-independent JWKS entries scope each signing key to
        # either a concrete issuer or the same {tenantid} template. Validate
        # that scope in addition to Authlib's signature and kid checks.
        kid = claims.header.get("kid")
        if not kid:
            return False
        keys = client.fetch_jwk_set().get("keys", [])
        for key in keys:
            if key.get("kid") != kid:
                continue
            key_issuer = key.get("issuer")
            if not key_issuer:
                return False
            expected_key_issuer = key_issuer.replace(
                "{tenantid}", canonical_tenant_id
            ).replace("{tenantId}", canonical_tenant_id)
            return expected_key_issuer == issuer
        return False

    return {
        "iss": {
            "essential": True,
            "validate": validate_issuer,
        },
        "tid": {
            "essential": True,
        },
    }


def current_user():
    """Return the active database user for this request, never a session role."""
    if hasattr(g, "_rooted_current_user"):
        return g._rooted_current_user

    user_id = session.get("user_id")
    if not user_id:
        g._rooted_current_user = None
        return None

    conn = get_conn()
    try:
        user = conn.execute(
            "SELECT * FROM users WHERE id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    if not user or user["role"] not in ("teacher", "student"):
        g._rooted_current_user = None
        return None

    g._rooted_current_user = user
    return user


def account_role(user=None) -> str | None:
    """Return the provider-neutral account role, falling back for legacy rows."""
    user = user or current_user()
    if not user:
        return None
    return user["account_role"] or user["role"]


def has_platform_role(platform_role: str, user=None) -> bool:
    if platform_role != "owner":
        return False
    user = user or current_user()
    if not user:
        return False

    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM user_platform_roles
            WHERE user_id = ?
              AND platform_role = ?
              AND revoked_at IS NULL
            LIMIT 1
            """,
            (user["id"], platform_role),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def has_instructional_authorization(
    instructional_role: str, user=None
) -> bool:
    if instructional_role != "teacher":
        return False
    user = user or current_user()
    if not user:
        return False
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM user_instructional_authorizations
            WHERE user_id = ?
              AND instructional_role = ?
              AND revoked_at IS NULL
            LIMIT 1
            """,
            (user["id"], instructional_role),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def is_teacher(user=None) -> bool:
    return has_instructional_authorization("teacher", user)


def is_student(user=None) -> bool:
    user = user or current_user()
    return bool(user and account_role(user) == "student")


def is_owner(user=None) -> bool:
    return has_platform_role("owner", user)


def can_access_teacher_tools(user=None) -> bool:
    user = user or current_user()
    return is_teacher(user)


def effective_session_role(user=None) -> str | None:
    """Compatibility value for existing views; never an authorization source."""
    user = user or current_user()
    if is_teacher(user):
        return "teacher"
    return account_role(user)


def can_access_student_instruction(user=None) -> bool:
    user = user or current_user()
    if not is_student(user) or not user["linked_student_id"]:
        return False
    conn = get_conn()
    try:
        return conn.execute(
            """
            SELECT 1 FROM class_enrollments ce
            JOIN class_sections cs ON cs.class_id=ce.class_id
            WHERE ce.student_id=? AND ce.is_active=1 AND cs.is_active=1 LIMIT 1
            """,
            (user["linked_student_id"],),
        ).fetchone() is not None
    finally:
        conn.close()


def get_post_login_destination(user=None) -> str:
    user = user or current_user()
    if is_owner(user):
        return url_for("owner_home")
    if can_access_teacher_tools(user):
        return url_for("teacher_home")
    if can_access_student_instruction(user):
        return url_for("student_view")
    return url_for("restricted_onboarding")


def safe_local_redirect(value: str | None) -> str | None:
    """Accept only an absolute-path destination on this application."""
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    return value


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            session.clear()
            flash("Please log in first.")
            return redirect(url_for("login"))
        session["role"] = effective_session_role(user)
        session["username"] = user["username"]
        return f(*args, **kwargs)

    return wrapper


def teacher_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            session.clear()
            flash("Please log in as a teacher.")
            return redirect(url_for("login"))
        session["role"] = effective_session_role(user)
        session["username"] = user["username"]
        if not can_access_teacher_tools(user):
            flash("Teacher access only.")
            return redirect(url_for("restricted_onboarding"))
        return f(*args, **kwargs)

    return wrapper


def student_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            session.clear()
            flash("Please log in as a student.")
            return redirect(url_for("login"))
        session["role"] = effective_session_role(user)
        session["username"] = user["username"]
        if not can_access_student_instruction(user):
            flash("Student access only.")
            return redirect(url_for("restricted_onboarding"))
        return f(*args, **kwargs)

    return wrapper


def instruction_required(f):
    """Allow only an authorized teacher preview or enrolled student."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            session.clear()
            flash("Please log in first.")
            return redirect(url_for("login"))
        session["role"] = effective_session_role(user)
        session["username"] = user["username"]
        if not (
            can_access_teacher_tools(user)
            or can_access_student_instruction(user)
        ):
            flash("Connect to an active class to continue.")
            return redirect(url_for("restricted_onboarding"))
        return f(*args, **kwargs)

    return wrapper


def owner_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            session.clear()
            flash("Please log in first.")
            return redirect(url_for("login"))
        session["role"] = effective_session_role(user)
        session["username"] = user["username"]
        if not is_owner(user):
            abort(403)
        return f(*args, **kwargs)

    return wrapper


# Backward-compatible names used by the existing routes.
require_login = login_required
require_teacher = teacher_required
require_student = student_required


# ---------- Schema ----------
def _backfill_routing_level_attempts(conn: sqlite3.Connection) -> None:
    """Assign legacy accepted responses to contiguous, auditable level visits."""
    legacy = conn.execute(
        """
        SELECT r.id, r.student_id, r.standard_id, r.level, r.correct, r.ts,
               q.objective_id
        FROM responses r
        JOIN questions q ON q.question_id = r.question_id
        WHERE r.routing_level_attempt_id IS NULL
        ORDER BY r.student_id, r.ts, r.id
        """
    ).fetchall()
    if not legacy:
        return

    groups = []
    current = None
    for row in legacy:
        key = (
            row["student_id"],
            row["standard_id"],
            row["objective_id"],
            int(row["level"]),
        )
        if current is None or current["key"] != key:
            current = {"key": key, "rows": []}
            groups.append(current)
        current["rows"].append(row)

    last_group_by_student = {}
    for group in groups:
        last_group_by_student[group["key"][0]] = group

    for group in groups:
        student_id, standard_id, objective_id, level = group["key"]
        rows = group["rows"]
        correct_count = sum(int(row["correct"]) for row in rows)
        response_count = len(rows)
        score = correct_count / response_count
        progress = conn.execute(
            """
            SELECT ps.current_level, sos.current_objective_id
            FROM progress_state ps
            LEFT JOIN student_objective_state sos
              ON sos.student_id = ps.student_id
             AND sos.standard_id = ps.standard_id
             AND sos.status = 'active'
            WHERE ps.student_id = ? AND ps.standard_id = ?
            """,
            (student_id, standard_id),
        ).fetchone()
        is_active = bool(
            group is last_group_by_student[student_id]
            and progress
            and int(progress["current_level"]) == level
            and progress["current_objective_id"] == objective_id
        )
        existing_active = conn.execute(
            """
            SELECT * FROM routing_level_attempts
            WHERE student_id = ? AND status = 'active'
            """,
            (student_id,),
        ).fetchone()
        if (
            is_active
            and existing_active
            and existing_active["standard_id"] == standard_id
            and existing_active["objective_id"] == objective_id
            and int(existing_active["level"]) == level
        ):
            conn.executemany(
                """
                UPDATE responses
                SET routing_level_attempt_id = ?
                WHERE id = ? AND routing_level_attempt_id IS NULL
                """,
                [(existing_active["attempt_id"], row["id"]) for row in rows],
            )
            continue
        if is_active and existing_active:
            conn.execute(
                """
                UPDATE routing_level_attempts
                SET status='closed', ended_at=?, end_reason='historical_boundary'
                WHERE attempt_id=?
                """,
                (int(rows[0]["ts"]), existing_active["attempt_id"]),
            )
        attempt_id = f"RLA{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO routing_level_attempts
              (attempt_id, student_id, standard_id, objective_id, level, status,
               started_at, ended_at, end_reason, final_correct_count,
               final_response_count, final_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                student_id,
                standard_id,
                objective_id,
                level,
                "active" if is_active else "closed",
                int(rows[0]["ts"]),
                None if is_active else int(rows[-1]["ts"]),
                None if is_active else "historical_boundary",
                None if is_active else correct_count,
                None if is_active else response_count,
                None if is_active else score,
            ),
        )
        conn.executemany(
            """
            UPDATE responses
            SET routing_level_attempt_id = ?
            WHERE id = ? AND routing_level_attempt_id IS NULL
            """,
            [(attempt_id, row["id"]) for row in rows],
        )


def ensure_schema(conn: sqlite3.Connection) -> None:
    # Config for thresholds etc.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS config (
          key   TEXT PRIMARY KEY,
          value TEXT NOT NULL
        )
        """
    )

    # Students roster
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS students (
          student_id   TEXT PRIMARY KEY,
          first_name   TEXT,
          last_name    TEXT,
          grade        INTEGER,
          class_period TEXT
        )
        """
    )

    # Classroom sections and self-enrollment
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS class_sections (
          class_id        TEXT PRIMARY KEY,
          teacher_user_id INTEGER,
          name            TEXT NOT NULL,
          class_period    TEXT,
          join_code       TEXT UNIQUE NOT NULL,
          is_active       INTEGER NOT NULL DEFAULT 1,
          created_at      INTEGER NOT NULL,
          updated_at      INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS class_enrollments (
          class_id    TEXT NOT NULL,
          student_id  TEXT NOT NULL,
          enrolled_at INTEGER NOT NULL,
          enrolled_by TEXT NOT NULL DEFAULT 'self',
          is_active   INTEGER NOT NULL DEFAULT 1,
          archived_at INTEGER,
          archived_by_user_id INTEGER,
          archive_reason TEXT,
          reactivated_at INTEGER,
          reactivated_by_user_id INTEGER,
          PRIMARY KEY (class_id, student_id),
          FOREIGN KEY(class_id) REFERENCES class_sections(class_id) ON DELETE CASCADE,
          FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
          FOREIGN KEY(archived_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
          FOREIGN KEY(reactivated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    enrollment_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(class_enrollments)")
    }
    for column_name, column_sql in [
        ("is_active", "INTEGER NOT NULL DEFAULT 1"),
        ("archived_at", "INTEGER"),
        ("archived_by_user_id", "INTEGER"),
        ("archive_reason", "TEXT"),
        ("reactivated_at", "INTEGER"),
        ("reactivated_by_user_id", "INTEGER"),
    ]:
        if column_name not in enrollment_columns:
            conn.execute(
                f"ALTER TABLE class_enrollments ADD COLUMN {column_name} {column_sql}"
            )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_class_enrollments_student
        ON class_enrollments(student_id)
        """
    )

    # NGSS standards (core idea + grade band)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS standards (
          standard_id TEXT PRIMARY KEY,
          core_idea   TEXT,
          grade_band  TEXT
        )
        """
    )

    # Objectives
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS objectives (
          objective_id   TEXT PRIMARY KEY,
          standard_id    TEXT NOT NULL,
          objective_text TEXT,
          display_name   TEXT,
          student_description TEXT,
          order_in_band  INTEGER,
          FOREIGN KEY(standard_id) REFERENCES standards(standard_id)
        )
        """
    )
    objective_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(objectives)")
    }
    for column_name in ("display_name", "student_description"):
        if column_name not in objective_columns:
            conn.execute(f"ALTER TABLE objectives ADD COLUMN {column_name} TEXT")
    conn.execute(
        """
        UPDATE objectives SET display_name='Cell Anatomy'
        WHERE objective_id='MS-LS1-2A'
          AND (display_name IS NULL OR TRIM(display_name)='')
        """
    )

    # Questions
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS questions (
          question_id  TEXT PRIMARY KEY,
          objective_id TEXT NOT NULL,
          stem         TEXT,
          choice_a     TEXT,
          choice_b     TEXT,
          choice_c     TEXT,
          choice_d     TEXT,
          answer_key   TEXT,
          FOREIGN KEY(objective_id) REFERENCES objectives(objective_id)
        )
        """
    )

    # Attempts
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attempts (
          attempt_id    TEXT PRIMARY KEY,
          student_id    TEXT NOT NULL,
          question_id   TEXT NOT NULL,
          timestamp     INTEGER NOT NULL,
          response      TEXT,
          is_correct    INTEGER NOT NULL,
          time_seconds  REAL,
          skills_missed TEXT,
          FOREIGN KEY(student_id)  REFERENCES students(student_id),
          FOREIGN KEY(question_id) REFERENCES questions(question_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attempts_student_obj_ts
        ON attempts(student_id, question_id, timestamp DESC)
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS question_flags (
          flag_id             TEXT PRIMARY KEY,
          question_id         TEXT NOT NULL,
          objective_id        TEXT NOT NULL,
          standard_id         TEXT NOT NULL,
          reporter_user_id    INTEGER,
          reporter_role       TEXT NOT NULL CHECK(reporter_role IN ('teacher', 'student')),
          class_id            TEXT,
          student_id          TEXT,
          category            TEXT NOT NULL,
          comment             TEXT,
          page_context        TEXT,
          created_ts          INTEGER NOT NULL,
          status              TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'teacher_resolved', 'escalated', 'owner_reviewing', 'fixed', 'closed')),
          escalated_by_user_id INTEGER,
          escalated_at        INTEGER,
          escalation_note     TEXT,
          owner_reviewing_at  INTEGER,
          owner_reviewing_by_user_id INTEGER,
          resolved_ts         INTEGER,
          resolution_note     TEXT,
          resolved_by_user_id INTEGER,
          FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE,
          FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE,
          FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
          FOREIGN KEY(class_id) REFERENCES class_sections(class_id) ON DELETE SET NULL,
          FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE SET NULL
        )
        """
    )
    flag_schema = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'question_flags'
        """
    ).fetchone()
    if flag_schema and "('open', 'resolved')" in (flag_schema["sql"] or ""):
        conn.execute("ALTER TABLE question_flags RENAME TO question_flags_old")
        conn.execute(
            """
            CREATE TABLE question_flags (
              flag_id             TEXT PRIMARY KEY,
              question_id         TEXT NOT NULL,
              objective_id        TEXT NOT NULL,
              standard_id         TEXT NOT NULL,
              reporter_user_id    INTEGER,
              reporter_role       TEXT NOT NULL CHECK(reporter_role IN ('teacher', 'student')),
              class_id            TEXT,
              student_id          TEXT,
              category            TEXT NOT NULL,
              comment             TEXT,
              page_context        TEXT,
              created_ts          INTEGER NOT NULL,
              status              TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'teacher_resolved', 'escalated', 'owner_reviewing', 'fixed', 'closed')),
              escalated_by_user_id INTEGER,
              escalated_at        INTEGER,
              escalation_note     TEXT,
              owner_reviewing_at  INTEGER,
              owner_reviewing_by_user_id INTEGER,
              resolved_ts         INTEGER,
              resolution_note     TEXT,
              resolved_by_user_id INTEGER,
              FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE,
              FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE,
              FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
              FOREIGN KEY(class_id) REFERENCES class_sections(class_id) ON DELETE SET NULL,
              FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO question_flags
              (flag_id, question_id, objective_id, standard_id, reporter_user_id,
               reporter_role, class_id, student_id, category, comment, page_context,
               created_ts, status, resolved_ts, resolution_note, resolved_by_user_id)
            SELECT flag_id, question_id, objective_id, standard_id, reporter_user_id,
                   reporter_role, class_id, student_id, category, comment, page_context,
                   created_ts,
                   CASE WHEN status = 'resolved' THEN 'teacher_resolved' ELSE status END,
                   resolved_ts, resolution_note, resolved_by_user_id
            FROM question_flags_old
            """
        )
        conn.execute("DROP TABLE question_flags_old")
        conn.commit()
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_question_flags_status_created
        ON question_flags(status, created_ts DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_question_flags_question_status
        ON question_flags(question_id, status)
        """
    )
    for col_name, col_type in [
        ("escalated_by_user_id", "INTEGER"),
        ("escalated_at", "INTEGER"),
        ("escalation_note", "TEXT"),
        ("owner_reviewing_at", "INTEGER"),
        ("owner_reviewing_by_user_id", "INTEGER"),
    ]:
        try:
            conn.execute(f"SELECT {col_name} FROM question_flags LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE question_flags ADD COLUMN {col_name} {col_type}")
            conn.commit()
    conn.execute(
        """
        UPDATE question_flags
        SET status = 'teacher_resolved'
        WHERE status = 'resolved'
        """
    )
    for legacy_category, code in LEGACY_QUESTION_FLAG_CATEGORIES.items():
        conn.execute(
            "UPDATE question_flags SET category = ? WHERE category = ?",
            (code, legacy_category),
        )
    conn.commit()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS question_flag_events (
          event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
          flag_id        TEXT NOT NULL,
          from_status    TEXT,
          to_status      TEXT NOT NULL CHECK(to_status IN ('open', 'teacher_resolved', 'escalated', 'owner_reviewing', 'fixed', 'closed')),
          actor_user_id  INTEGER NOT NULL,
          actor_authority TEXT NOT NULL,
          note           TEXT,
          created_ts     INTEGER NOT NULL,
          FOREIGN KEY(flag_id) REFERENCES question_flags(flag_id) ON DELETE CASCADE,
          FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_question_flag_events_flag_created
        ON question_flag_events(flag_id, created_ts, event_id)
        """
    )
    conn.commit()

    # Objective map overrides
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lists (
          objective_id   TEXT PRIMARY KEY,
          advance_to     TEXT,
          remediation_to TEXT
        )
        """
    )

    # Graph paths
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paths (
          id        INTEGER PRIMARY KEY AUTOINCREMENT,
          from_node TEXT NOT NULL,
          to_node   TEXT NOT NULL,
          note      TEXT
        )
        """
    )

    # Cross-band progression rules
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS progression_rules (
          id                INTEGER PRIMARY KEY AUTOINCREMENT,
          core_idea         TEXT NOT NULL,
          from_band         TEXT NOT NULL,
          to_band           TEXT NOT NULL,
          promote_threshold REAL NOT NULL
        )
        """
    )

    # Objective levels (1/2/3)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS objective_levels (
          objective_id TEXT PRIMARY KEY,
          level        INTEGER NOT NULL
        )
        """
    )

    # Standard-level responses
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS responses (
          id          INTEGER PRIMARY KEY,
          student_id  TEXT NOT NULL,
          standard_id TEXT NOT NULL,
          level       INTEGER NOT NULL DEFAULT 1,
          question_id TEXT NOT NULL,
          correct     INTEGER NOT NULL,
          ts          INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_responses_student_std_lvl_ts
        ON responses(student_id, standard_id, level, ts DESC)
        """
    )
    response_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(responses)").fetchall()
    }
    if "routing_level_attempt_id" not in response_columns:
        conn.execute(
            "ALTER TABLE responses ADD COLUMN routing_level_attempt_id TEXT"
        )

    # Rolling-7 engine state tables
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS progress_state (
          id            INTEGER PRIMARY KEY,
          student_id    TEXT NOT NULL,
          standard_id   TEXT NOT NULL,
          current_level INTEGER NOT NULL DEFAULT 1,
          status        TEXT NOT NULL DEFAULT 'practicing',
          rolling_avg   REAL NOT NULL DEFAULT 0.0,
          locked        INTEGER NOT NULL DEFAULT 0,
          locked_reason TEXT,
          last_update   INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_state_unique
        ON progress_state(student_id, standard_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_level_attempts (
          attempt_id TEXT PRIMARY KEY,
          student_id TEXT NOT NULL,
          standard_id TEXT NOT NULL,
          objective_id TEXT NOT NULL,
          level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3),
          status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('active', 'closed', 'invalidated')),
          started_at INTEGER NOT NULL,
          ended_at INTEGER,
          end_reason TEXT,
          final_correct_count INTEGER,
          final_response_count INTEGER,
          final_score REAL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_routing_level_attempt_active_student
        ON routing_level_attempts(student_id) WHERE status = 'active'
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_responses_routing_level_attempt
        ON responses(routing_level_attempt_id, id)
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS student_objective_state (
          student_id            TEXT NOT NULL,
          standard_id           TEXT NOT NULL,
          current_objective_id  TEXT NOT NULL,
          status                TEXT DEFAULT 'active',
          last_update           INTEGER,
          PRIMARY KEY (student_id, standard_id),
          FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
          FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
          FOREIGN KEY(current_objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
        )
        """
    )
    _backfill_routing_level_attempts(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS student_growth_progress (
          attempt_id TEXT PRIMARY KEY,
          student_id TEXT NOT NULL,
          standard_id TEXT NOT NULL,
          objective_id TEXT NOT NULL,
          visible_stage INTEGER NOT NULL DEFAULT 1 CHECK(visible_stage BETWEEN 1 AND 4),
          presentation_state TEXT NOT NULL DEFAULT 'strengthening'
            CHECK(presentation_state IN ('growing', 'strengthening', 'reviewing', 'complete')),
          is_active INTEGER NOT NULL DEFAULT 1,
          started_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          completed_at INTEGER,
          FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
          FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
          FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_student_growth_active
        ON student_growth_progress(student_id)
        WHERE is_active = 1
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS student_question_deliveries (
          submission_token TEXT PRIMARY KEY,
          student_id TEXT NOT NULL,
          growth_attempt_id TEXT NOT NULL,
          standard_id TEXT NOT NULL,
          objective_id TEXT NOT NULL,
          level INTEGER NOT NULL,
          question_id TEXT NOT NULL,
          sequence_number INTEGER NOT NULL,
          served_at INTEGER NOT NULL,
          consumed_at INTEGER,
          invalidated_at INTEGER,
          FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
          FOREIGN KEY(growth_attempt_id) REFERENCES student_growth_progress(attempt_id) ON DELETE CASCADE,
          FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE
        )
        """
    )
    delivery_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(student_question_deliveries)"
        ).fetchall()
    }
    if "sequence_number" not in delivery_columns:
        conn.execute(
            "ALTER TABLE student_question_deliveries ADD COLUMN sequence_number INTEGER"
        )
    conn.execute(
        """
        UPDATE student_question_deliveries
        SET sequence_number = (
          SELECT COUNT(*) - 1
          FROM student_question_deliveries AS prior
          WHERE prior.growth_attempt_id =
                  student_question_deliveries.growth_attempt_id
            AND prior.level = student_question_deliveries.level
            AND (
              prior.served_at < student_question_deliveries.served_at
              OR (
                prior.served_at = student_question_deliveries.served_at
                AND prior.submission_token <=
                    student_question_deliveries.submission_token
              )
            )
        )
        WHERE sequence_number IS NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_student_question_delivery_active
        ON student_question_deliveries(student_id)
        WHERE consumed_at IS NULL AND invalidated_at IS NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_student_question_delivery_sequence
        ON student_question_deliveries(growth_attempt_id, level, sequence_number)
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remediation_links (
          id                INTEGER PRIMARY KEY,
          standard_id       TEXT NOT NULL,
          lower_standard_id TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS progression_links (
          id               INTEGER PRIMARY KEY,
          standard_id      TEXT NOT NULL,
          next_standard_id TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS origin_links (
          id               INTEGER PRIMARY KEY,
          student_id       TEXT NOT NULL,
          rem_standard_id  TEXT NOT NULL,
          from_standard_id TEXT NOT NULL,
          from_level       INTEGER NOT NULL,
          ts               INTEGER NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
          id          INTEGER PRIMARY KEY,
          student_id  TEXT NOT NULL,
          standard_id TEXT NOT NULL,
          event       TEXT NOT NULL,
          details     TEXT NOT NULL,
          ts          INTEGER NOT NULL
        )
        """
    )

    # Error logs
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS error_logs (
          id        INTEGER PRIMARY KEY AUTOINCREMENT,
          ts        INTEGER NOT NULL,
          level     TEXT NOT NULL,
          path      TEXT,
          user_id   TEXT,
          role      TEXT,
          message   TEXT NOT NULL,
          traceback TEXT
        )
        """
    )

    # Users (local + SSO)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
          id                INTEGER PRIMARY KEY AUTOINCREMENT,
          username          TEXT UNIQUE NOT NULL,
          password_hash     TEXT NOT NULL,
          role              TEXT NOT NULL CHECK(role IN ('teacher', 'student')),
          account_role      TEXT CHECK(account_role IN ('pending', 'student', 'teacher')),
          linked_student_id TEXT,
          is_active         INTEGER NOT NULL DEFAULT 1,
          -- SSO fields (optional)
          sso_provider      TEXT,
          sso_subject       TEXT,
          sso_email         TEXT,
          last_login_ts     INTEGER,
          FOREIGN KEY(linked_student_id) REFERENCES students(student_id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_auth_identities (
          identity_id      INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id          INTEGER NOT NULL,
          provider         TEXT NOT NULL,
          provider_subject TEXT NOT NULL,
          verified_email   TEXT,
          display_name     TEXT,
          avatar_url       TEXT,
          created_at       INTEGER NOT NULL,
          last_login_at    INTEGER NOT NULL,
          revoked_at       INTEGER,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
          UNIQUE(provider, provider_subject)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_identities_email "
        "ON user_auth_identities(verified_email)"
    )
    now_ts = int(time.time())
    conn.execute(
        """
        INSERT OR IGNORE INTO user_auth_identities
          (user_id, provider, provider_subject, verified_email,
           created_at, last_login_at)
        SELECT id, sso_provider, sso_subject, sso_email,
               COALESCE(last_login_ts, ?), COALESCE(last_login_ts, ?)
        FROM users
        WHERE sso_provider IS NOT NULL AND sso_subject IS NOT NULL
        """,
        (now_ts, now_ts),
    )

    platform_role_table = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'user_platform_roles'
        """
    ).fetchone()
    if platform_role_table:
        platform_role_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(user_platform_roles)")
        }
        if "grant_id" not in platform_role_columns:
            conn.execute(
                "ALTER TABLE user_platform_roles RENAME TO user_platform_roles_legacy"
            )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_platform_roles (
          grant_id      INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id       INTEGER NOT NULL,
          platform_role TEXT NOT NULL CHECK(platform_role IN ('owner')),
          granted_at    INTEGER NOT NULL,
          granted_by    INTEGER,
          revoked_at    INTEGER,
          revoked_by    INTEGER,
          grant_note    TEXT,
          revoke_note   TEXT,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
          FOREIGN KEY(granted_by) REFERENCES users(id) ON DELETE SET NULL,
          FOREIGN KEY(revoked_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    legacy_platform_role_table = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'user_platform_roles_legacy'
        """
    ).fetchone()
    if legacy_platform_role_table:
        conn.execute(
            """
            INSERT INTO user_platform_roles
              (user_id, platform_role, granted_at, grant_note)
            SELECT user_id, platform_role, granted_at,
                   'Migrated from Owner Role Phase 1'
            FROM user_platform_roles_legacy
            """
        )
        conn.execute("DROP TABLE user_platform_roles_legacy")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_user_platform_roles_active
        ON user_platform_roles(user_id, platform_role)
        WHERE revoked_at IS NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_platform_roles_history
        ON user_platform_roles(platform_role, revoked_at, user_id, granted_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_instructional_authorizations (
          authorization_id  INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id           INTEGER NOT NULL,
          instructional_role TEXT NOT NULL CHECK(instructional_role IN ('teacher')),
          granted_at        INTEGER NOT NULL,
          granted_by        INTEGER,
          revoked_at        INTEGER,
          revoked_by        INTEGER,
          grant_note        TEXT,
          revoke_note       TEXT,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
          FOREIGN KEY(granted_by) REFERENCES users(id) ON DELETE SET NULL,
          FOREIGN KEY(revoked_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_user_instructional_authorizations_active
        ON user_instructional_authorizations(user_id, instructional_role)
        WHERE revoked_at IS NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_instructional_authorizations_history
        ON user_instructional_authorizations(
          instructional_role, revoked_at, user_id, granted_at
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS access_authorization_audit_log (
          audit_id          INTEGER PRIMARY KEY AUTOINCREMENT,
          actor_user_id     INTEGER NOT NULL,
          target_user_id    INTEGER NOT NULL,
          action            TEXT NOT NULL
                            CHECK(action IN ('teacher_authorized')),
          authorization_id  INTEGER,
          outcome           TEXT NOT NULL
                            CHECK(outcome IN ('granted', 'already_granted')),
          created_at        INTEGER NOT NULL,
          request_ip        TEXT,
          user_agent        TEXT,
          FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE RESTRICT,
          FOREIGN KEY(target_user_id) REFERENCES users(id) ON DELETE RESTRICT,
          FOREIGN KEY(authorization_id)
            REFERENCES user_instructional_authorizations(authorization_id)
            ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_access_authorization_audit_target
        ON access_authorization_audit_log(target_user_id, created_at, audit_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS authorization_lifecycle_audit_log (
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
          actor_user_id INTEGER NOT NULL,
          target_user_id INTEGER NOT NULL,
          action TEXT NOT NULL CHECK(action IN ('teacher_revoked')),
          authorization_id INTEGER,
          outcome TEXT NOT NULL CHECK(outcome IN ('revoked', 'already_revoked', 'blocked_active_classes')),
          reason TEXT,
          created_at INTEGER NOT NULL,
          FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE RESTRICT,
          FOREIGN KEY(target_user_id) REFERENCES users(id) ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_lifecycle_audit_log (
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
          actor_user_id INTEGER NOT NULL,
          target_user_id INTEGER NOT NULL,
          action TEXT NOT NULL CHECK(action IN ('account_deactivated', 'account_reactivated')),
          outcome TEXT NOT NULL CHECK(outcome IN ('deactivated', 'reactivated', 'already_deactivated', 'already_active', 'blocked_last_owner')),
          reason TEXT,
          created_at INTEGER NOT NULL,
          FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE RESTRICT,
          FOREIGN KEY(target_user_id) REFERENCES users(id) ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS class_membership_audit_log (
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
          actor_user_id INTEGER NOT NULL,
          class_id TEXT NOT NULL,
          student_id TEXT NOT NULL,
          action TEXT NOT NULL CHECK(action IN ('membership_archived')),
          outcome TEXT NOT NULL CHECK(outcome IN ('archived', 'already_archived')),
          reason TEXT,
          created_at INTEGER NOT NULL,
          FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE RESTRICT
        )
        """
    )
    # Preserve existing teacher access while moving the source of truth away
    # from the legacy role fields. Pending accounts are authorized explicitly.
    conn.execute(
        """
        INSERT OR IGNORE INTO user_instructional_authorizations
          (user_id, instructional_role, granted_at, grant_note)
        SELECT id, 'teacher', COALESCE(last_login_ts, ?),
               'Backfilled from existing teacher authorization'
        FROM users
        WHERE COALESCE(account_role, role) = 'teacher'
          AND NOT EXISTS (
            SELECT 1
            FROM user_instructional_authorizations existing
            WHERE existing.user_id = users.id
              AND existing.instructional_role = 'teacher'
          )
        """,
        (int(time.time()),),
    )

    # ---------- Diagnostic Arena Basic Tables ----------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diagnostic_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          question_id TEXT NOT NULL,
          domain TEXT NOT NULL,
          FOREIGN KEY(question_id) REFERENCES questions(question_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diagnostic_sessions (
          id TEXT PRIMARY KEY,          -- UUID
          student_id TEXT NOT NULL,
          started_at INTEGER NOT NULL,  -- store as UNIX timestamp
          completed_at INTEGER,         -- NULL until finished
          status TEXT NOT NULL,         -- 'in_progress' or 'completed'
          FOREIGN KEY(student_id) REFERENCES students(student_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diagnostic_responses (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL,
          diagnostic_item_id INTEGER NOT NULL,
          is_correct INTEGER,           -- 1 = correct, 0 = incorrect
          FOREIGN KEY(session_id) REFERENCES diagnostic_sessions(id),
          FOREIGN KEY(diagnostic_item_id) REFERENCES diagnostic_items(id)
        )
        """
    )

    conn.commit()

    # Backfill is_active if needed
    try:
        conn.execute("SELECT is_active FROM users LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
        )
        conn.commit()

    # Neutral authorization role. NULL means "use legacy role" for existing rows.
    try:
        conn.execute("SELECT account_role FROM users LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            """
            ALTER TABLE users ADD COLUMN account_role TEXT
            CHECK(account_role IN ('pending', 'student', 'teacher'))
            """
        )
        conn.commit()

    # Backfill SSO columns if needed
    for col_def in [
        ("sso_provider", "TEXT"),
        ("sso_subject", "TEXT"),
        ("sso_email", "TEXT"),
        ("last_login_ts", "INTEGER"),
    ]:
        col_name, col_type = col_def
        try:
            conn.execute(f"SELECT {col_name} FROM users LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
            conn.commit()

    # Optional: index for SSO lookups
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_sso ON users (sso_provider, sso_subject)"
        )
        conn.commit()
    except sqlite3.OperationalError:
        # If columns are missing for some reason, we just skip the index
        pass


# One-time schema boot
with sqlite3.connect(DB) as _conn_boot:
    _conn_boot.row_factory = sqlite3.Row
    ensure_schema(_conn_boot)


# ---------- Helpers ----------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    except sqlite3.OperationalError:
        return set()


def get_question_model_id(conn: sqlite3.Connection, question_id: str | None) -> str | None:
    """
    Return the model_id attached to a question, when production metadata exists.
    Missing metadata is non-blocking so question delivery can continue unchanged.
    """
    if (
        not question_id
        or not table_exists(conn, "question_metadata")
        or "model_id" not in table_columns(conn, "question_metadata")
    ):
        return None

    row = conn.execute(
        """
        SELECT model_id
        FROM question_metadata
        WHERE question_id = ?
        LIMIT 1
        """,
        (question_id,),
    ).fetchone()
    if not row:
        return None

    model_id = (row["model_id"] or "").strip()
    return model_id or None


def get_model_asset(conn: sqlite3.Connection, model_id: str | None) -> dict | None:
    """
    Look up a future model_assets row without assuming the table is already present.
    Expected columns are intentionally flexible for Phase 2 planning:
    model_id plus optional asset_type/type, path/url/src, alt_text/alt, title,
    caption, mime_type, and data_json/spec_json/content_json.
    """
    if not model_id or not table_exists(conn, "model_assets"):
        return None

    columns = table_columns(conn, "model_assets")
    if "model_id" not in columns:
        return None

    candidate_columns = [
        "model_id",
        "asset_type",
        "type",
        "path",
        "url",
        "src",
        "alt_text",
        "alt",
        "title",
        "caption",
        "mime_type",
        "data_json",
        "spec_json",
        "content_json",
    ]
    selected_columns = [column for column in candidate_columns if column in columns]
    if selected_columns == ["model_id"]:
        return {"model_id": model_id}

    sql = f"""
        SELECT {", ".join(selected_columns)}
        FROM model_assets
        WHERE model_id = ?
        LIMIT 1
    """
    row = conn.execute(sql, (model_id,)).fetchone()
    if not row:
        return None

    asset = {column: row[column] for column in selected_columns}
    asset["asset_type"] = asset.get("asset_type") or asset.get("type")
    asset["src"] = asset.get("url") or asset.get("path") or asset.get("src")
    asset["alt_text"] = asset.get("alt_text") or asset.get("alt") or ""
    return asset


def resolve_model_asset_for_question(conn: sqlite3.Connection, question_id: str | None) -> dict:
    """
    Resolve model metadata for a question. Rendering stays opt-in at the route/template
    layer so missing metadata or assets never interrupt question delivery.
    """
    model_id = get_question_model_id(conn, question_id)
    if not model_id:
        return {"model_id": None, "asset": None, "missing_reason": "no_model_id"}

    asset = get_model_asset(conn, model_id)
    if not asset:
        return {
            "model_id": model_id,
            "asset": None,
            "missing_reason": "asset_not_found",
        }

    return {"model_id": model_id, "asset": asset, "missing_reason": None}


def static_image_asset_for_render(resolved_asset: dict | None) -> dict | None:
    """
    Convert a resolved model asset into a static PNG/SVG payload for the student UI.
    Non-image, missing, or nonexistent files return None so the question renders normally.
    """
    asset = (resolved_asset or {}).get("asset")
    if not asset:
        return None

    asset_type = (asset.get("asset_type") or "").strip().lower()
    if asset_type and asset_type != "image":
        return None

    src = (asset.get("src") or "").strip().replace("\\", "/")
    if not src or src.startswith(("http://", "https://", "//")):
        return None

    if src.startswith("/static/"):
        static_filename = src[len("/static/") :]
    elif src.startswith("static/"):
        static_filename = src[len("static/") :]
    elif src.startswith("app_core/static/"):
        static_filename = src[len("app_core/static/") :]
    else:
        static_filename = src.lstrip("/")

    static_filename = os.path.normpath(static_filename).replace("\\", "/")
    if static_filename.startswith("../") or static_filename == "..":
        return None

    extension = os.path.splitext(static_filename)[1].lower()
    if extension not in {".png", ".svg"}:
        return None

    static_root = os.path.abspath(app.static_folder)
    asset_path = os.path.abspath(os.path.join(static_root, static_filename))
    static_root_check = os.path.normcase(static_root + os.sep)
    asset_path_check = os.path.normcase(asset_path)
    if not asset_path_check.startswith(static_root_check) or not os.path.isfile(asset_path):
        return None

    alt_text = (asset.get("alt_text") or "").strip()
    return {
        "filename": static_filename,
        "title": (asset.get("title") or "").strip(),
        "caption": (asset.get("caption") or "").strip(),
        "alt_text": alt_text,
    }


def locked_student_id_for_session(conn):
    """
    Returns the student_id this session is allowed to act as.
    Teachers can impersonate via request args/forms.
    Students are hard-locked to linked_student_id (or username fallback).
    """
    role = session.get("role")
    user_id = session.get("user_id")

    if role == "teacher":
        # teacher can pick who they are viewing
        return request.values.get("student_id")

    if role == "student":
        user = get_user_by_id(conn, user_id)
        if not user:
            return None
        return user["linked_student_id"] or user["username"]

    return None

import uuid

def create_diagnostic_session(conn: sqlite3.Connection, student_id: str) -> str:
    """
    Creates a new diagnostic session row and returns the session_id.
    """
    session_id = str(uuid.uuid4())
    started_at = int(time.time())

    conn.execute(
        """
        INSERT INTO diagnostic_sessions (id, student_id, started_at, completed_at, status)
        VALUES (?, ?, ?, NULL, 'in_progress')
        """,
        (session_id, student_id, started_at),
    )
    conn.commit()
    return session_id

def get_diagnostic_items(conn: sqlite3.Connection, domain: str, limit: int = 10):
    """
    Returns a list of diagnostic_items joined with question text for a given domain.
    """
    return conn.execute(
        """
        SELECT di.id AS diagnostic_item_id,
               q.question_id,
               q.stem, q.choice_a, q.choice_b, q.choice_c, q.choice_d,
               q.answer_key
        FROM diagnostic_items di
        JOIN questions q ON q.question_id = di.question_id
        WHERE LOWER(di.domain) = LOWER(?)
        ORDER BY di.id ASC
        LIMIT ?
        """,
        (domain, limit),
    ).fetchall()


def get_next_unanswered_diagnostic_item(conn: sqlite3.Connection, session_id: str):
    """
    Returns the next diagnostic item that has not been answered yet for this session.
    """
    return conn.execute(
        """
        SELECT di.id AS diagnostic_item_id,
               q.question_id,
               q.stem, q.choice_a, q.choice_b, q.choice_c, q.choice_d,
               q.answer_key
        FROM diagnostic_items di
        JOIN questions q ON q.question_id = di.question_id
        WHERE di.id NOT IN (
            SELECT diagnostic_item_id
            FROM diagnostic_responses
            WHERE session_id = ?
        )
        ORDER BY di.id ASC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()

def get_config(conn):
    try:
        rows = conn.execute("SELECT key,value FROM config").fetchall()
    except sqlite3.OperationalError:
        # table missing -> defaults
        return 0.9, 0.7
    cfg = {}
    for r in rows:
        try:
            cfg[r["key"]] = float(r["value"])
        except Exception:
            pass
    return cfg.get("mastery_threshold", 0.9), cfg.get("practice_lower", 0.7)


def set_config(conn, mastery, practice):
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES ('mastery_threshold', ?)",
        (str(mastery),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES ('practice_lower', ?)",
        (str(practice),),
    )
    conn.commit()


def rolling_avg(conn, sid, oid, n=7):
    """
    Return the percentage correct over the LAST n attempts
    for this student/objective.

    This is our official "Rolling-7" metric when n=7.
    """
    rows = conn.execute(
        """
        SELECT a.is_correct
        FROM attempts a
        JOIN questions q ON q.question_id = a.question_id
        WHERE a.student_id = ? AND q.objective_id = ?
        ORDER BY a.timestamp DESC
        LIMIT ?
        """,
        (sid, oid, n),
    ).fetchall()

    if not rows:
        return 0.0

    total = len(rows)
    correct = sum(1 for r in rows if int(r["is_correct"]) == 1)
    return correct / total


def attempt_hist(conn, sid, oid, m=7):
    """
    Return a list of the last m is_correct flags (1 or 0)
    for this student/objective, most recent first.
    """
    rows = conn.execute(
        """
        SELECT a.is_correct
        FROM attempts a
        JOIN questions q ON q.question_id = a.question_id
        WHERE a.student_id = ? AND q.objective_id = ?
        ORDER BY a.timestamp DESC
        LIMIT ?
        """,
        (sid, oid, m),
    ).fetchall()
    return [int(r["is_correct"]) for r in rows]

def frustration_active(conn, sid, oid):
    """
    Frustration rule:
    - Look at the last 5 attempts.
    - If we have exactly 5 AND all 5 are incorrect => "frustrated".
    This is an early-intervention signal separate from Rolling-7.
    """
    hist = attempt_hist(conn, sid, oid, m=5)
    return len(hist) == 5 and all(v == 0 for v in hist)

def lookup_map(conn, oid):
    r = conn.execute(
        "SELECT advance_to, remediation_to FROM lists WHERE objective_id = ?",
        (oid,),
    ).fetchone()
    if r:
        return r["advance_to"], r["remediation_to"]

    a = conn.execute(
        "SELECT to_node FROM paths WHERE from_node=? AND note LIKE '%Advance%'",
        (oid,),
    ).fetchone()
    m = conn.execute(
        "SELECT to_node FROM paths WHERE from_node=? AND note LIKE '%Remed%'",
        (oid,),
    ).fetchone()
    return (a["to_node"] if a else None), (m["to_node"] if m else None)


def mastered_fraction_in_band(conn, sid, core, band, mastery=0.9):
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    rows = order_objective_rows(conn.execute(
        f"""
        SELECT o.objective_id
        FROM objectives o
        JOIN standards s ON s.standard_id = o.standard_id
        WHERE s.core_idea = ? AND s.grade_band = ?
          AND o.objective_id IN ({objective_placeholders})
        """,
        (core, band, *LAUNCH_OBJECTIVE_IDS),
    ).fetchall())
    if not rows:
        return 0.0, []
    mastered = []
    total = len(rows)
    for r in rows:
        avg = rolling_avg(conn, sid, r["objective_id"], 7)
        if avg >= mastery:
            mastered.append(r["objective_id"])
    return len(mastered) / total, mastered


def first_objective_in_band(conn, core, band):
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    rows = order_objective_rows(conn.execute(
        f"""
        SELECT o.objective_id
        FROM objectives o
        JOIN standards s ON s.standard_id = o.standard_id
        WHERE s.core_idea = ? AND s.grade_band = ?
          AND o.objective_id IN ({objective_placeholders})
        """,
        (core, band, *LAUNCH_OBJECTIVE_IDS),
    ).fetchall())
    r = rows[0] if rows else None
    return r["objective_id"] if r else None


def upsert_student(conn, sid, first, last, grade, period):
    conn.execute(
        """
        INSERT INTO students (student_id, first_name, last_name, grade, class_period)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(student_id) DO UPDATE SET
          first_name=excluded.first_name,
          last_name=excluded.last_name,
          grade=excluded.grade,
          class_period=excluded.class_period
        """,
        (sid, first, last, grade, period),
    )
    conn.commit()


def decide_next(conn, sid, oid):
    """
    Decide the student's next objective based on:
    - Rolling-7 average for this objective
    - Global mastery/practice thresholds from config
    - Early-intervention rules (< 7 attempts)
    - Frustration rule (last 5 all wrong)
    - Cross-band promotion rules (progression_rules)
    """
    mastery, practice = get_config(conn)

    # --- Look up meta info for cross-band promotion ---
    meta = conn.execute(
        """
        SELECT s.core_idea AS core, s.grade_band AS band
        FROM objectives o
        JOIN standards s ON s.standard_id = o.standard_id
        WHERE o.objective_id = ?
        """,
        (oid,),
    ).fetchone()
    core = meta["core"] if meta else None
    band = meta["band"] if meta else None

    # --- Cross-band promotion (band-level, not per-objective) ---
    # If the student has mastered enough objectives in this grade band
    # for this core idea, jump them to the first objective in the next band.
    if core and band:
        rule = conn.execute(
            """
            SELECT to_band, promote_threshold
            FROM progression_rules
            WHERE core_idea = ? AND from_band = ?
            LIMIT 1
            """,
            (core, band),
        ).fetchone()
        if rule:
            frac, _ = mastered_fraction_in_band(conn, sid, core, band, mastery)
            if frac >= float(rule["promote_threshold"]):
                nxt = first_objective_in_band(conn, core, rule["to_band"])
                if nxt:
                    return (
                        frac,
                        nxt,
                        f'promote_{band}_to_{rule["to_band"]}',
                        False,
                    )

    # --- Rolling-7 stats for THIS objective ---
    recent = attempt_hist(conn, sid, oid, m=7)  # last 7 attempts (or fewer if early)
    total_recent = len(recent)
    correct_recent = sum(recent)
    avg = (correct_recent / total_recent) if total_recent > 0 else 0.0

    # Objective-level links for advance/remediation
    adv, rem = lookup_map(conn, oid)

    # --- Frustration rule: last 5 attempts all incorrect ---
    # This is an early-warning override: if they're clearly stuck, move to remediation.
    if frustration_active(conn, sid, oid):
        return avg, (rem or oid), "remediation_frustration_trigger", True

    # --- EARLY DATA RULE: fewer than 7 attempts ---
    # We don't fully trust the Rolling-7 rule until we actually have 7 attempts.
    if total_recent < 7:
        if total_recent == 0:
            # Student hasn't really worked here yet => stay on this objective in practice mode.
            return avg, oid, "insufficient_data_practice", False

        # We have 1–6 attempts: use the same thresholds, but call it out as "early".
        if avg < practice:
            # Not enough accuracy yet in the early window => send to remediation if possible.
            return avg, (rem or oid), "early_remediation_low_accuracy", False
        else:
            # Doing okay so far, keep them on this objective for more practice.
            return avg, oid, "early_practice", False

    # --- STANDARD ROLLING-7 RULES: 7 or more attempts recorded ---
    if avg >= mastery:
        # Mastery on this objective: move forward if we have an advance node,
        # otherwise stay here but mark it as mastered.
        return avg, (adv or oid), ("advance" if adv else "mastered"), False

    if avg >= practice:
        # In the practice band: keep them on this objective.
        return avg, oid, "practice", False

    # Below practice threshold: send to remediation if possible.
    return avg, (rem or oid), "remediation", False


def get_objectives(conn):
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    return order_objective_rows(conn.execute(
        f"""
        SELECT
            o.objective_id,
            o.objective_text,
            o.standard_id,
            s.core_idea,
            s.grade_band
        FROM objectives o
        JOIN standards s ON s.standard_id = o.standard_id
        WHERE o.objective_id IN ({objective_placeholders})
        """,
        LAUNCH_OBJECTIVE_IDS,
    ).fetchall())


def get_available_standards(conn):
    standard_placeholders = sql_placeholders(LAUNCH_STANDARD_IDS)
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    return conn.execute(
        f"""
        SELECT
            s.standard_id,
            s.core_idea,
            s.grade_band,
            COUNT(DISTINCT o.objective_id) AS objective_count,
            COUNT(DISTINCT q.question_id) AS question_count
        FROM standards s
        LEFT JOIN objectives o ON o.standard_id = s.standard_id
          AND o.objective_id IN ({objective_placeholders})
        LEFT JOIN questions q ON q.objective_id = o.objective_id
        WHERE s.standard_id IN ({standard_placeholders})
        GROUP BY s.standard_id, s.core_idea, s.grade_band
        ORDER BY s.core_idea, s.grade_band, s.standard_id
        """,
        (*LAUNCH_OBJECTIVE_IDS, *LAUNCH_STANDARD_IDS),
    ).fetchall()


def get_standard_meta(conn, standard_id):
    if not standard_id:
        return None
    return conn.execute(
        """
        SELECT standard_id, core_idea, grade_band
        FROM standards
        WHERE standard_id = ?
        """,
        (standard_id,),
    ).fetchone()


def get_response_count_for_level(conn, student_id, standard_id, level):
    if not student_id or not standard_id or not level:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM responses
        WHERE student_id = ? AND standard_id = ? AND level = ?
        """,
        (student_id, standard_id, level),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def get_progress_by_standard(conn, student_id):
    if not student_id:
        return {}
    rows = conn.execute(
        """
        SELECT standard_id, current_level, status, rolling_avg, locked, last_update
        FROM progress_state
        WHERE student_id = ?
          AND status <> 'inactive'
          AND standard_id IN ({})
        ORDER BY last_update DESC
        """.format(sql_placeholders(LAUNCH_STANDARD_IDS)),
        (student_id, *LAUNCH_STANDARD_IDS),
    ).fetchall()
    return {row["standard_id"]: row for row in rows}


def get_student_activity_counts(conn, student_id):
    attempts = conn.execute(
        "SELECT COUNT(*) AS n, MAX(timestamp) AS last_ts FROM attempts WHERE student_id=?",
        (student_id,),
    ).fetchone()
    responses = conn.execute(
        "SELECT COUNT(*) AS n, MAX(ts) AS last_ts FROM responses WHERE student_id=?",
        (student_id,),
    ).fetchone()
    progress = conn.execute(
        "SELECT COUNT(*) AS n, MAX(last_update) AS last_ts FROM progress_state WHERE student_id=?",
        (student_id,),
    ).fetchone()

    last_values = [
        row_get(attempts, "last_ts", None),
        row_get(responses, "last_ts", None),
        row_get(progress, "last_ts", None),
    ]
    last_values = [v for v in last_values if v is not None]
    return {
        "attempts": int(row_get(attempts, "n", 0)),
        "responses": int(row_get(responses, "n", 0)),
        "progress_rows": int(row_get(progress, "n", 0)),
        "last_activity": max(last_values) if last_values else None,
    }


def evidence_aware_rolling_avg(value, evidence_count: int):
    if int(evidence_count or 0) <= 0:
        return None
    return float(value or 0.0)


def get_latest_student_progress(conn, student_id):
    return get_current_student_placement(conn, student_id)["progress"]


def get_active_objective_for_student(conn, student_id, standard_id):
    if not student_id or not is_launch_standard_id(standard_id):
        return None
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    try:
        return conn.execute(
            f"""
            SELECT sos.current_objective_id AS objective_id,
                   o.objective_text
            FROM student_objective_state sos
            LEFT JOIN objectives o ON o.objective_id = sos.current_objective_id
            WHERE sos.student_id = ?
              AND sos.standard_id = ?
              AND sos.status = 'active'
              AND sos.current_objective_id IN ({objective_placeholders})
            LIMIT 1
            """,
            (student_id, standard_id, *LAUNCH_OBJECTIVE_IDS),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def get_current_student_placement(conn, student_id):
    """
    Read the current durable placement used by both student resume and teacher display.

    progress_state is the authoritative active standard/level source. Inactive rows
    are historical and must not drive current placement displays or student routing.
    """
    if not student_id:
        return {
            "progress": None,
            "objective": None,
            "standard_id": None,
            "current_level": 1,
            "objective_id": None,
        }

    progress_columns = table_columns(conn, "progress_state")
    optional_progress_cols = []
    if "active_route_type" in progress_columns:
        optional_progress_cols.append("ps.active_route_type")
    else:
        optional_progress_cols.append("NULL AS active_route_type")
    if "origin_standard_id" in progress_columns:
        optional_progress_cols.append("ps.origin_standard_id")
    else:
        optional_progress_cols.append("NULL AS origin_standard_id")

    progress = conn.execute(
        f"""
        SELECT ps.student_id,
               ps.standard_id,
               ps.current_level,
               ps.status,
               ps.rolling_avg,
               ps.locked,
               ps.locked_reason,
               ps.last_update,
               {", ".join(optional_progress_cols)},
               s.core_idea,
               s.grade_band
        FROM progress_state ps
        LEFT JOIN standards s ON s.standard_id = ps.standard_id
        WHERE ps.student_id = ?
          AND ps.status <> 'inactive'
          AND ps.standard_id IN ({sql_placeholders(LAUNCH_STANDARD_IDS)})
        ORDER BY ps.last_update DESC, ps.id DESC
        LIMIT 1
        """,
        (student_id, *LAUNCH_STANDARD_IDS),
    ).fetchone()

    standard_id = row_get(progress, "standard_id", None)
    current_level = int(row_get(progress, "current_level", 1) or 1)
    objective = get_active_objective_for_student(conn, student_id, standard_id)
    objective_id = row_get(objective, "objective_id", None)

    return {
        "progress": progress,
        "objective": objective,
        "standard_id": standard_id,
        "current_level": current_level,
        "objective_id": objective_id,
    }


def get_dashboard_student_rows(conn, students, mastery, practice):
    rows = []
    for student in students:
        sid = student["student_id"]
        counts = get_student_activity_counts(conn, sid)
        progress = get_latest_student_progress(conn, sid)
        standard_id = row_get(progress, "standard_id", "")
        objective = get_active_objective_for_student(conn, sid, standard_id)
        objective_id = row_get(objective, "objective_id", "")
        response_count = get_response_count_for_level(
            conn,
            sid,
            standard_id,
            row_get(progress, "current_level", None),
        )
        avg = evidence_aware_rolling_avg(row_get(progress, "rolling_avg", None), response_count)
        status = (row_get(progress, "status", "") or "").lower()
        locked = bool(row_get(progress, "locked", 0))
        frustrated = frustration_active(conn, sid, objective_id) if objective_id else False

        not_started = (
            counts["attempts"] == 0
            and counts["responses"] == 0
        )
        mastered = status in ("mastered", "completed", "complete") or (
            response_count >= 7 and avg is not None and avg >= mastery
        )
        low_average = response_count > 0 and avg is not None and avg < practice
        no_recent_progress = False
        needs_help = bool(locked or frustrated or low_average or no_recent_progress)
        progressing = bool(progress and not mastered and not needs_help)

        reasons = []
        if locked:
            reasons.append(row_get(progress, "locked_reason", "locked") or "locked")
        if frustrated:
            reasons.append("frustration signal")
        if low_average and avg is not None:
            reasons.append(f"rolling avg {round(avg * 100)}%")
        if not_started and progress:
            reasons.append("placed; no responses yet")
        if not reasons and progressing:
            reasons.append("active practice")
        if not reasons and mastered:
            reasons.append("at or above mastery")

        if not_started:
            category = "not_started"
            label = "Not Started"
        elif mastered:
            category = "mastered"
            label = "Mastered/Completed"
        elif needs_help:
            category = "needs_help"
            label = "Needs Attention"
        elif progressing:
            category = "progressing"
            label = "Progressing"
        else:
            category = "unknown"
            label = "No Current Signal"

        rows.append(
            {
                "student_id": sid,
                "name": display_name(student),
                "grade": student["grade"],
                "period": student["class_period"],
                "category": category,
                "status_label": label,
                "standard_id": standard_id,
                "level": row_get(progress, "current_level", ""),
                "objective_id": objective_id,
                "objective_text": row_get(objective, "objective_text", ""),
                "rolling_avg": avg,
                "response_count": response_count,
                "last_activity": counts["last_activity"],
                "reason": "; ".join(reasons),
                "locked": locked,
            }
        )
    return rows


def get_student_linked_accounts(conn, student_id):
    if not student_id:
        return []
    return conn.execute(
        """
        SELECT id, username, role, is_active, linked_student_id, sso_provider, sso_email, last_login_ts
        FROM users
        WHERE role = 'student'
          AND (linked_student_id = ? OR username = ?)
        ORDER BY
          CASE WHEN linked_student_id = ? THEN 0 ELSE 1 END,
          username
        """,
        (student_id, student_id, student_id),
    ).fetchall()


def get_student_learning_history(conn, student_id):
    empty = {
        "summary": {
            "standards_worked_on": 0,
            "objectives_attempted": 0,
            "current_objective": None,
            "latest_activity": None,
        },
        "standards": [],
    }
    if not student_id or not table_exists(conn, "objectives"):
        return empty

    mastery, practice = get_config(conn)

    current_progress = None
    if table_exists(conn, "progress_state"):
        current_progress = conn.execute(
            """
            SELECT standard_id, status, last_update
            FROM progress_state
            WHERE student_id = ?
              AND status <> 'inactive'
            ORDER BY last_update DESC
            LIMIT 1
            """,
            (student_id,),
        ).fetchone()

    current_standard_id = row_get(current_progress, "standard_id", None)
    current_objective_id = None
    if current_standard_id:
        active_objective = get_active_objective_for_student(
            conn, student_id, current_standard_id
        )
        current_objective_id = row_get(active_objective, "objective_id", None)

    standard_ids = set()
    if current_standard_id:
        standard_ids.add(current_standard_id)

    if table_exists(conn, "attempts") and table_exists(conn, "questions"):
        for row in conn.execute(
            """
            SELECT DISTINCT o.standard_id
            FROM attempts a
            JOIN questions q ON q.question_id = a.question_id
            JOIN objectives o ON o.objective_id = q.objective_id
            WHERE a.student_id = ?
              AND o.standard_id IS NOT NULL
            """,
            (student_id,),
        ).fetchall():
            standard_ids.add(row["standard_id"])

    if table_exists(conn, "responses"):
        for row in conn.execute(
            """
            SELECT DISTINCT standard_id
            FROM responses
            WHERE student_id = ?
              AND standard_id IS NOT NULL
            """,
            (student_id,),
        ).fetchall():
            standard_ids.add(row["standard_id"])

    if table_exists(conn, "progress_state"):
        for row in conn.execute(
            """
            SELECT DISTINCT standard_id
            FROM progress_state
            WHERE student_id = ?
              AND standard_id IS NOT NULL
            """,
            (student_id,),
        ).fetchall():
            standard_ids.add(row["standard_id"])

    if not standard_ids:
        return empty

    standard_placeholders = ",".join("?" for _ in standard_ids)
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    objective_rows = order_objective_rows(conn.execute(
        f"""
        SELECT o.objective_id,
               o.standard_id,
               o.objective_text,
               o.order_in_band,
               s.core_idea,
               s.grade_band
        FROM objectives o
        LEFT JOIN standards s ON s.standard_id = o.standard_id
        WHERE o.standard_id IN ({standard_placeholders})
          AND o.objective_id IN ({objective_placeholders})
        """,
        (*tuple(sorted(standard_ids)), *LAUNCH_OBJECTIVE_IDS),
    ).fetchall())

    objective_metrics = {}
    if table_exists(conn, "attempts") and table_exists(conn, "questions"):
        for row in conn.execute(
            f"""
            SELECT q.objective_id,
                   COUNT(*) AS attempt_count,
                   SUM(CASE WHEN a.is_correct = 1 THEN 1 ELSE 0 END) AS correct_count,
                   MIN(a.timestamp) AS first_activity,
                   MAX(a.timestamp) AS last_activity,
                   COUNT(DISTINCT a.question_id) AS questions_attempted
            FROM attempts a
            JOIN questions q ON q.question_id = a.question_id
            JOIN objectives o ON o.objective_id = q.objective_id
            WHERE a.student_id = ?
              AND o.standard_id IN ({standard_placeholders})
              AND o.objective_id IN ({objective_placeholders})
            GROUP BY q.objective_id
            """,
            (student_id, *tuple(sorted(standard_ids)), *LAUNCH_OBJECTIVE_IDS),
        ).fetchall():
            objective_metrics[row["objective_id"]] = {
                "attempt_count": int(row_get(row, "attempt_count", 0)),
                "correct_count": int(row_get(row, "correct_count", 0)),
                "first_activity": row_get(row, "first_activity", None),
                "last_activity": row_get(row, "last_activity", None),
                "questions_attempted": int(row_get(row, "questions_attempted", 0)),
                "recent_count": 0,
                "recent_correct": 0,
                "recent_avg": None,
                "last_five": [],
            }

        for objective_id in list(objective_metrics.keys()):
            recent_rows = conn.execute(
                """
                SELECT a.is_correct
                FROM attempts a
                JOIN questions q ON q.question_id = a.question_id
                WHERE a.student_id = ?
                  AND q.objective_id = ?
                ORDER BY a.timestamp DESC
                LIMIT 7
                """,
                (student_id, objective_id),
            ).fetchall()
            recent_values = [int(r["is_correct"]) for r in recent_rows]
            recent_count = len(recent_values)
            recent_correct = sum(recent_values)
            objective_metrics[objective_id]["recent_count"] = recent_count
            objective_metrics[objective_id]["recent_correct"] = recent_correct
            objective_metrics[objective_id]["recent_avg"] = (
                recent_correct / recent_count if recent_count else None
            )
            objective_metrics[objective_id]["last_five"] = recent_values[:5]

    standards = {}
    latest_activity = row_get(current_progress, "last_update", None)
    objectives_attempted = 0

    for row in objective_rows:
        objective_id = row["objective_id"]
        metrics = objective_metrics.get(
            objective_id,
            {
                "attempt_count": 0,
                "correct_count": 0,
                "first_activity": None,
                "last_activity": None,
                "questions_attempted": 0,
                "recent_count": 0,
                "recent_correct": 0,
                "recent_avg": None,
                "last_five": [],
            },
        )

        attempt_count = metrics["attempt_count"]
        if attempt_count:
            objectives_attempted += 1
        if metrics["last_activity"] is not None:
            latest_activity = max(latest_activity or 0, metrics["last_activity"])

        is_current = objective_id == current_objective_id
        recent_count = metrics["recent_count"]
        recent_avg = metrics["recent_avg"]
        last_five = metrics["last_five"]

        if is_current:
            status = "Current"
            status_class = "pill-current"
        elif attempt_count == 0:
            status = "Not Started"
            status_class = "pill-muted"
        elif len(last_five) == 5 and all(v == 0 for v in last_five):
            status = "Needs Attention"
            status_class = "pill-alert"
        elif recent_count >= 7 and recent_avg is not None and recent_avg >= mastery:
            status = "Completed"
            status_class = "pill-ok"
        elif recent_count >= 7 and recent_avg is not None and recent_avg < practice:
            status = "Needs Attention"
            status_class = "pill-alert"
        elif attempt_count > 0:
            status = "Progressing"
            status_class = "pill-warn"
        else:
            status = "No Current Signal"
            status_class = "pill-muted"

        standard_id = row["standard_id"]
        standards.setdefault(
            standard_id,
            {
                "standard_id": standard_id,
                "core_idea": row_get(row, "core_idea", ""),
                "grade_band": row_get(row, "grade_band", ""),
                "status": row_get(current_progress, "status", "")
                if standard_id == current_standard_id
                else "",
                "is_current": standard_id == current_standard_id,
                "objectives": [],
            },
        )
        standards[standard_id]["objectives"].append(
            {
                "objective_id": objective_id,
                "objective_text": row_get(row, "objective_text", "") or "",
                "status": status,
                "status_class": status_class,
                "attempt_count": attempt_count,
                "correct_count": metrics["correct_count"],
                "recent_count": recent_count,
                "recent_correct": metrics["recent_correct"],
                "recent_avg": recent_avg,
                "last_activity": metrics["last_activity"],
                "questions_attempted": metrics["questions_attempted"],
            }
        )

    return {
        "summary": {
            "standards_worked_on": len(standards),
            "objectives_attempted": objectives_attempted,
            "current_objective": current_objective_id,
            "latest_activity": latest_activity,
        },
        "standards": list(standards.values()),
    }


def get_student_overview(conn, student_id):
    student = conn.execute(
        """
        SELECT student_id, first_name, last_name, grade, class_period
        FROM students
        WHERE student_id = ?
        """,
        (student_id,),
    ).fetchone()
    if not student:
        return None

    placement = get_current_student_placement(conn, student_id)
    latest_progress = placement["progress"]
    standard_id = placement["standard_id"]
    current_level = placement["current_level"] if latest_progress else None
    objective = placement["objective"]
    objective_id = placement["objective_id"]

    rolling_responses = []
    rolling_count = 0
    rolling_correct = 0
    rolling_avg = None
    if standard_id and current_level:
        rolling_responses = conn.execute(
            """
            SELECT correct, ts, question_id
            FROM responses
            WHERE student_id = ? AND standard_id = ? AND level = ?
            ORDER BY ts DESC
            LIMIT 7
            """,
            (student_id, standard_id, current_level),
        ).fetchall()
        rolling_count = len(rolling_responses)
        rolling_correct = sum(1 for r in rolling_responses if int(r["correct"]) == 1)
        if rolling_count:
            rolling_avg = rolling_correct / rolling_count

    total_responses = conn.execute(
        "SELECT COUNT(*) AS n, MAX(ts) AS last_ts FROM responses WHERE student_id = ?",
        (student_id,),
    ).fetchone()
    total_attempts = conn.execute(
        "SELECT COUNT(*) AS n, MAX(timestamp) AS last_ts FROM attempts WHERE student_id = ?",
        (student_id,),
    ).fetchone()

    if standard_id:
        recent_attempts = conn.execute(
            """
            SELECT a.timestamp, a.question_id, a.is_correct, q.objective_id
            FROM attempts a
            LEFT JOIN questions q ON q.question_id = a.question_id
            WHERE a.student_id = ?
              AND q.objective_id IN (
                  SELECT objective_id FROM objectives WHERE standard_id = ?
              )
            ORDER BY a.timestamp DESC
            LIMIT 10
            """,
            (student_id, standard_id),
        ).fetchall()
    else:
        recent_attempts = conn.execute(
            """
            SELECT a.timestamp, a.question_id, a.is_correct, q.objective_id
            FROM attempts a
            LEFT JOIN questions q ON q.question_id = a.question_id
            WHERE a.student_id = ?
            ORDER BY a.timestamp DESC
            LIMIT 10
            """,
            (student_id,),
        ).fetchall()

    recent_responses = []
    if standard_id:
        recent_responses = conn.execute(
            """
            SELECT r.ts, r.level, r.question_id, r.correct, q.objective_id
            FROM responses r
            LEFT JOIN questions q ON q.question_id = r.question_id
            WHERE r.student_id = ? AND r.standard_id = ?
            ORDER BY r.ts DESC
            LIMIT 10
            """,
            (student_id, standard_id),
        ).fetchall()

    recent_activity = []
    seen_activity = set()
    for row in recent_attempts:
        item = {
            "time": row_get(row, "timestamp", None),
            "objective_id": row_get(row, "objective_id", None),
            "question_id": row_get(row, "question_id", None),
            "is_correct": row_get(row, "is_correct", 0),
        }
        key = (
            item["time"],
            item["objective_id"],
            item["question_id"],
            int(item["is_correct"] or 0),
        )
        if key not in seen_activity:
            recent_activity.append(item)
            seen_activity.add(key)

    for row in recent_responses:
        item = {
            "time": row_get(row, "ts", None),
            "objective_id": row_get(row, "objective_id", None),
            "question_id": row_get(row, "question_id", None),
            "is_correct": row_get(row, "correct", 0),
        }
        key = (
            item["time"],
            item["objective_id"],
            item["question_id"],
            int(item["is_correct"] or 0),
        )
        if key not in seen_activity:
            recent_activity.append(item)
            seen_activity.add(key)

    recent_activity = sorted(
        recent_activity,
        key=lambda item: item["time"] or 0,
        reverse=True,
    )[:15]

    origin_links = []
    if standard_id:
        origin_links = conn.execute(
            """
            SELECT rem_standard_id, from_standard_id, from_level, ts
            FROM origin_links
            WHERE student_id = ? AND rem_standard_id = ?
            ORDER BY ts DESC
            LIMIT 3
            """,
            (student_id, standard_id),
        ).fetchall()

    linked_accounts = get_student_linked_accounts(conn, student_id)
    frustration = frustration_active(conn, student_id, objective_id) if objective_id else False

    latest_activity = max(
        [
            ts
            for ts in [
                row_get(total_responses, "last_ts", None),
                row_get(total_attempts, "last_ts", None),
                row_get(latest_progress, "last_update", None),
            ]
            if ts is not None
        ],
        default=None,
    )

    focus = []
    if objective_id:
        focus.append(f"Current objective: {objective_id}")
    elif standard_id:
        focus.append("Current objective: not recorded yet")
    else:
        focus.append("Current objective: no active learning record yet")

    if rolling_count:
        focus.append(
            f"Recent check-ins: {rolling_correct}/{rolling_count} correct at Level {current_level}"
        )
    elif standard_id and current_level:
        focus.append(f"Recent check-ins: no responses yet at Level {current_level}")
    else:
        focus.append("Recent check-ins: not available yet")

    if row_get(latest_progress, "locked", 0):
        reason = row_get(latest_progress, "locked_reason", "locked") or "locked"
        focus.append(f"Locked/intervention state: {reason}")
    elif frustration:
        focus.append("Locked/intervention state: frustration signal active")
    else:
        focus.append("Locked/intervention state: none recorded")

    notes = []
    if not latest_progress:
        notes.append("No progress state has been recorded for this student yet.")
    if latest_progress and not objective_id:
        notes.append("A current standard exists, but no active objective is recorded.")
    if rolling_count and rolling_count < 7:
        notes.append(f"Recent check-ins are still collecting data: {rolling_count}/7 responses available.")
    if linked_accounts and not any(row_get(a, "linked_student_id", None) == student_id for a in linked_accounts):
        notes.append("Account match is based on username fallback, not linked_student_id.")
    if not linked_accounts:
        notes.append("No linked student login account was found for this roster record.")

    return {
        "student": student,
        "display_name": display_name(student),
        "latest_progress": latest_progress,
        "objective": objective,
        "rolling_count": rolling_count,
        "rolling_correct": rolling_correct,
        "rolling_avg": rolling_avg,
        "total_responses": int(row_get(total_responses, "n", 0)),
        "total_attempts": int(row_get(total_attempts, "n", 0)),
        "recent_attempts": recent_attempts,
        "recent_responses": recent_responses,
        "recent_activity": recent_activity,
        "origin_links": origin_links,
        "linked_accounts": linked_accounts,
        "frustration": frustration,
        "latest_activity": latest_activity,
        "learning_history": get_student_learning_history(conn, student_id),
        "focus": focus,
        "notes": notes,
    }


def build_dashboard_snapshot(student_rows):
    return {
        "needs_help": sum(1 for row in student_rows if row["category"] == "needs_help"),
        "not_started": sum(1 for row in student_rows if row["category"] == "not_started"),
        "progressing": sum(1 for row in student_rows if row["category"] == "progressing"),
        "mastered": sum(1 for row in student_rows if row["category"] == "mastered"),
        "active_standards": len(
            {
                row["standard_id"]
                for row in student_rows
                if row["standard_id"] and row["category"] != "not_started"
            }
        ),
    }


def get_active_standard_summary(conn, selected_period=None):
    where_clauses = ["ps.status <> 'inactive'"]
    params = []
    if selected_period and selected_period != "ALL":
        where_clauses.append("st.class_period = ?")
        params.append(selected_period)
    where_clauses.append(f"ps.standard_id IN ({sql_placeholders(LAUNCH_STANDARD_IDS)})")
    params.extend(LAUNCH_STANDARD_IDS)
    where_sql = "WHERE " + " AND ".join(where_clauses)
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)

    return conn.execute(
        f"""
        SELECT ps.standard_id,
               COALESCE(s.core_idea, '') AS core_idea,
               COALESCE(s.grade_band, '') AS grade_band,
               COUNT(DISTINCT ps.student_id) AS student_count,
               SUM(CASE WHEN ps.locked = 1 THEN 1 ELSE 0 END) AS locked_count,
               SUM(CASE WHEN ps.status IN ('mastered', 'completed', 'complete') THEN 1 ELSE 0 END) AS completed_count,
               ROUND(AVG(CASE WHEN COALESCE(rs.response_count, 0) > 0 THEN ps.rolling_avg END), 2) AS avg_progress,
               COUNT(DISTINCT CASE WHEN COALESCE(rs.response_count, 0) > 0 THEN ps.student_id END) AS students_with_evidence,
               COUNT(DISTINCT o.objective_id) AS objective_count,
               COUNT(DISTINCT q.question_id) AS question_count
        FROM progress_state ps
        JOIN students st ON st.student_id = ps.student_id
        LEFT JOIN standards s ON s.standard_id = ps.standard_id
        LEFT JOIN (
          SELECT student_id, standard_id, level, COUNT(*) AS response_count
          FROM responses
          GROUP BY student_id, standard_id, level
        ) rs ON rs.student_id = ps.student_id
            AND rs.standard_id = ps.standard_id
            AND rs.level = ps.current_level
        LEFT JOIN objectives o ON o.standard_id = ps.standard_id
            AND o.objective_id IN ({objective_placeholders})
        LEFT JOIN questions q ON q.objective_id = o.objective_id
        {where_sql}
        GROUP BY ps.standard_id, s.core_idea, s.grade_band
        ORDER BY student_count DESC, ps.standard_id
        """,
        (*LAUNCH_OBJECTIVE_IDS, *params),
    ).fetchall()


def format_ts(ts):
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return ""


def row_get(row, key, default=None):
    if not row or key not in row.keys():
        return default
    value = row[key]
    return default if value is None else value


def close_active_routing_level_attempt(
    conn: sqlite3.Connection,
    student_id: str,
    reason: str,
    *,
    ended_at: int | None = None,
):
    active = conn.execute(
        """
        SELECT *
        FROM routing_level_attempts
        WHERE student_id = ? AND status = 'active'
        """,
        (student_id,),
    ).fetchone()
    if not active:
        return None
    metrics = conn.execute(
        """
        SELECT COUNT(*) AS total, COALESCE(SUM(correct), 0) AS correct
        FROM responses
        WHERE routing_level_attempt_id = ?
        """,
        (active["attempt_id"],),
    ).fetchone()
    total = int(metrics["total"] or 0)
    correct = int(metrics["correct"] or 0)
    conn.execute(
        """
        UPDATE routing_level_attempts
        SET status = 'closed', ended_at = ?, end_reason = ?,
            final_correct_count = ?, final_response_count = ?, final_score = ?
        WHERE attempt_id = ? AND status = 'active'
        """,
        (
            ended_at or int(time.time()),
            reason,
            correct,
            total,
            (correct / total) if total else 0.0,
            active["attempt_id"],
        ),
    )
    return active


def ensure_routing_level_attempt(
    conn: sqlite3.Connection,
    *,
    student_id: str,
    standard_id: str,
    objective_id: str,
    level: int,
    boundary_reason: str = "routing_boundary",
    started_at: int | None = None,
):
    active = conn.execute(
        """
        SELECT *
        FROM routing_level_attempts
        WHERE student_id = ? AND status = 'active'
        """,
        (student_id,),
    ).fetchone()
    if (
        active
        and active["standard_id"] == standard_id
        and active["objective_id"] == objective_id
        and int(active["level"]) == int(level)
    ):
        return active
    if active:
        close_active_routing_level_attempt(
            conn,
            student_id,
            boundary_reason,
            ended_at=started_at,
        )
    attempt_id = f"RLA{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO routing_level_attempts
          (attempt_id, student_id, standard_id, objective_id, level, status, started_at)
        VALUES (?, ?, ?, ?, ?, 'active', ?)
        """,
        (
            attempt_id,
            student_id,
            standard_id,
            objective_id,
            level,
            int(started_at or time.time()),
        ),
    )
    return conn.execute(
        "SELECT * FROM routing_level_attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()


def get_students(conn, period=None):
    if period and period != "ALL":
        return conn.execute(
            """
            SELECT student_id, first_name, last_name, grade, class_period
            FROM students
            WHERE class_period = ?
            ORDER BY student_id
            """,
            (period,),
        ).fetchall()
    return conn.execute(
        """
        SELECT student_id, first_name, last_name, grade, class_period
        FROM students
        ORDER BY student_id
        """
    ).fetchall()


CLASS_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def normalize_join_code(join_code: str | None) -> str:
    return "".join((join_code or "").upper().split())


def generate_join_code(conn: sqlite3.Connection, length: int = 6) -> str:
    for _ in range(24):
        code = "".join(secrets.choice(CLASS_CODE_ALPHABET) for _ in range(length))
        row = conn.execute(
            "SELECT 1 FROM class_sections WHERE join_code = ?",
            (code,),
        ).fetchone()
        if not row:
            return code
    raise RuntimeError("Could not generate a unique class code.")


def create_class_section(
    conn: sqlite3.Connection,
    teacher_user_id: int | None,
    name: str,
    class_period: str | None = None,
) -> str:
    class_id = str(uuid.uuid4())
    now = int(time.time())
    join_code = generate_join_code(conn)
    conn.execute(
        """
        INSERT INTO class_sections
          (class_id, teacher_user_id, name, class_period, join_code, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (class_id, teacher_user_id, name, class_period, join_code, now, now),
    )
    conn.commit()
    return class_id


def get_class_sections_for_teacher(conn: sqlite3.Connection, teacher_user_id: int | None):
    return conn.execute(
        """
        SELECT cs.*,
               COUNT(ce.student_id) AS enrolled_count
        FROM class_sections cs
        LEFT JOIN class_enrollments ce ON ce.class_id = cs.class_id AND ce.is_active=1
        WHERE cs.teacher_user_id = ? OR cs.teacher_user_id IS NULL
        GROUP BY cs.class_id
        ORDER BY cs.is_active DESC, cs.name COLLATE NOCASE
        """,
        (teacher_user_id,),
    ).fetchall()


def get_class_roster_map(conn: sqlite3.Connection, class_ids: list[str]) -> dict[str, list[sqlite3.Row]]:
    if not class_ids:
        return {}
    placeholders = ",".join(["?"] * len(class_ids))
    rows = conn.execute(
        f"""
        SELECT ce.class_id,
               ce.enrolled_at,
               s.student_id,
               s.first_name,
               s.last_name,
               s.grade,
               s.class_period
        FROM class_enrollments ce
        JOIN students s ON s.student_id = ce.student_id
        WHERE ce.class_id IN ({placeholders}) AND ce.is_active=1
        ORDER BY s.last_name COLLATE NOCASE, s.first_name COLLATE NOCASE, s.student_id
        """,
        class_ids,
    ).fetchall()

    roster_map: dict[str, list[sqlite3.Row]] = {class_id: [] for class_id in class_ids}
    for row in rows:
        roster_map.setdefault(row["class_id"], []).append(row)
    return roster_map


def enroll_student_by_code(
    conn: sqlite3.Connection,
    student_id: str,
    join_code: str,
    enrolled_by: str = "self",
) -> tuple[bool, str]:
    code = normalize_join_code(join_code)
    if not code:
        return False, "Enter a class code to join."

    class_row = conn.execute(
        """
        SELECT class_id, name, class_period, is_active
        FROM class_sections
        WHERE join_code = ?
        """,
        (code,),
    ).fetchone()
    if not class_row:
        return False, "That class code was not found."
    if int(class_row["is_active"] or 0) != 1:
        return False, "That class code is not active right now."

    existing = conn.execute(
        """
        SELECT is_active
        FROM class_enrollments
        WHERE class_id = ? AND student_id = ?
        """,
        (class_row["class_id"], student_id),
    ).fetchone()
    if existing:
        if int(existing["is_active"] or 0) == 1:
            return True, f"You are already enrolled in {class_row['name']}."
        conn.execute(
            """
            UPDATE class_enrollments
            SET is_active=1, reactivated_at=?, reactivated_by_user_id=NULL
            WHERE class_id=? AND student_id=?
            """,
            (int(time.time()), class_row["class_id"], student_id),
        )
        conn.commit()
        return True, f"You rejoined {class_row['name']}."

    now = int(time.time())
    conn.execute(
        """
        INSERT INTO class_enrollments (class_id, student_id, enrolled_at, enrolled_by)
        VALUES (?, ?, ?, ?)
        """,
        (class_row["class_id"], student_id, now, enrolled_by),
    )
    if class_row["class_period"]:
        conn.execute(
            """
            UPDATE students
            SET class_period = COALESCE(NULLIF(class_period, ''), ?)
            WHERE student_id = ?
            """,
            (class_row["class_period"], student_id),
        )
    conn.commit()
    return True, f"You joined {class_row['name']}."


def get_student_class_sections(conn: sqlite3.Connection, student_id: str):
    return conn.execute(
        """
        SELECT cs.class_id,
               cs.name,
               cs.class_period,
               cs.join_code,
               ce.enrolled_at
        FROM class_enrollments ce
        JOIN class_sections cs ON cs.class_id = ce.class_id
        WHERE ce.student_id = ? AND ce.is_active=1
        ORDER BY ce.enrolled_at DESC
        """,
        (student_id,),
    ).fetchall()


def teacher_controls_student(
    conn: sqlite3.Connection,
    teacher_user_id: int | None,
    student_id: str | None,
) -> bool:
    if not teacher_user_id or not student_id:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM class_enrollments ce
        JOIN class_sections cs ON cs.class_id = ce.class_id
        WHERE ce.student_id = ?
          AND cs.teacher_user_id = ?
          AND ce.is_active = 1
          AND cs.is_active = 1
        LIMIT 1
        """,
        (student_id, teacher_user_id),
    ).fetchone()
    return row is not None


def get_launch_question_for_flag(conn: sqlite3.Connection, question_id: str | None):
    question_id = (question_id or "").strip()
    if not question_id:
        return None
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    return conn.execute(
        f"""
        SELECT q.question_id,
               q.objective_id,
               o.standard_id,
               COALESCE(o.objective_text, '') AS objective_text
        FROM questions q
        JOIN objectives o ON o.objective_id = q.objective_id
        WHERE q.question_id = ?
          AND q.objective_id IN ({objective_placeholders})
          AND o.standard_id IN ({sql_placeholders(LAUNCH_STANDARD_IDS)})
        LIMIT 1
        """,
        (question_id, *LAUNCH_OBJECTIVE_IDS, *LAUNCH_STANDARD_IDS),
    ).fetchone()


def normalize_question_flag_category(category: str | None) -> str | None:
    category = (category or "").strip().lower()
    category = LEGACY_QUESTION_FLAG_CATEGORIES.get(category, category)
    return category if category in QUESTION_FLAG_CATEGORY_SET else None


def question_flag_category_label(category: str | None) -> str:
    category = normalize_question_flag_category(category)
    return QUESTION_FLAG_CATEGORY_LABELS.get(category or "", "Something else")


def normalize_question_flag_status_filter(value: str | None) -> str:
    value = (value or "").strip().lower()
    return value if value in QUESTION_FLAG_STATUS_FILTERS else QUESTION_FLAG_DEFAULT_STATUS_FILTER


def normalize_question_flag_category_filter(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value == QUESTION_FLAG_DEFAULT_CATEGORY_FILTER:
        return QUESTION_FLAG_DEFAULT_CATEGORY_FILTER
    return value if value in QUESTION_FLAG_CATEGORY_SET else QUESTION_FLAG_DEFAULT_CATEGORY_FILTER


def question_flag_category_filter_options():
    return ((QUESTION_FLAG_DEFAULT_CATEGORY_FILTER, "All categories"), *QUESTION_FLAG_CATEGORIES)


def question_reference_meta_text(
    objective_code: str | None,
    objective_title: str | None,
    question_id: str | None,
) -> str:
    objective_code = (objective_code or "").strip()
    objective_title = (objective_title or "").strip()
    question_id = (question_id or "").strip()
    if objective_code and objective_title:
        return f"{objective_code} — {objective_title} • {question_id}"
    if objective_code:
        return f"{objective_code} • {question_id}"
    return question_id


def question_selector_label(question_row, max_stem_length: int = 90) -> str:
    stem = (row_get(question_row, "stem", None) or "").strip()
    objective_code = row_get(question_row, "objective_id", None)
    question_id = row_get(question_row, "question_id", None)
    if stem and len(stem) > max_stem_length:
        stem = stem[: max_stem_length - 3].rstrip() + "..."
    if not stem:
        stem = "Question stem unavailable"
    return f"{stem} — {objective_code} • {question_id}"


def render_question_reference(
    *,
    question_stem: str | None,
    objective_code: str | None,
    objective_title: str | None,
    question_id: str | None,
    preview_url: str | None = None,
    missing: bool = False,
) -> Markup:
    meta_text = question_reference_meta_text(objective_code, objective_title, question_id)
    html = """
<article class="question-reference{% if missing %} question-reference-missing{% endif %}" aria-label="Question reference">
  <div class="question-reference-label">Question</div>
  {% if missing %}
    <div class="question-reference-stem question-reference-stem-missing">Question no longer available</div>
    <div class="question-reference-meta">{{ meta_text }}</div>
  {% else %}
    <a class="question-reference-stem question-reference-stem-link" href="{{ preview_url }}">{{ question_stem or 'Question stem unavailable' }}</a>
    <a class="question-reference-meta question-reference-meta-link" href="{{ preview_url }}">{{ meta_text }}</a>
  {% endif %}
</article>
    """
    return Markup(
        render_template_string(
            html,
            question_stem=question_stem,
            meta_text=meta_text,
            preview_url=preview_url,
            missing=missing or not preview_url,
        ).strip()
    )


def question_reference_for_flag(flag, preview_url: str | None = None) -> Markup:
    question_exists = bool(row_get(flag, "live_question_id", None))
    return render_question_reference(
        question_stem=row_get(flag, "question_stem", None),
        objective_code=row_get(flag, "objective_id", None),
        objective_title=row_get(flag, "objective_text", None),
        question_id=row_get(flag, "question_id", None),
        preview_url=preview_url if question_exists else None,
        missing=not question_exists,
    )


def current_student_id_for_flag(conn: sqlite3.Connection) -> str | None:
    if session.get("role") != "student":
        return None
    student_id = locked_student_id_for_session(conn)
    return student_id.strip() if student_id else None


def get_default_flag_class_id(conn: sqlite3.Connection, student_id: str | None) -> str | None:
    if not student_id:
        return None
    row = conn.execute(
        """
        SELECT cs.class_id
        FROM class_enrollments ce
        JOIN class_sections cs ON cs.class_id = ce.class_id
        WHERE ce.student_id = ?
          AND ce.is_active = 1
          AND cs.is_active = 1
        ORDER BY ce.enrolled_at DESC
        LIMIT 1
        """,
        (student_id,),
    ).fetchone()
    return row["class_id"] if row else None


def create_question_flag(
    conn: sqlite3.Connection,
    *,
    question_id: str,
    category: str | None,
    comment: str | None,
    page_context: str | None,
    student_id: str | None = None,
    class_id: str | None = None,
) -> tuple[bool, str, str | None]:
    question = get_launch_question_for_flag(conn, question_id)
    if not question:
        return False, "That question cannot be flagged from this classroom launch.", None

    clean_category = normalize_question_flag_category(category)
    if not clean_category:
        return False, "Choose a valid flag category.", None

    role = session.get("role")
    if request.method == "GET" and role == "student":
        feedback = session.pop("student_feedback", None)
    if role not in ("teacher", "student"):
        return False, "Please log in before flagging a question.", None

    reporter_user_id = session.get("user_id")
    clean_comment = (comment or "").strip()
    if len(clean_comment) > QUESTION_FLAG_COMMENT_MAX_LENGTH:
        return (
            False,
            f"Flag comments must be {QUESTION_FLAG_COMMENT_MAX_LENGTH} characters or fewer.",
            None,
        )
    clean_context = (page_context or "").strip()[:120] or None

    if role == "student":
        student_id = current_student_id_for_flag(conn)
        if not student_id:
            return False, "Your account is not linked to a student record.", None
        class_id = get_default_flag_class_id(conn, student_id)
    else:
        student_id = None
        class_id = None

    flag_id = f"QF-{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO question_flags
          (flag_id, question_id, objective_id, standard_id, reporter_user_id,
           reporter_role, class_id, student_id, category, comment, page_context,
           created_ts, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """,
        (
            flag_id,
            question["question_id"],
            question["objective_id"],
            question["standard_id"],
            reporter_user_id,
            role,
            class_id,
            student_id,
            clean_category,
            clean_comment or None,
            clean_context,
            int(time.time()),
        ),
    )
    conn.commit()
    return True, "Thanks. Your flag was submitted for review.", flag_id


def teacher_authorized_for_flag(conn: sqlite3.Connection, flag_row) -> bool:
    if session.get("role") != "teacher":
        return False
    teacher_user_id = session.get("user_id")
    if not teacher_user_id:
        return False
    if row_get(flag_row, "reporter_user_id", None) == teacher_user_id:
        return True
    class_id = row_get(flag_row, "class_id", None)
    if class_id:
        row = conn.execute(
            """
            SELECT 1
            FROM class_sections
            WHERE class_id = ?
              AND teacher_user_id = ?
              AND is_active = 1
            LIMIT 1
            """,
            (class_id, teacher_user_id),
        ).fetchone()
        return row is not None
    return False


def teacher_flag_authorization_sql(alias: str = "qf") -> str:
    return f"""
    ({alias}.reporter_user_id = ?
     OR {alias}.class_id IN (
         SELECT class_id
         FROM class_sections
         WHERE teacher_user_id = ?
           AND is_active = 1
     ))
    """


def question_flag_status_filter_counts(
    conn: sqlite3.Connection,
    teacher_user_id: int | None,
    category_filter: str = QUESTION_FLAG_DEFAULT_CATEGORY_FILTER,
) -> dict[str, int]:
    if not teacher_user_id:
        return {key: 0 for key in QUESTION_FLAG_STATUS_FILTERS}
    category_filter = normalize_question_flag_category_filter(category_filter)
    auth_sql = teacher_flag_authorization_sql("qf")
    counts = {}
    for key, config in QUESTION_FLAG_STATUS_FILTERS.items():
        status_placeholders = sql_placeholders(config["statuses"])
        where = [auth_sql, f"qf.status IN ({status_placeholders})"]
        params = [teacher_user_id, teacher_user_id, *config["statuses"]]
        if category_filter != QUESTION_FLAG_DEFAULT_CATEGORY_FILTER:
            where.append("qf.category = ?")
            params.append(category_filter)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM question_flags qf
            WHERE {" AND ".join(where)}
            """,
            params,
        ).fetchone()
        counts[key] = int(row["count"] or 0) if row else 0
    return counts


def teacher_actionable_question_flag_count(
    conn: sqlite3.Connection,
    teacher_user_id: int | None,
) -> int:
    return question_flag_status_filter_counts(
        conn,
        teacher_user_id,
        QUESTION_FLAG_DEFAULT_CATEGORY_FILTER,
    )[QUESTION_FLAG_DEFAULT_STATUS_FILTER]


def get_question_flags_for_teacher(
    conn: sqlite3.Connection,
    teacher_user_id: int | None,
    status_filter: str = QUESTION_FLAG_DEFAULT_STATUS_FILTER,
    category_filter: str = QUESTION_FLAG_DEFAULT_CATEGORY_FILTER,
):
    if not teacher_user_id:
        return []
    status_filter = normalize_question_flag_status_filter(status_filter)
    category_filter = normalize_question_flag_category_filter(category_filter)
    statuses = QUESTION_FLAG_STATUS_FILTERS[status_filter]["statuses"]
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    status_placeholders = sql_placeholders(statuses)
    where_clauses = [
        teacher_flag_authorization_sql("qf"),
        f"qf.status IN ({status_placeholders})",
    ]
    params = [
        *LAUNCH_OBJECTIVE_IDS,
        teacher_user_id,
        teacher_user_id,
        *statuses,
    ]
    if category_filter != QUESTION_FLAG_DEFAULT_CATEGORY_FILTER:
        where_clauses.append("qf.category = ?")
        params.append(category_filter)
    rows = conn.execute(
        f"""
        SELECT qf.*,
               COALESCE(o.objective_text, '') AS objective_text,
               q.question_id AS live_question_id,
               q.stem AS question_stem,
               cs.name AS class_name,
               cs.teacher_user_id AS class_teacher_user_id,
               COUNT(*) OVER (PARTITION BY qf.question_id) AS question_flag_count,
               SUM(CASE WHEN qf.status = 'open' THEN 1 ELSE 0 END)
                   OVER (PARTITION BY qf.question_id) AS question_open_count
        FROM question_flags qf
        LEFT JOIN objectives o ON o.objective_id = qf.objective_id
        LEFT JOIN questions q ON q.question_id = qf.question_id
             AND q.objective_id IN ({objective_placeholders})
        LEFT JOIN class_sections cs ON cs.class_id = qf.class_id
        WHERE {" AND ".join(where_clauses)}
        ORDER BY CASE
                   WHEN qf.status IN ('open', 'escalated', 'owner_reviewing') THEN 0
                   ELSE 1
                 END,
                 qf.created_ts DESC
        """,
        params,
    ).fetchall()
    return rows


def get_question_flag_by_id(conn: sqlite3.Connection, flag_id: str | None):
    return conn.execute(
        "SELECT * FROM question_flags WHERE flag_id = ?",
        ((flag_id or "").strip(),),
    ).fetchone()


def normalize_owner_question_flag_filter(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value in OWNER_QUESTION_FLAG_FILTERS:
        return value
    return OWNER_QUESTION_FLAG_DEFAULT_FILTER


def question_flag_count_for_statuses(
    conn: sqlite3.Connection,
    statuses: tuple[str, ...],
) -> int:
    placeholders = sql_placeholders(statuses)
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM question_flags WHERE status IN ({placeholders})",
        statuses,
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def owner_escalated_question_flag_count(conn: sqlite3.Connection) -> int:
    return question_flag_count_for_statuses(conn, ("escalated",))


def owner_question_flag_filter_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        key: question_flag_count_for_statuses(conn, config["statuses"])
        for key, config in OWNER_QUESTION_FLAG_FILTERS.items()
    }


def get_question_flags_for_owner(
    conn: sqlite3.Connection,
    status_filter: str = OWNER_QUESTION_FLAG_DEFAULT_FILTER,
):
    status_filter = normalize_owner_question_flag_filter(status_filter)
    statuses = OWNER_QUESTION_FLAG_FILTERS[status_filter]["statuses"]
    status_placeholders = sql_placeholders(statuses)
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    return conn.execute(
        f"""
        SELECT qf.*,
               COALESCE(o.objective_text, '') AS objective_text,
               q.question_id AS live_question_id,
               q.stem AS question_stem,
               cs.name AS class_name,
               cs.class_period AS class_period,
               reporter.username AS reporter_username
        FROM question_flags qf
        LEFT JOIN objectives o ON o.objective_id = qf.objective_id
        LEFT JOIN questions q ON q.question_id = qf.question_id
             AND q.objective_id IN ({objective_placeholders})
        LEFT JOIN class_sections cs ON cs.class_id = qf.class_id
        LEFT JOIN users reporter ON reporter.id = qf.reporter_user_id
        WHERE qf.status IN ({status_placeholders})
        ORDER BY CASE
                   WHEN qf.status = 'escalated' THEN 0
                   WHEN qf.status = 'owner_reviewing' THEN 1
                   ELSE 2
                 END,
                 qf.created_ts DESC
        """,
        (*LAUNCH_OBJECTIVE_IDS, *statuses),
    ).fetchall()


def get_question_flag_events(conn: sqlite3.Connection, flag_id: str):
    return conn.execute(
        """
        SELECT qfe.*, u.username AS actor_username
        FROM question_flag_events qfe
        LEFT JOIN users u ON u.id = qfe.actor_user_id
        WHERE qfe.flag_id = ?
        ORDER BY qfe.created_ts, qfe.event_id
        """,
        (flag_id,),
    ).fetchall()


def get_placeable_learning_nodes(conn: sqlite3.Connection):
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    return order_objective_rows(conn.execute(
        f"""
        SELECT s.standard_id,
               s.core_idea,
               s.grade_band,
               o.objective_id,
               o.objective_text,
               o.order_in_band,
               COUNT(q.question_id) AS question_count
        FROM standards s
        JOIN objectives o ON o.standard_id = s.standard_id
        LEFT JOIN questions q ON q.objective_id = o.objective_id
        WHERE o.objective_id IN ({objective_placeholders})
        GROUP BY
          s.standard_id,
          s.core_idea,
          s.grade_band,
          o.objective_id,
          o.objective_text,
          o.order_in_band
        HAVING COUNT(q.question_id) > 0
        """,
        LAUNCH_OBJECTIVE_IDS,
    ).fetchall())


def first_objective_for_standard(conn: sqlite3.Connection, standard_id: str) -> str | None:
    if not is_launch_standard_id(standard_id):
        return None
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    rows = order_objective_rows(conn.execute(
        f"""
        SELECT o.objective_id
        FROM objectives o
        WHERE o.standard_id = ?
          AND o.objective_id IN ({objective_placeholders})
          AND EXISTS (
              SELECT 1
              FROM questions q
              WHERE q.objective_id = o.objective_id
          )
        """,
        (standard_id, *LAUNCH_OBJECTIVE_IDS),
    ).fetchall())
    row = rows[0] if rows else None
    return row["objective_id"] if row else None


def validate_learning_placement(
    conn: sqlite3.Connection,
    *,
    student_id: str,
    standard_id: str,
    objective_id: str,
    current_level: int,
) -> tuple[bool, str]:
    if current_level not in (1, 2, 3):
        return False, "Current level must be 1, 2, or 3."

    if not is_launch_standard_id(standard_id) or not is_launch_objective_id(objective_id):
        return False, "Selected objective is not part of the classroom launch manifest."

    student = conn.execute(
        "SELECT 1 FROM students WHERE student_id = ?",
        (student_id,),
    ).fetchone()
    if not student:
        return False, "Student was not found."

    standard = conn.execute(
        "SELECT 1 FROM standards WHERE standard_id = ?",
        (standard_id,),
    ).fetchone()
    if not standard:
        return False, "Standard was not found."

    objective = conn.execute(
        """
        SELECT 1
        FROM objectives
        WHERE objective_id = ?
          AND standard_id = ?
        """,
        (objective_id, standard_id),
    ).fetchone()
    if not objective:
        return False, "Objective does not belong to the selected standard."

    question = conn.execute(
        "SELECT 1 FROM questions WHERE objective_id = ? LIMIT 1",
        (objective_id,),
    ).fetchone()
    if not question:
        return False, "Selected objective has no available questions."

    return True, ""


def place_student_learning_node(
    conn: sqlite3.Connection,
    *,
    student_id: str,
    standard_id: str,
    objective_id: str,
    current_level: int,
    placed_by_user_id: int | None = None,
    require_teacher_control: bool = True,
) -> tuple[bool, str]:
    """
    Durable placement bridge for teacher placement now and diagnostic placement later.

    progress_state is authoritative for active standard/level/adaptive status.
    student_objective_state stores the active objective within that standard.
    Both rows are written in one transaction by explicit placement actions only.
    """
    if require_teacher_control and not teacher_controls_student(conn, placed_by_user_id, student_id):
        return False, "You can only place students enrolled in one of your active classes."

    ok, message = validate_learning_placement(
        conn,
        student_id=student_id,
        standard_id=standard_id,
        objective_id=objective_id,
        current_level=current_level,
    )
    if not ok:
        return False, message

    now = int(time.time())
    try:
        conn.execute("BEGIN")
        close_active_routing_level_attempt(
            conn,
            student_id,
            "teacher_placement",
            ended_at=now,
        )
        conn.execute(
            """
            UPDATE progress_state
            SET status = 'inactive',
                locked = 0,
                locked_reason = NULL,
                last_update = ?
            WHERE student_id = ?
              AND standard_id <> ?
            """,
            (now, student_id, standard_id),
        )
        conn.execute(
            """
            INSERT INTO progress_state
              (student_id, standard_id, current_level, status, rolling_avg, locked, locked_reason, last_update)
            VALUES (?, ?, ?, 'practicing', 0.0, 0, NULL, ?)
            ON CONFLICT(student_id, standard_id) DO UPDATE SET
              current_level = excluded.current_level,
              status = 'practicing',
              rolling_avg = 0.0,
              locked = 0,
              locked_reason = NULL,
              last_update = excluded.last_update
            """,
            (student_id, standard_id, current_level, now),
        )
        conn.execute(
            """
            INSERT INTO student_objective_state
              (student_id, standard_id, current_objective_id, status, last_update)
            VALUES (?, ?, ?, 'active', ?)
            ON CONFLICT(student_id, standard_id) DO UPDATE SET
              current_objective_id = excluded.current_objective_id,
              status = 'active',
              last_update = excluded.last_update
            """,
            (student_id, standard_id, objective_id, now),
        )
        ensure_routing_level_attempt(
            conn,
            student_id=student_id,
            standard_id=standard_id,
            objective_id=objective_id,
            level=current_level,
            boundary_reason="teacher_placement",
            started_at=now,
        )
        ensure_student_growth_attempt(
            conn,
            student_id,
            standard_id,
            objective_id,
            force_new=True,
            commit=False,
        )
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    return True, f"Placed {student_id} at {standard_id} / {objective_id}, Level {current_level}."


def ensure_student_growth_attempt(
    conn: sqlite3.Connection,
    student_id: str,
    standard_id: str,
    objective_id: str,
    *,
    force_new: bool = False,
    commit: bool = True,
):
    """Return the active presentation attempt, starting one only when learning changes."""
    active = conn.execute(
        """
        SELECT *
        FROM student_growth_progress
        WHERE student_id = ? AND is_active = 1
        """,
        (student_id,),
    ).fetchone()
    if (
        active
        and not force_new
        and active["standard_id"] == standard_id
        and active["objective_id"] == objective_id
    ):
        return active

    now = int(time.time())
    if active:
        invalidate_student_question_deliveries(
            conn,
            student_id,
            growth_attempt_id=active["attempt_id"],
            commit=False,
        )
        conn.execute(
            """
            UPDATE student_growth_progress
            SET is_active = 0, updated_at = ?
            WHERE attempt_id = ?
            """,
            (now, active["attempt_id"]),
        )

    attempt_id = f"GP{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO student_growth_progress
          (attempt_id, student_id, standard_id, objective_id, visible_stage,
           presentation_state, is_active, started_at, updated_at)
        VALUES (?, ?, ?, ?, 1, 'growing', 1, ?, ?)
        """,
        (attempt_id, student_id, standard_id, objective_id, now, now),
    )
    if commit:
        conn.commit()
    return conn.execute(
        "SELECT * FROM student_growth_progress WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()


def invalidate_student_question_deliveries(
    conn: sqlite3.Connection,
    student_id: str,
    *,
    growth_attempt_id: str | None = None,
    commit: bool = True,
) -> None:
    now = int(time.time())
    sql = """
        UPDATE student_question_deliveries
        SET invalidated_at = ?
        WHERE student_id = ?
          AND consumed_at IS NULL
          AND invalidated_at IS NULL
    """
    params = [now, student_id]
    if growth_attempt_id:
        sql += " AND growth_attempt_id = ?"
        params.append(growth_attempt_id)
    conn.execute(sql, params)
    if commit:
        conn.commit()


def get_or_create_student_question_delivery(
    conn: sqlite3.Connection,
    *,
    student_id: str,
    growth_attempt_id: str,
    standard_id: str,
    objective_id: str,
    level: int,
    eligible_questions,
):
    """Reuse one current delivery or persist the next attempt-scoped question."""
    eligible_by_id = {row["question_id"]: row for row in eligible_questions}
    active = conn.execute(
        """
        SELECT *
        FROM student_question_deliveries
        WHERE student_id = ?
          AND consumed_at IS NULL
          AND invalidated_at IS NULL
        """,
        (student_id,),
    ).fetchone()
    if (
        active
        and active["growth_attempt_id"] == growth_attempt_id
        and active["standard_id"] == standard_id
        and active["objective_id"] == objective_id
        and int(active["level"]) == int(level)
        and active["question_id"] in eligible_by_id
    ):
        return active, eligible_by_id[active["question_id"]]

    if active:
        invalidate_student_question_deliveries(conn, student_id, commit=False)

    if not eligible_questions:
        conn.commit()
        return None, None

    sequence = conn.execute(
        """
        SELECT MAX(sequence_number) AS last_sequence
        FROM student_question_deliveries
        WHERE student_id = ?
          AND growth_attempt_id = ?
          AND level = ?
        """,
        (student_id, growth_attempt_id, level),
    ).fetchone()
    last_sequence = row_get(sequence, "last_sequence", None)
    if last_sequence is None:
        growth_row = conn.execute(
            """
            SELECT started_at
            FROM student_growth_progress
            WHERE attempt_id = ?
            """,
            (growth_attempt_id,),
        ).fetchone()
        qids = list(eligible_by_id)
        placeholders = ",".join(["?"] * len(qids))
        historical = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM attempts
            WHERE student_id = ?
              AND question_id IN ({placeholders})
              AND timestamp >= ?
            """,
            [student_id, *qids, int(row_get(growth_row, "started_at", 0) or 0)],
        ).fetchone()
        sequence_number = int(row_get(historical, "n", 0) or 0)
    else:
        sequence_number = int(last_sequence) + 1
    sequence_index = sequence_number % len(eligible_questions)
    question = eligible_questions[sequence_index]
    token = uuid.uuid4().hex
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO student_question_deliveries
          (submission_token, student_id, growth_attempt_id, standard_id,
           objective_id, level, question_id, sequence_number, served_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            token,
            student_id,
            growth_attempt_id,
            standard_id,
            objective_id,
            level,
            question["question_id"],
            sequence_number,
            now,
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM student_question_deliveries WHERE submission_token = ?",
        (token,),
    ).fetchone(), question


def consume_student_question_delivery(
    conn: sqlite3.Connection,
    *,
    submission_token: str,
    student_id: str,
    growth_attempt_id: str,
    standard_id: str,
    objective_id: str,
    level: int,
    submitted_question_id: str | None,
    response: str,
):
    """Atomically consume a current delivery and record exactly one response."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        delivery = conn.execute(
            """
            SELECT d.*, q.answer_key
            FROM student_question_deliveries d
            JOIN questions q ON q.question_id = d.question_id
            WHERE d.submission_token = ?
            """,
            (submission_token,),
        ).fetchone()
        valid = bool(
            delivery
            and delivery["student_id"] == student_id
            and delivery["growth_attempt_id"] == growth_attempt_id
            and delivery["standard_id"] == standard_id
            and delivery["objective_id"] == objective_id
            and int(delivery["level"]) == int(level)
            and (
                not submitted_question_id
                or delivery["question_id"] == submitted_question_id
            )
            and delivery["consumed_at"] is None
            and delivery["invalidated_at"] is None
        )
        if not valid:
            conn.rollback()
            return None

        now = int(time.time())
        consumed = conn.execute(
            """
            UPDATE student_question_deliveries
            SET consumed_at = ?
            WHERE submission_token = ?
              AND consumed_at IS NULL
              AND invalidated_at IS NULL
            """,
            (now, submission_token),
        )
        if consumed.rowcount != 1:
            conn.rollback()
            return None

        correct = 1 if delivery["answer_key"] == response else 0
        std_id, lvl, ts = record_attempt_and_response(
            conn,
            student_id=student_id,
            question_id=delivery["question_id"],
            response=response,
            is_correct=correct,
            objective_id=objective_id,
            level_override=level,
            timestamp=now,
            attempt_prefix="ST",
        )
        conn.commit()
        return {
            "correct": correct,
            "question_id": delivery["question_id"],
            "standard_id": std_id,
            "level": lvl,
            "timestamp": ts,
        }
    except Exception:
        conn.rollback()
        raise


def update_student_growth_presentation(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    correct: bool,
    engine_decision,
):
    """Update presentation evidence without changing any adaptive input or decision."""
    row = conn.execute(
        "SELECT * FROM student_growth_progress WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if not row:
        return None

    decision = engine_decision if isinstance(engine_decision, dict) else {}
    action = decision.get("action")
    status = decision.get("status")
    review_actions = {
        "drop_level",
        "remediate_lower_band",
        "locked",
        "mini_pass_return",
        "mini_pass_resume",
    }
    is_complete = status in ("complete", "completed") or action == "standard_complete"

    stage = float(row["visible_stage"])
    state = "growing" if correct else "strengthening"
    if correct and not is_complete:
        # Ten perceptible presentation positions across a typical objective.
        # This value never feeds the adaptive engine and deliberately has no
        # engine-unit meaning.
        stage = min(3.85, round(stage + 0.32, 2))
    if action in review_actions or status == "locked":
        state = "reviewing"
    if is_complete:
        stage = 4
        state = "complete"

    now = int(time.time())
    conn.execute(
        """
        UPDATE student_growth_progress
        SET visible_stage = MAX(visible_stage, ?),
            presentation_state = ?,
            is_active = CASE WHEN ? THEN 0 ELSE is_active END,
            completed_at = CASE WHEN ? THEN ? ELSE completed_at END,
            updated_at = ?
        WHERE attempt_id = ?
        """,
        (stage, state, is_complete, is_complete, now, now, attempt_id),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM student_growth_progress WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()


def student_growth_view_model(row, *, reviewing: bool = False, complete: bool = False):
    """Convert stored presentation state to an intentionally coarse UI model."""
    stage = float(row_get(row, "visible_stage", 1) or 1)
    state = row_get(row, "presentation_state", "growing")
    if reviewing:
        state = "reviewing"
    if complete:
        state = "complete"
        stage = 4

    messages = {
        "growing": (
            "Growing",
            "You're building your understanding."
            if stage <= 1
            else (
                "You're connecting these ideas."
                if stage >= 3.7
                else "Your understanding is growing."
            ),
        ),
        "strengthening": ("Strengthening", "Let's strengthen this idea."),
        "reviewing": ("Reviewing", "RootED is helping you review this concept."),
        "complete": ("Completed", "This understanding has taken root."),
    }
    stage_classes = (
        "growth-seed",
        "growth-root",
        "growth-shoot",
        "growth-leaf",
        "growth-branch",
        "growth-bud",
        "growth-canopy",
        "growth-ready",
        "growth-nearly",
        "growth-strong",
    )
    if stage >= 4:
        stage_class = "growth-complete"
    else:
        # Round upward so existing persisted growth never appears to move
        # backward when mapped onto the smaller set of visual positions.
        step_index = max(
            0,
            min(
                len(stage_classes) - 1,
                int(((stage - 1) / 0.32) + 0.999999),
            ),
        )
        stage_class = stage_classes[step_index]
    label, message = messages.get(state, messages["growing"])
    return {
        "state": "completed" if state == "complete" else state,
        "label": label,
        "message": message,
        "stage_class": stage_class,
    }


def cumulative_plant_view_model(conn, student_id: str, standard_id: str):
    """Derive cumulative standard growth from completed objective attempts."""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT objective_id) AS completed_objectives
        FROM student_growth_progress
        WHERE student_id = ?
          AND standard_id = ?
          AND presentation_state = 'complete'
          AND completed_at IS NOT NULL
        """,
        (student_id, standard_id),
    ).fetchone()
    completed = int(row_get(row, "completed_objectives", 0) or 0)
    if completed <= 0:
        plant_class = "plant-sprout"
        visible_text = "Your learning is beginning to sprout."
    elif completed == 1:
        plant_class = "plant-first-branch"
        visible_text = "Your learning plant has grown a new branch."
    elif completed == 2:
        plant_class = "plant-second-branch"
        visible_text = "Your learning plant is growing more branches."
    elif completed < 5:
        plant_class = "plant-leafy"
        visible_text = "Your learning plant is growing fuller."
    else:
        plant_class = "plant-canopy"
        visible_text = "Your learning plant has a strong, growing canopy."
    return {
        "plant_class": plant_class,
        "visible_text": visible_text,
        "screen_reader_text": (
            "Cumulative standard growth based on parts of this learning goal "
            "you have completed. " + visible_text
        ),
    }


# ---------- User account helpers ----------
def create_user(conn, username, password, role="teacher", linked_student_id=None):
    pw_hash = generate_password_hash(password)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, linked_student_id) VALUES (?, ?, ?, ?)",
        (username, pw_hash, role, linked_student_id),
    )
    conn.commit()


def get_user_by_username(conn, username):
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def get_user_by_id(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def verify_user(conn, username, password):
    user = get_user_by_username(conn, username)
    if not user:
        return None
    try:
        is_active = int(user["is_active"])
    except Exception:
        is_active = 1
    if is_active != 1:
        return None
    if check_password_hash(user["password_hash"], password):
        return user
    return None

def resolve_sso_user(
    conn,
    provider: str,
    subject: str,
    email: str | None = None,
    *,
    email_verified: bool = False,
    display_name: str | None = None,
    avatar_url: str | None = None,
    allow_create: bool = False,
):
    """Resolve an OIDC identity without granting classroom or platform authority."""
    if not provider or not subject:
        return None
    now_ts = int(time.time())
    row = conn.execute(
        """
        SELECT u.* FROM user_auth_identities i
        JOIN users u ON u.id=i.user_id
        WHERE i.provider=? AND i.provider_subject=? AND i.revoked_at IS NULL
        LIMIT 1
        """,
        (provider, subject),
    ).fetchone()
    if row:
        if not int(row["is_active"]):
            return None
        conn.execute(
            """
            UPDATE user_auth_identities
            SET verified_email=COALESCE(?, verified_email),
                display_name=COALESCE(?, display_name),
                avatar_url=COALESCE(?, avatar_url), last_login_at=?
            WHERE provider=? AND provider_subject=?
            """,
            (email if email_verified else None, display_name, avatar_url,
             now_ts, provider, subject),
        )
        conn.execute("UPDATE users SET last_login_ts=? WHERE id=?", (now_ts, row["id"]))
        conn.commit()
        return conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()

    if not allow_create:
        return None
    if email and email_verified:
        matches = conn.execute(
            """
            SELECT DISTINCT u.* FROM users u
            LEFT JOIN user_auth_identities i ON i.user_id=u.id
            WHERE lower(u.sso_email)=lower(?) OR lower(i.verified_email)=lower(?)
            """,
            (email, email),
        ).fetchall()
        if len(matches) > 1:
            return None
        if len(matches) == 1:
            row = matches[0]
            conn.execute(
                """
                INSERT INTO user_auth_identities
                  (user_id, provider, provider_subject, verified_email,
                   display_name, avatar_url, created_at, last_login_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row["id"], provider, subject, email, display_name, avatar_url,
                 now_ts, now_ts),
            )
            conn.commit()
            return row

    base = email.split("@")[0] if email and "@" in email else subject
    username = base
    suffix = 1
    while conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        suffix += 1
        username = f"{base}{suffix}"
    conn.execute(
        """
        INSERT INTO users
          (username, password_hash, role, account_role, linked_student_id, is_active,
           sso_provider, sso_subject, sso_email, last_login_ts)
        VALUES (?, ?, 'student', 'pending', NULL, 1, ?, ?, ?, ?)
        """,
        (username, generate_password_hash(uuid.uuid4().hex), provider, subject,
         email if email_verified else None, now_ts),
    )
    user_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO user_auth_identities
          (user_id, provider, provider_subject, verified_email, display_name,
           avatar_url, created_at, last_login_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, provider, subject, email if email_verified else None,
         display_name, avatar_url, now_ts, now_ts),
    )
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_questions_for_objective(conn, oid):
    if not is_launch_objective_id(oid):
        return []
    return conn.execute(
        """
        SELECT q.question_id,
               q.objective_id,
               q.stem,
               q.choice_a,
               q.choice_b,
               q.choice_c,
               q.choice_d,
               q.answer_key,
               COALESCE(o.standard_id, '') AS standard_id,
               COALESCE(o.objective_text, '') AS objective_text
        FROM questions q
        LEFT JOIN objectives o ON o.objective_id = q.objective_id
        WHERE TRIM(UPPER(q.objective_id)) = TRIM(UPPER(?))
        ORDER BY q.question_id
        """,
        (oid,),
    ).fetchall()


def get_preview_question(conn, question_id):
    if not question_id:
        return None
    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    return conn.execute(
        f"""
        SELECT q.question_id,
               q.objective_id,
               q.stem,
               q.choice_a,
               q.choice_b,
               q.choice_c,
               q.choice_d,
               q.answer_key,
               COALESCE(o.standard_id, '') AS standard_id,
               COALESCE(o.objective_text, '') AS objective_text
        FROM questions q
        LEFT JOIN objectives o ON o.objective_id = q.objective_id
        WHERE q.question_id = ?
          AND q.objective_id IN ({objective_placeholders})
        LIMIT 1
        """,
        (question_id, *LAUNCH_OBJECTIVE_IDS),
    ).fetchone()


def get_first_preview_question_id(conn, objective_id=None):
    if objective_id:
        if not is_launch_objective_id(objective_id):
            return None
        row = conn.execute(
            """
            SELECT question_id
            FROM questions
            WHERE TRIM(UPPER(objective_id)) = TRIM(UPPER(?))
            ORDER BY question_id
            LIMIT 1
            """,
            (objective_id,),
        ).fetchone()
        if row:
            return row["question_id"]

    objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
    row = conn.execute(
        f"""
        SELECT question_id
        FROM questions
        WHERE objective_id IN ({objective_placeholders})
        ORDER BY question_id
        LIMIT 1
        """,
        LAUNCH_OBJECTIVE_IDS,
    ).fetchone()
    return row["question_id"] if row else None


def check_engine_tables(conn):
    tables = [
        "responses",
        "progress_state",
        "remediation_links",
        "progression_links",
        "origin_links",
        "notifications",
    ]
    checks = {}
    for name in tables:
        try:
            conn.execute(f"SELECT 1 FROM {name} LIMIT 1")
            checks[name] = True
        except sqlite3.OperationalError:
            checks[name] = False
    return checks

def get_recent_error_count(hours=24):
    """
    Return how many errors have been logged in the last `hours`.
    Used to show a small alert banner on the teacher dashboard.
    """
    conn = get_conn()
    cutoff = int(time.time()) - (hours * 3600)
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM error_logs WHERE ts >= ?",
        (cutoff,),
    ).fetchone()
    if row and row["c"] is not None:
        return row["c"]
    return 0

# ---------- FERPA / name handling ----------
def current_pii_mode():
    role = session.get("role")
    if role == "student":
        return "masked"
    return session.get("pii_mode", DEFAULT_PII_MODE)


def display_name(row: sqlite3.Row) -> str:
    first = (row["first_name"] or "").strip()
    last = (row["last_name"] or "").strip()
    if current_pii_mode() == "full":
        full = f"{first} {last}".strip()
        return full or row["student_id"]
    fi = first[:1].upper() if first else ""
    li = last[:1].upper() if last else ""
    init = (fi + li).strip()
    return init or row["student_id"]


# ---------- Misc helpers ----------
@app.route("/toggle_names", methods=["POST"])
@require_teacher
def toggle_names():
    mode = request.form.get("pii_mode")
    if mode in ("masked", "full"):
        session["pii_mode"] = mode
    return redirect(url_for("index"))


@app.route("/teacher/question_preview", methods=["GET"])
@require_teacher
def teacher_question_preview():
    conn = get_conn()
    objs = get_objectives(conn)

    selected_objective_id = request.args.get("objective_id")
    selected_question_id = request.args.get("question_id")
    return_to = request.args.get("return_to")
    flag_filter = normalize_question_flag_status_filter(request.args.get("filter"))
    flag_category = normalize_question_flag_category_filter(request.args.get("category"))
    back_url = (
        url_for("question_flags_review", filter=flag_filter, category=flag_category)
        if return_to == "question_flags"
        else url_for("index")
    )
    back_label = "Back to Question Flags" if return_to == "question_flags" else "Back to Dashboard"

    if not selected_question_id:
        selected_question_id = get_first_preview_question_id(
            conn,
            selected_objective_id,
        )

    current_question = get_preview_question(conn, selected_question_id)
    if current_question:
        selected_objective_id = current_question["objective_id"]

    qrows = (
        get_questions_for_objective(conn, selected_objective_id)
        if selected_objective_id
        else []
    )

    current_model_asset = None
    if current_question:
        resolved_asset = resolve_model_asset_for_question(
            conn,
            current_question["question_id"],
        )
        current_model_asset = static_image_asset_for_render(resolved_asset)

    html = """
<!doctype html>
<title>Teacher Question Preview</title>
<style>
  body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#f3f4f6;color:#1f2937}
  .card{max-width:760px;margin:0 auto 16px auto;background:#fff;border-radius:10px;padding:16px 20px;border:1px solid #e5e7eb}
  .header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}
  .btn{background:#2563eb;color:#fff;border:none;padding:8px 12px;border-radius:8px;cursor:pointer;text-decoration:none;display:inline-block}
  .btn-muted{background:#4b5563}
  .toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 12px 0}
  .banner{background:#fff7d6;border:1px solid #f2d27e;color:#6f4f00;border-radius:8px;padding:10px 12px;margin:12px 0}
  .banner strong,.banner span{display:block}
  .banner strong{margin-bottom:4px}
  .choice{margin:4px 0}
  .choice input{margin-right:8px}
  input,select,textarea{padding:6px 8px;border:1px solid #cbd5e1;border-radius:6px}
  textarea{width:100%;min-height:58px;font:inherit}
  .muted{font-size:13px;color:#555}
  .flag-panel{margin-top:14px;border-top:1px solid #e5e7eb;padding-top:12px}
  .report-toggle{margin-top:14px;background:transparent;color:#4b5563;border:1px solid #d1d5db;padding:5px 8px;border-radius:6px;font-size:12px;cursor:pointer}
  .flag-panel[hidden]{display:none}
  .flag-panel{border:1px solid #e5e7eb;border-radius:8px;background:#f9fafb;padding:10px;margin-top:8px}
  .flag-form{display:grid;gap:8px;margin-top:10px}
  .btn-flag{background:#eef2f7;color:#374151;border:1px solid #cbd5e1}
  .flash-box{background:#e7f7ee;border:1px solid #a8e0bf;color:#0f6b3a;padding:10px 12px;border-radius:8px;margin:10px 0;font-size:14px}
  .model-asset{margin:14px 0 16px 0;padding:12px;border:1px solid #d7dee8;border-radius:8px;background:#f8fafc}
  .model-asset-title{margin:0 0 8px 0;font-weight:700;color:#1f2937}
  .model-asset-caption{margin:8px 0 0 0;font-size:13px;color:#555;line-height:1.4}
  .model-asset img{display:block;max-width:100%;height:auto;margin:0 auto;border-radius:6px}
  .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
</style>

<div class="card">
  <div class="header">
    <div>
      <h2 style="margin:0;">Question Preview</h2>
      <p class="muted" style="margin:2px 0 0 0;">Read-only student rendering for teacher review.</p>
    </div>
    <a class="btn btn-muted" href="{{ back_url }}">{{ back_label }}</a>
  </div>

  {% with msgs = get_flashed_messages() %}
    {% if msgs %}
      <div class="flash-box">
        {% for m in msgs %}
          <div>{{ m }}</div>
        {% endfor %}
      </div>
    {% endif %}
  {% endwith %}

  <form method="get" class="toolbar">
    <label>Objective
      <select name="objective_id" onchange="this.form.submit()">
        {% for o in objs %}
          <option value="{{ o['objective_id'] }}" {% if o['objective_id'] == selected_objective_id %}selected{% endif %}>
            {{ o['objective_id'] }}
          </option>
        {% endfor %}
      </select>
    </label>
    <label>Question
      <select name="question_id" onchange="this.form.submit()">
        {% for q in qrows %}
          <option value="{{ q['question_id'] }}" {% if q['question_id'] == selected_question_id %}selected{% endif %}>
            {{ question_selector_label(q) }}
          </option>
        {% endfor %}
      </select>
    </label>
    <noscript><button class="btn" type="submit">Preview</button></noscript>
  </form>

  <div class="banner">
    <strong>Teacher Preview</strong>
    <span>This question is displayed as students see it.</span>
    <span>Responses are disabled.</span>
    <span>Student progress, adaptive routing, and Rolling-7 are not affected.</span>
  </div>

  {% if current_question %}
    <h3>Question</h3>
    <p>{{ current_question['stem'] }}</p>
    <p class="muted">
      {{ question_reference_meta_text(
           current_question['objective_id'],
           current_question['objective_text'],
           current_question['question_id']
         ) }}
      <span>({{ current_question['standard_id'] }})</span>
    </p>
    {% if current_model_asset %}
      <figure class="model-asset">
        {% if current_model_asset.title %}
          <figcaption class="model-asset-title">{{ current_model_asset.title }}</figcaption>
        {% endif %}
        <img src="{{ url_for('static', filename=current_model_asset.filename) }}" alt="{{ current_model_asset.alt_text }}">
        {% if current_model_asset.caption %}
          <p class="model-asset-caption">{{ current_model_asset.caption }}</p>
        {% endif %}
        {% if current_model_asset.alt_text %}
          <span class="sr-only">Image description: {{ current_model_asset.alt_text }}</span>
        {% endif %}
      </figure>
    {% endif %}

    <div aria-label="Answer choices">
      <div class="choice"><label><input type="radio" name="preview_response" value="A" disabled> A. {{ current_question['choice_a'] }}</label></div>
      <div class="choice"><label><input type="radio" name="preview_response" value="B" disabled> B. {{ current_question['choice_b'] }}</label></div>
      <div class="choice"><label><input type="radio" name="preview_response" value="C" disabled> C. {{ current_question['choice_c'] }}</label></div>
      <div class="choice"><label><input type="radio" name="preview_response" value="D" disabled> D. {{ current_question['choice_d'] }}</label></div>
    </div>
    <button class="report-toggle" type="button" aria-expanded="false" aria-controls="teacher-report-panel" onclick="toggleReportPanel('teacher-report-panel', this)">Report a problem</button>
    <div class="flag-panel" id="teacher-report-panel" hidden>
      <form class="flag-form" method="post" action="{{ url_for('submit_question_flag') }}">
        <input type="hidden" name="question_id" value="{{ current_question['question_id'] }}">
        <input type="hidden" name="page_context" value="teacher_question_preview">
        <input type="hidden" name="next" value="{{ url_for('teacher_question_preview', question_id=current_question['question_id'], return_to=return_to, filter=flag_filter, category=flag_category) if return_to else url_for('teacher_question_preview', question_id=current_question['question_id']) }}">
        <div class="muted">
          {{ question_reference_meta_text(
               current_question['objective_id'],
               current_question['objective_text'],
               current_question['question_id']
             ) }}
        </div>
        <label>Category
          <select name="category" required>
            {% for code, label in flag_categories %}
              <option value="{{ code }}">{{ label }}</option>
            {% endfor %}
          </select>
        </label>
        <label>Comment
          <textarea name="comment" maxlength="{{ flag_comment_max_length }}" placeholder="Optional note"></textarea>
        </label>
        <div>
          <button class="btn btn-flag" type="submit">Submit</button>
          <button class="btn btn-muted" type="button" onclick="closeReportPanel('teacher-report-panel')">Cancel</button>
        </div>
      </form>
    </div>
  {% else %}
    <p><em>No questions are available to preview yet.</em></p>
  {% endif %}
</div>
<script>
  function toggleReportPanel(panelId, button){
    const panel = document.getElementById(panelId);
    const isOpening = panel.hasAttribute('hidden');
    panel.toggleAttribute('hidden', !isOpening);
    button.setAttribute('aria-expanded', String(isOpening));
    if(isOpening){
      const firstField = panel.querySelector('select, textarea, button');
      if(firstField){ firstField.focus(); }
    }
  }
  function closeReportPanel(panelId){
    const panel = document.getElementById(panelId);
    const button = document.querySelector('[aria-controls="' + panelId + '"]');
    panel.setAttribute('hidden', '');
    if(button){
      button.setAttribute('aria-expanded', 'false');
      button.focus();
    }
  }
</script>
    """
    return render_template_string(
        html,
        objs=objs,
        qrows=qrows,
        selected_objective_id=selected_objective_id,
        selected_question_id=selected_question_id,
        current_question=current_question,
        current_model_asset=current_model_asset,
        flag_categories=QUESTION_FLAG_CATEGORIES,
        flag_comment_max_length=QUESTION_FLAG_COMMENT_MAX_LENGTH,
        back_url=back_url,
        back_label=back_label,
        return_to=return_to,
        flag_filter=flag_filter,
        flag_category=flag_category,
        question_reference_meta_text=question_reference_meta_text,
        question_selector_label=question_selector_label,
    )


@app.post("/question_flags")
@instruction_required
def submit_question_flag():
    conn = get_conn()
    ok, message, _ = create_question_flag(
        conn,
        question_id=request.form.get("question_id", ""),
        category=request.form.get("category"),
        comment=request.form.get("comment"),
        page_context=request.form.get("page_context"),
    )
    flash(message)
    next_url = request.form.get("next") or (
        url_for("student_view")
        if session.get("role") == "student"
        else url_for("question_flags_review")
    )
    if not ok and session.get("role") == "teacher":
        next_url = request.form.get("next") or url_for("teacher_question_preview")
    return redirect(next_url)


@app.route("/teacher/question_flags")
@require_teacher
def question_flags_review():
    conn = get_conn()
    selected_filter = normalize_question_flag_status_filter(request.args.get("filter"))
    selected_category = normalize_question_flag_category_filter(request.args.get("category"))
    flags = get_question_flags_for_teacher(
        conn,
        session.get("user_id"),
        selected_filter,
        selected_category,
    )
    status_counts = question_flag_status_filter_counts(
        conn,
        session.get("user_id"),
        selected_category,
    )
    selected_status_config = QUESTION_FLAG_STATUS_FILTERS[selected_filter]
    selected_category_label = question_flag_category_label(selected_category)
    active_category_filter = selected_category != QUESTION_FLAG_DEFAULT_CATEGORY_FILTER
    empty_message = (
        f"No {selected_status_config['label'].lower()} reports match {selected_category_label}."
        if active_category_filter
        else selected_status_config["empty"]
    )

    html = """
<!doctype html>
<title>Question Flags</title>
<style>
  body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#fbfaf4;color:#1f2937}
  .page{max-width:1180px;margin:0 auto}
  .topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:16px;padding:16px 18px;border:1px solid #dfe8d9;border-radius:12px;background:#fffdf7}
  .topbar h1{margin:0 0 6px;font-size:30px}
  .subtitle{margin:0;color:#4b5563;font-size:14px}
  .btn{display:inline-block;background:#2f6f4e;color:#fff;text-decoration:none;border:none;padding:8px 12px;border-radius:8px;cursor:pointer;font-size:13px}
  .btn-secondary{background:#eef4ec;color:#2f5138;border:1px solid #c8d9c4}
  .card{border:1px solid #e2decf;border-radius:10px;padding:16px;margin:16px 0;background:#fffefa;box-shadow:0 8px 18px rgba(47,111,78,.05)}
  table{border-collapse:collapse;width:100%}
  th,td{border:1px solid #e5e0d2;padding:8px;vertical-align:top;font-size:13px}
  th{background:#f5f1e8;text-align:left;color:#374151}
  textarea{width:100%;min-height:54px;padding:7px 8px;border:1px solid #cbd5e1;border-radius:8px;font:inherit}
  .pill{display:inline-block;padding:3px 8px;border-radius:999px;font-size:12px;font-weight:800}
  .pill-open{background:#fef3c7;color:#92400e}
  .pill-teacher_resolved,.pill-fixed,.pill-closed{background:#dcfce7;color:#166534}
  .pill-escalated,.pill-owner_reviewing{background:#dbeafe;color:#1d4ed8}
  .muted{color:#6b7280;font-size:12px}
  .empty{border:1px dashed #c8d9c4;border-radius:8px;padding:12px;background:#fbf8ef;color:#53665a}
  .flash{background:#e7f7ee;border:1px solid #a8e0bf;color:#0f6b3a;padding:10px 12px;border-radius:8px;margin:10px 0;font-size:14px}
  {{ question_reference_css }}
  .filter-tabs{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 12px}
  .filter-tab{display:inline-flex;gap:6px;align-items:center;text-decoration:none;border:1px solid #c8d9c4;border-radius:999px;padding:7px 10px;color:#2f5138;background:#eef4ec;font-weight:750;font-size:13px}
  .filter-tab[aria-current="page"]{background:#2f6f4e;color:#fff;border-color:#2f6f4e}
  .tab-count{display:inline-flex;align-items:center;justify-content:center;min-width:20px;height:20px;padding:0 6px;border-radius:999px;background:rgba(255,255,255,.75);color:#1f2937;font-size:12px}
  .category-filter{display:flex;gap:8px;align-items:end;flex-wrap:wrap;margin:0 0 14px}
  .category-filter label{font-size:13px;font-weight:750;color:#374151}
  .category-filter select{display:block;margin-top:5px;padding:7px 8px;border:1px solid #cbd5e1;border-radius:8px;background:#fff}
</style>
<div class="page">
  <div class="topbar">
    <div>
      <h1>Question Flags</h1>
      <p class="subtitle">Unresolved flags appear first. Student context is only shown for your active classes.</p>
    </div>
    <div>
      <a class="btn btn-secondary" href="{{ url_for('index') }}">Back to dashboard</a>
    </div>
  </div>

  {% with msgs = get_flashed_messages() %}
    {% if msgs %}
      <div class="flash">
        {% for m in msgs %}<div>{{ m }}</div>{% endfor %}
      </div>
    {% endif %}
  {% endwith %}

  <div class="card">
    <nav class="filter-tabs" aria-label="Question flag status filters">
      {% for key, config in status_filters.items() %}
        <a class="filter-tab"
           href="{{ url_for('question_flags_review', filter=key, category=selected_category) }}"
           {% if key == selected_filter %}aria-current="page"{% endif %}>
          <span>{{ config.label }}</span>
          <span class="tab-count">{{ status_counts[key] }}</span>
        </a>
      {% endfor %}
    </nav>

    <form class="category-filter" method="get" action="{{ url_for('question_flags_review') }}">
      <input type="hidden" name="filter" value="{{ selected_filter }}">
      <label>Category
        <select name="category" onchange="this.form.submit()">
          {% for code, label in category_options %}
            <option value="{{ code }}" {% if code == selected_category %}selected{% endif %}>{{ label }}</option>
          {% endfor %}
        </select>
      </label>
      <noscript><button class="btn btn-secondary" type="submit">Apply</button></noscript>
    </form>

    {% if flags %}
      <table>
        <tr>
          <th>Status</th>
          <th>Question</th>
          <th>Category</th>
          <th>Reporter</th>
          <th>Context</th>
          <th>Comment</th>
          <th>Submitted</th>
          <th>Next Step</th>
        </tr>
        {% for flag in flags %}
          <tr>
            <td><span class="pill pill-{{ flag['status'] }}">{{ flag['status'].replace('_', ' ') }}</span></td>
            <td>
              {{ question_reference_for_flag(
                   flag,
                   url_for('teacher_question_preview', question_id=flag['question_id'], return_to='question_flags', filter=selected_filter, category=selected_category)
                 ) }}
              {% if flag['question_flag_count'] > 1 %}
                <br><span class="muted">{{ flag['question_flag_count'] }} total flag(s), {{ flag['question_open_count'] or 0 }} open</span>
              {% endif %}
            </td>
            <td>{{ category_label(flag['category']) }}</td>
            <td>{{ flag['reporter_role'] }}</td>
            <td>
              {% if flag['class_name'] %}
                {{ flag['class_name'] }}<br>
              {% endif %}
              {% if flag['student_id'] and flag['class_teacher_user_id'] == session.get('user_id') %}
                <span class="muted">Student: {{ flag['student_id'] }}</span><br>
              {% elif flag['student_id'] %}
                <span class="muted">Student: hidden</span><br>
              {% endif %}
              <span class="muted">{{ flag['page_context'] or 'No page context' }}</span>
            </td>
            <td>{{ flag['comment'] or '' }}</td>
            <td>{{ format_ts(flag['created_ts']) }}</td>
            <td>
              {% if flag['status'] == 'teacher_resolved' %}
                <div>{{ flag['resolution_note'] or 'Resolved' }}</div>
                <div class="muted">{{ format_ts(flag['resolved_ts']) }}</div>
              {% elif flag['status'] in ['escalated', 'owner_reviewing', 'fixed', 'closed'] %}
                <div><strong>Sent to RootED Support</strong></div>
                {% if flag['escalation_note'] %}
                  <div class="muted">{{ flag['escalation_note'] }}</div>
                {% endif %}
                <div class="muted">{{ format_ts(flag['escalated_at']) }}</div>
              {% else %}
                <form method="post" action="{{ url_for('resolve_question_flag', flag_id=flag['flag_id']) }}">
                  <input type="hidden" name="filter" value="{{ selected_filter }}">
                  <input type="hidden" name="category" value="{{ selected_category }}">
                  <textarea name="resolution_note" placeholder="Resolution note"></textarea>
                  <button class="btn" type="submit" style="margin-top:6px;">Mark Resolved</button>
                </form>
                <form method="post" action="{{ url_for('escalate_question_flag', flag_id=flag['flag_id']) }}" style="margin-top:10px;">
                  <input type="hidden" name="filter" value="{{ selected_filter }}">
                  <input type="hidden" name="category" value="{{ selected_category }}">
                  <textarea name="escalation_note" placeholder="Optional note for RootED Support"></textarea>
                  <button class="btn btn-secondary" type="submit" style="margin-top:6px;">Send to RootED Support</button>
                </form>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <div class="empty">{{ empty_message }}</div>
    {% endif %}
  </div>
</div>
    """
    return render_template_string(
        html,
        flags=flags,
        format_ts=format_ts,
        category_label=question_flag_category_label,
        question_reference_for_flag=question_reference_for_flag,
        question_reference_css=Markup(QUESTION_REFERENCE_CSS),
        category_options=question_flag_category_filter_options(),
        status_filters=QUESTION_FLAG_STATUS_FILTERS,
        status_counts=status_counts,
        selected_filter=selected_filter,
        selected_category=selected_category,
        empty_message=empty_message,
    )


@app.post("/teacher/question_flags/<flag_id>/resolve")
@require_teacher
def resolve_question_flag(flag_id):
    conn = get_conn()
    selected_filter = normalize_question_flag_status_filter(request.form.get("filter"))
    selected_category = normalize_question_flag_category_filter(request.form.get("category"))
    flag = get_question_flag_by_id(conn, flag_id)
    if not flag:
        abort(404)
    if not teacher_authorized_for_flag(conn, flag):
        abort(403)
    note = (request.form.get("resolution_note") or "").strip()
    if len(note) > 1000:
        note = note[:1000]
    conn.execute(
        """
        UPDATE question_flags
        SET status = 'teacher_resolved',
            resolved_ts = ?,
            resolution_note = ?,
            resolved_by_user_id = ?
        WHERE flag_id = ?
        """,
        (int(time.time()), note or None, session.get("user_id"), flag_id),
    )
    conn.commit()
    flash("Flag marked resolved.")
    return redirect(url_for("question_flags_review", filter=selected_filter, category=selected_category))


@app.post("/teacher/question_flags/<flag_id>/escalate")
@require_teacher
def escalate_question_flag(flag_id):
    conn = get_conn()
    selected_filter = normalize_question_flag_status_filter(request.form.get("filter"))
    selected_category = normalize_question_flag_category_filter(request.form.get("category"))
    flag = get_question_flag_by_id(conn, flag_id)
    if not flag:
        abort(404)
    if not teacher_authorized_for_flag(conn, flag):
        abort(403)
    note = (request.form.get("escalation_note") or "").strip()
    if len(note) > 1000:
        note = note[:1000]
    conn.execute(
        """
        UPDATE question_flags
        SET status = 'escalated',
            escalated_by_user_id = ?,
            escalated_at = ?,
            escalation_note = ?
        WHERE flag_id = ?
        """,
        (session.get("user_id"), int(time.time()), note or None, flag_id),
    )
    conn.commit()
    flash("Flag sent to RootED Support.")
    return redirect(url_for("question_flags_review", filter=selected_filter, category=selected_category))


# ---------- Owner workspace ----------
def owner_csrf_token() -> str:
    token = session.get("owner_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["owner_csrf_token"] = token
    return token


def require_owner_csrf() -> None:
    supplied = request.form.get("csrf_token", "")
    expected = session.get("owner_csrf_token", "")
    if not supplied or not expected or not hmac.compare_digest(supplied, expected):
        abort(400, description="Invalid or expired form token.")


def get_people_access_rows(conn: sqlite3.Connection):
    rows = conn.execute(
        """
        SELECT
          u.id AS user_id,
          COALESCE(
            (
              SELECT i.display_name
              FROM user_auth_identities i
              WHERE i.user_id = u.id AND i.revoked_at IS NULL
                    AND i.display_name IS NOT NULL
              ORDER BY i.last_login_at DESC, i.identity_id DESC
              LIMIT 1
            ),
            u.username
          ) AS display_name,
          COALESCE(
            (
              SELECT GROUP_CONCAT(provider, ', ')
              FROM (
                SELECT DISTINCT i.provider AS provider
                FROM user_auth_identities i
                WHERE i.user_id = u.id AND i.revoked_at IS NULL
                ORDER BY i.provider
              )
            ),
            CASE WHEN u.sso_provider IS NULL THEN 'local' ELSE u.sso_provider END
          ) AS authentication_provider,
          COALESCE(
            (
              SELECT i.verified_email
              FROM user_auth_identities i
              WHERE i.user_id = u.id AND i.revoked_at IS NULL
                    AND i.verified_email IS NOT NULL
              ORDER BY i.last_login_at DESC, i.identity_id DESC
              LIMIT 1
            ),
            u.sso_email
          ) AS verified_email,
          CASE
            WHEN u.is_active=0 THEN 'Deactivated'
            WHEN u.account_role = 'pending'
             AND NOT EXISTS (
               SELECT 1 FROM user_platform_roles p
               WHERE p.user_id=u.id AND p.platform_role='owner'
                 AND p.revoked_at IS NULL
             )
             AND NOT EXISTS (
               SELECT 1 FROM user_instructional_authorizations a
               WHERE a.user_id=u.id AND a.instructional_role='teacher'
                 AND a.revoked_at IS NULL
             )
             AND NOT EXISTS (
               SELECT 1
               FROM class_enrollments ce
               JOIN class_sections cs ON cs.class_id=ce.class_id
               WHERE ce.student_id=u.linked_student_id
                 AND ce.is_active=1 AND cs.is_active=1
             )
            THEN 'Pending'
            ELSE 'Active'
          END AS account_status,
          CASE WHEN EXISTS (
            SELECT 1 FROM user_platform_roles p
            WHERE p.user_id = u.id
              AND p.platform_role = 'owner'
              AND p.revoked_at IS NULL
          ) THEN 'Owner' ELSE 'None' END AS platform_authority,
          CASE WHEN EXISTS (
            SELECT 1 FROM user_instructional_authorizations a
            WHERE a.user_id = u.id
              AND a.instructional_role = 'teacher'
              AND a.revoked_at IS NULL
          ) THEN 'Teacher' ELSE 'None' END AS instructional_authorization,
          (
            SELECT COUNT(*) FROM class_sections cs
            WHERE cs.teacher_user_id = u.id AND cs.is_active = 1
          ) AS active_teacher_class_count,
          (
            SELECT COUNT(*)
            FROM class_enrollments ce
            JOIN class_sections cs ON cs.class_id = ce.class_id
            WHERE ce.student_id = u.linked_student_id
              AND ce.is_active=1 AND cs.is_active = 1
          ) AS active_student_membership_count,
          CASE
            WHEN u.last_login_ts IS NULL THEN (
              SELECT MAX(i.last_login_at) FROM user_auth_identities i
              WHERE i.user_id=u.id AND i.revoked_at IS NULL
            )
            WHEN (
              SELECT MAX(i.last_login_at) FROM user_auth_identities i
              WHERE i.user_id=u.id AND i.revoked_at IS NULL
            ) > u.last_login_ts THEN (
              SELECT MAX(i.last_login_at) FROM user_auth_identities i
              WHERE i.user_id=u.id AND i.revoked_at IS NULL
            )
            ELSE u.last_login_ts
          END AS last_sign_in
        FROM users u
        GROUP BY u.id
        ORDER BY display_name COLLATE NOCASE, u.id
        """
    ).fetchall()

    people = []
    for row in rows:
        person = dict(row)
        if person["platform_authority"] == "Owner":
            person["expected_destination"] = "/owner"
        elif person["instructional_authorization"] == "Teacher":
            person["expected_destination"] = "/teacher"
        elif person["active_student_membership_count"]:
            person["expected_destination"] = "/student"
        else:
            person["expected_destination"] = "/restricted"
        people.append(person)
    return people


@app.get("/owner")
@owner_required
def owner_home():
    conn = get_conn()
    escalated_count = owner_escalated_question_flag_count(conn)
    user = current_user()
    html = """
<!doctype html>
<title>RootED Owner Workspace</title>
<style>
  body{font-family:Arial,Helvetica,sans-serif;margin:0;background:#fbfaf4;color:#1f2937}
  .page{max-width:960px;margin:0 auto;padding:24px}
  .topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;padding:18px 20px;border:1px solid #dfe8d9;border-radius:12px;background:linear-gradient(135deg,#fffdf7,#eef7ed)}
  h1{margin:0;color:#234b35}.subtitle{margin:7px 0 0;color:#4b6654}
  .actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .btn{display:inline-flex;align-items:center;gap:7px;background:#2f6f4e;color:#fff;text-decoration:none;border:none;padding:9px 13px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:700}
  .btn-secondary{background:#eef4ec;color:#2f5138;border:1px solid #c8d9c4}
  .card{margin-top:18px;padding:20px;border:1px solid #e2decf;border-radius:12px;background:#fffefa;box-shadow:0 8px 18px rgba(47,111,78,.06)}
  .card h2{margin:0 0 8px;color:#234b35}.muted{color:#647067;font-size:13px}
  .badge{display:inline-flex;align-items:center;justify-content:center;min-width:24px;height:24px;padding:0 7px;border-radius:999px;background:#b42318;color:#fff;font-size:12px;font-weight:850}
  .account{display:grid;grid-template-columns:max-content 1fr;gap:7px 12px;margin:10px 0 0}
</style>
<main class="page">
  <header class="topbar">
    <div>
      <h1>RootED Owner Workspace</h1>
      <p class="subtitle">Platform support and escalated question review.</p>
    </div>
    <div class="actions">
      {% if is_teacher_user %}
        <a class="btn btn-secondary" href="{{ url_for('teacher_home') }}">Teacher Workspace</a>
      {% endif %}
      <a class="btn btn-secondary" href="{{ url_for('owner_people_access') }}">People &amp; Access</a>
      <a class="btn btn-secondary" href="{{ url_for('logout') }}">Sign Out</a>
    </div>
  </header>

  <section class="card">
    <h2>Escalated Question Flags</h2>
    <p>Review reports teachers have sent to RootED Support.</p>
    <a class="btn" href="{{ url_for('owner_question_flags') }}">
      <span>Open Question Flags</span>
      {% if escalated_count %}
        <span class="badge" aria-label="{{ escalated_count }} escalated report{{ '' if escalated_count == 1 else 's' }} awaiting review">{{ escalated_count }}</span>
      {% endif %}
    </a>
  </section>

  <section class="card">
    <h2>Adaptive Engine Debug</h2>
    <p>Inspect a student's current routing attempt, cumulative evidence, decision, and delivery history.</p>
    <a class="btn" href="{{ url_for('owner_adaptive_debug') }}">Open Adaptive Debug</a>
  </section>

  <section class="card">
    <h2>Account</h2>
    <dl class="account">
      <dt>Username</dt><dd>{{ user['username'] }}</dd>
      <dt>Instructional authorization</dt><dd>{{ 'Teacher' if is_teacher_user else 'None' }}</dd>
      <dt>Platform authority</dt><dd>Owner</dd>
    </dl>
  </section>
</main>
    """
    return render_template_string(
        html,
        escalated_count=escalated_count,
        user=user,
        is_teacher_user=is_teacher(user),
    )


@app.get("/owner/people-access")
@owner_required
def owner_people_access():
    conn = get_conn()
    try:
        people = get_people_access_rows(conn)
    finally:
        conn.close()
    return render_template_string(
        """
<!doctype html>
<title>RootED People &amp; Access</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;margin:0;background:#fbfaf4;color:#1f2937}
.page{max-width:1440px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;gap:16px;align-items:center}
h1{color:#234b35;margin-bottom:6px}.muted{color:#647067;font-size:13px}
.btn{display:inline-block;background:#2f6f4e;color:#fff;text-decoration:none;border:0;padding:8px 12px;border-radius:8px;font-weight:700}
.btn-secondary{background:#eef4ec;color:#2f5138;border:1px solid #c8d9c4}
.table-wrap{overflow:auto;margin-top:18px;background:#fff;border:1px solid #e2decf;border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:11px 12px;text-align:left;border-bottom:1px solid #ebe8de;white-space:nowrap}
th{background:#eef4ec;color:#234b35}.name{font-weight:700}.empty{color:#7a837b}
</style>
<main class="page">
  <div class="top">
    <div><h1>People &amp; Access</h1><p class="muted">Protected account-administration information. Owner access only.</p></div>
    <a class="btn btn-secondary" href="{{ url_for('owner_home') }}">Owner Workspace</a>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Display name</th><th>User ID</th><th>Authentication provider</th>
        <th>Verified email</th><th>Account status</th><th>Platform authority</th>
        <th>Instructional authorization</th><th>Active teacher classes</th>
        <th>Active student memberships</th><th>Expected destination</th>
        <th>Last sign-in</th><th>Action</th>
      </tr></thead>
      <tbody>
      {% for person in people %}
        <tr>
          <td class="name">{{ person.display_name }}</td>
          <td>{{ person.user_id }}</td><td>{{ person.authentication_provider }}</td>
          <td>{{ person.verified_email or '—' }}</td><td>{{ person.account_status }}</td>
          <td>{{ person.platform_authority }}</td><td>{{ person.instructional_authorization }}</td>
          <td>{{ person.active_teacher_class_count }}</td>
          <td>{{ person.active_student_membership_count }}</td>
          <td>{{ person.expected_destination }}</td>
          <td>{{ format_ts(person.last_sign_in) or 'Not recorded' }}</td>
          <td>
            {% if person.instructional_authorization == 'Teacher' %}
              <span class="empty">Authorized</span>
              <a class="btn" href="{{ url_for('owner_confirm_teacher_revocation', user_id=person.user_id) }}">Revoke Teacher</a>
            {% else %}
              <a class="btn" href="{{ url_for('owner_confirm_teacher_authorization', user_id=person.user_id) }}">Authorize as Teacher</a>
            {% endif %}
            {% if person.account_status == 'Deactivated' %}
              <a class="btn" href="{{ url_for('owner_confirm_account_reactivation', user_id=person.user_id) }}">Reactivate</a>
            {% else %}
              <a class="btn" href="{{ url_for('owner_confirm_account_deactivation', user_id=person.user_id) }}">Deactivate</a>
            {% endif %}
          </td>
        </tr>
      {% else %}<tr><td colspan="12" class="empty">No active or pending accounts.</td></tr>{% endfor %}
      </tbody>
    </table>
  </div>
</main>
        """,
        people=people,
        format_ts=format_ts,
    )


@app.get("/owner/people-access/<int:user_id>/authorize-teacher")
@owner_required
def owner_confirm_teacher_authorization(user_id):
    conn = get_conn()
    try:
        target = conn.execute(
            """
            SELECT u.id,
                   COALESCE(
                     (SELECT i.display_name FROM user_auth_identities i
                      WHERE i.user_id=u.id AND i.revoked_at IS NULL
                      ORDER BY i.last_login_at DESC, i.identity_id DESC LIMIT 1),
                     u.username
                   ) AS display_name,
                   EXISTS(
                     SELECT 1 FROM user_instructional_authorizations a
                     WHERE a.user_id=u.id AND a.instructional_role='teacher'
                       AND a.revoked_at IS NULL
                   ) AS already_teacher
            FROM users u WHERE u.id=? AND u.is_active=1
            """,
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if not target:
        abort(404)
    return render_template_string(
        """
<!doctype html><title>Confirm Teacher Authorization</title>
<style>body{font-family:Arial;margin:40px;background:#fbfaf4;color:#1f2937}.card{max-width:620px;margin:auto;background:#fff;padding:24px;border:1px solid #e2decf;border-radius:12px}.actions{display:flex;gap:10px;margin-top:20px}.btn{background:#2f6f4e;color:#fff;border:0;padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;cursor:pointer}.secondary{background:#eef4ec;color:#2f5138;border:1px solid #c8d9c4}</style>
<main class="card"><h1>Confirm Teacher Authorization</h1>
  <p>You are authorizing <strong>{{ target['display_name'] }}</strong> (User ID {{ target['id'] }}) to access the Teacher Workspace and create classes.</p>
  <p>This will not change identity information, Owner authority, class membership, or subscription entitlement.</p>
  {% if target['already_teacher'] %}<p>This account is already authorized. Confirming again is safe and will not create a duplicate grant.</p>{% endif %}
  <div class="actions">
    <form method="post" action="{{ url_for('owner_authorize_teacher', user_id=target['id']) }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="btn" type="submit">Confirm Authorization</button>
    </form>
    <a class="btn secondary" href="{{ url_for('owner_people_access') }}">Cancel</a>
  </div>
</main>
        """,
        target=target,
        csrf_token=owner_csrf_token(),
    )


@app.post("/owner/people-access/<int:user_id>/authorize-teacher")
@owner_required
def owner_authorize_teacher(user_id):
    require_owner_csrf()
    actor_user_id = current_user()["id"]
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            "SELECT id FROM users WHERE id=? AND is_active=1",
            (user_id,),
        ).fetchone()
        if not target:
            conn.rollback()
            abort(404)
        authorization = conn.execute(
            """
            SELECT authorization_id
            FROM user_instructional_authorizations
            WHERE user_id=? AND instructional_role='teacher' AND revoked_at IS NULL
            """,
            (user_id,),
        ).fetchone()
        outcome = "already_granted"
        if not authorization:
            cursor = conn.execute(
                """
                INSERT INTO user_instructional_authorizations
                  (user_id, instructional_role, granted_at, granted_by, grant_note)
                VALUES (?, 'teacher', ?, ?, 'Authorized in Owner People & Access')
                """,
                (user_id, int(time.time()), actor_user_id),
            )
            authorization_id = cursor.lastrowid
            outcome = "granted"
        else:
            authorization_id = authorization["authorization_id"]
        conn.execute(
            """
            INSERT INTO access_authorization_audit_log
              (actor_user_id, target_user_id, action, authorization_id,
               outcome, created_at, request_ip, user_agent)
            VALUES (?, ?, 'teacher_authorized', ?, ?, ?, ?, ?)
            """,
            (
                actor_user_id, user_id, authorization_id, outcome, int(time.time()),
                request.remote_addr, request.user_agent.string[:500],
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    flash(
        "Teacher authorization granted."
        if outcome == "granted"
        else "This account was already authorized as a teacher."
    )
    return redirect(url_for("owner_people_access"))


def owner_account_target(conn, user_id):
    return conn.execute(
        """
        SELECT u.*,
               COALESCE(
                 (SELECT i.display_name FROM user_auth_identities i
                  WHERE i.user_id=u.id AND i.revoked_at IS NULL
                  ORDER BY i.last_login_at DESC, i.identity_id DESC LIMIT 1),
                 u.username
               ) AS display_name
        FROM users u WHERE u.id=?
        """,
        (user_id,),
    ).fetchone()


@app.get("/owner/people-access/<int:user_id>/revoke-teacher")
@owner_required
def owner_confirm_teacher_revocation(user_id):
    conn = get_conn()
    try:
        target = owner_account_target(conn, user_id)
        active_classes = conn.execute(
            """
            SELECT class_id, name FROM class_sections
            WHERE teacher_user_id=? AND is_active=1 ORDER BY name
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    if not target:
        abort(404)
    return render_template_string(
        """
<!doctype html><title>Confirm Teacher Revocation</title>
<main style="font-family:Arial;max-width:680px;margin:40px auto">
<h1>Confirm Teacher Revocation</h1>
<p>Revoke Teacher Workspace access for <strong>{{ target['display_name'] }}</strong>?</p>
{% if active_classes %}
<p>This authorization cannot be revoked until these active classes are reassigned or archived:</p>
<ul>{% for c in active_classes %}<li>{{ c['name'] }} ({{ c['class_id'] }})</li>{% endfor %}</ul>
{% else %}
<form method="post" action="{{ url_for('owner_revoke_teacher', user_id=target['id']) }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
<label>Reason <input name="reason"></label>
<button type="submit">Revoke Teacher Authorization</button>
</form>
{% endif %}
<p><a href="{{ url_for('owner_people_access') }}">Cancel</a></p>
</main>
        """,
        target=target, active_classes=active_classes, csrf_token=owner_csrf_token(),
    )


@app.post("/owner/people-access/<int:user_id>/revoke-teacher")
@owner_required
def owner_revoke_teacher(user_id):
    require_owner_csrf()
    actor = current_user()["id"]
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        target = owner_account_target(conn, user_id)
        if not target:
            conn.rollback()
            abort(404)
        active_classes = conn.execute(
            "SELECT class_id, name FROM class_sections WHERE teacher_user_id=? AND is_active=1",
            (user_id,),
        ).fetchall()
        authorization = conn.execute(
            """
            SELECT authorization_id FROM user_instructional_authorizations
            WHERE user_id=? AND instructional_role='teacher' AND revoked_at IS NULL
            """,
            (user_id,),
        ).fetchone()
        outcome = "already_revoked"
        authorization_id = None
        if active_classes and authorization:
            outcome = "blocked_active_classes"
        elif authorization:
            authorization_id = authorization["authorization_id"]
            conn.execute(
                """
                UPDATE user_instructional_authorizations
                SET revoked_at=?, revoked_by=?, revoke_note=?
                WHERE authorization_id=? AND revoked_at IS NULL
                """,
                (int(time.time()), actor, request.form.get("reason", "").strip() or None,
                 authorization_id),
            )
            outcome = "revoked"
        conn.execute(
            """
            INSERT INTO authorization_lifecycle_audit_log
              (actor_user_id,target_user_id,action,authorization_id,outcome,reason,created_at)
            VALUES (?,?,'teacher_revoked',?,?,?,?)
            """,
            (actor, user_id, authorization_id, outcome,
             request.form.get("reason", "").strip() or None, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()
    if outcome == "blocked_active_classes":
        flash("Teacher authorization was not revoked. Reassign or archive active classes first.")
    elif outcome == "revoked":
        flash("Teacher authorization revoked.")
    else:
        flash("Teacher authorization was already revoked.")
    return redirect(url_for("owner_people_access"))


def render_account_lifecycle_confirmation(target, action):
    is_deactivate = action == "deactivate"
    return render_template_string(
        """
<!doctype html><title>Confirm Account {{ 'Deactivation' if is_deactivate else 'Reactivation' }}</title>
<main style="font-family:Arial;max-width:680px;margin:40px auto">
<h1>Confirm Account {{ 'Deactivation' if is_deactivate else 'Reactivation' }}</h1>
<p>Account: <strong>{{ target['display_name'] }}</strong></p>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
{% if is_deactivate %}
<label>Type <strong>{{ target['display_name'] }}</strong>
<input name="confirmation" required autocomplete="off"></label>
{% endif %}
<p><label>Reason <input name="reason" required></label></p>
<button type="submit">{{ 'Deactivate Account' if is_deactivate else 'Reactivate Account' }}</button>
</form>
<p><a href="{{ url_for('owner_people_access') }}">Cancel</a></p>
</main>
        """,
        target=target, is_deactivate=is_deactivate, csrf_token=owner_csrf_token(),
    )


@app.get("/owner/people-access/<int:user_id>/deactivate")
@owner_required
def owner_confirm_account_deactivation(user_id):
    conn = get_conn()
    try:
        target = owner_account_target(conn, user_id)
    finally:
        conn.close()
    if not target:
        abort(404)
    return render_account_lifecycle_confirmation(target, "deactivate")


@app.post("/owner/people-access/<int:user_id>/deactivate")
@owner_required
def owner_deactivate_account(user_id):
    require_owner_csrf()
    actor = current_user()["id"]
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        target = owner_account_target(conn, user_id)
        if not target:
            conn.rollback()
            abort(404)
        if request.form.get("confirmation", "") != target["display_name"]:
            conn.rollback()
            abort(400, description="The typed confirmation did not match.")
        outcome = "already_deactivated"
        if int(target["is_active"] or 0) == 1:
            target_is_owner = conn.execute(
                """
                SELECT 1 FROM user_platform_roles
                WHERE user_id=? AND platform_role='owner' AND revoked_at IS NULL
                """, (user_id,),
            ).fetchone()
            active_owner_count = conn.execute(
                """
                SELECT COUNT(DISTINCT p.user_id)
                FROM user_platform_roles p JOIN users u ON u.id=p.user_id
                WHERE p.platform_role='owner' AND p.revoked_at IS NULL AND u.is_active=1
                """
            ).fetchone()[0]
            if target_is_owner and active_owner_count <= 1:
                outcome = "blocked_last_owner"
            else:
                conn.execute("UPDATE users SET is_active=0 WHERE id=?", (user_id,))
                outcome = "deactivated"
        conn.execute(
            """
            INSERT INTO account_lifecycle_audit_log
              (actor_user_id,target_user_id,action,outcome,reason,created_at)
            VALUES (?,?,'account_deactivated',?,?,?)
            """,
            (actor, user_id, outcome, request.form.get("reason", "").strip(),
             int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()
    flash({
        "deactivated": "Account deactivated.",
        "already_deactivated": "Account was already deactivated.",
        "blocked_last_owner": "Account was not deactivated because it is the only active Owner.",
    }[outcome])
    return redirect(url_for("owner_people_access"))


@app.get("/owner/people-access/<int:user_id>/reactivate")
@owner_required
def owner_confirm_account_reactivation(user_id):
    conn = get_conn()
    try:
        target = owner_account_target(conn, user_id)
    finally:
        conn.close()
    if not target:
        abort(404)
    return render_account_lifecycle_confirmation(target, "reactivate")


@app.post("/owner/people-access/<int:user_id>/reactivate")
@owner_required
def owner_reactivate_account(user_id):
    require_owner_csrf()
    actor = current_user()["id"]
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        target = owner_account_target(conn, user_id)
        if not target:
            conn.rollback()
            abort(404)
        outcome = "already_active"
        if int(target["is_active"] or 0) == 0:
            conn.execute("UPDATE users SET is_active=1 WHERE id=?", (user_id,))
            outcome = "reactivated"
        conn.execute(
            """
            INSERT INTO account_lifecycle_audit_log
              (actor_user_id,target_user_id,action,outcome,reason,created_at)
            VALUES (?,?,'account_reactivated',?,?,?)
            """,
            (actor, user_id, outcome, request.form.get("reason", "").strip(),
             int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()
    flash("Account reactivated." if outcome == "reactivated" else "Account is already active.")
    return redirect(url_for("owner_people_access"))


@app.get("/owner/question-flags")
@owner_required
def owner_question_flags():
    conn = get_conn()
    selected_filter = normalize_owner_question_flag_filter(request.args.get("filter"))
    flags = get_question_flags_for_owner(conn, selected_filter)
    status_counts = owner_question_flag_filter_counts(conn)
    flag_events = {
        flag["flag_id"]: get_question_flag_events(conn, flag["flag_id"])
        for flag in flags
    }
    html = """
<!doctype html>
<title>RootED Support - Question Flags</title>
<style>
  body{font-family:Arial,Helvetica,sans-serif;margin:0;background:#fbfaf4;color:#1f2937}
  .page{max-width:1240px;margin:0 auto;padding:24px}
  .topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;padding:18px 20px;border:1px solid #dfe8d9;border-radius:12px;background:linear-gradient(135deg,#fffdf7,#eef7ed)}
  h1{margin:0;color:#234b35}.subtitle{margin:7px 0 0;color:#4b6654}
  .btn{display:inline-block;background:#2f6f4e;color:#fff;text-decoration:none;border:none;padding:8px 12px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:700}
  .btn-secondary{background:#eef4ec;color:#2f5138;border:1px solid #c8d9c4}
  .card{margin-top:18px;padding:18px;border:1px solid #e2decf;border-radius:12px;background:#fffefa}
  .tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}.tab{display:inline-flex;gap:7px;align-items:center;padding:8px 11px;border-radius:999px;background:#eef4ec;color:#2f5138;text-decoration:none;border:1px solid #c8d9c4;font-weight:700;font-size:13px}.tab[aria-current="page"]{background:#2f6f4e;color:#fff}.count{font-size:11px;font-weight:850}
  table{border-collapse:collapse;width:100%}th,td{border:1px solid #e5e0d2;padding:9px;vertical-align:top;font-size:13px}th{background:#f5f1e8;text-align:left}
  .pill{display:inline-block;padding:3px 8px;border-radius:999px;font-size:12px;font-weight:800}.pill-escalated{background:#fee2e2;color:#991b1b}.pill-owner_reviewing{background:#dbeafe;color:#1d4ed8}.pill-fixed,.pill-closed{background:#dcfce7;color:#166534}
  .muted{color:#6b7280;font-size:12px}.note{white-space:pre-wrap}.empty{padding:14px;border:1px dashed #c8d9c4;border-radius:8px;color:#53665a;background:#fbf8ef}
  textarea{width:100%;box-sizing:border-box;min-height:58px;padding:7px;border:1px solid #cbd5e1;border-radius:7px;font:inherit}.action-form{display:grid;gap:7px;margin-top:8px}.history{margin:8px 0 0;padding-left:18px}.history li{margin:4px 0}
  {{ question_reference_css }}
</style>
<main class="page">
  <header class="topbar">
    <div>
      <h1>Escalated Question Flags</h1>
      <p class="subtitle">RootED Support review queue.</p>
    </div>
    <a class="btn btn-secondary" href="{{ url_for('owner_home') }}">Owner Workspace</a>
  </header>
  <section class="card">
    <nav class="tabs" aria-label="Owner question flag filters">
      {% for key, config in filters.items() %}
        <a class="tab" href="{{ url_for('owner_question_flags', filter=key) }}" {% if key == selected_filter %}aria-current="page"{% endif %}>
          <span>{{ config.label }}</span><span class="count">{{ status_counts[key] }}</span>
        </a>
      {% endfor %}
    </nav>

    {% if flags %}
      <table>
        <tr>
          <th>Status</th><th>Question</th><th>Report</th><th>Context</th><th>Timeline</th><th>Action</th>
        </tr>
        {% for flag in flags %}
          <tr>
            <td><span class="pill pill-{{ flag['status'] }}">{{ flag['status'].replace('_', ' ') }}</span></td>
            <td>
              {{ question_reference_for_flag(
                   flag,
                   url_for('owner_question_preview', question_id=flag['question_id'], return_filter=selected_filter)
                 ) }}
            </td>
            <td>
              <strong>{{ category_label(flag['category']) }}</strong>
              <div class="note">{{ flag['comment'] or 'No reporter comment.' }}</div>
              <div class="muted">Reporter: {{ flag['reporter_role'] }}{% if flag['reporter_username'] %} ({{ flag['reporter_username'] }}){% endif %}</div>
            </td>
            <td>
              {% if flag['class_name'] %}<div>{{ flag['class_name'] }}{% if flag['class_period'] %} · {{ flag['class_period'] }}{% endif %}</div>{% endif %}
              {% if flag['student_id'] %}<div class="muted">Student: {{ flag['student_id'] }}</div>{% endif %}
              <div class="muted">{{ flag['page_context'] or 'No page context' }}</div>
            </td>
            <td>
              <div>Submitted: {{ format_ts(flag['created_ts']) }}</div>
              <div>Escalated: {{ format_ts(flag['escalated_at']) }}</div>
              {% if flag['escalation_note'] %}<div class="note"><strong>Escalation note:</strong> {{ flag['escalation_note'] }}</div>{% endif %}
              {% if flag['owner_reviewing_at'] %}<div>Review started: {{ format_ts(flag['owner_reviewing_at']) }}</div>{% endif %}
              {% if flag['resolution_note'] %}<div class="note"><strong>Owner resolution:</strong> {{ flag['resolution_note'] }}</div>{% endif %}
              {% if flag_events[flag['flag_id']] %}
                <ul class="history" aria-label="Report history">
                  {% for event in flag_events[flag['flag_id']] %}
                    <li>{{ event['from_status'] or 'created' }} → {{ event['to_status'] }} · {{ format_ts(event['created_ts']) }} · {{ event['actor_username'] or ('user ' ~ event['actor_user_id']) }}{% if event['note'] %}<br><span class="muted">{{ event['note'] }}</span>{% endif %}</li>
                  {% endfor %}
                </ul>
              {% endif %}
            </td>
            <td>
              {% if flag['status'] == 'escalated' %}
                <form class="action-form" method="post" action="{{ url_for('owner_start_flag_review', flag_id=flag['flag_id']) }}">
                  <input type="hidden" name="filter" value="{{ selected_filter }}">
                  <textarea name="note" maxlength="1000" placeholder="Optional review note"></textarea>
                  <button class="btn" type="submit">Start Review</button>
                </form>
              {% elif flag['status'] == 'owner_reviewing' %}
                <form class="action-form" method="post" action="{{ url_for('owner_fix_question_flag', flag_id=flag['flag_id']) }}">
                  <input type="hidden" name="filter" value="{{ selected_filter }}">
                  <textarea name="resolution_note" maxlength="1000" required placeholder="Required resolution note"></textarea>
                  <button class="btn" type="submit">Mark Fixed</button>
                </form>
                <form class="action-form" method="post" action="{{ url_for('owner_close_question_flag', flag_id=flag['flag_id']) }}">
                  <input type="hidden" name="filter" value="{{ selected_filter }}">
                  <textarea name="resolution_note" maxlength="1000" required placeholder="Required closure note"></textarea>
                  <button class="btn btn-secondary" type="submit">Close Report</button>
                </form>
              {% else %}
                <span class="muted">Completed {{ format_ts(flag['resolved_ts']) }}</span>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <div class="empty">{{ filters[selected_filter].empty }}</div>
    {% endif %}
  </section>
</main>
    """
    return render_template_string(
        html,
        flags=flags,
        flag_events=flag_events,
        filters=OWNER_QUESTION_FLAG_FILTERS,
        selected_filter=selected_filter,
        status_counts=status_counts,
        category_label=question_flag_category_label,
        question_reference_for_flag=question_reference_for_flag,
        question_reference_css=Markup(QUESTION_REFERENCE_CSS),
        format_ts=format_ts,
    )


@app.get("/owner/question-preview")
@owner_required
def owner_question_preview():
    conn = get_conn()
    question_id = (request.args.get("question_id") or "").strip()
    return_filter = normalize_owner_question_flag_filter(
        request.args.get("return_filter")
    )
    current_question = get_preview_question(conn, question_id)
    if not current_question:
        abort(404)
    resolved_asset = resolve_model_asset_for_question(conn, question_id)
    current_model_asset = static_image_asset_for_render(resolved_asset)
    html = """
<!doctype html>
<title>Owner Question Preview</title>
<style>
  body{font-family:Arial,Helvetica,sans-serif;margin:24px;background:#f3f4f6;color:#1f2937}
  .card{max-width:780px;margin:0 auto;background:#fff;border-radius:12px;padding:20px;border:1px solid #e5e7eb}
  .header{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.header h1{margin:0;color:#234b35}.muted{color:#5f6b64;font-size:13px}
  .btn{display:inline-block;background:#2f6f4e;color:#fff;text-decoration:none;padding:8px 12px;border-radius:8px;font-size:13px;font-weight:700}
  .banner{margin:16px 0;padding:12px;border-radius:8px;background:#eef7ed;border:1px solid #c8d9c4;color:#2f5138}.choice{margin:8px 0;padding:8px;border:1px solid #e5e7eb;border-radius:7px;background:#fafafa}
  .model-asset{margin:14px 0;padding:12px;border:1px solid #d7dee8;border-radius:8px;background:#f8fafc}.model-asset img{display:block;max-width:100%;height:auto;margin:0 auto}.model-asset-title{font-weight:700}.model-asset-caption{font-size:13px;color:#555}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
</style>
<main class="card">
  <header class="header">
    <div><h1>Question Preview</h1><p class="muted">Read-only owner review.</p></div>
    <a class="btn" href="{{ url_for('owner_question_flags', filter=return_filter) }}">Back to Question Flags</a>
  </header>
  <div class="banner">Responses are disabled. This preview cannot record attempts, change placement, or alter adaptive progress.</div>
  <h2>{{ current_question['stem'] }}</h2>
  <p class="muted">{{ question_reference_meta_text(current_question['objective_id'], current_question['objective_text'], current_question['question_id']) }} · {{ current_question['standard_id'] }}</p>
  {% if current_model_asset %}
    <figure class="model-asset">
      {% if current_model_asset.title %}<figcaption class="model-asset-title">{{ current_model_asset.title }}</figcaption>{% endif %}
      <img src="{{ url_for('static', filename=current_model_asset.filename) }}" alt="{{ current_model_asset.alt_text }}">
      {% if current_model_asset.caption %}<p class="model-asset-caption">{{ current_model_asset.caption }}</p>{% endif %}
      {% if current_model_asset.alt_text %}<span class="sr-only">Image description: {{ current_model_asset.alt_text }}</span>{% endif %}
    </figure>
  {% endif %}
  <div aria-label="Answer choices">
    <div class="choice">A. {{ current_question['choice_a'] }}</div>
    <div class="choice">B. {{ current_question['choice_b'] }}</div>
    <div class="choice">C. {{ current_question['choice_c'] }}</div>
    <div class="choice">D. {{ current_question['choice_d'] }}</div>
  </div>
</main>
    """
    return render_template_string(
        html,
        current_question=current_question,
        current_model_asset=current_model_asset,
        return_filter=return_filter,
        question_reference_meta_text=question_reference_meta_text,
    )


def transition_owner_question_flag(
    flag_id: str,
    *,
    expected_status: str,
    target_status: str,
    note: str | None,
    require_note: bool,
) -> None:
    clean_note = (note or "").strip()
    if require_note and not clean_note:
        abort(400, description="An owner resolution note is required.")
    if len(clean_note) > 1000:
        abort(400, description="Owner notes must be 1000 characters or fewer.")

    user = current_user()
    now = int(time.time())
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        flag = get_question_flag_by_id(conn, flag_id)
        if not flag:
            conn.rollback()
            abort(404)
        if flag["status"] != expected_status:
            conn.rollback()
            abort(409, description="That report is no longer in the required status.")

        if target_status == "owner_reviewing":
            cursor = conn.execute(
                """
                UPDATE question_flags
                SET status = 'owner_reviewing',
                    owner_reviewing_at = ?,
                    owner_reviewing_by_user_id = ?
                WHERE flag_id = ? AND status = 'escalated'
                """,
                (now, user["id"], flag_id),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE question_flags
                SET status = ?,
                    resolved_ts = ?,
                    resolution_note = ?,
                    resolved_by_user_id = ?
                WHERE flag_id = ? AND status = 'owner_reviewing'
                """,
                (target_status, now, clean_note, user["id"], flag_id),
            )
        if cursor.rowcount != 1:
            conn.rollback()
            abort(409, description="The report changed before this action completed.")
        conn.execute(
            """
            INSERT INTO question_flag_events
              (flag_id, from_status, to_status, actor_user_id, actor_authority, note, created_ts)
            VALUES (?, ?, ?, ?, 'owner', ?, ?)
            """,
            (
                flag_id,
                expected_status,
                target_status,
                user["id"],
                clean_note or None,
                now,
            ),
        )
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/owner/question-flags/<flag_id>/review")
@owner_required
def owner_start_flag_review(flag_id):
    transition_owner_question_flag(
        flag_id,
        expected_status="escalated",
        target_status="owner_reviewing",
        note=request.form.get("note"),
        require_note=False,
    )
    flash("Report moved to In Review.")
    return redirect(url_for("owner_question_flags", filter="in-review"))


@app.post("/owner/question-flags/<flag_id>/fixed")
@owner_required
def owner_fix_question_flag(flag_id):
    transition_owner_question_flag(
        flag_id,
        expected_status="owner_reviewing",
        target_status="fixed",
        note=request.form.get("resolution_note"),
        require_note=True,
    )
    flash("Report marked fixed.")
    return redirect(url_for("owner_question_flags", filter="completed"))


@app.post("/owner/question-flags/<flag_id>/closed")
@owner_required
def owner_close_question_flag(flag_id):
    transition_owner_question_flag(
        flag_id,
        expected_status="owner_reviewing",
        target_status="closed",
        note=request.form.get("resolution_note"),
        require_note=True,
    )
    flash("Report closed.")
    return redirect(url_for("owner_question_flags", filter="completed"))


def get_any_question_id(conn, objective_id):
    if not is_launch_objective_id(objective_id):
        return None
    row = conn.execute(
        """
        SELECT question_id
        FROM questions
        WHERE TRIM(UPPER(objective_id)) = TRIM(UPPER(?))
        ORDER BY question_id
        LIMIT 1
        """,
        (objective_id,),
    ).fetchone()
    if row:
        return row["question_id"]
    return None

def get_level_for_objective(conn, objective_id):
    try:
        row = conn.execute(
            "SELECT level FROM objective_levels WHERE objective_id=?",
            (objective_id,),
        ).fetchone()
        if row:
            lvl = int(row["level"])
            if lvl in (1, 2, 3):
                return lvl
    except sqlite3.OperationalError:
        pass
    return 1

def record_attempt_and_response(
    conn,
    *,
    student_id: str,
    question_id: str,
    response: str,
    is_correct: int,
    objective_id: str | None = None,
    level_override: int | None = None,
    timestamp: int | None = None,
    attempt_prefix: str = "A",
):
    """
    Insert one row into attempts and one row into responses.

    Returns: (standard_id, level, ts)
    """
    ts = timestamp or int(time.time())
    attempt_id = f"{attempt_prefix}{uuid.uuid4().hex}"

    # Insert into attempts
    conn.execute(
        """
        INSERT OR REPLACE INTO attempts
          (attempt_id, student_id, question_id, timestamp, response, is_correct, time_seconds, skills_missed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (attempt_id, student_id, question_id, ts, response, is_correct, 0, "[]"),
    )

    # Work out standard + level
    if objective_id:
        # Use the provided objective_id (works even for quick attempts with fake QIDs)
        std_row = conn.execute(
            "SELECT standard_id FROM objectives WHERE objective_id=?",
            (objective_id,),
        ).fetchone()
        std_id = std_row["standard_id"] if std_row else "UNKNOWN"
        obj_id_for_level = objective_id
    else:
        # Normal case: derive from the question → objective → standard
        std_row = conn.execute(
            """
            SELECT s.standard_id, o.objective_id
            FROM objectives o
            JOIN standards s ON s.standard_id = o.standard_id
            JOIN questions q ON q.objective_id = o.objective_id
            WHERE q.question_id = ?
            """,
            (question_id,),
        ).fetchone()
        std_id = std_row["standard_id"] if std_row else "UNKNOWN"
        obj_id_for_level = std_row["objective_id"] if std_row else None

    if level_override is not None:
        lvl = level_override
    elif obj_id_for_level:
        lvl = get_level_for_objective(conn, obj_id_for_level)
    else:
        lvl = 1

    if std_id != "UNKNOWN" and obj_id_for_level:
        level_attempt = ensure_routing_level_attempt(
            conn,
            student_id=student_id,
            standard_id=std_id,
            objective_id=obj_id_for_level,
            level=lvl,
            started_at=ts,
        )
        routing_level_attempt_id = level_attempt["attempt_id"]
    else:
        routing_level_attempt_id = None

    # Insert into responses
    conn.execute(
        """
        INSERT INTO responses
          (student_id, standard_id, level, question_id, correct, ts,
           routing_level_attempt_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            student_id,
            std_id,
            lvl,
            question_id,
            is_correct,
            ts,
            routing_level_attempt_id,
        ),
    )

    return std_id, lvl, ts

# ---------- Quick attempt (teacher) ----------
@app.post("/quick_attempt")
@require_teacher
def quick_attempt():
    conn = get_conn()
    student_id = request.form.get("student_id")
    objective_id = request.form.get("objective_id")
    result = request.form.get("result")  # "correct" or "incorrect"

    class_objective = request.form.get("class_objective", objective_id or "")
    period = request.form.get("period", "ALL")
    active_student = request.form.get("active_student", student_id or "S1")

    if not (student_id and objective_id and result in ("correct", "incorrect")):
        flash("Missing student/objective/result for quick score.")
        return redirect(
            url_for(
                "index",
                class_objective=class_objective,
                period=period,
                student_id=active_student,
            )
        )

    qid = get_any_question_id(conn, objective_id)
    is_correct = 1 if result == "correct" else 0

    std_id, lvl, _ = record_attempt_and_response(
        conn,
        student_id=student_id,
        question_id=qid,
        response=f"Quick:{result}",
        is_correct=is_correct,
        objective_id=objective_id,  # ensures we get the right standard even with quick QIDs
        attempt_prefix="QA",
    )
    conn.commit()

    flash(
        f"Quick score saved for {student_id} on {objective_id} "
        f"({'correct' if is_correct else 'incorrect'})."
    )
    return redirect(
        url_for(
            "index",
            class_objective=class_objective,
            period=period,
            student_id=active_student,
        )
    )


# ---------- CSV import ----------
@app.post("/import_csv")
@require_teacher
def import_csv():
    file = request.files.get("file")
    dataset = request.form.get("dataset")

    if not file or file.filename == "":
        flash("Please choose a CSV file to upload.")
        return redirect(url_for("index"))

    if dataset not in ("standards", "objectives", "questions"):
        flash("Please choose what type of data you are importing.")
        return redirect(url_for("index"))

    conn = get_conn()

    try:
        text_data = file.stream.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text_data))
        rows = list(reader)

        if not rows:
            flash("CSV appears to be empty.")
            return redirect(url_for("index"))

        if dataset == "standards":
            for r in rows:
                sid = (r.get("standard_id") or "").strip()
                core = (r.get("core_idea") or "").strip()
                band = (r.get("grade_band") or "").strip()
                if not sid:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO standards (standard_id, core_idea, grade_band)
                    VALUES (?, ?, ?)
                    """,
                    (sid, core, band),
                )

        elif dataset == "objectives":
            for r in rows:
                oid = (r.get("objective_id") or "").strip()
                sid = (r.get("standard_id") or "").strip()
                text = (r.get("objective_text") or "").strip()
                objective_display_name = (r.get("display_name") or "").strip()
                student_description = (r.get("student_description") or "").strip() or None
                order_val = r.get("order_in_band") or ""
                try:
                    order_val = int(order_val) if order_val != "" else None
                except ValueError:
                    order_val = None
                if not oid or not sid or not objective_display_name:
                    raise ValueError(
                        "Every imported objective requires objective_id, "
                        "standard_id, and display_name."
                    )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO objectives
                      (objective_id, standard_id, objective_text, display_name,
                       student_description, order_in_band)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (oid, sid, text, objective_display_name,
                     student_description, order_val),
                )

        elif dataset == "questions":
            for r in rows:
                qid = (r.get("question_id") or "").strip()
                oid = (r.get("objective_id") or "").strip()
                stem = (r.get("stem") or "").strip()
                a = (r.get("choice_a") or "").strip()
                b = (r.get("choice_b") or "").strip()
                c = (r.get("choice_c") or "").strip()
                d = (r.get("choice_d") or "").strip()
                ans = (r.get("answer_key") or "").strip().upper()
                if not qid or not oid:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO questions
                      (question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (qid, oid, stem, a, b, c, d, ans),
                )

        conn.commit()
        flash(f"Imported {len(rows)} row(s) into {dataset}.")
    except Exception as e:
        conn.rollback()
        flash(f"Error importing CSV: {e}")

    return redirect(url_for("index"))


# ---------- Config update ----------
@app.post("/update_config")
@require_teacher
def update_config():
    conn = get_conn()
    mastery = request.form.get("mastery_threshold", "0.9")
    practice = request.form.get("practice_lower", "0.7")
    set_config(conn, mastery, practice)
    flash(f"Config updated — Mastery: {mastery}, Practice: {practice}")
    return redirect(url_for("index"))


# ---------- Auth routes ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    conn = get_conn()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        user = verify_user(conn, username, password)
        if user:
            try:
                active_val = int(user["is_active"])
            except Exception:
                active_val = 1
            if active_val != 1:
                flash("This account is disabled. Please contact your teacher.")
                return redirect(url_for("login"))

            # Update last_login_ts for local logins
            conn.execute(
                "UPDATE users SET last_login_ts = ? WHERE id = ?",
                (int(time.time()), user["id"]),
            )
            conn.commit()

            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = effective_session_role(user)
            flash(f"Welcome, {user['username']}!")
            session["current_mode"] = "home"
            session["locked_payload"] = None
            return redirect(
                safe_local_redirect(request.form.get("next"))
                or get_post_login_destination(user)
            )
        else:
            flash("Invalid username or password.")

    login_html = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>RootED | Where learning takes root</title>
      <style>
        :root{
          --ink:#1f2933;
          --muted:#5f6f64;
          --leaf:#2f6f4e;
          --leaf-dark:#24543d;
          --moss:#dce9d5;
          --sprout:#f3f8ee;
          --gold:#c98f35;
          --sky:#e6f0f6;
          --line:#d9e2d4;
          --shadow:0 24px 70px rgba(31,41,51,.14);
        }
        *{box-sizing:border-box;}
        body{
          margin:0;
          font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, Helvetica, sans-serif;
          color:var(--ink);
          background:
            radial-gradient(circle at 15% 15%, rgba(220,233,213,.88), transparent 30%),
            linear-gradient(135deg, #fffdf8 0%, #f3f8ee 48%, #e6f0f6 100%);
          min-height:100vh;
        }
        body:before{
          content:"";
          position:fixed;
          inset:0;
          pointer-events:none;
          background:
            linear-gradient(90deg, rgba(47,111,78,.06) 1px, transparent 1px),
            linear-gradient(180deg, rgba(47,111,78,.05) 1px, transparent 1px);
          background-size:56px 56px;
          mask-image:linear-gradient(to bottom, rgba(0,0,0,.45), transparent 70%);
        }
        a{color:var(--leaf-dark);}
        .page{
          position:relative;
          width:min(1180px, calc(100% - 40px));
          margin:0 auto;
          padding:36px 0 44px;
        }
        .hero{
          display:grid;
          grid-template-columns:minmax(0, 1.16fr) minmax(340px, .84fr);
          gap:32px;
          align-items:stretch;
          min-height:calc(100vh - 80px);
        }
        .brand-panel{
          display:flex;
          flex-direction:column;
          justify-content:space-between;
          gap:28px;
          padding:26px 0;
        }
        .brand-mark{
          display:inline-flex;
          align-items:center;
          gap:12px;
          color:var(--leaf-dark);
          font-weight:800;
          letter-spacing:0;
        }
        .brand-logo{
          width:72px;
          height:72px;
          object-fit:contain;
          border-radius:8px;
          background:#fffdf8;
          box-shadow:0 12px 28px rgba(47,111,78,.16);
        }
        h1{
          margin:44px 0 8px;
          font-size:clamp(54px, 7vw, 92px);
          line-height:.95;
          letter-spacing:0;
          color:#183629;
        }
        .tagline{
          margin:0;
          font-size:clamp(24px, 3vw, 38px);
          color:var(--leaf);
          font-weight:700;
          letter-spacing:0;
        }
        .hero-copy{
          max-width:710px;
          margin:24px 0 0;
          font-size:20px;
          line-height:1.6;
          color:#304037;
        }
        .hero-actions{
          display:flex;
          gap:14px;
          flex-wrap:wrap;
          margin-top:30px;
        }
        .cta-link{
          display:inline-flex;
          align-items:center;
          justify-content:center;
          min-height:46px;
          padding:12px 18px;
          border-radius:8px;
          text-decoration:none;
          font-weight:750;
          border:1px solid transparent;
        }
        .cta-primary{
          background:var(--leaf);
          color:#fff;
          box-shadow:0 14px 28px rgba(47,111,78,.2);
        }
        .cta-secondary{
          background:rgba(255,255,255,.58);
          color:var(--leaf-dark);
          border-color:rgba(47,111,78,.24);
        }
        .story-grid{
          display:grid;
          grid-template-columns:repeat(3, minmax(0, 1fr));
          gap:14px;
          margin-top:34px;
        }
        .story-card{
          background:rgba(255,255,255,.7);
          border:1px solid rgba(47,111,78,.16);
          border-radius:8px;
          padding:18px;
          min-height:142px;
          box-shadow:0 12px 30px rgba(31,41,51,.07);
        }
        .story-card h2{
          margin:0 0 10px;
          font-size:16px;
          letter-spacing:0;
          color:#214332;
        }
        .story-card p{
          margin:0;
          color:var(--muted);
          line-height:1.5;
          font-size:14px;
        }
        .pathway{
          margin-top:16px;
          padding:18px 20px;
          background:rgba(230,240,246,.74);
          border:1px solid rgba(47,111,78,.14);
          border-radius:8px;
          color:#40524a;
          line-height:1.55;
          font-size:14px;
        }
        .login-panel{
          align-self:center;
          background:rgba(255,255,255,.9);
          border:1px solid rgba(47,111,78,.18);
          border-radius:8px;
          box-shadow:var(--shadow);
          overflow:hidden;
        }
        .login-header{
          padding:28px 28px 22px;
          background:linear-gradient(135deg, rgba(47,111,78,.1), rgba(201,143,53,.1));
          border-bottom:1px solid var(--line);
        }
        .login-header h2{
          margin:0;
          font-size:28px;
          letter-spacing:0;
          color:#183629;
        }
        .login-header p{
          margin:10px 0 0;
          color:#4d5e55;
          line-height:1.5;
        }
        .access-notes{
          display:grid;
          grid-template-columns:1fr 1fr;
          gap:10px;
          padding:18px 28px 0;
        }
        .access-note{
          border:1px solid var(--line);
          border-radius:8px;
          padding:12px;
          background:#fffefa;
        }
        .access-note strong{
          display:block;
          margin-bottom:4px;
          color:var(--leaf-dark);
          font-size:14px;
        }
        .access-note span{
          display:block;
          color:var(--muted);
          font-size:13px;
          line-height:1.4;
        }
        .login-body{padding:22px 28px 28px;}
        .flash-box{
          background:#e7f7ee;
          border:1px solid #a8e0bf;
          color:#0f6b3a;
          padding:10px 12px;
          border-radius:8px;
          margin:0 0 16px;
          font-size:14px;
        }
        label{
          display:block;
          margin-bottom:14px;
          color:#34443b;
          font-size:14px;
          font-weight:700;
        }
        input{
          width:100%;
          min-height:44px;
          margin-top:7px;
          padding:10px 12px;
          border:1px solid #cbd8cd;
          border-radius:8px;
          background:#fff;
          color:var(--ink);
          font:inherit;
        }
        input:focus{
          outline:3px solid rgba(47,111,78,.18);
          border-color:var(--leaf);
        }
        .btn,
        .btn-sso-google,
        .btn-sso-ms{
          display:flex;
          align-items:center;
          justify-content:center;
          gap:10px;
          width:100%;
          border:none;
          min-height:46px;
          padding:11px 14px;
          border-radius:8px;
          cursor:pointer;
          font-size:15px;
          font-weight:750;
        }
        .sso-icon{
          flex:0 0 20px;
          width:20px;
          height:20px;
        }
        .btn{
          background:var(--leaf);
          color:#fff;
          box-shadow:0 14px 24px rgba(47,111,78,.18);
        }
        .btn:hover,
        .cta-primary:hover{background:var(--leaf-dark);}
        .btn-sso-google{
          background:#fff;
          color:#2f3b34;
          border:1px solid #d7ded8;
        }
        .btn-sso-ms{
          background:#0078d4;
          color:#fff;
        }
        .account-note{
          font-size:13px;
          color:var(--muted);
          margin:10px 0 0;
          line-height:1.45;
        }
        .divider{
          display:flex;
          align-items:center;
          gap:10px;
          margin:20px 0;
          text-align:center;
          font-size:12px;
          color:#6b7280;
          font-weight:700;
        }
        .divider:before,
        .divider:after{
          content:"";
          flex:1;
          border-top:1px solid #e1e7e2;
        }
        .sso-copy{
          font-size:13px;
          color:#4b5563;
          margin:0 0 10px;
          line-height:1.45;
        }
        .sso-form{margin:0 0 10px;}
        .sso-form:last-child{margin-bottom:0;}
        @media (max-width:900px){
          .page{width:min(100% - 28px, 680px);padding:20px 0 34px;}
          .hero{grid-template-columns:1fr;min-height:0;}
          .brand-panel{padding:8px 0 0;}
          h1{margin-top:32px;}
          .hero-copy{font-size:18px;}
          .story-grid{grid-template-columns:1fr;}
          .login-panel{align-self:stretch;}
        }
        @media (max-width:520px){
          .page{width:min(100% - 20px, 680px);}
          .access-notes{grid-template-columns:1fr;padding:16px 18px 0;}
          .login-header,
          .login-body{padding-left:18px;padding-right:18px;}
          .hero-actions{flex-direction:column;}
          .cta-link{width:100%;}
        }
      </style>
    </head>
    <body>
      <main class="page">
        <section class="hero" aria-label="RootED public landing page">
          <div class="brand-panel">
            <div>
              <div class="brand-mark" aria-label="RootED">
                <img class="brand-logo" src="{{ url_for('static', filename='Logo.png') }}" alt="RootED logo">
                <span>RootED</span>
              </div>
              <h1>RootED</h1>
              <p class="tagline">Where learning takes root.</p>
              <p class="hero-copy">
                RootED helps learners grow through curiosity, discovery, and meaningful challenge.
              </p>
              <div class="hero-actions">
                <a class="cta-link cta-primary" href="#access">Log in to RootED</a>
                <a class="cta-link cta-secondary" href="#mission">Explore the mission</a>
              </div>
            </div>

            <div>
              <div class="story-grid" id="mission">
                <section class="story-card">
                  <h2>Mission</h2>
                  <p>
                    RootED helps learners grow through curiosity, discovery, and meaningful challenge,
                    building deep foundations of understanding that empower them to thrive.
                  </p>
                </section>
                <section class="story-card">
                  <h2>Learning Philosophy</h2>
                  <p>
                    Curiosity is sacred, creativity is calling, and learning should feel like discovery.
                    Challenge becomes a supportive path toward growth.
                  </p>
                </section>
                <section class="story-card">
                  <h2>Adaptive Support</h2>
                  <p>
                    Learners progress at their own level while RootED responds to demonstrated
                    understanding and helps mastery build from strong foundations.
                  </p>
                </section>
              </div>
              <div class="pathway">
                The current RootED learning pathway supports science learning through structured
                progressions, with standards alignment working quietly in the background.
              </div>
            </div>
          </div>

          <aside class="login-panel" id="access" aria-label="RootED access">
            <div class="login-header">
              <h2>Access RootED</h2>
              <p>Students continue learning and discovery. Teachers guide learner growth and monitor progress.</p>
            </div>
            <div class="access-notes">
              <div class="access-note">
                <strong>Students</strong>
                <span>Continue your learning journey at the level that fits your current understanding.</span>
              </div>
              <div class="access-note">
                <strong>Teachers</strong>
                <span>Guide growth, review progress, and support meaningful next steps.</span>
              </div>
            </div>
            <div class="login-body">
              {% with msgs = get_flashed_messages() %}
                {% if msgs %}
                  <div class="flash-box">
                    {% for m in msgs %}
                      <div>{{ m }}</div>
                    {% endfor %}
                  </div>
                {% endif %}
              {% endwith %}

              <form method="post">
                <label>Username
                  <input name="username" required autocomplete="username">
                </label>
                <label>Password
                  <input type="password" name="password" required autocomplete="current-password">
                </label>
                <button type="submit" class="btn">Log in</button>
              </form>
              <p class="account-note">
                Do not have an account? Contact your teacher to be added.
              </p>

              {% if enable_google_auth or enable_microsoft_auth %}
                <div class="divider"><span>OR</span></div>

                <p class="sso-copy">Sign in with your school account:</p>
                {% if enable_google_auth %}
                  <form method="get" action="{{ url_for('sso_login', provider='google') }}" class="sso-form">
                    <button type="submit" class="btn-sso-google">
                      <svg class="sso-icon sso-icon-google" aria-hidden="true" focusable="false"
                           viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg">
                        <path fill="#4285F4" d="M17.64 9.205c0-.639-.057-1.252-.164-1.841H9v3.482h4.844a4.14 4.14 0 0 1-1.797 2.715v2.258h2.909c1.702-1.567 2.684-3.875 2.684-6.614Z"/>
                        <path fill="#34A853" d="M9 18c2.43 0 4.468-.806 5.956-2.181l-2.909-2.258c-.806.54-1.835.859-3.047.859-2.344 0-4.328-1.585-5.037-3.714H.956v2.332A9 9 0 0 0 9 18Z"/>
                        <path fill="#FBBC05" d="M3.963 10.706A5.42 5.42 0 0 1 3.681 9c0-.592.102-1.167.282-1.706V4.962H.956A9 9 0 0 0 0 9c0 1.452.347 2.827.956 4.038l3.007-2.332Z"/>
                        <path fill="#EA4335" d="M9 3.58c1.322 0 2.508.454 3.441 1.346l2.581-2.581C13.464.892 11.426 0 9 0A9 9 0 0 0 .956 4.962l3.007 2.332C4.672 5.165 6.656 3.58 9 3.58Z"/>
                      </svg>
                      <span>Continue with Google</span>
                    </button>
                  </form>
                {% endif %}
                {% if enable_microsoft_auth %}
                  <form method="get" action="{{ url_for('sso_login', provider='microsoft') }}" class="sso-form">
                    <button type="submit" class="btn-sso-ms">
                      <svg class="sso-icon sso-icon-microsoft" aria-hidden="true" focusable="false"
                           viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">
                        <rect x="1" y="1" width="8" height="8" fill="#F25022"/>
                        <rect x="11" y="1" width="8" height="8" fill="#7FBA00"/>
                        <rect x="1" y="11" width="8" height="8" fill="#00A4EF"/>
                        <rect x="11" y="11" width="8" height="8" fill="#FFB900"/>
                      </svg>
                      <span>Continue with Microsoft</span>
                    </button>
                  </form>
                {% endif %}
              {% endif %}
            </div>
          </aside>
        </section>
      </main>
    </body>
    </html>
    """
    return render_template_string(
        login_html,
        enable_google_auth=ENABLE_GOOGLE_AUTH,
        enable_microsoft_auth=ENABLE_MICROSOFT_AUTH,
    )


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("login"))


# ---------- SSO login flows ----------
@app.route("/login/<provider>")
def sso_login(provider):
    """
    Start an SSO login with the given provider ("google" or "microsoft").
    """
    provider = provider.lower()
    if provider not in ("google", "microsoft"):
        abort(404)
    if not is_sso_provider_enabled(provider):
        flash("This sign-in method is not currently available.")
        return redirect(url_for("not_authorized"))
    intended = safe_local_redirect(request.args.get("next"))
    if intended:
        session["post_login_destination"] = intended

    client = oauth.create_client(provider)
    if not client:
        flash(f"SSO provider '{provider}' is not configured.")
        return redirect(url_for("login"))

    redirect_uri = url_for("sso_callback", provider=provider, _external=True)

    # Log the redirect target for debugging
    logger.debug("SSO redirect for %s: %s", provider, redirect_uri)

    # (optional but nice) force account picker for Google
    extra_kwargs = {}
    if provider == "google":
        extra_kwargs["prompt"] = "select_account"

    return client.authorize_redirect(redirect_uri, **extra_kwargs)

@app.route("/auth/callback/<provider>")
def sso_callback(provider):
    """
    Handle the OAuth callback for SSO.
    """
    provider = provider.lower()
    if provider not in ("google", "microsoft"):
        abort(404)
    if not is_sso_provider_enabled(provider):
        flash("This sign-in method is not currently available.")
        return redirect(url_for("not_authorized"))

    client = oauth.create_client(provider)
    if not client:
        flash(f"SSO provider '{provider}' is not configured.")
        return redirect(url_for("login"))

    try:
        token_kwargs = {}
        microsoft_tenant = os.environ.get("MICROSOFT_TENANT", "common").lower()
        if provider == "microsoft" and microsoft_tenant in {
            "common",
            "organizations",
            "consumers",
        }:
            token_kwargs["claims_options"] = microsoft_multitenant_claims_options(
                client
            )
        token = client.authorize_access_token(**token_kwargs)
    except Exception as e:
        flash(f"SSO login failed: {e}")
        return redirect(url_for("login"))

    # Try to get user info / ID token
    userinfo = None
    sub = None
    email = None
    email_verified = False

    # authorize_access_token already validated the ID token with the original
    # state-bound nonce and stores the validated claims in token["userinfo"].
    id_token = token.get("userinfo")

    if id_token:
        # OIDC-compliant: subject + email come from ID token
        sub = id_token.get("sub")
        email = id_token.get("email") or id_token.get("preferred_username")
        email_verified = bool(id_token.get("email_verified")) or (
            provider == "microsoft" and id_token.get("xms_edov") is True
        )
        userinfo = id_token
    else:
        # Fallback: some providers give a userinfo endpoint or put claims in token
        userinfo = token.get("userinfo") or token
        sub = (userinfo or {}).get("sub") or (userinfo or {}).get("id")
        email = (userinfo or {}).get("email")

    if not sub:
        flash("SSO provider did not return a valid user ID.")
        return redirect(url_for("login"))

    conn = get_conn()
    provider_subject = str(sub)
    if provider == "microsoft":
        tenant_id = (userinfo or {}).get("tid")
        if not tenant_id:
            flash("Microsoft did not return a valid tenant identifier.")
            return redirect(url_for("login"))
        provider_subject = f"{tenant_id}:{sub}"

    user = resolve_sso_user(
        conn,
        provider=provider,
        subject=provider_subject,
        email=email,
        email_verified=email_verified,
        display_name=(userinfo or {}).get("name"),
        avatar_url=(userinfo or {}).get("picture"),
        allow_create=ALLOW_SSO_AUTO_CREATE,
    )

    if not user:
        flash("This account is not authorized for RootED access.")
        return redirect(url_for("not_authorized"))

    # Write login info to session (same as local login)
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = effective_session_role(user)

    flash(f"Welcome, {user['username']} (SSO via {provider})!")

    session["current_mode"] = "home"
    session["locked_payload"] = None
    return redirect(
        safe_local_redirect(session.pop("post_login_destination", None))
        or get_post_login_destination(user)
    )


@app.get("/restricted")
@login_required
def restricted_onboarding():
    user = current_user()
    conn = get_conn()
    try:
        identity = conn.execute(
            """
            SELECT provider, verified_email, display_name
            FROM user_auth_identities
            WHERE user_id=? AND revoked_at IS NULL
            ORDER BY last_login_at DESC LIMIT 1
            """,
            (user["id"],),
        ).fetchone()
    finally:
        conn.close()
    return render_template_string(
        """
        <!doctype html><html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>RootED | Connect to a class</title>
        <style>
        body{font-family:Arial,sans-serif;background:#fbfaf4;color:#183629;margin:0;display:grid;place-items:center;min-height:100vh}
        main{width:min(560px,calc(100% - 32px));background:white;border:1px solid #dfe8d9;border-radius:12px;padding:28px;box-shadow:0 18px 45px #1f29330f}
        input,button{box-sizing:border-box;width:100%;padding:12px;border-radius:8px;font:inherit}input{border:1px solid #cbd8cd}
        button{margin-top:10px;border:0;background:#2f6f4e;color:white;font-weight:700}a{color:#24543d;font-weight:700}
        .account{background:#f1f6ef;padding:12px;border-radius:8px;margin:18px 0;color:#4b5563}
        </style></head><body><main>
        <h1>Welcome to RootED</h1>
        <p>You’re signed in successfully, but you’re not currently connected to a class. Enter the class code provided by your teacher to continue.</p>
        <div class="account"><strong>{{ identity['display_name'] or user['username'] }}</strong><br>
        {{ identity['verified_email'] or '' }}</div>
        <form><label for="code">Class code</label><input id="code" disabled placeholder="Class-code entry coming soon">
        <button type="button" disabled>Connect to class</button></form>
        <p><a href="{{ url_for('logout') }}">Sign out</a></p>
        </main></body></html>
        """,
        user=user,
        identity=identity or {"display_name": None, "verified_email": None},
    )


@app.get("/not-authorized")
def not_authorized():
    html = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>RootED | Not authorized</title>
      <style>
        body{font-family:Arial, Helvetica, sans-serif;margin:0;min-height:100vh;display:grid;place-items:center;background:#fbfaf4;color:#1f2937}
        .card{width:min(520px, calc(100% - 32px));background:#fff;border:1px solid #e5e0d2;border-radius:8px;padding:24px;box-shadow:0 18px 45px rgba(31,41,51,.08)}
        h1{margin:0 0 10px;color:#183629;font-size:28px}
        p{line-height:1.5;color:#4b5563}
        a{display:inline-block;margin-top:8px;color:#24543d;font-weight:700}
      </style>
    </head>
    <body>
      <main class="card">
        <h1>Not authorized</h1>
        <p>This account is not approved for RootED access. Please contact your teacher or RootED administrator if you believe this is a mistake.</p>
        <a href="{{ url_for('login') }}">Return to login</a>
      </main>
    </body>
    </html>
    """
    return render_template_string(html), 403


# ---------- Public landing page ----------
@app.get("/")
@app.get("/landing")
def public_landing():
    landing_html = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>RootED | Science Learning. Built differently.</title>
      <meta name="description" content="RootED is a science learning platform in development, designed to help learners build deep understanding through curiosity, discovery, and meaningful challenge.">
      <link rel="canonical" href="https://rooted.school/">
      <link rel="icon" href="{{ url_for('static', filename='favicon.ico') }}">
      <meta property="og:title" content="RootED | Science Learning. Built differently.">
      <meta property="og:description" content="RootED is a science learning platform in development, designed to help learners build deep understanding through curiosity, discovery, and meaningful challenge.">
      <meta property="og:type" content="website">
      <meta property="og:url" content="https://rooted.school/">
      <meta property="og:image" content="https://rooted.school/static/rooted-og-preview.png">
      <meta name="twitter:card" content="summary_large_image">
      <meta name="twitter:title" content="RootED | Science Learning. Built differently.">
      <meta name="twitter:description" content="RootED is a science learning platform in development, designed to help learners build deep understanding through curiosity, discovery, and meaningful challenge.">
      <meta name="twitter:image" content="https://rooted.school/static/rooted-og-preview.png">
      <style>
        :root {
          --landing-ink: #213326;
          --landing-muted: #52665a;
          --landing-deep: #1f4d37;
          --landing-leaf: #2f7a4f;
          --landing-sprout: #d8ead1;
          --landing-earth: #f6f2ea;
          --landing-paper: #fffdf8;
          --landing-line: #d9e3d6;
          --landing-shadow: 0 16px 42px rgba(33, 51, 38, 0.09);
        }

        * {
          box-sizing: border-box;
        }

        body.rooted-landing {
          margin: 0;
          color: var(--landing-ink);
          background:
            radial-gradient(circle at 15% 5%, rgba(216, 234, 209, 0.75), transparent 32rem),
            linear-gradient(180deg, var(--landing-paper) 0%, var(--landing-earth) 100%);
          font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          line-height: 1.6;
        }

        .rooted-landing a {
          color: inherit;
        }

        .rooted-page {
          min-height: 100vh;
          overflow: hidden;
        }

        .rooted-shell {
          width: min(1120px, calc(100% - 40px));
          margin: 0 auto;
        }

        .rooted-nav {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 24px;
          padding: 24px 0;
        }

        .rooted-brand {
          display: inline-flex;
          align-items: center;
          gap: 12px;
          font-weight: 800;
          letter-spacing: 0;
          color: var(--landing-deep);
          text-decoration: none;
        }

        .rooted-brand-mark {
          display: grid;
          place-items: center;
          width: 42px;
          height: 42px;
          border-radius: 50%;
          background: var(--landing-sprout);
          border: 1px solid var(--landing-line);
          overflow: hidden;
        }

        .rooted-brand-mark img {
          width: 100%;
          height: 100%;
          object-fit: cover;
        }

        .rooted-brand-fallback {
          color: var(--landing-deep);
          font-size: 1.15rem;
        }

        .rooted-nav-links {
          display: flex;
          gap: 18px;
          color: var(--landing-muted);
          font-size: 0.95rem;
        }

        .rooted-nav-links a {
          text-decoration: none;
        }

        .rooted-nav-links a:focus,
        .rooted-nav-links a:hover {
          color: var(--landing-deep);
          text-decoration: underline;
          text-underline-offset: 5px;
        }

        .rooted-hero {
          display: grid;
          grid-template-columns: minmax(0, 1.1fr) minmax(280px, 0.9fr);
          align-items: center;
          gap: 56px;
          min-height: 68vh;
          padding: 44px 0 76px;
        }

        .rooted-kicker {
          margin: 0 0 14px;
          color: var(--landing-leaf);
          font-size: 0.82rem;
          font-weight: 800;
          letter-spacing: 0.12em;
          text-transform: uppercase;
        }

        .rooted-hero h1 {
          margin: 0;
          color: var(--landing-deep);
          font-size: clamp(3.45rem, 7vw, 6.8rem);
          line-height: 0.98;
          letter-spacing: 0;
        }

        .rooted-hero h1 span {
          display: block;
        }

        .rooted-tagline {
          margin: 22px 0 0;
          color: var(--landing-ink);
          font-size: clamp(1.55rem, 3vw, 2.55rem);
          font-weight: 750;
          line-height: 1.15;
        }

        .rooted-hero-copy {
          max-width: 660px;
          margin: 24px 0 0;
          color: var(--landing-muted);
          font-size: 1.13rem;
        }

        .rooted-mission {
          max-width: 660px;
          margin: 18px 0 0;
          padding-left: 18px;
          border-left: 4px solid var(--landing-leaf);
          color: var(--landing-ink);
          font-size: 1.03rem;
          font-weight: 650;
        }

        .rooted-hero-panel {
          position: relative;
          padding: 24px;
          border: 1px solid var(--landing-line);
          border-radius: 8px;
          background: rgba(255, 253, 248, 0.86);
          box-shadow: var(--landing-shadow);
        }

        .rooted-growth-art {
          min-height: 280px;
          border-radius: 8px;
          background: #fff;
          border: 1px solid rgba(31, 77, 55, 0.16);
          display: grid;
          place-items: center;
          padding: 28px;
          overflow: hidden;
        }

        .rooted-hero-logo {
          display: block;
          width: min(100%, 408px);
          max-height: 408px;
          aspect-ratio: 1 / 1;
          object-fit: contain;
          border-radius: 8px;
        }

        .rooted-section {
          padding: 68px 0;
          border-top: 1px solid rgba(31, 77, 55, 0.12);
        }

        .rooted-section-header {
          max-width: 720px;
          margin-bottom: 28px;
        }

        .rooted-section h2 {
          margin: 0 0 12px;
          color: var(--landing-deep);
          font-size: clamp(1.85rem, 4vw, 3rem);
          line-height: 1.1;
          letter-spacing: 0;
        }

        .rooted-section p {
          margin: 0;
          color: var(--landing-muted);
          font-size: 1.06rem;
        }

        .rooted-feature-grid {
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: 16px;
        }

        .rooted-feature {
          min-height: 190px;
          padding: 22px;
          border: 1px solid var(--landing-line);
          border-radius: 8px;
          background: rgba(255, 253, 248, 0.78);
        }

        .rooted-feature-icon {
          display: grid;
          place-items: center;
          width: 42px;
          height: 42px;
          margin-bottom: 18px;
          border-radius: 50%;
          color: var(--landing-deep);
          background: var(--landing-sprout);
          font-weight: 900;
        }

        .rooted-feature h3 {
          margin: 0 0 10px;
          color: var(--landing-ink);
          font-size: 1.08rem;
          line-height: 1.25;
        }

        .rooted-feature p {
          font-size: 0.98rem;
        }

        .rooted-public-note {
          display: grid;
          grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.2fr);
          gap: 28px;
          align-items: start;
          padding: 30px;
          border-radius: 8px;
          background: var(--landing-deep);
          color: #fffdf8;
        }

        .rooted-public-note h2,
        .rooted-public-note p {
          color: inherit;
        }

        .rooted-public-note p {
          opacity: 0.9;
        }

        .rooted-follow-list {
          display: flex;
          flex-wrap: wrap;
          gap: 12px;
          margin-top: 26px;
        }

        .rooted-follow-item {
          display: inline-flex;
          align-items: center;
          gap: 10px;
          min-height: 48px;
          padding: 12px 16px;
          border: 1px solid var(--landing-line);
          border-radius: 8px;
          background: rgba(255, 253, 248, 0.82);
          color: var(--landing-deep);
          font-weight: 750;
          text-decoration: none;
        }

        a.rooted-follow-item:focus,
        a.rooted-follow-item:hover {
          border-color: var(--landing-leaf);
          background: var(--landing-sprout);
          color: var(--landing-deep);
          text-decoration: none;
        }

        .rooted-social-icon {
          display: grid;
          place-items: center;
          width: 28px;
          height: 28px;
          flex: 0 0 28px;
          border-radius: 50%;
          color: #fffdf8;
          line-height: 1;
          margin-left: 0;
        }

        .rooted-social-icon svg {
          display: block;
          width: 28px;
          height: 28px;
        }

        .rooted-follow-item span {
          margin-left: 12px;
          color: var(--landing-muted);
          font-weight: 650;
        }

        .rooted-footer {
          padding: 34px 0 44px;
          color: var(--landing-muted);
        }

        .rooted-footer strong {
          display: block;
          color: var(--landing-deep);
          font-size: 1.18rem;
        }

        .rooted-footer span {
          display: block;
          margin-top: 4px;
        }

        @media (max-width: 880px) {
          .rooted-nav {
            align-items: flex-start;
            flex-direction: column;
          }

          .rooted-hero,
          .rooted-public-note {
            grid-template-columns: 1fr;
          }

          .rooted-hero {
            gap: 34px;
            min-height: 0;
            padding-top: 28px;
          }

          .rooted-feature-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
          }
        }

        @media (max-width: 620px) {
          .rooted-shell {
            width: min(100% - 28px, 1120px);
          }

          .rooted-nav-links {
            width: 100%;
            justify-content: space-between;
            gap: 10px;
            font-size: 0.9rem;
          }

          .rooted-hero {
            padding-bottom: 54px;
          }

          .rooted-hero-panel,
          .rooted-public-note {
            padding: 20px;
          }

          .rooted-growth-art {
            min-height: 230px;
          }

          .rooted-feature-grid {
            grid-template-columns: 1fr;
          }

          .rooted-section {
            padding: 54px 0;
          }

          .rooted-follow-list {
            flex-direction: column;
          }

          .rooted-follow-item {
            width: 100%;
          }
        }
      </style>
    </head>
    <body class="rooted-landing">
      <div class="rooted-page">
        <header class="rooted-shell rooted-nav" aria-label="RootED landing navigation">
          <a class="rooted-brand" href="{{ url_for('public_landing') }}" aria-label="RootED landing page">
            <span class="rooted-brand-mark" aria-hidden="true">
              <img src="{{ url_for('static', filename='Logo.png') }}" alt="" onerror="this.style.display='none'; this.nextElementSibling.style.display='inline';">
              <span class="rooted-brand-fallback" style="display:none;">R</span>
            </span>
            <span>RootED</span>
          </a>
          <nav class="rooted-nav-links" aria-label="Page sections">
            <a href="#what-rooted-is">What it is</a>
            <a href="#building-public">Building</a>
            <a href="#follow-along">Follow</a>
            <a href="{{ url_for('login') }}">Login</a>
          </nav>
        </header>

        <main>
          <section class="rooted-shell rooted-hero" aria-labelledby="rooted-hero-title">
            <div>
              <p class="rooted-kicker">Science learning platform</p>
              <h1 id="rooted-hero-title"><span>Science Learning.</span><span>Built differently.</span></h1>
              <p class="rooted-tagline">Where learning takes root.</p>
              <p class="rooted-hero-copy">
                RootED helps learners build deep understanding through curiosity, discovery, and meaningful challenge.
              </p>
              <p class="rooted-mission">
                Mission: help students build deep understanding while giving teachers meaningful instructional support.
              </p>
            </div>
            <!-- TODO: Future branding pass may add a dedicated hero illustration; currently using official RootED logo. -->
            <div class="rooted-hero-panel">
              <div class="rooted-growth-art">
                <img class="rooted-hero-logo" src="{{ url_for('static', filename='Logo.png') }}" alt="RootED logo">
              </div>
            </div>
          </section>

          <section class="rooted-shell rooted-section" id="what-rooted-is" aria-labelledby="what-rooted-title">
            <div class="rooted-section-header">
              <h2 id="what-rooted-title">What RootED Is</h2>
              <p>
                RootED is being shaped around purposeful classroom learning: responsive practice, meaningful standards alignment, and tools that support teachers without replacing their judgment.
              </p>
            </div>
            <div class="rooted-feature-grid">
              <article class="rooted-feature">
                <div class="rooted-feature-icon" aria-hidden="true">1</div>
                <h3>Adaptive learning</h3>
                <p>Learning experiences respond to student understanding and create room for productive challenge.</p>
              </article>
              <article class="rooted-feature">
                <div class="rooted-feature-icon" aria-hidden="true">2</div>
                <h3>Standards-based growth</h3>
                <p>Progress is grounded in standards-aligned objectives that help make growth visible over time.</p>
              </article>
              <article class="rooted-feature">
                <div class="rooted-feature-icon" aria-hidden="true">3</div>
                <h3>Teacher-centered support</h3>
                <p>RootED is designed to support classroom decision-making and keep teachers at the center of instruction.</p>
              </article>
              <article class="rooted-feature">
                <div class="rooted-feature-icon" aria-hidden="true">4</div>
                <h3>Science first</h3>
                <p>RootED is being built first for science, with a long-term vision for cross-curricular learning.</p>
              </article>
            </div>
          </section>

          <section class="rooted-shell rooted-section" id="teacher-built" aria-labelledby="teacher-built-title">
            <div class="rooted-section-header">
              <h2 id="teacher-built-title">Designed by a middle school science teacher.</h2>
              <p>
                RootED is being designed from real classroom experience to support science learning, strengthen understanding, and give teachers meaningful instructional tools.
              </p>
            </div>
          </section>

          <section class="rooted-shell rooted-section" id="building-public" aria-labelledby="building-public-title">
            <div class="rooted-public-note">
              <h2 id="building-public-title">Building in Public</h2>
              <p>
                We are building RootED in public - sharing design decisions, instructional thinking, classroom milestones, and the journey of building RootED from the ground up.
              </p>
            </div>
          </section>

          <section class="rooted-shell rooted-section" id="follow-along" aria-labelledby="follow-along-title">
            <div class="rooted-section-header">
              <h2 id="follow-along-title">Follow Along</h2>
              <p>RootED is currently in development. Follow the journey as the platform grows.</p>
            </div>
            <div class="rooted-follow-list" aria-label="RootED public social links">
              <a class="rooted-follow-item" href="https://www.instagram.com/rooted.school/" target="_blank" rel="noopener noreferrer">
                <span class="rooted-social-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" role="img" focusable="false">
                    <rect x="3" y="3" width="18" height="18" rx="5.2" fill="none" stroke="currentColor" stroke-width="2"></rect>
                    <circle cx="12" cy="12" r="4.2" fill="none" stroke="currentColor" stroke-width="2"></circle>
                    <circle cx="17.2" cy="6.8" r="1.2" fill="currentColor"></circle>
                  </svg>
                </span>
                Instagram
              </a>
              <a class="rooted-follow-item" href="https://www.facebook.com/people/Rootedschool/61591842830766/" target="_blank" rel="noopener noreferrer">
                <span class="rooted-social-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" role="img" focusable="false">
                    <path fill="currentColor" d="M14 8.5V6.8c0-.8.3-1.2 1.3-1.2H17V3h-2.6C11.8 3 10 4.7 10 7.2v1.3H7.8v3H10V21h3.1v-9.5h3.1l.5-3H13.1z"></path>
                  </svg>
                </span>
                Facebook
              </a>
            </div>
          </section>
        </main>

        <footer class="rooted-shell rooted-footer">
          <strong>RootED</strong>
          <span>Where learning takes root.</span>
        </footer>
      </div>
    </body>
    </html>
    """
    return render_template_string(landing_html)


# ---------- Teacher dashboard ----------
@app.get("/teacher")
@require_teacher
def teacher_home():
    return redirect(url_for("index"))


@app.route("/dashboard", methods=["GET", "POST"])
@require_teacher
def index():
    conn = get_conn()
    msg = ""

    engine_checks = check_engine_tables(conn)
    engine_ok = all(engine_checks.values()) if engine_checks else False

    recent_errors = get_recent_error_count()

    all_users = conn.execute(
        "SELECT id, username, role, is_active, linked_student_id FROM users ORDER BY username"
    ).fetchall()
    class_sections = get_class_sections_for_teacher(conn, session.get("user_id"))
    class_rosters = get_class_roster_map(
        conn,
        [class_row["class_id"] for class_row in class_sections],
    )

    # Save / update student
    if request.method == "POST" and request.form.get("action") == "save_student":
        sid = request.form.get("student_id", "").strip()
        first = request.form.get("first_name", "").strip()
        last = request.form.get("last_name", "").strip()
        grade = request.form.get("grade", "").strip()
        period = request.form.get("class_period", "").strip()
        if sid:
            try:
                gval = int(grade) if grade else None
            except Exception:
                gval = None
            upsert_student(conn, sid, first, last, gval, period)
            msg = f"Saved student {sid}"
        else:
            msg = "Student ID is required."

    # Create user
    if request.method == "POST" and request.form.get("action") == "create_user":
        u_username = request.form.get("username", "").strip()
        u_password = request.form.get("password", "").strip()
        u_role = request.form.get("role", "student").strip()
        linked_student_id = request.form.get("linked_student_id", "").strip() or None

        if not u_username or not u_password:
            flash("Username and password are required to create a user.")
            return redirect(url_for("index"))

        try:
            create_user(conn, u_username, u_password, u_role, linked_student_id)
            flash(f"User '{u_username}' created.")

            if u_role == "student":
                student_id = linked_student_id or u_username
                row = conn.execute(
                    "SELECT 1 FROM students WHERE student_id=?", (student_id,)
                ).fetchone()
                if not row:
                    conn.execute(
                        """
                        INSERT INTO students (student_id, first_name, last_name, grade, class_period)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (student_id, "", "", None, None),
                    )
                    conn.commit()
                    flash(f"Student record created for ID '{student_id}'.")
        except sqlite3.IntegrityError:
            flash(f"Username '{u_username}' already exists.")

        return redirect(url_for("index"))

    # Create class section with a self-enrollment code
    if request.method == "POST" and request.form.get("action") == "create_class":
        class_name = request.form.get("class_name", "").strip()
        class_period = request.form.get("class_period", "").strip() or None
        if not class_name:
            flash("Class name is required.")
            return redirect(url_for("index") + "#class-enrollment")

        create_class_section(conn, session.get("user_id"), class_name, class_period)
        flash(f"Class '{class_name}' created with a join code.")
        return redirect(url_for("index") + "#class-enrollment")

    # Manage class codes
    if request.method == "POST" and request.form.get("action") in {
        "rotate_class_code",
        "toggle_class_active",
    }:
        action = request.form.get("action")
        class_id = request.form.get("class_id", "").strip()
        class_row = conn.execute(
            """
            SELECT class_id, teacher_user_id, name, is_active
            FROM class_sections
            WHERE class_id = ?
            """,
            (class_id,),
        ).fetchone()
        if not class_row:
            flash("Class not found.")
            return redirect(url_for("index") + "#class-enrollment")
        if class_row["teacher_user_id"] not in (None, session.get("user_id")):
            flash("You can only manage your own class codes.")
            return redirect(url_for("index") + "#class-enrollment")

        if action == "rotate_class_code":
            conn.execute(
                """
                UPDATE class_sections
                SET join_code = ?, updated_at = ?
                WHERE class_id = ?
                """,
                (generate_join_code(conn), int(time.time()), class_id),
            )
            conn.commit()
            flash(f"New join code created for {class_row['name']}.")
        else:
            conn.execute(
                """
                UPDATE class_sections
                SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END,
                    updated_at = ?
                WHERE class_id = ?
                """,
                (int(time.time()), class_id),
            )
            conn.commit()
            flash("Class code status updated.")

        return redirect(url_for("index") + "#class-enrollment")

    # Save attempt (teacher-driven)
    if request.method == "POST" and request.form.get("action") == "save_attempt":
        student_id = request.form.get("student_id") or "S1"
        objective_id = request.form.get("objective_id")
        qid = (request.form.get("question_id") or "").strip()
        resp = request.form.get("response")

        keep_obj = request.values.get("class_objective") or request.values.get(
            "objective_id"
        )
        if not is_launch_objective_id(keep_obj):
            ordered_objectives = get_objectives(conn)
            keep_obj = ordered_objectives[0]["objective_id"] if ordered_objectives else None
        keep_period = request.values.get("period", "ALL")
        redirect_args = {
            "class_objective": keep_obj,
            "period": keep_period,
            "student_id": student_id,
            "objective_id": objective_id or keep_obj,
            "question_id": qid,
            "response": resp or "",
        }

        question = get_preview_question(conn, qid)
        if not question:
            flash("Selected question is no longer available. Choose an available launch question before saving an attempt.")
            return redirect(
                url_for(
                    "index",
                    **redirect_args,
                )
            )

        correct = 1 if question["answer_key"] == resp else 0

        std_id, lvl, _ = record_attempt_and_response(
            conn,
            student_id=student_id,
            question_id=qid,
            response=(resp or f"Quick:{correct}"),
            is_correct=correct,
            objective_id=question["objective_id"],
            attempt_prefix="TA",
        )
        conn.commit()

        flash(f"Saved attempt for {qid}. Correct={bool(correct)}")
        return redirect(
            url_for(
                "index",
                class_objective=keep_obj,
                period=keep_period,
                student_id=student_id,
            )
        )

    # ===== Data for panels =====
    objs = get_objectives(conn)

    selected_period = request.values.get("period", "ALL")
    students = get_students(conn, selected_period if selected_period != "ALL" else None)
    active_student = request.values.get(
        "student_id", students[0]["student_id"] if students else "S1"
    )
    class_obj = request.values.get("class_objective")
    if not is_launch_objective_id(class_obj):
        class_obj = objs[0]["objective_id"] if objs else None

    current_obj_for_questions = request.values.get("objective_id", class_obj)
    if not is_launch_objective_id(current_obj_for_questions):
        current_obj_for_questions = class_obj
    qrows = (
        get_questions_for_objective(conn, current_obj_for_questions)
        if current_obj_for_questions
        else []
    )
    attempt_question_id = (request.values.get("question_id") or "").strip()
    attempt_question_ids = {row["question_id"] for row in qrows}
    if not attempt_question_id and qrows:
        attempt_question_id = qrows[0]["question_id"]
    selected_attempt_question = next(
        (row for row in qrows if row["question_id"] == attempt_question_id),
        None,
    )
    attempt_question_reference = render_question_reference(
        question_stem=row_get(selected_attempt_question, "stem", None),
        objective_code=(
            row_get(selected_attempt_question, "objective_id", None)
            or current_obj_for_questions
        ),
        objective_title=row_get(selected_attempt_question, "objective_text", None),
        question_id=attempt_question_id,
        preview_url=(
            url_for("teacher_question_preview", question_id=attempt_question_id)
            if selected_attempt_question
            else None
        ),
        missing=bool(attempt_question_id and attempt_question_id not in attempt_question_ids),
    ) if attempt_question_id else None
    selected_attempt_response = request.values.get("response", "A")
    if selected_attempt_response not in ("A", "B", "C", "D"):
        selected_attempt_response = "A"
    preview_objective_id = request.values.get("preview_objective_id", class_obj)
    if not is_launch_objective_id(preview_objective_id):
        preview_objective_id = class_obj
    preview_qrows = (
        get_questions_for_objective(conn, preview_objective_id)
        if preview_objective_id
        else []
    )
    preview_question_id = request.values.get("preview_question_id")
    preview_question_ids = {row["question_id"] for row in preview_qrows}
    if preview_question_id not in preview_question_ids:
        preview_question_id = (
            preview_qrows[0]["question_id"] if preview_qrows else None
        )

    pii_mode = current_pii_mode()

    single_table = []
    for o in objs:
        avg, nxt, reason, fr = decide_next(conn, active_student, o["objective_id"])
        single_table.append(
            (
                o["objective_id"],
                o["standard_id"],
                o["core_idea"],
                o["grade_band"],
                o["objective_text"],
                round(avg, 2),
                nxt,
                reason,
                fr,
            )
        )

    class_rows = []
    class_std_id = None
    if class_obj:
        row_std = conn.execute(
            "SELECT standard_id FROM objectives WHERE objective_id=?",
            (class_obj,),
        ).fetchone()
        class_std_id = row_std["standard_id"] if row_std else None

    if class_obj and students:
        for s in students:
            avg, nxt, reason, fr = decide_next(conn, s["student_id"], class_obj)
            name = display_name(s)
            class_rows.append(
                (
                    s["student_id"],
                    name,
                    s["grade"],
                    s["class_period"],
                    round(avg, 2),
                    nxt,
                    reason,
                    fr,
                    class_std_id,
                )
            )

    # Build list of unique, non-empty class periods for the dropdown
    raw_periods = {
        s["class_period"]
        for s in get_students(conn)
        if s["class_period"] is not None and str(s["class_period"]).strip() != ""
    }
    period_opts = ["ALL"] + sorted(str(p) for p in raw_periods)

    class_agg_rows = []
    mastery, practice = get_config(conn)
    dashboard_student_rows = get_dashboard_student_rows(conn, students, mastery, practice)
    dashboard_snapshot = build_dashboard_snapshot(dashboard_student_rows)
    needs_attention_rows = [
        row for row in dashboard_student_rows if row["category"] == "needs_help"
    ]
    not_started_rows = [
        row for row in dashboard_student_rows if row["category"] == "not_started"
    ]
    progressing_rows = [
        row for row in dashboard_student_rows if row["category"] == "progressing"
    ]
    mastered_rows = [
        row for row in dashboard_student_rows if row["category"] == "mastered"
    ]
    active_standard_rows = get_active_standard_summary(conn, selected_period)
    question_flags_action_count = teacher_actionable_question_flag_count(
        conn,
        session.get("user_id"),
    )
    current_user_is_owner = is_owner()
    owner_escalated_flag_count = (
        owner_escalated_question_flag_count(conn) if current_user_is_owner else 0
    )

    for o in objs:
        per_student = (
            [
                rolling_avg(conn, s["student_id"], o["objective_id"], 7)
                for s in students
            ]
            if students
            else []
        )
        class_avg = round(sum(per_student) / len(per_student), 2) if per_student else 0.0
        adv, rem = lookup_map(conn, o["objective_id"])
        if class_avg >= mastery and adv:
            nxt, reason = adv, "advance(class)"
        elif class_avg >= practice:
            nxt, reason = o["objective_id"], "practice(class)"
        else:
            nxt, reason = (rem or o["objective_id"]), "remediation(class)"
        fr_count = sum(
            1
            for s in (students or [])
            if frustration_active(conn, s["student_id"], o["objective_id"])
        )
        class_agg_rows.append(
            (
                o["objective_id"],
                o["standard_id"],
                o["objective_text"],
                class_avg,
                nxt,
                reason,
                fr_count,
            )
        )

    db_name = os.path.basename(DB)
    db_time = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(DB))
    )

    html = """
<!doctype html>
<title>RootED - Teacher Dashboard</title>
<div class="topbar">
  <div>
    <h1 style="margin:0;">RootED Teacher Dashboard</h1>
    <p class="dashboard-subtitle">Here's what's happening in your classroom today.</p>
    <p style="font-size:13px;color:#555;margin:4px 0 0 0;">
      Logged in as <strong>{{ session.get('username', 'teacher') }}</strong>
    </p>
  </div>
  <div class="top-actions">
    <form action="{{ url_for('student_view') }}" method="get" target="_blank" style="margin:0;">
      <button type="submit" class="btn btn-primary">Student Mode</button>
    </form>

    <a href="{{ url_for('diagnostic_home') }}"
       class="btn btn-accent"
       style="text-decoration:none;">
      Diagnostic Arena
    </a>

    <a href="{{ url_for('error_list') }}"
       class="btn btn-secondary"
       style="text-decoration:none;">
      View Error Log
    </a>

    <a href="{{ url_for('question_flags_review') }}"
       class="btn btn-secondary btn-with-badge"
       style="text-decoration:none;">
      <span>Question Flags</span>
      {% if question_flags_action_count %}
        <span class="action-badge" aria-label="{{ question_flags_action_count }} open question report{{ '' if question_flags_action_count == 1 else 's' }} requiring action">
          {{ question_flags_action_count }}
        </span>
      {% endif %}
    </a>

    {% if current_user_is_owner %}
      <a href="{{ url_for('owner_home') }}"
         class="btn btn-secondary btn-with-badge"
         style="text-decoration:none;">
        <span>Owner Workspace</span>
        {% if owner_escalated_flag_count %}
          <span class="action-badge" aria-label="{{ owner_escalated_flag_count }} escalated question report{{ '' if owner_escalated_flag_count == 1 else 's' }} awaiting RootED review">
            {{ owner_escalated_flag_count }}
          </span>
        {% endif %}
      </a>
    {% endif %}

    <form action="{{ url_for('logout') }}" method="get" style="margin:0;">
      <button type="submit" class="btn btn-ghost">Logout</button>
    </form>
  </div>
</div>
<style>
  body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#fbfaf4;color:#1f2937}
  table{border-collapse:collapse;width:100%}
  th,td{border:1px solid #e5e0d2;padding:8px;vertical-align:top}
  th{background:#f5f1e8;text-align:left;color:#374151}
  .ok{color:#0f7a46}.warn{color:#a66f00}.bad{color:#b42318}
  .topbar{display:flex;justify-content:space-between;align-items:center;gap:18px;margin-bottom:16px;padding:16px 18px;border:1px solid #dfe8d9;border-radius:12px;background:linear-gradient(135deg,#fffdf7,#eef7ed)}
  .dashboard-subtitle{margin:8px 0 0;color:#3f5f48;font-size:16px}
  .top-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
  .card{border:1px solid #e2decf;border-radius:10px;padding:16px;margin:16px 0;background:#fffefa;box-shadow:0 8px 18px rgba(47,111,78,.06)}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .btn{background:#2f6f4e;color:#fff;border:none;padding:8px 12px;border-radius:8px;cursor:pointer}
  .btn-primary{background:#2f6f4e}
  .btn-accent{background:#8a6234}
  .btn-secondary{background:#eef4ec;color:#2f5138;border:1px solid #c8d9c4}
  .btn-ghost{background:transparent;color:#6b4f3a;border:1px solid #dacdbb}
  .btn-with-badge{display:inline-flex;align-items:center;gap:7px}
  .action-badge{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:22px;padding:0 6px;border-radius:999px;background:#b42318;color:#fff;font-size:12px;font-weight:850;border:1px solid rgba(255,255,255,.7)}
  {{ question_reference_css }}
  .selected-question-reference{margin:10px 0 12px;padding:10px;border:1px solid #e5e7eb;border-radius:8px;background:#f9fafb}
  input,select{padding:6px 8px;border:1px solid #cbd5e1;border-radius:6px}
  .toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  .headerbar{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px}
  .btn-mini{font-size:12px;padding:4px 8px;border-radius:6px;border:none;cursor:pointer}
  .btn-ok{background:#16a34a;color:#fff}
  .btn-no{background:#dc2626;color:#fff;margin-left:6px}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;border:1px solid transparent}
  .pill-adv{background:#e6f6ee;border-color:#9ae6b4;color:#065f46}
  .pill-prac{background:#fff7d6;border-color:#f2d27e;color:#8a6d00}
  .pill-rem{background:#fdecea;border-color:#f5c2c0;color:#7f1d1d}
  .pill-prom{background:#efe5ff;border-color:#c4b5fd;color:#553c9a}
  .pill-fr{background:#fee2e2;border-color:#fecaca;color:#991b1b;margin-left:6px}
  .snapshot-grid{display:grid;grid-template-columns:repeat(5,minmax(130px,1fr));gap:10px;margin-top:12px}
  .snapshot-card{display:block;border:1px solid #d7dee8;border-radius:10px;padding:14px;background:#fffdf7;text-decoration:none}
  .snapshot-card:hover{border-color:#9fbe9a;box-shadow:0 8px 16px rgba(47,111,78,.1)}
  .snapshot-card strong{display:block;font-size:30px;line-height:1;color:#111827;margin-bottom:5px}
  .snapshot-card span{font-size:13px;color:#4b5563;font-weight:700}
  .snapshot-help{background:#fff1f2;border-color:#fecdd3}
  .snapshot-ready{background:#ecfdf5;border-color:#a7f3d0}
  .pilot-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(280px,.65fr);gap:16px}
  .subgrid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .section-note{font-size:13px;color:#6b7280;margin:4px 0 12px}
  .empty-state{border:1px dashed #c8d9c4;border-radius:8px;padding:14px;background:#fbf8ef;color:#53665a}
  .priority-list{display:flex;flex-direction:column;gap:10px}
  .priority-item{border:1px solid #e5e7eb;border-radius:8px;padding:12px;background:#fff}
  .priority-top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
  .priority-name{font-weight:800;color:#111827}
  .priority-meta{font-size:13px;color:#4b5563;margin-top:4px}
  .priority-reason{font-size:13px;color:#7f1d1d;margin-top:6px}
  .status-pill{display:inline-block;padding:3px 8px;border-radius:999px;font-size:12px;font-weight:800}
  .status-help{background:#fee2e2;color:#991b1b}
  .status-start{background:#e0f2fe;color:#075985}
  .status-progress{background:#fef3c7;color:#92400e}
  .status-mastered{background:#dcfce7;color:#166534}
  .code-token{display:inline-block;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:18px;letter-spacing:1px;background:#111827;color:#fff;padding:5px 8px;border-radius:6px}
  .tool-section{border-top:3px solid #dfe8d9;margin-top:20px;padding-top:4px}
  .tool-details{margin-top:10px}
  .tool-details > summary{cursor:pointer;list-style:none;border:1px solid #dfe8d9;border-radius:10px;background:#f5f8f1;padding:12px 14px;font-weight:800;color:#2f5138}
  .tool-details > summary::-webkit-details-marker{display:none}
  .tool-details > summary::after{content:"Show/hide setup tools";float:right;font-weight:600;color:#6b7280;font-size:12px}
  .tool-group{margin:16px 0 8px;padding:10px 12px;border-left:4px solid #9fbe9a;background:#f7fbf3;border-radius:8px;color:#2f5138}
  .tool-group h3{margin:0;font-size:16px}
  .tool-group p{margin:4px 0 0;color:#6b7280;font-size:13px}
  @media(max-width:900px){.snapshot-grid{grid-template-columns:repeat(2,minmax(130px,1fr))}.pilot-grid,.subgrid{grid-template-columns:1fr}}
</style>

{% with msgs = get_flashed_messages() %}
  {% if msgs %}
    <div style="background:#e7f7ee;border:1px solid #a8e0bf;color:#0f6b3a;padding:10px 12px;border-radius:8px;margin:10px 0;">
      {% for m in msgs %}
        <div>✅ {{ m }}</div>
      {% endfor %}
    </div>
  {% endif %}
{% endwith %}

{% if recent_errors > 0 %}
  <div style="background:#fff7d6;border:1px solid #f2d27e;color:#8a6d00;padding:10px 12px;border-radius:8px;margin:10px 0;">
    ⚠️ <strong>{{ recent_errors }} error(s)</strong> occurred in the last 24 hours.
    <a href="{{ url_for('error_list') }}" style="margin-left:6px;">View details</a>
  </div>
{% endif %}

{% if msg %}<p><strong>{{msg}}</strong></p>{% endif %}
{% if pii_mode == 'masked' %}
  <div style="background:#fff7d6;border:1px solid #f2d27e;color:#8a6d00;padding:10px 12px;border-radius:8px;margin:10px 0;">
    🔒 <strong>Names masked</strong> — student names are hidden (initials only).
  </div>
{% else %}
  <div style="background:#e7f7ee;border:1px solid #a8e0bf;color:#0f6b3a;padding:10px 12px;border-radius:8px;margin:10px 0;">
    👩‍🏫 <strong>Full names visible (teacher view)</strong> — ensure you’re in a private setting before sharing.
    <div style="font-size:12px;color:#0f6b3a;opacity:0.9;margin-top:4px;">
      🔒 Note: Students are always masked in the Student Practice view.
    </div>
  </div>
{% endif %}

<div class="card" style="background:#f7fbf3;">
  <div class="headerbar">
    <div>
      <h2 style="margin:0;">Class Snapshot</h2>
      <p class="section-note">Period {{ selected_period }} - first look at who needs attention and what is active.</p>
    </div>
    <form method="get" class="toolbar" style="margin:0;">
      <input type="hidden" name="student_id" value="{{active_student}}">
      <input type="hidden" name="class_objective" value="{{class_obj}}">
      <label>Period
        <select name="period" onchange="this.form.submit()">
          {% for p in period_opts %}
            <option value="{{p}}" {% if p==selected_period %}selected{% endif %}>{{p}}</option>
          {% endfor %}
        </select>
      </label>
    </form>
  </div>

  <div class="snapshot-grid">
    <a class="snapshot-card snapshot-help" href="#needs-attention">
      <strong>{{ dashboard_snapshot.needs_help }}</strong>
      <span>Needs Attention</span>
    </a>
    <a class="snapshot-card" href="#not-started">
      <strong>{{ dashboard_snapshot.not_started }}</strong>
      <span>Not Started</span>
    </a>
    <a class="snapshot-card" href="#progressing-mastered">
      <strong>{{ dashboard_snapshot.progressing }}</strong>
      <span>Progressing</span>
    </a>
    <a class="snapshot-card snapshot-ready" href="#progressing-mastered">
      <strong>{{ dashboard_snapshot.mastered }}</strong>
      <span>Mastered/Completed</span>
    </a>
    <a class="snapshot-card" href="#active-standards">
      <strong>{{ dashboard_snapshot.active_standards }}</strong>
      <span>Active Standards</span>
    </a>
  </div>
</div>

<div class="pilot-grid">
  <div class="card" id="needs-attention">
    <h2>Needs Attention</h2>
    <p class="section-note">Uses existing locked status, frustration signal, and low Rolling-7 average when response evidence exists.</p>
    {% if needs_attention_rows %}
      <div class="priority-list">
        {% for row in needs_attention_rows %}
          <div class="priority-item">
            <div class="priority-top">
              <div>
                <div class="priority-name">{{ row.name }} <span style="font-weight:500;color:#6b7280;">({{ row.student_id }})</span></div>
                <div class="priority-meta">
                  {{ row.standard_id or "No active standard" }}
                  {% if row.level %} | Level {{ row.level }}{% endif %}
                  {% if row.response_count is not none %} | {{ row.response_count }} response(s) at level{% endif %}
                </div>
              </div>
              <span class="status-pill status-help">{{ row.status_label }}</span>
            </div>
            <div class="priority-reason">{{ row.reason or "Review current progress." }}</div>
            <div class="priority-meta">
              Rolling avg: {% if row.rolling_avg is not none %}{{ (row.rolling_avg * 100)|round|int }}%{% else %}No responses yet{% endif %}
              {% if row.objective_id %} | Objective {{ row.objective_id }}{% endif %}
              {% if row.last_activity %} | Last activity {{ format_ts(row.last_activity) }}{% endif %}
            </div>
            <p style="margin:10px 0 0;">
              <a class="btn-mini" style="background:#4b5563;color:#fff;text-decoration:none;"
                 href="{{ url_for('student_overview', student_id=row.student_id) }}"
                 target="_blank">Overview</a>
            </p>
          </div>
        {% endfor %}
      </div>
    {% else %}
      <div class="empty-state">Great news - no students currently need immediate attention.</div>
    {% endif %}
  </div>

  <div class="card" id="active-standards">
    <h2>Active Standards</h2>
    <p class="section-note">Standards with current progress rows for this period.</p>
    {% if active_standard_rows %}
      <table>
        <tr><th>Standard</th><th>Students</th><th>Avg</th><th>Content</th></tr>
        {% for row in active_standard_rows %}
          <tr>
            <td>
              <strong>{{ row["standard_id"] }}</strong><br>
              <span style="font-size:12px;color:#6b7280;">{{ row["core_idea"] }} {{ row["grade_band"] }}</span>
            </td>
            <td>{{ row["student_count"] }}{% if row["locked_count"] %}<br><span class="bad">{{ row["locked_count"] }} locked</span>{% endif %}</td>
            <td>{% if row["students_with_evidence"] %}{{ ((row["avg_progress"] or 0) * 100)|round|int }}%{% else %}No responses yet{% endif %}</td>
            <td>{{ row["objective_count"] }} obj<br>{{ row["question_count"] }} q</td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <div class="empty-state">No active standards yet for this period. Once students begin practice, standards will appear here.</div>
    {% endif %}
  </div>
</div>

<div class="subgrid">
  <div class="card" id="not-started">
    <h2>Not Started</h2>
    <p class="section-note">Students with no response evidence yet, including newly placed students.</p>
    {% if not_started_rows %}
      <table>
        <tr><th>Student</th><th>Grade</th><th>Period</th><th>Next Click</th></tr>
        {% for row in not_started_rows %}
          <tr>
            <td><strong>{{ row.name }}</strong><br><span style="font-size:12px;color:#6b7280;">{{ row.student_id }}</span></td>
            <td>{{ row.grade or "" }}</td>
            <td>{{ row.period or "" }}</td>
            <td><a class="btn-mini" style="background:#2f6f4e;color:#fff;text-decoration:none;" href="{{ url_for('student_overview', student_id=row.student_id) }}" target="_blank">Overview</a></td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <div class="empty-state">Everyone in this view has started or has a progress signal.</div>
    {% endif %}
  </div>

  <div class="card" id="progressing-mastered">
    <h2>Progressing / Mastered</h2>
    <p class="section-note">Students with active progress or current mastery signals.</p>
    {% if progressing_rows or mastered_rows %}
      <table>
        <tr><th>Student</th><th>Status</th><th>Standard</th><th>Rolling Avg</th><th>Action</th></tr>
        {% for row in progressing_rows + mastered_rows %}
          <tr>
            <td><strong>{{ row.name }}</strong><br><span style="font-size:12px;color:#6b7280;">{{ row.student_id }}</span></td>
            <td>
              {% if row.category == "mastered" %}
                <span class="status-pill status-mastered">{{ row.status_label }}</span>
              {% else %}
                <span class="status-pill status-progress">{{ row.status_label }}</span>
              {% endif %}
            </td>
            <td>{{ row.standard_id or "" }}{% if row.level %}<br><span style="font-size:12px;color:#6b7280;">Level {{ row.level }}</span>{% endif %}</td>
            <td>{% if row.rolling_avg is not none %}{{ (row.rolling_avg * 100)|round|int }}%{% else %}No responses yet{% endif %}</td>
            <td><a class="btn-mini" style="background:#2f6f4e;color:#fff;text-decoration:none;" href="{{ url_for('student_overview', student_id=row.student_id) }}" target="_blank">Overview</a></td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <div class="empty-state">Progress and mastery signals will appear here as students answer questions.</div>
    {% endif %}
  </div>
</div>

<div class="tool-section" id="teacher-tools">
  <h2 style="margin-bottom:0;">Teacher Tools</h2>
  <p class="section-note">Quick classroom tools for previewing and supporting student practice.</p>
</div>

<div class="grid">
  <div class="card" id="question-preview">
    <div class="headerbar">
      <div>
        <h2 style="margin:0;">Question Preview</h2>
        <p class="section-note">Open a read-only teacher preview without recording student work or changing adaptive state.</p>
      </div>
      {% if preview_question_id %}
        <a class="btn"
           href="{{ url_for('teacher_question_preview', question_id=preview_question_id) }}"
           target="_blank">Open Preview</a>
      {% endif %}
    </div>

    {% if not objs %}
      <div class="empty-state">Objectives will appear here after learning content is available.</div>
    {% else %}
      <form method="get" action="{{ url_for('index') }}#question-preview" class="toolbar" style="margin:0 0 8px 0;">
        <input type="hidden" name="student_id" value="{{active_student}}">
        <input type="hidden" name="class_objective" value="{{class_obj}}">
        <input type="hidden" name="period" value="{{selected_period}}">
        <label>Objective
          <select name="preview_objective_id" onchange="this.form.submit()">
            {% for o in objs %}
              <option value="{{o['objective_id']}}" {% if o['objective_id'] == preview_objective_id %}selected{% endif %}>
                {{o['objective_id']}}
              </option>
            {% endfor %}
          </select>
        </label>
        <noscript><button class="btn" type="submit">Load Questions</button></noscript>
      </form>

      {% if preview_qrows %}
        <form method="get" action="{{ url_for('index') }}#question-preview" class="toolbar" style="margin:0;">
          <input type="hidden" name="student_id" value="{{active_student}}">
          <input type="hidden" name="class_objective" value="{{class_obj}}">
          <input type="hidden" name="period" value="{{selected_period}}">
          <input type="hidden" name="preview_objective_id" value="{{preview_objective_id}}">
          <label>Question
            <select name="preview_question_id" onchange="this.form.submit()">
              {% for q in preview_qrows %}
                <option value="{{q['question_id']}}" {% if q['question_id'] == preview_question_id %}selected{% endif %}>
                  {{ question_selector_label(q) }}
                </option>
              {% endfor %}
            </select>
          </label>
          <noscript><button class="btn" type="submit">Select Question</button></noscript>
        </form>
      {% else %}
        <div class="empty-state">No questions are available for this objective yet.</div>
      {% endif %}
    {% endif %}
  </div>
</div>

<div class="tool-section">
  <h2 style="margin-bottom:0;">Management and Setup Tools</h2>
  <p class="section-note">Existing configuration, roster, account, import, maintenance, and detailed progress tools remain available below.</p>
</div>

<div class="card" id="class-enrollment">
  <h2>Class Enrollment</h2>
  <p class="section-note">Create class codes students can use to enroll themselves.</p>

  <form method="post" style="margin-bottom:14px;">
    <input type="hidden" name="action" value="create_class">
    <div style="display:grid;grid-template-columns:1.4fr .8fr auto;gap:8px;align-items:end">
      <label>Class name
        <input name="class_name" placeholder="Period 1 Science" required>
      </label>
      <label>Period
        <input name="class_period" placeholder="1">
      </label>
      <button class="btn" type="submit">Create Class Code</button>
    </div>
  </form>

  {% if class_sections %}
    <table>
      <tr>
        <th>Class / Period</th>
        <th>Join Code</th>
        <th>Status</th>
        <th>Students / Roster</th>
        <th>Actions</th>
      </tr>
      {% for c in class_sections %}
        <tr>
          <td>
            <strong>{{ c['name'] }}</strong><br>
            <span style="font-size:12px;color:#6b7280;">Class period: {{ c['class_period'] or 'Not set' }}</span>
          </td>
          <td><span class="code-token">{{ c['join_code'] }}</span></td>
          <td>
            {% if c['is_active'] %}
              <span class="pill pill-adv">active</span>
            {% else %}
              <span class="pill pill-rem">inactive</span>
            {% endif %}
          </td>
          <td>
            <strong>{{ c['enrolled_count'] }}</strong>
            <span style="font-size:12px;color:#6b7280;">student(s)</span><br>
            <span style="font-size:12px;color:#6b7280;">Class period: {{ c['class_period'] or 'Not set' }}</span>
            {% set roster = class_rosters.get(c['class_id'], []) %}
            {% if roster %}
              <details style="margin-top:6px;">
                <summary style="cursor:pointer;color:#2f5138;">View roster for period {{ c['class_period'] or 'Not set' }}</summary>
                <ul style="margin:8px 0 0 18px;padding:0;">
                  {% for s in roster %}
                    <li>
                      {{ display_name(s) }}
                      <span style="font-size:12px;color:#6b7280;">
                        ({{ s['student_id'] }}; student period {{ s['class_period'] or 'Not set' }})
                      </span>
                      <a class="btn-mini" href="{{ url_for('teacher_confirm_archive_enrollment', class_id=c['class_id'], student_id=s['student_id']) }}">Remove from Class</a>
                    </li>
                  {% endfor %}
                </ul>
              </details>
            {% endif %}
          </td>
          <td>
            <form method="post" style="display:inline">
              <input type="hidden" name="action" value="rotate_class_code">
              <input type="hidden" name="class_id" value="{{ c['class_id'] }}">
              <button class="btn-mini" type="submit">New Code</button>
            </form>
            <form method="post" style="display:inline;margin-left:4px;">
              <input type="hidden" name="action" value="toggle_class_active">
              <input type="hidden" name="class_id" value="{{ c['class_id'] }}">
              <button class="btn-mini" type="submit">
                {% if c['is_active'] %}Deactivate{% else %}Activate{% endif %}
              </button>
            </form>
          </td>
        </tr>
      {% endfor %}
    </table>
  {% else %}
    <div class="empty-state">No class codes yet. Create one and share the code with students.</div>
  {% endif %}
</div>

<details class="tool-details">
  <summary>Management and setup panels</summary>

<div class="tool-group">
  <h3>Teacher Tools</h3>
  <p>Practice support tools teachers may use during a pilot.</p>
</div>

<div class="grid">
  <div class="card">
    <h2>Record an Attempt</h2>
    {% if not objs %}
      <p>No objectives found. Run the importer first.</p>
    {% else %}
      <form method="get" class="toolbar" style="margin:0 0 8px 0">
        <input type="hidden" name="student_id" value="{{active_student}}">
        <input type="hidden" name="class_objective" value="{{class_obj}}">
        <input type="hidden" name="period" value="{{selected_period}}">
        <input type="hidden" name="response" value="{{selected_attempt_response}}">
        <label>Objective
          <select name="objective_id" onchange="this.form.submit()">
            {% for o in objs %}
              <option value="{{o['objective_id']}}"
                {% if o['objective_id'] == request.values.get('objective_id', class_obj) %}selected{% endif %}>
                {{o['objective_id']}}
              </option>
            {% endfor %}
          </select>
        </label>
      </form>

      <form method="post">
        <input type="hidden" name="action" value="save_attempt">
        <input type="hidden" name="objective_id" value="{{ current_obj_for_questions }}">
        <input type="hidden" name="class_objective" value="{{class_obj}}">
        <input type="hidden" name="period" value="{{selected_period}}">

        <label>Student
          <select name="student_id">
            {% if not students %}
              <option value="S1">S1</option>
            {% else %}
              {% for s in students %}
                <option value="{{s['student_id']}}" {% if s['student_id']==active_student %}selected{% endif %}>
                  {{s['student_id']}}
                </option>
              {% endfor %}
            {% endif %}
          </select>
        </label>

        {% if qrows %}
          <label>Question
            <select name="question_id" onchange="this.form.method='get'; this.form.action='{{ url_for('index') }}#teacher-tools'; this.form.submit()">
              {% if attempt_question_id and not selected_attempt_question %}
                <option value="{{ attempt_question_id }}" selected>
                  Question no longer available — {{ question_reference_meta_text(current_obj_for_questions, '', attempt_question_id) }}
                </option>
              {% endif %}
              {% for q in qrows %}
                <option value="{{q['question_id']}}" {% if q['question_id'] == attempt_question_id %}selected{% endif %}>
                  {{ question_selector_label(q) }}
                </option>
              {% endfor %}
            </select>
          </label>
          <div class="selected-question-reference">
            {{ attempt_question_reference }}
          </div>
          <label>Response
            <select name="response">
              {% for option in ['A', 'B', 'C', 'D'] %}
                <option value="{{ option }}" {% if option == selected_attempt_response %}selected{% endif %}>{{ option }}</option>
              {% endfor %}
            </select>
          </label>
          <p><button class="btn" type="submit">Save Attempt</button></p>
        {% elif attempt_question_reference %}
          <div class="selected-question-reference">
            {{ attempt_question_reference }}
          </div>
          <p>No available launch question is selected. Choose an available question before saving an attempt.</p>
        {% else %}
          <p>No questions for this objective.</p>
        {% endif %}
      </form>
    {% endif %}
  </div>
</div> <!-- end teacher tools grid -->

<details class="card">
  <summary>Class Aggregate — Rolling Avg & Next Node (Period: {{selected_period}})</summary>
  <table>
    <tr>
      <th>Objective</th>
      <th>Standard</th>
      <th>Objective Text</th>
      <th>Class Avg (last 7)</th>
      <th>Next Node</th>
      <th>Reason</th>
      <th># Frustrated</th>
    </tr>
    {% for oid, sid, text, avg, nxt, reason, fr_count in class_agg_rows %}
    <tr>
      <td>{{oid}}</td>
      <td>{{sid}}</td>
      <td>{{text}}</td>
      <td>
        {% if avg>=0.9 %}<span class="ok">{{avg}}</span>
        {% elif avg>=0.7 %}<span class="warn">{{avg}}</span>
        {% else %}<span class="bad">{{avg}}</span>
        {% endif %}
      </td>
      <td>{{nxt}}</td>
      <td>{{reason}}</td>
      <td>{{fr_count}}</td>
    </tr>
    {% endfor %}
  </table>
</details>

<details class="card">
  <summary>Class View</summary>
  {% if not objs %}
    <p>No objectives found. Run the importer first.</p>
  {% else %}
    <form method="get" class="toolbar">
      <label>Objective
        <select name="class_objective" onchange="this.form.submit()">
          {% for o in objs %}
            <option value="{{o['objective_id']}}" {% if o['objective_id']==class_obj %}selected{% endif %}>{{o['objective_id']}}</option>
          {% endfor %}
        </select>
      </label>
      <label>Period
        <select name="period" onchange="this.form.submit()">
          {% for p in period_opts %}
            <option value="{{p}}" {% if p==selected_period %}selected{% endif %}>{{p}}</option>
          {% endfor %}
        </select>
      </label>
      <input type="hidden" name="student_id" value="{{active_student}}">
      <a class="btn" href="/export_csv?class_objective={{class_obj}}&period={{selected_period}}">Export CSV</a>
    </form>

    <form method="post" action="/toggle_names" class="toolbar" style="justify-content:flex-end; gap:8px; margin-top:8px">
      <label>Names:
        <select name="pii_mode" onchange="this.form.submit()">
          <option value="masked" {% if pii_mode=='masked' %}selected{% endif %}>Masked</option>
          <option value="full" {% if pii_mode=='full' %}selected{% endif %}>Full (teacher)</option>
        </select>
      </label>
      <span style="font-size:12px;color:#6b7280;display:flex;align-items:center;gap:6px;">
        🔒 Students are always masked in Student Practice.
      </span>
    </form>

    <table>
      <tr>
        <th>Student</th>
        <th>Name</th>
        <th>Grade</th>
        <th>Period</th>
        <th>Rolling Avg (last 7)</th>
        <th>Next Node</th>
        <th>Reason</th>
        <th>Quick Score</th>
        <th>Overview</th>
      </tr>
      {% for sid, name, grade, period, avg, nxt, reason, fr, std_id in class_rows %}
        <tr>
          <td>{{sid}}</td>
          <td>{{name}}</td>
          <td>{{grade or ""}}</td>
          <td>{{period or ""}}</td>
          <td>
            {% if avg>=0.9 %}
              <span class="ok">{{avg}}</span>
            {% elif avg>=0.7 %}
              <span class="warn">{{avg}}</span>
            {% else %}
              <span class="bad">{{avg}}</span>
            {% endif %}
          </td>
          <td>{{nxt}}</td>
          <td>
            {% set cls = 'pill' %}
            {% if reason.startswith('promote') %}
              {% set cls = 'pill pill-prom' %}
            {% elif reason == 'advance' %}
              {% set cls = 'pill pill-adv' %}
            {% elif reason == 'practice' %}
              {% set cls = 'pill pill-prac' %}
            {% elif reason == 'remediation' %}
              {% set cls = 'pill pill-rem' %}
            {% endif %}
            <span class="{{cls}}">{{reason}}</span>
            {% if fr %}
              <span class="pill pill-fr">frustration</span>
            {% endif %}
          </td>
          <td>
            <form method="post" action="/quick_attempt" style="display:inline">
              <input type="hidden" name="student_id" value="{{sid}}">
              <input type="hidden" name="objective_id" value="{{class_obj}}">
              <input type="hidden" name="class_objective" value="{{class_obj}}">
              <input type="hidden" name="period" value="{{selected_period}}">
              <input type="hidden" name="active_student" value="{{active_student}}">
              <button class="btn-mini btn-ok" name="result" value="correct">✅</button>
            </form>
            <form method="post" action="/quick_attempt" style="display:inline">
              <input type="hidden" name="student_id" value="{{sid}}">
              <input type="hidden" name="objective_id" value="{{class_obj}}">
              <input type="hidden" name="class_objective" value="{{class_obj}}">
              <input type="hidden" name="period" value="{{selected_period}}">
              <input type="hidden" name="active_student" value="{{active_student}}">
              <button class="btn-mini btn-no" name="result" value="incorrect">❌</button>
            </form>
          </td>
          <td>
            <a
              href="{{ url_for('student_overview', student_id=sid) }}"
              target="_blank"
              class="btn-mini"
              style="background:#4b5563;color:#fff;text-decoration:none;"
            >
              Overview
            </a>
          </td>
        </tr>
      {% endfor %}
    </table>
  {% endif %}
</details>

<div class="tool-group">
  <h3>Admin Setup</h3>
  <p>Roster, account, configuration, and content import tools.</p>
</div>

<div class="grid">
  <div class="card">
    <h2>Configuration</h2>
    <form method="post" action="/update_config">
      <label>Mastery Threshold (0–1)
        <input type="number" name="mastery_threshold" step="0.05" min="0" max="1" value="{{ mastery }}">
      </label>
      <label>Practice Threshold (0–1)
        <input type="number" name="practice_lower" step="0.05" min="0" max="1" value="{{ practice }}">
      </label>
      <p><button class="btn" type="submit">Save Settings</button></p>
    </form>
  </div>

  <div class="card">
    <h2>Add / Update Student</h2>
    <form method="post">
      <input type="hidden" name="action" value="save_student">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        <label>Student ID <input name="student_id" placeholder="S1"></label>
        <label>Grade <input name="grade" type="number" min="3" max="12" placeholder="6"></label>
        <label>First name <input name="first_name" placeholder="Avery"></label>
        <label>Last name <input name="last_name" placeholder="Lee"></label>
        <label>Class period <input name="class_period" placeholder="1"></label>
      </div>
      <p><button class="btn" type="submit">Save Student</button></p>
    </form>
    {% if students %}
      <p><em>{{students|length}} student(s) in roster (period {{selected_period}}).</em></p>
    {% endif %}
  </div>

  {% if false %}
  <div class="card">
    <h2>User Accounts</h2>
    <p style="font-size:14px;color:#555;">
      Create login accounts and manage who can sign in.
    </p>

    <form method="post" style="margin-bottom:12px;">
      <input type="hidden" name="action" value="create_user">
      <div style="display:grid;grid-template-columns:1.2fr 1.2fr 1fr 1.2fr;gap:8px;align-items:end">
        <label>Username
          <input name="username" placeholder="student123">
        </label>
        <label>Temp password
          <input name="password" placeholder="changeme">
        </label>
        <label>Role
          <select name="role">
            <option value="student">student</option>
            <option value="teacher">teacher</option>
          </select>
        </label>
        <label>Linked student ID (optional)
          <input name="linked_student_id" placeholder="S1">
        </label>
      </div>
      <p style="margin-top:8px;">
        <button class="btn" type="submit">Create User</button>
      </p>
    </form>

    {% if all_users %}
      <table>
        <tr>
          <th>ID</th>
          <th>Username</th>
          <th>Role</th>
          <th>Linked Student</th>
          <th>Status</th>
          <th>Actions</th>
        </tr>
        {% for u in all_users %}
          <tr>
            <td>{{u["id"]}}</td>
            <td>{{u["username"]}}</td>
            <td>{{u["role"]}}</td>
            <td>{{u["linked_student_id"] or ""}}</td>
            <td>
              {% if u["is_active"] %}
                <span class="pill pill-adv">active</span>
              {% else %}
                <span class="pill pill-rem">inactive</span>
              {% endif %}
            </td>
            <td>
              <form method="post" action="/user_admin" style="display:inline">
                <input type="hidden" name="user_id" value="{{u['id']}}">
                <input type="hidden" name="action" value="reset_pw">
                <input type="password" name="new_password" placeholder="new password" style="width:120px;font-size:12px;">
                <button class="btn-mini" type="submit">Reset</button>
              </form>
              <form method="post" action="/user_admin" style="display:inline;margin-left:4px;">
                <input type="hidden" name="user_id" value="{{u['id']}}">
                <input type="hidden" name="action" value="toggle_active">
                <button class="btn-mini" type="submit">
                  {% if u["is_active"] %}Deactivate{% else %}Activate{% endif %}
                </button>
              </form>
              {% if u["role"] == "student" %}
                <a
                  class="btn-mini"
                  style="background:#2f6f4e;color:#fff;text-decoration:none;margin-left:4px;"
                  href="{{ url_for('student_overview', student_id=(u['linked_student_id'] or u['username'])) }}"
                  target="_blank"
                >Overview</a>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <p><em>No users in the system yet.</em></p>
    {% endif %}
  </div>
  {% endif %}

</div> <!-- end admin setup grid -->

<div class="tool-group">
  <h3>Admin Setup</h3>
  <p>Content import tools for setup and maintenance.</p>
</div>

<div class="card">
  <h2>Import Data from CSV</h2>
  <form method="post" action="/import_csv" enctype="multipart/form-data">
    <div style="display:flex;flex-direction:column;gap:8px;max-width:420px;">
      <label>Dataset
        <select name="dataset">
          <option value="standards">Standards</option>
          <option value="objectives">Objectives</option>
          <option value="questions">Questions</option>
        </select>
      </label>
      <label>CSV File
        <input type="file" name="file" accept=".csv">
      </label>
      <p style="font-size:12px;color:#555;margin:0;">
        Expected headers:<br>
        <strong>Standards:</strong> standard_id, core_idea, grade_band<br>
        <strong>Objectives:</strong> objective_id, standard_id, objective_text, order_in_band<br>
        <strong>Questions:</strong> question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key
      </p>
      <button class="btn" type="submit">Import CSV</button>
    </div>
  </form>
</div>

<div class="tool-group">
  <h3>Owner / Maintenance</h3>
  <p>Backup, reset, and restore actions for practice data.</p>
</div>

<div class="card">
  <h2>Maintenance / Data Tools</h2>
  <p style="font-size:14px; color:#555;">
    Use these tools to back up, reset, or restore practice data.
  </p>

  <form method="post" action="/admin_action" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px;">
    <button class="btn" type="submit" name="action" value="export_attempts">
      ⬇ Download attempts backup (CSV)
    </button>
    <button class="btn" type="submit" name="action" value="reset_practice"
            onclick="return confirm('This will delete ALL attempts and responses. Students and content will stay. Are you sure?');">
      🧹 Clear all attempts/responses
    </button>
  </form>

  <hr style="border:none;border-top:1px solid #e5e7eb;margin:10px 0;">

  <form method="post" action="/restore_attempts" enctype="multipart/form-data" style="display:flex;flex-direction:column;gap:8px;max-width:420px;">
    <label>Restore attempts from backup CSV
      <input type="file" name="file" accept=".csv">
    </label>
    <p style="font-size:12px;color:#555;margin:0;">
      Use a file previously downloaded with
      <em>"Download attempts backup (CSV)"</em>.
    </p>
    <button class="btn" type="submit"
            onclick="return confirm('This will clear current attempts/responses and replace them with the backup. Continue?');">
      🔁 Restore attempts from CSV
    </button>
  </form>
</div>

<div class="tool-group">
  <h3>Developer / Debug</h3>
  <p>Detailed internal progress calculations and engine traces.</p>
</div>

<div class="card" style="background:#f9fafb;">
  <h2>{{ APP_NAME }}</h2>
  <p><strong>Version:</strong> {{ APP_VERSION }}</p>
  <p><strong>Database:</strong> {{ db_name }}</p>
  <p><strong>Last Modified:</strong> {{ db_time }}</p>
</div>

<div class="card" style="background:#eff6ff;">
  <h2>Adaptive Engine Status</h2>
  {% if engine_ok %}
    <p>✅ Rolling-7 engine tables are ready.</p>
  {% else %}
    <p>⚠️ Some engine tables are missing or not initialized:</p>
    <ul>
      {% for name, ok in engine_checks.items() %}
        {% if not ok %}
          <li>{{ name }}</li>
        {% endif %}
      {% endfor %}
    </ul>
    <p style="font-size:12px;color:#555;">
      If this keeps showing, re-run <code>migrate_schema.py</code>.
    </p>
  {% endif %}
</div>

<details class="card">
  <summary>Objectives — Rolling Avg & Next Node</summary>
  <div class="headerbar">
    <form method="get" style="margin:0">
      <input type="hidden" name="class_objective" value="{{class_obj}}">
      <input type="hidden" name="period" value="{{selected_period}}">
      <label>Student
        <select name="student_id" onchange="this.form.submit()">
          {% for s in students %}
            <option value="{{s['student_id']}}" {% if s['student_id']==active_student %}selected{% endif %}>
              {{ s['student_id'] }} — {{ display_name(s) }}
            </option>
          {% endfor %}
        </select>
      </label>
    </form>
  </div>

  <table>
    <tr>
      <th>Objective</th><th>Standard</th><th>Objective Text</th>
      <th>Rolling Avg (last 7)</th><th>Next Node</th><th>Reason</th>
    </tr>
    {% for oid, sid, core, band, text, avg, nxt, reason, fr in single_table %}
    <tr>
      <td>{{oid}}</td><td>{{sid}}</td><td>{{text}}</td>
      <td>{% if avg>=0.9 %}<span class="ok">{{avg}}</span>{% elif avg>=0.7 %}<span class="warn">{{avg}}</span>{% else %}<span class="bad">{{avg}}</span>{% endif %}</td>
      <td>{{nxt}}</td><td>{{reason}}</td>
    </tr>
    {% endfor %}
  </table>
</details>

</details>
    """

    return render_template_string(
        html,
        msg=msg,
        objs=objs,
        students=students,
        active_student=active_student,
        single_table=single_table,
        class_obj=class_obj,
        class_rows=class_rows,
        class_agg_rows=class_agg_rows,
        dashboard_snapshot=dashboard_snapshot,
        needs_attention_rows=needs_attention_rows,
        not_started_rows=not_started_rows,
        progressing_rows=progressing_rows,
        mastered_rows=mastered_rows,
        active_standard_rows=active_standard_rows,
        qrows=qrows,
        current_obj_for_questions=current_obj_for_questions,
        attempt_question_id=attempt_question_id,
        attempt_question_reference=attempt_question_reference,
        selected_attempt_response=selected_attempt_response,
        question_selector_label=question_selector_label,
        question_reference_meta_text=question_reference_meta_text,
        preview_objective_id=preview_objective_id,
        preview_qrows=preview_qrows,
        preview_question_id=preview_question_id,
        period_opts=period_opts,
        selected_period=selected_period,
        pii_mode=pii_mode,
        display_name=display_name,
        format_ts=format_ts,
        mastery=mastery,
        practice=practice,
        APP_NAME=APP_NAME,
        APP_VERSION=APP_VERSION,
        db_name=db_name,
        db_time=db_time,
        engine_ok=engine_ok,
        engine_checks=engine_checks,
        recent_errors=recent_errors,
        question_flags_action_count=question_flags_action_count,
        current_user_is_owner=current_user_is_owner,
        owner_escalated_flag_count=owner_escalated_flag_count,
        question_reference_css=Markup(QUESTION_REFERENCE_CSS),
        all_users=all_users,
        class_sections=class_sections,
        class_rosters=class_rosters,
    )


# ---------- Student view ----------
@app.route("/student", methods=["GET", "POST"])
@instruction_required
def student_view():
    conn = get_conn()
    feedback = None

    role = session.get("role")
    if role == "student" and request.args.get("home") == "1":
        session["current_mode"] = "home"
    if "current_mode" not in session:
        session["current_mode"] = "home" if role == "student" else "question"
    if "locked_payload" not in session:
        session["locked_payload"] = None

    if role not in ("teacher", "student"):
        session.clear()
        flash("Session error. Please log in again.")
        return redirect(url_for("login"))

    all_students = get_students(conn)
    objs = get_objectives(conn)
    locked_student_id = locked_student_id_for_session(conn)

    if role == "student":
        if not locked_student_id:
            flash("Your account is not linked to a student record yet. Please tell your teacher.")
            return redirect(url_for("logout"))

        row = conn.execute(
            """
            SELECT student_id, first_name, last_name, grade, class_period
            FROM students
            WHERE student_id = ?
            """,
            (locked_student_id,),
        ).fetchone()

        if not row:
            conn.execute(
                """
                INSERT INTO students (student_id, first_name, last_name, grade, class_period)
                VALUES (?, ?, ?, ?, ?)
                """,
                (locked_student_id, "", "", None, None),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT student_id, first_name, last_name, grade, class_period
                FROM students
                WHERE student_id = ?
                """,
                (locked_student_id,),
            ).fetchone()

        students = [row]
        student_id = locked_student_id
    else:
        students = all_students
        student_id = request.values.get("student_id")
        if not student_id and students:
            student_id = students[0]["student_id"]

    if role == "student" and request.method == "POST" and request.form.get("action") == "join_class":
        ok, message = enroll_student_by_code(
            conn,
            student_id,
            request.form.get("join_code", ""),
        )
        flash(message)
        return redirect(url_for("student_view"))

    enrolled_classes = (
        get_student_class_sections(conn, student_id)
        if role == "student" and student_id
        else []
    )

    def ms_ls1_1_objective_for_level(level: int) -> str:
        if level == 1:
            return "MS-LS1-1A"
        return "MS-LS1-1B"
    
    def get_first_objective_for_standard(std):
        if not is_launch_standard_id(std):
            return None
        objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
        rows = order_objective_rows(conn.execute(
            f"""
SELECT o.objective_id
FROM objectives o
WHERE o.standard_id = ?
  AND o.objective_id IN ({objective_placeholders})
  AND EXISTS (
      SELECT 1
      FROM questions q
      WHERE q.objective_id = o.objective_id
  )
            """,
            (std, *LAUNCH_OBJECTIVE_IDS),
        ).fetchall())
        row = rows[0] if rows else None
        return row["objective_id"] if row else None

    def get_next_objective_for_standard(std, current_objective_id):
        if not is_launch_standard_id(std) or not is_launch_objective_id(current_objective_id):
            return None
        next_objective_id = next_launch_objective_id(current_objective_id)
        if not next_objective_id:
            return None
        objective_placeholders = sql_placeholders(LAUNCH_OBJECTIVE_IDS)
        rows = order_objective_rows(conn.execute(
            f"""
SELECT o.objective_id
FROM objectives o
WHERE o.standard_id = ?
  AND o.objective_id IN ({objective_placeholders})
  AND EXISTS (
      SELECT 1
      FROM questions q
      WHERE q.objective_id = o.objective_id
  )
            """,
            (std, *LAUNCH_OBJECTIVE_IDS),
        ).fetchall())

        objective_ids = [row["objective_id"] for row in rows]
        return next_objective_id if next_objective_id in objective_ids else None

    def set_student_objective(student_id, std, objective_id):
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO student_objective_state
              (student_id, standard_id, current_objective_id, status, last_update)
            VALUES (?, ?, ?, 'active', ?)
            ON CONFLICT(student_id, standard_id) DO UPDATE SET
              current_objective_id=excluded.current_objective_id,
              status='active',
              last_update=excluded.last_update
            """,
            (student_id, std, objective_id, now),
        )
        level_row = conn.execute(
            """
            SELECT current_level FROM progress_state
            WHERE student_id = ? AND standard_id = ?
            """,
            (student_id, std),
        ).fetchone()
        ensure_routing_level_attempt(
            conn,
            student_id=student_id,
            standard_id=std,
            objective_id=objective_id,
            level=int(level_row["current_level"]) if level_row else 1,
            boundary_reason="objective_transition",
            started_at=now,
        )
        ensure_student_growth_attempt(
            conn,
            student_id,
            std,
            objective_id,
            commit=False,
        )
        conn.commit()

    def get_engine_target():
        """
        Choose the student's current standard/objective from durable placement.
        If no content exists yet, objective_id stays None.
        """
        placement = get_current_student_placement(conn, student_id)
        ps = placement["progress"]

        if ps:
            std = placement["standard_id"]
            level = placement["current_level"]

            if std == "MS-LS1-1" and ps["status"] == "completed":
                return "MS-LS1-1", level, None, ps

            if placement["objective_id"]:
                return std, level, placement["objective_id"], ps

            first_obj = get_first_objective_for_standard(std)
            if first_obj:
                set_student_objective(student_id, std, first_obj)

            return std, level, first_obj, ps

        return "MS-LS1-1", 1, "MS-LS1-1A", ps

    current_std, current_level, objective_id, progress_row = get_engine_target()
    objective_meta = conn.execute(
        """
        SELECT objective_id, objective_text, display_name, student_description
        FROM objectives WHERE objective_id=?
        """,
        (objective_id,),
    ).fetchone() if objective_id else None
    objective_display_name = (
        row_get(objective_meta, "display_name", None)
        or row_get(objective_meta, "objective_text", None)
        or "Learning Objective"
    )
    teacher_attribution = None
    if role == "student" and student_id:
        teacher_rows = conn.execute(
            """
            SELECT DISTINCT COALESCE(
              (SELECT i.display_name FROM user_auth_identities i
               WHERE i.user_id=u.id AND i.revoked_at IS NULL
               ORDER BY i.last_login_at DESC, i.identity_id DESC LIMIT 1),
              u.username
            ) AS teacher_name
            FROM class_enrollments ce
            JOIN class_sections cs ON cs.class_id=ce.class_id
            JOIN users u ON u.id=cs.teacher_user_id
            WHERE ce.student_id=? AND ce.is_active=1 AND cs.is_active=1
              AND u.is_active=1
            """,
            (student_id,),
        ).fetchall()
        if len(teacher_rows) == 1:
            teacher_attribution = teacher_rows[0]["teacher_name"]

    if role == "teacher":
        requested_objective = request.values.get("objective_id")
        if is_launch_objective_id(requested_objective):
            objective_id = requested_objective
            std_row = conn.execute(
                "SELECT standard_id FROM objectives WHERE objective_id=?",
                (objective_id,),
            ).fetchone()
            current_std = std_row["standard_id"] if std_row else current_std
            current_level = get_level_for_objective(conn, objective_id)

    growth_attempt = (
        ensure_student_growth_attempt(conn, student_id, current_std, objective_id)
        if role == "student" and student_id and objective_id
        else None
    )

    # Completion page for the temporary MS-LS1-1 walkthrough.
    if progress_row and progress_row["standard_id"] == "MS-LS1-1" and progress_row["status"] == "completed":
        html_done = """
<!doctype html>
<title>Understanding Grown</title>
<style>
  body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#f3f4f6}
  .card{max-width:700px;margin:0 auto 16px auto;background:#fff;border-radius:10px;padding:16px 20px;border:1px solid #e5e7eb}
  .btn{background:#2563eb;color:#fff;border:none;padding:8px 12px;border-radius:8px;cursor:pointer;text-decoration:none;display:inline-block}
</style>
<div class="card">
  <h2>✅ MS-LS1-1 Complete</h2>
  <section role="status" aria-label="Growing Understanding">
    <h2>Growing Understanding</h2>
    <p aria-label="Completed cumulative learning plant">🌳 Your learning plant is fully rooted.</p>
    <div role="progressbar" aria-label="Growing Understanding" aria-valuetext="This understanding has taken root." style="height:12px;background:#e1e7e2;border-radius:999px;overflow:hidden;"><div style="height:100%;width:100%;background:#2f6f4e;"></div></div>
    <p><strong>Completed</strong> — This understanding has taken root.</p>
  </section>
  <p style="font-size:13px;color:#555;">Logged in as <strong>{{ session.get('username', 'student') }}</strong></p>
  <p><a class="btn" href="{{ url_for('logout') }}" style="background:#dc2626;">Logout</a></p>
</div>
        """
        return render_template_string(html_done)

    # ---------- Handle continue after review ----------
    if request.method == "POST" and request.form.get("action") == "continue_after_review":
        session["current_mode"] = "question"
        session["locked_payload"] = None
        return redirect(url_for("student_view"))

    if request.method == "POST" and request.form.get("action") == "continue_learning":
       session["current_mode"] = "question"
       session["locked_payload"] = None
       return redirect(url_for("student_view"))
    
    if request.method == "POST" and request.form.get("action") == "return_dashboard":
       session["current_mode"] = "home"
       session["locked_payload"] = None
       session.pop("terminal_completion_payload", None)
       return redirect(url_for("student_view"))

    # ---------- Handle answer submission ----------
    if request.method == "POST" and request.form.get("action") == "answer":
        if role == "teacher":
            student_id = request.form.get("student_id") or student_id
            objective_id = request.form.get("objective_id") or objective_id

        qid = request.form.get("question_id")
        resp = request.form.get("response")

        if not qid and objective_id and role == "teacher":
            qid = get_any_question_id(conn, objective_id)

        if qid:
            if role == "student":
                consumed_delivery = consume_student_question_delivery(
                    conn,
                    submission_token=request.form.get("submission_token", ""),
                    student_id=student_id,
                    growth_attempt_id=row_get(growth_attempt, "attempt_id", ""),
                    standard_id=current_std,
                    objective_id=objective_id,
                    level=current_level,
                    submitted_question_id=qid,
                    response=resp,
                )
                if not consumed_delivery:
                    flash(
                        "That question was already submitted or is no longer active. "
                        "Your current question is shown below."
                    )
                    return redirect(url_for("student_view"))
                correct = consumed_delivery["correct"]
                std_id = consumed_delivery["standard_id"]
                lvl = consumed_delivery["level"]
                ts = consumed_delivery["timestamp"]
                qid = consumed_delivery["question_id"]
            else:
                row = conn.execute(
                    "SELECT answer_key FROM questions WHERE question_id=?",
                    (qid,),
                ).fetchone()
                correct = 1 if row and row["answer_key"] == resp else 0
                std_id, lvl, ts = record_attempt_and_response(
                    conn,
                    student_id=student_id,
                    question_id=qid,
                    response=resp,
                    is_correct=correct,
                    objective_id=objective_id,
                    attempt_prefix="ST",
                )
                conn.commit()

            submitted_growth_attempt_id = row_get(growth_attempt, "attempt_id", None)
            engine_decision = None
            engine_summary = None
            objective_transition = False

            if std_id != "UNKNOWN":
                engine_decision = ae.process_after_response(student_id, std_id)

                if (
                    isinstance(engine_decision, dict)
                    and engine_decision.get("action") in ("next_standard_found", "standard_complete")
                ):
                    next_objective = get_next_objective_for_standard(
                        std_id,
                        objective_id,
                    )

                    if next_objective:
                        objective_transition = True
                        ae.set_state(student_id, std_id, 1, "practicing", 0.0)
                        set_student_objective(student_id, std_id, next_objective)

                        engine_decision = {
                            "status": "question",
                            "action": "next_objective_found",
                            "standard": std_id,
                            "level": 1,
                            "objective": next_objective,
                            "reason": "next_objective_in_standard_found",
                        }

                if isinstance(engine_decision, dict):
                    action = engine_decision.get("action")
                    level = engine_decision.get("level")
                    std = engine_decision.get("standard")
                    reason = engine_decision.get("reason")
                    avg_val = engine_decision.get("avg")
                    parts = []
                    if action:
                        parts.append(action)
                    if std:
                        parts.append(std)
                    if level is not None:
                        parts.append(f"Level {level}")
                    if avg_val is not None:
                        parts.append(f"avg={round(avg_val, 2)}")
                    if reason:
                        parts.append(f"({reason})")
                    engine_summary = " ".join(parts)
                elif engine_decision is not None:
                    engine_summary = str(engine_decision)

                if isinstance(engine_decision, dict) and engine_decision.get("status") == "locked":
                    session["current_mode"] = "locked"
                    session["locked_payload"] = engine_decision
                elif (
                    isinstance(engine_decision, dict)
                    and engine_decision.get("status") in ("completed", "complete")
                ):
                    session["current_mode"] = "completed"
                    session["terminal_completion_payload"] = engine_decision
                    session["locked_payload"] = None
                else:
                    session["current_mode"] = "question"
                    session["locked_payload"] = None
                    session.pop("terminal_completion_payload", None)

                if submitted_growth_attempt_id:
                    update_student_growth_presentation(
                        conn,
                        submitted_growth_attempt_id,
                        correct=bool(correct),
                        engine_decision=(
                            {"status": "complete", "action": "objective_complete"}
                            if objective_transition
                            else engine_decision
                        ),
                    )

            feedback = {
                "correct": bool(correct),
                "engine_summary": engine_summary,
                "engine_decision": engine_decision,
            }

            # Re-read engine target after response processing.
            current_std, current_level, objective_id, progress_row = get_engine_target()
            objective_meta = conn.execute(
                """
                SELECT objective_id, objective_text, display_name, student_description
                FROM objectives WHERE objective_id=?
                """,
                (objective_id,),
            ).fetchone() if objective_id else None
            objective_display_name = (
                row_get(objective_meta, "display_name", None)
                or row_get(objective_meta, "objective_text", None)
                or "Learning Objective"
            )
            growth_attempt = (
                ensure_student_growth_attempt(conn, student_id, current_std, objective_id)
                if role == "student"
                and objective_id
                and session.get("current_mode") != "completed"
                else growth_attempt
            )
            if (
                objective_transition
                and row_get(growth_attempt, "attempt_id", None)
                and row_get(growth_attempt, "attempt_id", None) != submitted_growth_attempt_id
            ):
                session["growth_transition_attempt_id"] = growth_attempt["attempt_id"]
            if (
                row_get(growth_attempt, "attempt_id", None)
                and row_get(growth_attempt, "attempt_id", None) != submitted_growth_attempt_id
                and isinstance(engine_decision, dict)
                and engine_decision.get("action")
                in {"drop_level", "remediate_lower_band", "locked"}
            ):
                growth_attempt = update_student_growth_presentation(
                    conn,
                    growth_attempt["attempt_id"],
                    correct=False,
                    engine_decision=engine_decision,
                )
            if role == "student":
                session["student_feedback"] = {"correct": bool(correct)}
                return redirect(url_for("student_view"))
        else:
            if role == "student":
                flash(
                    "That question was already submitted or is no longer active. "
                    "Your current question is shown below."
                )
                return redirect(url_for("student_view"))
            feedback = {"error": "No question found for this objective."}

    if session.get("current_mode") == "completed":
        html_done = """
<!doctype html>
<title>Understanding Grown</title>
<style>
  body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#f3f4f6}
  .card{max-width:720px;margin:0 auto;background:#fff;border-radius:10px;padding:20px 24px;border:1px solid #e5e7eb}
  .btn{background:#2563eb;color:#fff;border:none;padding:10px 14px;border-radius:8px;cursor:pointer;text-decoration:none;display:inline-block}
  .muted{font-size:13px;color:#555}
</style>
<div class="card">
  <h2>🎉 Standard Complete!</h2>
  <section role="status" aria-label="Growing Understanding">
    <h2>Growing Understanding</h2>
    <p aria-label="Completed cumulative learning plant">🌳 Your learning plant is fully rooted.</p>
    <div role="progressbar" aria-label="Growing Understanding" aria-valuetext="This understanding has taken root." style="height:12px;background:#e1e7e2;border-radius:999px;overflow:hidden;"><div style="height:100%;width:100%;background:#2f6f4e;"></div></div>
    <p><strong>Completed</strong> — This understanding has taken root.</p>
  </section>
  <p>RootED has saved your learning and will keep your existing pathway ready.</p>

  <form method="post" style="margin-top:16px;">
    <input type="hidden" name="action" value="return_dashboard">
    <button class="btn" type="submit">Return to Student Dashboard</button>
  </form>
</div>
        """
        return render_template_string(html_done)

    available_standards = get_available_standards(conn)
    progress_by_standard = get_progress_by_standard(conn, student_id)
    available_standard_cards = [
        {
            "standard_id": row_get(standard, "standard_id", "Unknown standard"),
            "core_idea": row_get(standard, "core_idea", "Science"),
            "grade_band": row_get(standard, "grade_band", "Available"),
            "objective_count": row_get(standard, "objective_count", 0),
            "question_count": row_get(standard, "question_count", 0),
        }
        for standard in available_standards
    ]
    for standard in available_standard_cards:
        standard_progress = progress_by_standard.get(standard["standard_id"])
        standard["level"] = row_get(standard_progress, "current_level", None)
        standard_response_count = get_response_count_for_level(
            conn,
            student_id,
            standard["standard_id"],
            standard["level"],
        )
        standard["response_count"] = standard_response_count
        standard["rolling_avg"] = evidence_aware_rolling_avg(
            row_get(standard_progress, "rolling_avg", None),
            standard_response_count,
        )
        standard["is_locked"] = bool(row_get(standard_progress, "locked", 0))
        status = row_get(standard_progress, "status", None)
        if standard["standard_id"] == current_std:
            standard["status_label"] = "Current"
            standard["status_class"] = "current"
        elif status == "completed":
            standard["status_label"] = "Completed"
            standard["status_class"] = "complete"
        elif standard_progress:
            standard["status_label"] = "In progress"
            standard["status_class"] = "progress"
        else:
            standard["status_label"] = "Available"
            standard["status_class"] = "available"
        if standard["is_locked"]:
            standard["status_label"] = "Review"
            standard["status_class"] = "review"
    current_standard_meta = get_standard_meta(conn, current_std)
    growth_view = student_growth_view_model(
        growth_attempt,
        reviewing=session.get("current_mode") == "locked",
    )
    plant_view = cumulative_plant_view_model(conn, student_id, current_std)
    current_core_idea = row_get(current_standard_meta, "core_idea", "Science")
    current_grade_band = row_get(current_standard_meta, "grade_band", "Current band")

    if role == "student" and session.get("current_mode") == "home":
        home_html = """
<!doctype html>
<title>RootED - Student Home</title>
<style>
  :root{
    --ink:#1f2933;
    --muted:#5f6f64;
    --leaf:#2f6f4e;
    --leaf-dark:#24543d;
    --moss:#dce9d5;
    --sprout:#f3f8ee;
    --gold:#c98f35;
    --sky:#e6f0f6;
    --line:#d9e2d4;
    --paper:#fffefa;
    --shadow:0 20px 50px rgba(31,41,51,.11);
  }
  *{box-sizing:border-box}
  body{
    margin:0;
    min-height:100vh;
    font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, Helvetica, sans-serif;
    color:var(--ink);
    background:
      radial-gradient(circle at 12% 10%, rgba(220,233,213,.84), transparent 28%),
      linear-gradient(135deg, #fffdf8 0%, #f3f8ee 48%, #e6f0f6 100%);
  }
  body:before{
    content:"";
    position:fixed;
    inset:0;
    pointer-events:none;
    background:
      linear-gradient(90deg, rgba(47,111,78,.06) 1px, transparent 1px),
      linear-gradient(180deg, rgba(47,111,78,.05) 1px, transparent 1px);
    background-size:56px 56px;
    mask-image:linear-gradient(to bottom, rgba(0,0,0,.42), transparent 72%);
  }
  .shell{position:relative;width:min(1080px, calc(100% - 40px));margin:0 auto;padding:28px 0 42px}
  .topbar{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:18px}
  .brand{display:flex;align-items:center;gap:12px;color:var(--leaf-dark);font-weight:800}
  .brand img{width:58px;height:58px;object-fit:contain;border-radius:8px;background:#fffdf8;box-shadow:0 12px 24px rgba(47,111,78,.14)}
  .brand span{font-size:20px}
  .btn{border:none;border-radius:8px;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;font-weight:750}
  .btn-danger{min-height:40px;padding:9px 12px;background:rgba(255,255,255,.72);color:#8a2f2f;border:1px solid rgba(138,47,47,.22)}
  .hero-card,.card{background:rgba(255,255,255,.88);border:1px solid rgba(47,111,78,.16);border-radius:8px;box-shadow:var(--shadow)}
  .hero-card{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(300px,.8fr);gap:22px;padding:28px;margin-bottom:16px}
  .eyebrow{margin:0 0 8px;color:var(--leaf);font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.06em}
  h1,h2,h3,p{letter-spacing:0}
  h1{margin:0;font-size:clamp(34px,5vw,58px);line-height:1;color:#183629}
  .lead{margin:12px 0 0;color:#304037;font-size:17px;line-height:1.55;max-width:660px}
  .current-panel{background:linear-gradient(135deg, rgba(47,111,78,.1), rgba(201,143,53,.1));border:1px solid var(--line);border-radius:8px;padding:18px}
  .standard-code{font-size:28px;font-weight:850;color:#183629;margin:2px 0 6px}
  .muted{font-size:13px;color:var(--muted)}
  .status-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0}
  .pill{display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:800}
  .pill-current{background:#dce9d5;color:#24543d}
  .pill-complete{background:#e7f7ee;color:#0f6b3a}
  .pill-progress{background:#e6f0f6;color:#24506d}
  .pill-available{background:#f4efe5;color:#7a5729}
  .pill-review{background:#fff0cf;color:#7a4d00}
  .growth{margin-top:14px;background:rgba(255,255,255,.72);border:1px solid rgba(47,111,78,.13);border-radius:8px;padding:14px}
  .growth-head{display:flex;justify-content:space-between;gap:12px;align-items:center}
  .growth-title{font-weight:850;color:#1f3d2f}
  .growth-status{font-weight:800}
  .growth-focus{margin:10px 0 0;color:#536259}
  .growth-focus-label{display:block;font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase}
  .growth-focus-text{display:block;margin-top:2px;font-size:13px;font-weight:500;line-height:1.4}
  .plant-summary{display:flex;align-items:center;gap:10px;margin:12px 0;padding:10px;border-radius:8px;background:#f3f8ee}
  .plant-visual{min-width:54px;font-size:24px;letter-spacing:-5px}.plant-visual span{display:none}
  .plant-sprout .sprout,.plant-first-branch .sprout,.plant-first-branch .leaf-one,
  .plant-second-branch .sprout,.plant-second-branch .leaf-one,.plant-second-branch .leaf-two,
  .plant-leafy .leafy,.plant-canopy .canopy{display:inline}
  .plant-copy{font-size:13px;color:#405348}
  .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
  .growth-track{height:12px;background:#e1e7e2;border-radius:999px;overflow:hidden;margin:10px 0}
  .growth-fill{height:100%;border-radius:inherit;background:#c98f35}
  .growth.growing .growth-fill,.growth.completed .growth-fill{background:#2f6f4e}
  .growth.reviewing .growth-fill{background:#49657a}
  .growth-seed{width:8%}.growth-root{width:18%}.growth-shoot{width:28%}.growth-leaf{width:38%}
  .growth-branch{width:48%}.growth-bud{width:58%}.growth-canopy{width:68%}.growth-ready{width:78%}
  .growth-nearly{width:88%}.growth-strong{width:94%}.growth-complete{width:100%}
  .cta-panel{display:flex;flex-direction:column;justify-content:space-between;gap:16px;background:#fffefa;border:1px solid var(--line);border-radius:8px;padding:18px}
  .cta-panel h2{margin:0;color:#183629;font-size:24px}
  .cta-panel p{margin:8px 0 0;color:#4d5e55;line-height:1.5}
  .cta-button{width:100%;min-height:52px;padding:13px 16px;background:var(--leaf);color:#fff;box-shadow:0 14px 24px rgba(47,111,78,.18);font-size:16px}
  .cta-button:hover{background:var(--leaf-dark)}
  .join-card{border-top:1px solid var(--line);padding-top:16px}
  .join-card h3{margin:0;color:#183629;font-size:20px}
  .join-form{display:flex;gap:8px;align-items:end;margin-top:10px}
  .join-form label{display:block;flex:1;color:#40524a;font-size:13px;font-weight:750}
  .join-form input{width:100%;margin-top:5px;padding:10px 11px;border:1px solid #cbd5c7;border-radius:8px;background:#fff;font:inherit;text-transform:uppercase}
  .join-button{min-height:41px;padding:10px 12px;background:var(--leaf-dark);color:#fff}
  .class-list{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
  .class-chip{display:inline-flex;align-items:center;background:#e7f7ee;color:#0f6b3a;border:1px solid #a8e0bf;border-radius:999px;padding:4px 8px;font-size:12px;font-weight:800}
  .card{padding:20px 22px;margin-bottom:16px}
  .section-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:14px}
  .section-head h2{margin:0;color:#183629;font-size:24px}
  .standards-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
  .standard-card{border:1px solid var(--line);border-radius:8px;background:rgba(255,254,250,.78);padding:14px}
  .standard-card-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
  .standard-card h3{margin:0;color:#183629;font-size:18px}
  .standard-card p{margin:8px 0 0;color:var(--muted);font-size:13px;line-height:1.45}
  .standard-meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;color:#40524a;font-size:12px}
  .empty-state{border:1px dashed rgba(47,111,78,.35);border-radius:8px;background:rgba(255,255,255,.58);padding:18px;color:var(--muted);line-height:1.5}
  .flash-box{background:#e7f7ee;border:1px solid #a8e0bf;color:#0f6b3a;padding:10px 12px;border-radius:8px;margin:0 0 16px;font-size:14px}
  @media(max-width:820px){.shell{width:min(100% - 28px, 760px);padding-top:18px}.hero-card{grid-template-columns:1fr}.standards-grid{grid-template-columns:1fr}.stats{grid-template-columns:1fr}}
  @media(max-width:520px){.topbar{align-items:flex-start}.brand img{width:48px;height:48px}.hero-card,.card{padding:16px}.section-head{display:block}.btn-danger{margin-top:8px}}
</style>

<div class="shell">
  <div class="topbar">
    <div class="brand" aria-label="RootED">
      <img src="{{ url_for('static', filename='Logo.png') }}" alt="RootED logo">
      <span>RootED</span>
    </div>
    <form action="{{ url_for('logout') }}" method="get" style="margin:0;">
      <button type="submit" class="btn btn-danger">Logout</button>
    </form>
  </div>

  {% with msgs = get_flashed_messages() %}
    {% if msgs %}
      <div class="flash-box">
        {% for m in msgs %}
          <div>{{ m }}</div>
        {% endfor %}
      </div>
    {% endif %}
  {% endwith %}

  <section class="hero-card" aria-label="Student learning overview">
    <div>
      <p class="eyebrow">Student Home</p>
      <h1>Keep growing from here.</h1>
      <p class="lead">
        You are signed in as <strong>{{ session.get('username', 'student') }}</strong>.
        RootED will continue at the level that matches your current progress.
      </p>
      <div class="current-panel" style="margin-top:20px;">
        <div class="muted">Current assigned standard</div>
        <div class="standard-code">{{ current_std }}</div>
        {% if current_standard_meta %}
          <div class="muted">{{ current_core_idea }} | {{ current_grade_band }}</div>
        {% endif %}
        <div class="growth {{ growth_view.state }}" role="status" aria-live="polite">
          <div class="growth-head">
            <span class="growth-title">🌱 Growing Understanding</span>
            <span class="growth-status">{{ growth_view.label }}</span>
          </div>
          <p class="growth-focus">
            <span class="growth-focus-label">Learning Goal</span>
            <span class="growth-focus-text">{{ objective_display_name }}</span>
          </p>
          <div class="plant-summary">
            <span class="plant-visual {{ plant_view.plant_class }}" aria-hidden="true">
              <span class="sprout">🌱</span><span class="leaf-one">🍃</span><span class="leaf-two">🍃</span>
              <span class="leafy">🌿</span><span class="canopy">🌳</span>
            </span>
            <span class="plant-copy">{{ plant_view.visible_text }}</span>
            <span class="sr-only">{{ plant_view.screen_reader_text }}</span>
          </div>
          <div class="growth-track" role="progressbar" aria-label="Growing Understanding" aria-valuetext="{{ growth_view.message }}">
            <div class="growth-fill {{ growth_view.stage_class }}"></div>
          </div>
          <div>{{ growth_view.message }}</div>
        </div>
      </div>
    </div>

    <aside class="cta-panel" aria-label="Continue learning">
      <div>
        <h2>Ready for the next question?</h2>
        <p>Continue Learning opens the existing practice flow without changing your assigned route.</p>
      </div>
      <form method="post" style="margin:0;">
        <input type="hidden" name="action" value="continue_learning">
        <button class="btn cta-button" type="submit">Continue Learning</button>
      </form>
      <div class="join-card" aria-label="Join a class">
        <div>
          <h3>Join a Class</h3>
          <p>Enter the class code your teacher shared.</p>
        </div>
        <form method="post" class="join-form">
          <input type="hidden" name="action" value="join_class">
          <label>Class code
            <input name="join_code" placeholder="ABC123" autocomplete="off">
          </label>
          <button class="btn join-button" type="submit">Join</button>
        </form>
        {% if enrolled_classes %}
          <div class="class-list" aria-label="Joined classes">
            {% for c in enrolled_classes %}
              <span class="class-chip">{{ c['name'] }}</span>
            {% endfor %}
          </div>
        {% endif %}
      </div>
    </aside>
  </section>

  <div class="card">
    <div class="section-head">
      <div>
        <h2>Available Standards</h2>
        <p class="muted" style="margin:6px 0 0;">Current, completed, and in-progress labels use existing progress data.</p>
      </div>
    </div>
    {% if available_standards %}
      <div class="standards-grid">
        {% for standard in available_standards %}
          <section class="standard-card">
            <div class="standard-card-head">
              <h3>{{ standard.standard_id }}</h3>
              <span class="pill pill-{{ standard.status_class }}">{{ standard.status_label }}</span>
            </div>
            <p>{{ standard.core_idea }} | {{ standard.grade_band }}</p>
            <div class="standard-meta">
              <span>{{ standard.objective_count }} objectives</span>
            </div>
            {% if standard.question_count == 0 %}
              <p class="muted">Questions are not available for this standard yet.</p>
            {% endif %}
          </section>
        {% endfor %}
      </div>
    {% else %}
      <div class="empty-state">
        Standards will appear here after learning content is available.
      </div>
    {% endif %}
  </div>
</div>
        """
        return render_template_string(
            home_html,
            available_standards=available_standard_cards,
            current_standard_meta=current_standard_meta,
            current_std=current_std,
            current_level=current_level,
            current_core_idea=current_core_idea,
            current_grade_band=current_grade_band,
            growth_view=growth_view,
            plant_view=plant_view,
            objective_display_name=objective_display_name,
            enrolled_classes=enrolled_classes,
        )

    locked_review = session.get("locked_payload") if session.get("current_mode") == "locked" else None
    growth_view = student_growth_view_model(growth_attempt, reviewing=bool(locked_review))
    plant_view = cumulative_plant_view_model(conn, student_id, current_std)
    transition_announcement = (
        session.pop("growth_transition_attempt_id", None)
        == row_get(growth_attempt, "attempt_id", None)
    )
    current_question = None
    current_delivery = None
    current_model_asset = None

    if not locked_review and objective_id:
        qrows = get_questions_for_objective(conn, objective_id)

        if qrows:
            total = len(qrows)

            if total >= 21:
                level_1_rows = qrows[:7]
                level_2_rows = qrows[7:14]
                level_3_rows = qrows[14:21]

            elif total >= 6:
                level_1_rows = qrows[:3]
                level_2_rows = qrows[3:5]
                level_3_rows = qrows[5:]

            else:
                level_1_rows = qrows
                level_2_rows = qrows
                level_3_rows = qrows

            if current_level == 1:
                level_qrows = level_1_rows
            elif current_level == 2:
                level_qrows = level_2_rows if level_2_rows else level_1_rows
            else:
                level_qrows = level_3_rows if level_3_rows else level_2_rows or level_1_rows

            if level_qrows:
                if role == "student" and growth_attempt:
                    current_delivery, current_question = (
                        get_or_create_student_question_delivery(
                            conn,
                            student_id=student_id,
                            growth_attempt_id=growth_attempt["attempt_id"],
                            standard_id=current_std,
                            objective_id=objective_id,
                            level=current_level,
                            eligible_questions=level_qrows,
                        )
                    )
                else:
                    qids = [row["question_id"] for row in level_qrows]
                    placeholders = ",".join(["?"] * len(qids))
                    attempt_row = conn.execute(
                        f"""
                        SELECT COUNT(*) AS n
                        FROM attempts
                        WHERE student_id = ?
                          AND question_id IN ({placeholders})
                        """,
                        [student_id] + qids,
                    ).fetchone()
                    attempt_count = attempt_row["n"] if attempt_row else 0
                    question_index = attempt_count % len(level_qrows)
                    current_question = level_qrows[question_index]
                resolved_asset = resolve_model_asset_for_question(
                    conn,
                    current_question["question_id"],
                )
                current_model_asset = static_image_asset_for_render(resolved_asset)

    student_html = """
<!doctype html>
<title>RootED Learning</title>
<style>
  body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#f3f4f6}
  .card{max-width:700px;margin:0 auto 16px auto;background:#fff;
        border-radius:10px;padding:16px 20px;border:1px solid #e5e7eb}
  .header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
  .btn{background:#2f6f4e;color:#fff;border:none;padding:9px 13px;border-radius:8px;cursor:pointer;text-decoration:none;display:inline-block}
  .choice{margin:8px 0;padding:8px;border:1px solid #e5e7eb;border-radius:8px}
  .ok{color:#16a34a}
  .bad{color:#dc2626}
  input,select,textarea{padding:6px 8px;border:1px solid #cbd5e1;border-radius:6px}
  textarea{width:100%;min-height:58px;font:inherit}
  .toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
  .report-toggle{margin-top:14px;background:transparent;color:#4b5563;border:1px solid #d1d5db;padding:5px 8px;border-radius:6px;font-size:12px;cursor:pointer}
  .flag-panel[hidden]{display:none}
  .flag-panel{border:1px solid #e5e7eb;border-radius:8px;background:#f9fafb;padding:10px;margin-top:8px}
  .flag-form{display:grid;gap:8px;margin-top:10px}
  .btn-flag{background:#eef2f7;color:#374151;border:1px solid #cbd5e1}
  .model-asset{margin:14px 0 16px 0;padding:12px;border:1px solid #d7dee8;border-radius:8px;background:#f8fafc}
  .model-asset-title{margin:0 0 8px 0;font-weight:700;color:#1f2937}
  .model-asset-caption{margin:8px 0 0 0;font-size:13px;color:#555;line-height:1.4}
  .model-asset img{display:block;max-width:100%;height:auto;margin:0 auto;border-radius:6px}
  .enrollment-box{border:1px solid #d7dee8;border-radius:8px;background:#f8fafc;padding:12px;margin:12px 0}
  .class-chip{display:inline-block;background:#e7f7ee;color:#0f6b3a;border:1px solid #a8e0bf;border-radius:999px;padding:4px 8px;margin:3px;font-size:12px}
  .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
  :focus-visible{outline:3px solid #c98f35;outline-offset:3px}
  .learning-meta{color:#55645b;font-size:14px;line-height:1.5}
  .growth{margin:14px 0 18px;border:1px solid #d7e2d5;border-radius:10px;padding:14px;background:#fbfdf9}
  .growth-head{display:flex;justify-content:space-between;align-items:center;gap:12px}
  .growth-title{font-weight:800;color:#234b35}.growth-status{font-weight:800;color:#8a611e}
  .growth-focus{margin:10px 0 0;color:#536259}
  .growth-focus-label{display:block;font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase}
  .growth-focus-text{display:block;margin-top:2px;font-size:13px;font-weight:500;line-height:1.4}
  .assessment{margin-top:30px;padding-top:24px;border-top:1px solid #e2e8e3}
  .assessment-label{margin:0 0 7px;color:#637067;font-size:12px;font-weight:800;letter-spacing:.06em;text-transform:uppercase}
  .assessment-question{margin:0 0 18px;color:#17261e;font-size:clamp(22px,3.4vw,30px);line-height:1.3;font-weight:800}
  .transition-note{margin:10px 0;padding:9px 11px;border-left:4px solid #2f6f4e;background:#eef6ea;color:#234b35;font-weight:700}
  .plant-summary{display:flex;align-items:center;gap:10px;margin:12px 0;padding:10px;border-radius:8px;background:#f3f8ee}
  .plant-visual{min-width:54px;font-size:24px;letter-spacing:-5px}.plant-visual span{display:none}
  .plant-sprout .sprout,.plant-first-branch .sprout,.plant-first-branch .leaf-one,
  .plant-second-branch .sprout,.plant-second-branch .leaf-one,.plant-second-branch .leaf-two,
  .plant-leafy .leafy,.plant-canopy .canopy{display:inline}
  .plant-copy{font-size:13px;color:#405348}
  .growth.growing .growth-status,.growth.completed .growth-status{color:#2f6f4e}
  .growth.reviewing .growth-status{color:#49657a}
  .growth-track{height:12px;background:#e1e7e2;border-radius:999px;overflow:hidden;margin:10px 0}
  .growth-fill{height:100%;border-radius:inherit;background:#c98f35}
  .growth.growing .growth-fill,.growth.completed .growth-fill{background:#2f6f4e}
  .growth.reviewing .growth-fill{background:#49657a}
  .growth-seed{width:8%}.growth-root{width:18%}.growth-shoot{width:28%}.growth-leaf{width:38%}
  .growth-branch{width:48%}.growth-bud{width:58%}.growth-canopy{width:68%}.growth-ready{width:78%}
  .growth-nearly{width:88%}.growth-strong{width:94%}.growth-complete{width:100%}
  @media(max-width:600px){body{margin:12px}.card{padding:14px}.header{align-items:flex-start;gap:12px}}
</style>

<div class="card">
  <div class="header">
    <div>
      <h2 style="margin:0;color:#234b35;">RootED Learning</h2>
      <p style="font-size:13px;color:#555;margin:2px 0 0 0;">
        👩‍🎓 Logged in as <strong>{{ session.get('username', 'student') }}</strong>
        {% if teacher_attribution %}<br>Assigned by {{ teacher_attribution }}{% endif %}
      </p>
    </div>
    <a class="btn" href="{{ url_for('student_view', home=1) }}">Back to Home</a>
  </div>

  <section class="growth {{ growth_view.state }}" role="status" aria-live="polite" aria-label="Growing Understanding">
    <div class="growth-head">
      <span class="growth-title">🌱 Growing Understanding</span>
      <span class="growth-status">{{ growth_view.label }}</span>
    </div>
    <p class="growth-focus">
      <span class="growth-focus-label">Learning Goal</span>
      <span class="growth-focus-text">{{ objective_display_name }}</span>
    </p>
    {% if transition_announcement %}
      <p class="transition-note">A new branch of learning is beginning.</p>
    {% endif %}
    <div class="plant-summary">
      <span class="plant-visual {{ plant_view.plant_class }}" aria-hidden="true">
        <span class="sprout">🌱</span><span class="leaf-one">🍃</span><span class="leaf-two">🍃</span>
        <span class="leafy">🌿</span><span class="canopy">🌳</span>
      </span>
      <span class="plant-copy">{{ plant_view.visible_text }}</span>
      <span class="sr-only">{{ plant_view.screen_reader_text }}</span>
    </div>
    <div class="growth-track" role="progressbar" aria-label="Growing Understanding" aria-valuetext="{{ growth_view.message }}">
      <div class="growth-fill {{ growth_view.stage_class }}"></div>
    </div>
    <div>{{ growth_view.message }}</div>
  </section>

  {% with msgs = get_flashed_messages() %}
    {% if msgs %}
      <div style="background:#e7f7ee;border:1px solid #a8e0bf;color:#0f6b3a;padding:10px 12px;border-radius:8px;margin:10px 0;font-size:14px;">
        {% for m in msgs %}
          <div>✅ {{ m }}</div>
        {% endfor %}
      </div>
    {% endif %}
  {% endwith %}

  {% if false %}
    <div class="enrollment-box">
      <form method="post" class="toolbar" style="margin:0;">
        <input type="hidden" name="action" value="join_class">
        <label>Class code
          <input name="join_code" placeholder="ABC123" autocomplete="off">
        </label>
        <button class="btn" type="submit">Join Class</button>
      </form>
      {% if enrolled_classes %}
        <div style="font-size:13px;color:#555;margin-top:8px;">
          Enrolled:
          {% for c in enrolled_classes %}
            <span class="class-chip">{{ c['name'] }}</span>
          {% endfor %}
        </div>
      {% endif %}
    </div>
  {% endif %}

  <form method="get" class="toolbar">
    {% if session.get('role') == 'teacher' %}
      <label>Student
        <select name="student_id" onchange="this.form.submit()">
          {% for s in students %}
            <option value="{{s['student_id']}}" {% if s['student_id']==student_id %}selected{% endif %}>
              {{s['student_id']}}
            </option>
          {% endfor %}
        </select>
      </label>
      <label>Objective
        <select name="objective_id" onchange="this.form.submit()">
          {% for o in objs %}
            <option value="{{o['objective_id']}}" {% if o['objective_id']==objective_id %}selected{% endif %}>
              {{o['objective_id']}}
            </option>
          {% endfor %}
        </select>
      </label>
    {% endif %}
    <noscript><button class="btn" type="submit">Go</button></noscript>
  </form>

  {% if feedback %}
    {% if feedback.error %}
      <p class="bad"><strong>{{feedback.error}}</strong></p>
    {% else %}
      {% if feedback.correct %}
        <p class="ok"><strong>✅ Correct!</strong></p>
      {% else %}
        <p class="bad"><strong>❌ Not yet. Keep trying!</strong></p>
      {% endif %}

      {% if feedback.engine_summary and session.get('role') == 'teacher' %}
        <p style="font-size:13px;color:#555;">
          Engine (standard-level): {{ feedback.engine_summary }}
        </p>
      {% endif %}
      {% if session.get('role') == 'teacher' %}<p style="font-size:13px;color:#555;">
        Debug target: objective={{ objective_id }}
      </p>{% endif %}
      <hr>
    {% endif %}
  {% endif %}

  {% if locked_review %}
    <div style="border: 1px solid #d9c27a; background: #fff8e1; padding: 16px; border-radius: 10px; margin-bottom: 20px;">
      <h3 style="margin-top: 0;">You need to review this concept before continuing.</h3>
      {% if locked_review.mini_lesson %}
        <h4>{{ locked_review.mini_lesson.title or "Quick Review" }}</h4>
        {% if locked_review.mini_lesson.objective_text %}
          <p><strong>Focus Skill:</strong> {{ locked_review.mini_lesson.objective_text }}</p>
        {% endif %}
        {% if locked_review.mini_lesson.summary %}
          <p>{{ locked_review.mini_lesson.summary }}</p>
        {% endif %}
        {% if locked_review.mini_lesson.key_points %}
          <ul>
            {% for point in locked_review.mini_lesson.key_points %}
              <li>{{ point }}</li>
            {% endfor %}
          </ul>
        {% endif %}
      {% endif %}
      <form method="post">
        <input type="hidden" name="action" value="continue_after_review">
        <button class="btn" type="submit">Continue After Review</button>
      </form>
    </div>
  {% endif %}

  {% if current_question %}
    <section class="assessment" aria-labelledby="question-prompt">
    <p class="assessment-label">Assessment question</p>
    <h2 class="assessment-question" id="question-prompt">{{current_question['stem']}}</h2>
    {% if current_model_asset %}
      <figure class="model-asset">
        {% if current_model_asset.title %}
          <figcaption class="model-asset-title">{{ current_model_asset.title }}</figcaption>
        {% endif %}
        <img src="{{ url_for('static', filename=current_model_asset.filename) }}" alt="{{ current_model_asset.alt_text }}">
        {% if current_model_asset.caption %}
          <p class="model-asset-caption">{{ current_model_asset.caption }}</p>
        {% endif %}
        {% if current_model_asset.alt_text %}
          <span class="sr-only">Image description: {{ current_model_asset.alt_text }}</span>
        {% endif %}
      </figure>
    {% endif %}
    <form method="post" aria-labelledby="question-prompt">
      <input type="hidden" name="action" value="answer">
      {% if session.get('role') == 'teacher' %}
      <input type="hidden" name="student_id" value="{{student_id}}">
      <input type="hidden" name="objective_id" value="{{objective_id}}">
      {% endif %}
      <input type="hidden" name="question_id" value="{{current_question['question_id']}}">
      {% if session.get('role') == 'student' and current_delivery %}
      <input type="hidden" name="submission_token" value="{{ current_delivery['submission_token'] }}">
      {% endif %}

      <div class="choice"><label><input type="radio" name="response" value="A" required> A. {{current_question['choice_a']}}</label></div>
      <div class="choice"><label><input type="radio" name="response" value="B"> B. {{current_question['choice_b']}}</label></div>
      <div class="choice"><label><input type="radio" name="response" value="C"> C. {{current_question['choice_c']}}</label></div>
      <div class="choice"><label><input type="radio" name="response" value="D"> D. {{current_question['choice_d']}}</label></div>

      <p><button class="btn" type="submit">Submit Answer</button></p>
    </form>
    <button class="report-toggle" type="button" aria-expanded="false" aria-controls="student-report-panel" onclick="toggleReportPanel('student-report-panel', this)">Report a problem</button>
    <div class="flag-panel" id="student-report-panel" hidden>
      <form class="flag-form" method="post" action="{{ url_for('submit_question_flag') }}">
        <input type="hidden" name="question_id" value="{{ current_question['question_id'] }}">
        <input type="hidden" name="page_context" value="student_question_view">
        <input type="hidden" name="next" value="{{ url_for('student_view') }}">
        <label>Category
          <select name="category" required>
            {% for code, label in flag_categories %}
              <option value="{{ code }}">{{ label }}</option>
            {% endfor %}
          </select>
        </label>
        <label>Comment
          <textarea name="comment" maxlength="{{ flag_comment_max_length }}" placeholder="Optional note"></textarea>
        </label>
        <div>
          <button class="btn btn-flag" type="submit">Submit</button>
          <button class="btn" type="button" style="background:#4b5563;" onclick="closeReportPanel('student-report-panel')">Cancel</button>
        </div>
      </form>
    </div>
    </section>
  {% else %}
    <p><em>No questions are available for this objective yet.</em></p>
  {% endif %}
</div>
<script>
  function toggleReportPanel(panelId, button){
    const panel = document.getElementById(panelId);
    const isOpening = panel.hasAttribute('hidden');
    panel.toggleAttribute('hidden', !isOpening);
    button.setAttribute('aria-expanded', String(isOpening));
    if(isOpening){
      const firstField = panel.querySelector('select, textarea, button');
      if(firstField){ firstField.focus(); }
    }
  }
  function closeReportPanel(panelId){
    const panel = document.getElementById(panelId);
    const button = document.querySelector('[aria-controls="' + panelId + '"]');
    panel.setAttribute('hidden', '');
    if(button){
      button.setAttribute('aria-expanded', 'false');
      button.focus();
    }
  }
</script>
    """
    return render_template_string(
        student_html,
        students=students,
        objs=objs,
        student_id=student_id,
        objective_id=objective_id,
        current_question=current_question,
        current_delivery=current_delivery,
        current_model_asset=current_model_asset,
        feedback=feedback,
        locked_review=locked_review,
        current_level=current_level,
        objective_display_name=objective_display_name,
        teacher_attribution=teacher_attribution,
        growth_view=growth_view,
        plant_view=plant_view,
        transition_announcement=transition_announcement,
        enrolled_classes=enrolled_classes,
        flag_categories=QUESTION_FLAG_CATEGORIES,
        flag_comment_max_length=QUESTION_FLAG_COMMENT_MAX_LENGTH,
    )

# ---------- Diagnostic Arena ----------
@app.post("/diagnostic/start")
@require_teacher
def diagnostic_start():
    """
    Teacher starts a diagnostic session for a given student_id.
    Redirects to the diagnostic session page.
    """
    conn = get_conn()

    student_id = (request.form.get("student_id") or "").strip()
    if not student_id:
        flash("Missing student_id for diagnostic start.")
        return redirect(url_for("index"))

    # Ensure student exists (optional, but prevents FK issues later)
    row = conn.execute("SELECT 1 FROM students WHERE student_id=?", (student_id,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO students (student_id, first_name, last_name, grade, class_period) VALUES (?, ?, ?, ?, ?)",
            (student_id, "", "", None, None),
        )
        conn.commit()

    session_id = create_diagnostic_session(conn, student_id)

    flash(f"Diagnostic session started for {student_id}. Session: {session_id[:8]}...")
    return redirect(url_for("diagnostic_session_v2", session_id=session_id))

@app.route("/diagnostic/<session_id>", methods=["GET", "POST"])
@require_teacher
def diagnostic_session(session_id):
    """
    Simple diagnostic loop:
    - GET: show next unanswered diagnostic item
    - POST: record response, then reload to next item
    """
    conn = get_conn()

    # Confirm session exists
    sess = conn.execute(
        "SELECT * FROM diagnostic_sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    if not sess:
        flash("Diagnostic session not found.")
        return redirect(url_for("index"))

    student_id = sess["student_id"]

    # On submit, record response
    if request.method == "POST":
        diagnostic_item_id = request.form.get("diagnostic_item_id")
        is_correct = request.form.get("is_correct")  # "1" or "0"

        if diagnostic_item_id is not None and is_correct in ("0", "1"):
            conn.execute(
                """
                INSERT INTO diagnostic_responses (session_id, diagnostic_item_id, is_correct)
                VALUES (?, ?, ?)
                """,
                (session_id, int(diagnostic_item_id), int(is_correct)),
            )
            conn.commit()

        return redirect(url_for("diagnostic_session_v2", session_id=session_id))

    # Find the next diagnostic_item not yet answered in this session
    next_item = conn.execute(
        """
        SELECT di.id, di.question_id, di.domain, q.stem, q.choice_a, q.choice_b, q.choice_c, q.choice_d
        FROM diagnostic_items di
        JOIN questions q ON q.question_id = di.question_id
        LEFT JOIN diagnostic_responses dr
          ON dr.diagnostic_item_id = di.id AND dr.session_id = ?
        WHERE dr.id IS NULL
        ORDER BY di.id
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()

    # If none left, mark session complete
    if not next_item:
        conn.execute(
            """
            UPDATE diagnostic_sessions
            SET status='completed', completed_at=?
            WHERE id=?
            """,
            (int(time.time()), session_id),
        )
        conn.commit()

        summary = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) AS correct
            FROM diagnostic_responses
            WHERE session_id=?
            """,
            (session_id,),
        ).fetchone()

        total = int(summary["total"] or 0)
        correct = int(summary["correct"] or 0)

        done_html = """
        <!doctype html>
        <title>Diagnostic Complete</title>
        <div style="font-family:Arial;margin:24px;">
          <h2>✅ Diagnostic Complete</h2>
          <p><strong>Student:</strong> {{ student_id }}</p>
          <p><strong>Score:</strong> {{ correct }} / {{ total }}</p>
          <p><a href="{{ url_for('index') }}">⬅ Back to dashboard</a></p>
        </div>
        """
        return render_template_string(done_html, student_id=student_id, total=total, correct=correct)

    # Render the question
    html = """
    <!doctype html>
    <title>Diagnostic Session</title>
    <div style="font-family:Arial;margin:24px;max-width:760px;">
      <h2>View Diagnostic Session</h2>
      <p><strong>Student:</strong> {{ student_id }} | <strong>Domain:</strong> {{ item['domain'] }}</p>
      <hr>
      <h3>{{ item['stem'] }}</h3>

      <ol type="A">
        <li>{{ item['choice_a'] }}</li>
        <li>{{ item['choice_b'] }}</li>
        <li>{{ item['choice_c'] }}</li>
        <li>{{ item['choice_d'] }}</li>
      </ol>

      <form method="post" style="margin-top:16px;">
        <input type="hidden" name="diagnostic_item_id" value="{{ item['id'] }}">
        <button type="submit" name="is_correct" value="1" style="padding:8px 12px;border-radius:8px;border:none;background:#16a34a;color:#fff;cursor:pointer;">
          ✅ Correct
        </button>
        <button type="submit" name="is_correct" value="0" style="padding:8px 12px;border-radius:8px;border:none;background:#dc2626;color:#fff;cursor:pointer;margin-left:8px;">
          ❌ Incorrect
        </button>
      </form>

      <p style="margin-top:18px;font-size:12px;color:#666;">
        Session: {{ session_id }}
      </p>
      <p><a href="{{ url_for('index') }}">⬅ Back to dashboard</a></p>
    </div>
    """
    return render_template_string(html, session_id=session_id, student_id=student_id, item=next_item)

# ---------- Diagnostic Arena (MVP) ----------
@app.route("/diagnostic", methods=["GET", "POST"])
@require_teacher
def diagnostic_home():
    conn = get_conn()

    # simple domain list (later we can make this dynamic)
    domains = ["LS", "PS", "ESS"]

    students = get_students(conn)
    selected_student = request.values.get("student_id") or (students[0]["student_id"] if students else "")
    selected_domain = request.values.get("domain") or "LS"

    if request.method == "POST":
        # start a session
        if not selected_student:
            flash("No student found to start a diagnostic.")
            return redirect(url_for("diagnostic_home"))

        session_id = create_diagnostic_session(conn, selected_student)
        # (Optional) We could filter by domain here, but MVP just starts session and serves items table order.
        flash(f"Diagnostic started for {selected_student}. Session: {session_id[:8]}")
        return redirect(url_for("diagnostic_session_v2", session_id=session_id))

    html = """
    <!doctype html>
    <title>Diagnostic Arena</title>
    <style>
      body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#f3f4f6;}
      .card{max-width:700px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;}
      .btn{background:#2563eb;color:#fff;border:none;padding:8px 12px;border-radius:8px;cursor:pointer}
      input,select{padding:6px 8px;border:1px solid #cbd5e1;border-radius:6px}
      .row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
      a{color:#2563eb;text-decoration:none}
    </style>

    <div class="card">
      <h2 style="margin-top:0;">View Diagnostic Arena (MVP)</h2>
      <p style="color:#555;font-size:13px;margin-top:0;">
        Start a diagnostic session and record results.
      </p>

      {% with msgs = get_flashed_messages() %}
        {% if msgs %}
          <div style="background:#e7f7ee;border:1px solid #a8e0bf;color:#0f6b3a;padding:10px 12px;border-radius:8px;margin:10px 0;font-size:14px;">
            {% for m in msgs %}<div>✅ {{ m }}</div>{% endfor %}
          </div>
        {% endif %}
      {% endwith %}

      <form method="post" class="row">
        <label>Student<br>
          <select name="student_id" required>
            {% for s in students %}
              <option value="{{s['student_id']}}" {% if s['student_id']==selected_student %}selected{% endif %}>
                {{s['student_id']}} — {{ display_name(s) }}
              </option>
            {% endfor %}
          </select>
        </label>

        <label>Domain<br>
          <select name="domain">
            {% for d in domains %}
              <option value="{{d}}" {% if d==selected_domain %}selected{% endif %}>{{d}}</option>
            {% endfor %}
          </select>
        </label>

        <button class="btn" type="submit">Start Diagnostic</button>
      </form>

      <p style="margin-top:14px;">
        <a href="{{ url_for('index') }}">⬅ Back to dashboard</a>
      </p>
    </div>
    """
    return render_template_string(
        html,
        students=students,
        domains=domains,
        selected_student=selected_student,
        selected_domain=selected_domain,
        display_name=display_name,
    )


@app.route("/diagnostic/session/<session_id>", methods=["GET", "POST"], endpoint="diagnostic_session_v2")
@require_teacher
def diagnostic_session_v2(session_id):
    conn = get_conn()

    # confirm session exists
    sess = conn.execute(
        "SELECT * FROM diagnostic_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if not sess:
        flash("Diagnostic session not found.")
        return redirect(url_for("diagnostic_home"))

    if request.method == "POST":
        diagnostic_item_id = request.form.get("diagnostic_item_id")
        answer = request.form.get("response")

        if diagnostic_item_id and answer:
            row = conn.execute(
                """
                SELECT di.id AS diagnostic_item_id, q.answer_key
                FROM diagnostic_items di
                JOIN questions q ON q.question_id = di.question_id
                WHERE di.id = ?
                """,
                (diagnostic_item_id,),
            ).fetchone()

            if row:
                is_correct = 1 if str(answer).strip().upper() == str(row["answer_key"]).strip().upper() else 0
                conn.execute(
                    """
                    INSERT INTO diagnostic_responses (session_id, diagnostic_item_id, is_correct)
                    VALUES (?, ?, ?)
                    """,
                    (session_id, int(diagnostic_item_id), is_correct),
                )
                conn.commit()

    # get next question
    next_item = get_next_unanswered_diagnostic_item(conn, session_id)

    # progress count
    answered = conn.execute(
        "SELECT COUNT(*) AS c FROM diagnostic_responses WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    answered_count = int(answered["c"]) if answered else 0

    if not next_item:
        # complete session
        conn.execute(
            """
            UPDATE diagnostic_sessions
            SET status='completed', completed_at=?
            WHERE id=?
            """,
            (int(time.time()), session_id),
        )
        conn.commit()

        html_done = """
        <!doctype html>
        <title>Diagnostic Complete</title>
        <style>
          body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#f3f4f6;}
          .card{max-width:700px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;}
          a{color:#2563eb;text-decoration:none}
        </style>
        <div class="card">
          <h2 style="margin-top:0;">✅ Diagnostic Complete</h2>
          <p>Session: <strong>{{ session_id }}</strong></p>
          <p>Answered: <strong>{{ answered_count }}</strong></p>
          <p><a href="{{ url_for('diagnostic_home') }}">Start another diagnostic</a></p>
          <p><a href="{{ url_for('index') }}">⬅ Back to dashboard</a></p>
        </div>
        """
        return render_template_string(html_done, session_id=session_id, answered_count=answered_count)

    html = """
    <!doctype html>
    <title>Diagnostic Session</title>
    <style>
      body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#f3f4f6;}
      .card{max-width:800px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;}
      .btn{background:#2563eb;color:#fff;border:none;padding:8px 12px;border-radius:8px;cursor:pointer}
      .choice{margin:6px 0;}
      .muted{color:#6b7280;font-size:12px}
      a{color:#2563eb;text-decoration:none}
    </style>

    <div class="card">
      <h2 style="margin-top:0;">View Diagnostic Session</h2>
      <p class="muted">Session: {{ session_id }} | Answered: {{ answered_count }}</p>

      <h3 style="margin-bottom:6px;">{{ next_item['question_id'] }}</h3>
      <p style="margin-top:0;">{{ next_item['stem'] }}</p>

      <form method="post">
        <input type="hidden" name="diagnostic_item_id" value="{{ next_item['diagnostic_item_id'] }}">

        <div class="choice"><label><input type="radio" name="response" value="A" required> A. {{ next_item['choice_a'] }}</label></div>
        <div class="choice"><label><input type="radio" name="response" value="B"> B. {{ next_item['choice_b'] }}</label></div>
        <div class="choice"><label><input type="radio" name="response" value="C"> C. {{ next_item['choice_c'] }}</label></div>
        <div class="choice"><label><input type="radio" name="response" value="D"> D. {{ next_item['choice_d'] }}</label></div>

        <p><button class="btn" type="submit">Submit</button></p>
      </form>

      <p><a href="{{ url_for('diagnostic_home') }}">⬅ Back to Diagnostic Home</a></p>
    </div>
    """
    return render_template_string(
        html,
        session_id=session_id,
        answered_count=answered_count,
        next_item=next_item,
    )

# ---------- User admin ----------
@app.route("/user_admin", methods=["GET", "POST"])
@require_teacher
def user_admin():
    if request.method == "GET":
        flash("Teachers manage student memberships from their class rosters.")
        return redirect(url_for("index"))
    abort(403)


@app.get("/teacher/classes/<class_id>/students/<student_id>/archive")
@require_teacher
def teacher_confirm_archive_enrollment(class_id, student_id):
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT cs.class_id, cs.name AS class_name, s.student_id,
                   s.first_name, s.last_name
            FROM class_sections cs
            JOIN class_enrollments ce ON ce.class_id=cs.class_id
            JOIN students s ON s.student_id=ce.student_id
            WHERE cs.class_id=? AND ce.student_id=? AND ce.is_active=1
              AND cs.teacher_user_id=?
            """,
            (class_id, student_id, current_user()["id"]),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)
    return render_template_string(
        """
<!doctype html><title>Remove from Class</title>
<main style="font-family:Arial;max-width:680px;margin:40px auto">
<h1>Remove from Class</h1>
<p>Archive <strong>{{ display_name(row) }}</strong>'s enrollment in <strong>{{ row['class_name'] }}</strong>?</p>
<p>The RootED account, other classes, identities, learning records, and enrollment history will remain.</p>
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
<label>Reason <input name="reason"></label>
<button type="submit">Archive Enrollment</button>
</form>
<p><a href="{{ url_for('index') }}#class-enrollment">Cancel</a></p>
</main>
        """,
        row=row, display_name=display_name, csrf_token=owner_csrf_token(),
    )


@app.post("/teacher/classes/<class_id>/students/<student_id>/archive")
@require_teacher
def teacher_archive_enrollment(class_id, student_id):
    require_owner_csrf()
    actor = current_user()["id"]
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute(
            "SELECT 1 FROM class_sections WHERE class_id=? AND teacher_user_id=?",
            (class_id, actor),
        ).fetchone():
            conn.rollback()
            abort(403)
        enrollment = conn.execute(
            "SELECT is_active FROM class_enrollments WHERE class_id=? AND student_id=?",
            (class_id, student_id),
        ).fetchone()
        if not enrollment:
            conn.rollback()
            abort(404)
        outcome = "already_archived"
        if int(enrollment["is_active"] or 0) == 1:
            conn.execute(
                """
                UPDATE class_enrollments
                SET is_active=0, archived_at=?, archived_by_user_id=?,
                    archive_reason=?
                WHERE class_id=? AND student_id=? AND is_active=1
                """,
                (int(time.time()), actor, request.form.get("reason", "").strip() or None,
                 class_id, student_id),
            )
            outcome = "archived"
        conn.execute(
            """
            INSERT INTO class_membership_audit_log
              (actor_user_id,class_id,student_id,action,outcome,reason,created_at)
            VALUES (?,?,?,'membership_archived',?,?,?)
            """,
            (actor, class_id, student_id, outcome,
             request.form.get("reason", "").strip() or None, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()
    flash("Enrollment archived." if outcome == "archived" else "Enrollment was already archived.")
    return redirect(url_for("index") + "#class-enrollment")


# ---------- Admin actions ----------
@app.get("/admin_action")
@require_teacher
def admin_action_landing():
    flash("Use the Maintenance / Data Tools panel on the dashboard to run admin actions.")
    return redirect(url_for("index"))


@app.post("/admin_action")
@require_teacher
def admin_action():
    conn = get_conn()
    action = request.form.get("action")

    if action == "export_attempts":
        rows = conn.execute(
            """
            SELECT attempt_id, student_id, question_id, timestamp, response, is_correct, time_seconds, skills_missed
            FROM attempts
            ORDER BY timestamp DESC
            """
        ).fetchall()
        output = io.StringIO()
        w = csv.writer(output)
        w.writerow(
            [
                "attempt_id",
                "student_id",
                "question_id",
                "timestamp",
                "response",
                "is_correct",
                "time_seconds",
                "skills_missed",
            ]
        )
        for r in rows:
            w.writerow(
                [
                    r["attempt_id"],
                    r["student_id"],
                    r["question_id"],
                    r["timestamp"],
                    r["response"],
                    r["is_correct"],
                    r["time_seconds"],
                    r["skills_missed"],
                ]
            )
        resp = make_response(output.getvalue())
        resp.headers["Content-Type"] = "text/csv"
        resp.headers["Content-Disposition"] = (
            'attachment; filename="attempts_backup.csv"'
        )
        return resp

    if action == "reset_practice":
        conn.execute("DELETE FROM attempts")
        conn.execute("DELETE FROM responses")
        conn.execute("DELETE FROM student_question_deliveries")
        conn.execute("DELETE FROM student_growth_progress")
        conn.commit()
        flash(
            "All attempts and responses have been cleared. Students and content were kept."
        )
        return redirect(url_for("index"))

    flash("Unknown admin action.")
    return redirect(url_for("index"))


# ---------- Restore attempts ----------
@app.post("/restore_attempts")
@require_teacher
def restore_attempts():
    conn = get_conn()
    file = request.files.get("file")

    if not file or file.filename == "":
        flash("Please choose a CSV file to restore from.")
        return redirect(url_for("index"))

    try:
        conn.execute("DELETE FROM attempts")
        conn.execute("DELETE FROM responses")
        conn.execute("DELETE FROM student_question_deliveries")
        conn.execute("DELETE FROM student_growth_progress")

        text_data = file.stream.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text_data))

        count = 0
        for r in reader:
            attempt_id = (r.get("attempt_id") or "").strip()
            student_id = (r.get("student_id") or "").strip()
            question_id = (r.get("question_id") or "").strip()
            timestamp_raw = (r.get("timestamp") or "").strip()
            response = (r.get("response") or "").strip()
            is_correct_raw = (r.get("is_correct") or "").strip()
            time_raw = (r.get("time_seconds") or "").strip()
            skills_missed = (r.get("skills_missed") or "[]").strip()

            try:
                timestamp = int(timestamp_raw) if timestamp_raw else 0
            except ValueError:
                timestamp = 0
            try:
                is_correct = int(is_correct_raw) if is_correct_raw != "" else 0
            except ValueError:
                is_correct = 0
            try:
                time_seconds = int(time_raw) if time_raw != "" else 0
            except ValueError:
                time_seconds = 0

            if not attempt_id or not student_id or not question_id:
                continue

            conn.execute(
                """
                INSERT OR REPLACE INTO attempts
                  (attempt_id, student_id, question_id, timestamp, response, is_correct, time_seconds, skills_missed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    student_id,
                    question_id,
                    timestamp,
                    response,
                    is_correct,
                    time_seconds,
                    skills_missed,
                ),
            )
            count += 1

        conn.commit()
        flash(
            f"Restored {count} attempts from backup. Responses table was cleared and will rebuild over time."
        )
    except Exception as e:
        conn.rollback()
        flash(f"Error restoring attempts: {e}")

    return redirect(url_for("index"))


# ---------- Export CSV ----------
@app.get("/export_csv")
@require_teacher
def export_csv():
    conn = get_conn()
    selected_period = request.args.get("period", "ALL")
    students = get_students(conn, selected_period if selected_period != "ALL" else None)
    objs = get_objectives(conn)

    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(
        [
            "student_id",
            "name",
            "grade",
            "period",
            "objective_id",
            "objective_text",
            "rolling_avg_last7",
            "next_node",
            "reason",
            "frustration_active",
        ]
    )

    for s in students:
        name = f"{s['first_name'] or ''} {s['last_name'] or ''}".strip()
        for o in objs:
            avg, nxt, reason, fr = decide_next(conn, s["student_id"], o["objective_id"])
            w.writerow(
                [
                    s["student_id"],
                    name,
                    s["grade"] or "",
                    s["class_period"] or "",
                    o["objective_id"],
                    o["objective_text"],
                    round(avg, 2),
                    nxt,
                    reason,
                    "TRUE" if fr else "FALSE",
                ]
            )

    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers[
        "Content-Disposition"
    ] = f'attachment; filename=class_progress_period_{selected_period}.csv'
    return resp


# ---------- Student Overview ----------
@app.post("/teacher/student/<student_id>/placement")
@require_teacher
def update_student_placement(student_id):
    conn = get_conn()
    node_value = request.form.get("learning_node", "").strip()
    level_raw = request.form.get("current_level", "1").strip()

    if "||" not in node_value:
        flash("Select a valid standard and objective.")
        return redirect(url_for("student_overview", student_id=student_id))

    standard_id, objective_id = [part.strip() for part in node_value.split("||", 1)]
    try:
        current_level = int(level_raw)
    except Exception:
        current_level = 1

    ok, message = place_student_learning_node(
        conn,
        student_id=student_id,
        standard_id=standard_id,
        objective_id=objective_id,
        current_level=current_level,
        placed_by_user_id=session.get("user_id"),
        require_teacher_control=True,
    )
    flash(message)
    return redirect(url_for("student_overview", student_id=student_id))


@app.route("/teacher/student/<student_id>")
@require_teacher
def student_overview(student_id):
    conn = get_conn()
    overview = get_student_overview(conn, student_id)
    if not overview:
        abort(404)
    placement_options = get_placeable_learning_nodes(conn)
    ms_ls1_1_first_objective = first_objective_for_standard(conn, "MS-LS1-1")

    html = """
<!doctype html>
<title>Student Overview - {{ overview.display_name }}</title>
<style>
  body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#fbfaf4;color:#1f2937}
  .page{max-width:1120px;margin:0 auto}
  .topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:16px;padding:16px 18px;border:1px solid #dfe8d9;border-radius:12px;background:#fffdf7}
  .topbar h1{margin:0 0 6px;font-size:30px}
  .subtitle{margin:0;color:#4b5563;font-size:14px}
  .btn{display:inline-block;background:#2f6f4e;color:#fff;text-decoration:none;border:none;padding:8px 12px;border-radius:8px;cursor:pointer;font-size:13px}
  .btn-secondary{background:#eef4ec;color:#2f5138;border:1px solid #c8d9c4}
  .card{border:1px solid #e2decf;border-radius:10px;padding:16px;margin:16px 0;background:#fffefa;box-shadow:0 8px 18px rgba(47,111,78,.05)}
  details.card > summary{cursor:pointer;font-size:24px;font-weight:700;margin:-4px 0 0;list-style:none}
  details.card > summary::-webkit-details-marker{display:none}
  details.card > summary::after{content:"Expand";float:right;font-size:13px;font-weight:700;color:#2f5138;background:#eef4ec;border:1px solid #c8d9c4;border-radius:999px;padding:4px 10px}
  details.card[open] > summary::after{content:"Collapse"}
  .focus{background:#f7fbf3;border-color:#c8d9c4}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .stat-grid{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:10px}
  .stat{border:1px solid #e5e0d2;border-radius:8px;padding:12px;background:#fff}
  .label{font-size:12px;text-transform:uppercase;color:#6b7280;font-weight:700;letter-spacing:.02em}
  .value{font-size:18px;font-weight:800;color:#111827;margin-top:4px}
  table{border-collapse:collapse;width:100%}
  th,td{border:1px solid #e5e0d2;padding:8px;vertical-align:top;font-size:13px}
  th{background:#f5f1e8;text-align:left;color:#374151}
  h2{margin:0 0 8px}
  h3{margin:14px 0 8px}
  .note{font-size:13px;color:#6b7280;margin:4px 0 12px}
  .empty{border:1px dashed #c8d9c4;border-radius:8px;padding:12px;background:#fbf8ef;color:#53665a}
  .pill{display:inline-block;padding:3px 8px;border-radius:999px;font-size:12px;font-weight:800}
  .pill-ok{background:#dcfce7;color:#166534}
  .pill-warn{background:#fef3c7;color:#92400e}
  .pill-alert{background:#fee2e2;color:#991b1b}
  .pill-current{background:#e0f2fe;color:#075985}
  .pill-muted{background:#f3f4f6;color:#4b5563}
  .placement-form{display:grid;grid-template-columns:2fr 120px auto;gap:10px;align-items:end}
  .placement-form label{font-size:13px;color:#374151;font-weight:700}
  .placement-form select{width:100%;margin-top:6px;padding:8px;border:1px solid #cbd5e1;border-radius:8px;background:#fff}
  .history-standard{border-top:1px solid #e5e0d2;padding-top:14px;margin-top:14px}
  .history-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:8px}
  .history-title{font-weight:800;color:#111827}
  .objective-title{font-weight:800;color:#111827}
  .objective-id{font-size:12px;color:#6b7280;margin-top:3px}
  .muted{color:#6b7280}
  ul.clean{margin:8px 0 0 18px;padding:0}
  li{margin:4px 0}
  @media(max-width:850px){.grid,.stat-grid,.placement-form{grid-template-columns:1fr}.topbar{display:block}.topbar .actions{margin-top:12px}}
</style>
<div class="page">
  <div class="topbar">
    <div>
      <h1>Student Overview</h1>
      <p class="subtitle">Read-only classroom view for existing RootED progress data.</p>
    </div>
    <div class="actions">
      <a class="btn btn-secondary" href="{{ url_for('index') }}">Back to dashboard</a>
    </div>
  </div>

  <div class="card focus">
    <h2>Current Focus</h2>
    <p class="note">Generated from existing factual data only.</p>
    <ul class="clean">
      {% for item in overview.focus %}
        <li>{{ item }}</li>
      {% endfor %}
    </ul>
  </div>

  <div class="card">
    <h2>Student</h2>
    <div class="stat-grid">
      <div class="stat">
        <div class="label">Name</div>
        <div class="value">{{ overview.display_name }}</div>
      </div>
      <div class="stat">
        <div class="label">Student ID</div>
        <div class="value">{{ overview.student['student_id'] }}</div>
      </div>
      <div class="stat">
        <div class="label">Grade</div>
        <div class="value">{{ overview.student['grade'] or 'Not set' }}</div>
      </div>
      <div class="stat">
        <div class="label">Class Period</div>
        <div class="value">{{ overview.student['class_period'] or 'Not set' }}</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Current Progress</h2>
    {% if overview.latest_progress %}
      <div class="grid">
        <div>
          <table>
            <tr><th>Current standard</th><td>{{ overview.latest_progress['standard_id'] }}</td></tr>
            <tr><th>Current objective</th><td>{{ overview.objective['objective_id'] if overview.objective else 'Not recorded yet' }}</td></tr>
            <tr><th>Objective text</th><td>{{ overview.objective['objective_text'] if overview.objective else 'Not available' }}</td></tr>
            <tr><th>Current level</th><td>{{ overview.latest_progress['current_level'] }}</td></tr>
          </table>
        </div>
        <div>
          <table>
            <tr><th>Status</th><td>{{ overview.latest_progress['status'] }}</td></tr>
            <tr><th>Recent accuracy</th><td>{% if overview.rolling_avg is not none %}{{ (overview.rolling_avg * 100)|round|int }}%{% else %}Not available{% endif %}</td></tr>
            <tr><th>Last update</th><td>{{ format_ts(overview.latest_progress['last_update']) or 'Not available' }}</td></tr>
            <tr><th>Latest activity</th><td>{{ format_ts(overview.latest_activity) or 'Not available' }}</td></tr>
          </table>
        </div>
      </div>
    {% else %}
      <div class="empty">No current progress record exists for this student yet.</div>
    {% endif %}
  </div>

  <div class="card">
    <h2>Teacher Placement</h2>
    <p class="note">Explicitly place or reposition this student. This updates the durable current standard, objective, and level without deleting prior work.</p>
    {% if placement_options %}
      <form class="placement-form" method="post" action="{{ url_for('update_student_placement', student_id=overview.student['student_id']) }}">
        <label>Standard and objective
          <select name="learning_node" required>
            {% for node in placement_options %}
              {% set value = node['standard_id'] ~ '||' ~ node['objective_id'] %}
              <option value="{{ value }}"
                {% if overview.latest_progress and node['standard_id'] == overview.latest_progress['standard_id'] and overview.objective and node['objective_id'] == overview.objective['objective_id'] %}selected
                {% elif not overview.latest_progress and node['standard_id'] == 'MS-LS1-1' and node['objective_id'] == ms_ls1_1_first_objective %}selected{% endif %}>
                {{ node['standard_id'] }} - {{ node['objective_id'] }}{% if node['objective_text'] %}: {{ node['objective_text'] }}{% endif %}
              </option>
            {% endfor %}
          </select>
        </label>
        <label>Level
          <select name="current_level" required>
            {% for level in [1, 2, 3] %}
              <option value="{{ level }}" {% if overview.latest_progress and overview.latest_progress['current_level'] == level %}selected{% elif not overview.latest_progress and level == 1 %}selected{% endif %}>{{ level }}</option>
            {% endfor %}
          </select>
        </label>
        <button class="btn" type="submit">Place Student</button>
      </form>
    {% else %}
      <div class="empty">No placeable objectives are available yet.</div>
    {% endif %}
  </div>

  <div class="card">
    <h2>Learning History</h2>
    <p class="note">Objective-level evidence from existing attempts. This is not an official grade.</p>
    <div class="stat-grid">
      <div class="stat">
        <div class="label">Standards Worked On</div>
        <div class="value">{{ overview.learning_history.summary.standards_worked_on }}</div>
      </div>
      <div class="stat">
        <div class="label">Objectives Attempted</div>
        <div class="value">{{ overview.learning_history.summary.objectives_attempted }}</div>
      </div>
      <div class="stat">
        <div class="label">Current Objective</div>
        <div class="value">{{ overview.learning_history.summary.current_objective or 'Not recorded' }}</div>
      </div>
      <div class="stat">
        <div class="label">Latest History Activity</div>
        <div class="value">{{ format_ts(overview.learning_history.summary.latest_activity) or 'Not available' }}</div>
      </div>
    </div>

    {% if overview.learning_history.standards %}
      {% for standard in overview.learning_history.standards %}
        <div class="history-standard">
          <div class="history-head">
            <div>
              <div class="history-title">{{ standard.standard_id }}</div>
              <div class="note">
                {{ standard.core_idea or '' }} {{ standard.grade_band or '' }}
                {% if standard.is_current %} | Current standard{% endif %}
                {% if standard.status %} | {{ standard.status }}{% endif %}
              </div>
            </div>
          </div>
          <table>
            <tr>
              <th>Status</th>
              <th>Objective</th>
              <th>Attempts</th>
              <th>Correct/Total</th>
              <th>Recent Check-ins</th>
              <th>Last Activity</th>
            </tr>
            {% for objective in standard.objectives %}
              <tr>
                <td><span class="pill {{ objective.status_class }}">{{ objective.status }}</span></td>
                <td>
                  <div class="objective-title">{{ objective.objective_text or 'Not available' }}</div>
                  <div class="objective-id">{{ objective.objective_id }}</div>
                </td>
                <td>{{ objective.attempt_count }}</td>
                <td>
                  {% if objective.attempt_count %}
                    {{ objective.correct_count }}/{{ objective.attempt_count }}
                  {% else %}
                    Not available
                  {% endif %}
                </td>
                <td>
                  {% if objective.recent_count %}
                    {{ objective.recent_correct }}/{{ objective.recent_count }}
                  {% else %}
                    Not available
                  {% endif %}
                </td>
                <td>{{ format_ts(objective.last_activity) or 'Not available' }}</td>
              </tr>
            {% endfor %}
          </table>
        </div>
      {% endfor %}
    {% else %}
      <div class="empty">No learning history evidence is available yet.</div>
    {% endif %}
  </div>

  <div class="card">
    <h2>Recent Activity</h2>
    <div class="stat-grid">
      <div class="stat">
        <div class="label">Recent Check-ins</div>
        <div class="value">{{ overview.rolling_count }}/7</div>
      </div>
      <div class="stat">
        <div class="label">Recent Correct</div>
        <div class="value">{{ overview.rolling_correct }}/{{ overview.rolling_count }}</div>
      </div>
      <div class="stat">
        <div class="label">Total Responses</div>
        <div class="value">{{ overview.total_responses }}</div>
      </div>
      <div class="stat">
        <div class="label">Total Attempts</div>
        <div class="value">{{ overview.total_attempts }}</div>
      </div>
    </div>

    {% if overview.recent_activity %}
      <table>
        <tr><th>Time</th><th>Objective</th><th>Question ID</th><th>Result</th></tr>
        {% for item in overview.recent_activity %}
          <tr>
            <td>{{ format_ts(item.time) }}</td>
            <td>{{ item.objective_id or 'Not recorded' }}</td>
            <td>{{ item.question_id }}</td>
            <td>{{ 'Correct' if item.is_correct else 'Incorrect' }}</td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <div class="empty">No recent activity is available yet.</div>
    {% endif %}
  </div>

  <details class="card">
    <summary>Adaptive Details</summary>
    <p class="note">Technical adaptive details from existing progress records.</p>
    {% if overview.latest_progress %}
      <div class="grid">
        <div>
          <table>
            <tr>
              <th>Locked/intervention state</th>
              <td>
                {% if overview.latest_progress['locked'] %}
                  <span class="pill pill-alert">Locked</span>
                  {{ overview.latest_progress['locked_reason'] or '' }}
                {% elif overview.frustration %}
                  <span class="pill pill-warn">Frustration signal active</span>
                {% else %}
                  <span class="pill pill-ok">None recorded</span>
                {% endif %}
              </td>
            </tr>
            <tr><th>Active route type</th><td>{{ overview.latest_progress['active_route_type'] or 'Not recorded' }}</td></tr>
            <tr><th>Origin standard</th><td>{{ overview.latest_progress['origin_standard_id'] or 'Not recorded' }}</td></tr>
          </table>
        </div>
        <div>
          <h3 style="margin-top:0;">Progress notes</h3>
          {% if overview.notes %}
            <ul class="clean">
              {% for note in overview.notes %}
                <li>{{ note }}</li>
              {% endfor %}
            </ul>
          {% else %}
            <p class="note">No additional consistency notes are present.</p>
          {% endif %}
        </div>
      </div>

      <h3>Recent route origin</h3>
      {% if overview.origin_links %}
        <table>
          <tr><th>Time</th><th>From standard</th><th>From level</th><th>Current remediation standard</th></tr>
          {% for o in overview.origin_links %}
            <tr>
              <td>{{ format_ts(o['ts']) }}</td>
              <td>{{ o['from_standard_id'] }}</td>
              <td>{{ o['from_level'] }}</td>
              <td>{{ o['rem_standard_id'] }}</td>
            </tr>
          {% endfor %}
        </table>
      {% else %}
        <div class="empty">No route origin record is available for the current standard.</div>
      {% endif %}
    {% else %}
      <div class="empty">Adaptive details will appear after progress is recorded.</div>
    {% endif %}
  </details>

  <details class="card">
    <summary>Login Information</summary>
    <p class="note">Roster and login-link details.</p>
    {% if overview.linked_accounts %}
      <table>
        <tr><th>Username</th><th>Link type</th><th>Status</th><th>SSO</th><th>Last login</th></tr>
        {% for account in overview.linked_accounts %}
          <tr>
            <td>{{ account['username'] }}</td>
            <td>
              {% if account['linked_student_id'] == overview.student['student_id'] %}
                linked_student_id
              {% else %}
                username fallback
              {% endif %}
            </td>
            <td>{{ 'Active' if account['is_active'] else 'Inactive' }}</td>
            <td>{{ account['sso_provider'] or 'Not recorded' }}</td>
            <td>{{ format_ts(account['last_login_ts']) or 'Not recorded' }}</td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <div class="empty">No student login account is linked to this roster record.</div>
    {% endif %}
  </details>
</div>
    """

    return render_template_string(
        html,
        overview=overview,
        format_ts=format_ts,
        placement_options=placement_options,
        ms_ls1_1_first_objective=ms_ls1_1_first_objective,
    )


@app.get("/owner/adaptive-debug")
@owner_required
def owner_adaptive_debug():
    conn = get_conn()
    attempts = conn.execute(
        """
        SELECT rla.*, s.first_name, s.last_name, o.objective_text
        FROM routing_level_attempts rla
        LEFT JOIN students s ON s.student_id=rla.student_id
        LEFT JOIN objectives o ON o.objective_id=rla.objective_id
        WHERE rla.status='active'
        ORDER BY s.last_name COLLATE NOCASE, s.first_name COLLATE NOCASE,
                 rla.started_at DESC
        """
    ).fetchall()
    return render_template_string(
        """
<!doctype html>
<title>RootED Adaptive Engine Debug</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;margin:0;background:#fbfaf4;color:#1f2937}
.page{max-width:1100px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;gap:16px;align-items:center}
h1{color:#234b35;margin-bottom:6px}.muted{color:#647067;font-size:13px}
.btn{display:inline-block;background:#2f6f4e;color:#fff;text-decoration:none;padding:8px 12px;border-radius:8px;font-weight:700}
.btn-secondary{background:#eef4ec;color:#2f5138;border:1px solid #c8d9c4}
.table-wrap{overflow:auto;margin-top:18px;background:#fff;border:1px solid #e2decf;border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:11px 12px;text-align:left;border-bottom:1px solid #ebe8de}
th{background:#eef4ec;color:#234b35}.name{font-weight:700}.empty{padding:24px;color:#647067}
</style>
<main class="page">
  <div class="top">
    <div>
      <h1>Adaptive Engine Debug</h1>
      <p class="muted">Owner-only, read-only view of active routing-level attempts.</p>
    </div>
    <a class="btn btn-secondary" href="{{ url_for('owner_home') }}">Owner Workspace</a>
  </div>
  <div class="table-wrap">
    {% if attempts %}
      <table>
        <thead><tr><th>Student</th><th>Standard</th><th>Objective</th><th>Level</th><th>Started</th><th></th></tr></thead>
        <tbody>
        {% for attempt in attempts %}
          <tr>
            <td class="name">{{ ((attempt['first_name'] or '') ~ ' ' ~ (attempt['last_name'] or ''))|trim or attempt['student_id'] }}</td>
            <td>{{ attempt['standard_id'] }}</td>
            <td>{{ attempt['objective_text'] or attempt['objective_id'] }}</td>
            <td>{{ attempt['level'] }}</td>
            <td>{{ format_ts(attempt['started_at']) }}</td>
            <td><a class="btn" href="{{ url_for('engine_debug', student_id=attempt['student_id'], standard_id=attempt['standard_id']) }}">Inspect</a></td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    {% else %}
      <div class="empty">No active routing-level attempts.</div>
    {% endif %}
  </div>
</main>
        """,
        attempts=attempts,
        format_ts=lambda value: time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(int(value))
        ),
    )


# ---------- Engine debug viewer ----------
@app.route("/engine_debug/<student_id>/<standard_id>")
@owner_required
def engine_debug(student_id, standard_id):
    conn = get_conn()

    student = conn.execute(
        "SELECT * FROM students WHERE student_id=?", (student_id,)
    ).fetchone()
    standard = conn.execute(
        "SELECT * FROM standards WHERE standard_id=?", (standard_id,)
    ).fetchone()
    if not student or not standard:
        abort(404)

    routing_attempt = conn.execute(
        """
        SELECT rla.*, o.objective_text
        FROM routing_level_attempts rla
        LEFT JOIN objectives o ON o.objective_id=rla.objective_id
        WHERE rla.student_id=? AND rla.standard_id=? AND rla.status='active'
        ORDER BY rla.started_at DESC
        LIMIT 1
        """,
        (student_id, standard_id),
    ).fetchone()

    if routing_attempt:
        metrics = conn.execute(
            """
            SELECT COUNT(*) AS response_count,
                   COALESCE(SUM(correct), 0) AS correct_count
            FROM responses
            WHERE routing_level_attempt_id=?
            """,
            (routing_attempt["attempt_id"],),
        ).fetchone()
        response_count = int(metrics["response_count"])
        correct_count = int(metrics["correct_count"])
        incorrect_count = response_count - correct_count
        accuracy = correct_count / response_count if response_count else 0.0
        minimum_met = response_count >= ae.MIN_EVIDENCE
        if not minimum_met:
            decision = "Collecting"
            reason = "insufficient_minimum_evidence"
        elif accuracy >= ae.MASTERY:
            decision = "Advance"
            reason = "mastery_threshold_met"
        elif accuracy >= ae.REMEDIATE:
            decision = "Practice"
            reason = "middle_band_continue"
        else:
            decision = "Remediate"
            reason = "below_remediation_threshold"

        deliveries = conn.execute(
            """
            SELECT
              SUM(CASE WHEN consumed_at IS NULL AND invalidated_at IS NULL THEN 1 ELSE 0 END) AS active_count,
              SUM(CASE WHEN consumed_at IS NOT NULL THEN 1 ELSE 0 END) AS consumed_count,
              SUM(CASE WHEN consumed_at IS NULL AND invalidated_at IS NOT NULL THEN 1 ELSE 0 END) AS invalidated_count
            FROM student_question_deliveries
            WHERE student_id=? AND standard_id=? AND objective_id=? AND level=?
              AND served_at>=?
            """,
            (
                student_id,
                standard_id,
                routing_attempt["objective_id"],
                routing_attempt["level"],
                routing_attempt["started_at"],
            ),
        ).fetchone()
        recent_responses = conn.execute(
            """
            SELECT r.*, q.stem
            FROM responses r
            LEFT JOIN questions q ON q.question_id=r.question_id
            WHERE r.routing_level_attempt_id=?
            ORDER BY r.id DESC
            LIMIT 20
            """,
            (routing_attempt["attempt_id"],),
        ).fetchall()
    else:
        response_count = correct_count = incorrect_count = 0
        accuracy = 0.0
        minimum_met = False
        decision = "Collecting"
        reason = "no_active_routing_level_attempt"
        deliveries = {
            "active_count": 0,
            "consumed_count": 0,
            "invalidated_count": 0,
        }
        recent_responses = []

    return render_template_string(
        """
<!doctype html>
<title>Adaptive Debug - {{ student_id }} / {{ standard_id }}</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;margin:0;background:#fbfaf4;color:#1f2937}
.page{max-width:1050px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px}
h1,h2{color:#234b35}.muted{color:#647067;font-size:13px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.card{background:#fffefa;border:1px solid #e2decf;border-radius:12px;padding:18px;margin-top:16px;box-shadow:0 8px 18px rgba(47,111,78,.05)}
.facts{display:grid;grid-template-columns:minmax(140px,max-content) 1fr;gap:8px 14px;margin:0}.facts dt{font-weight:700;color:#405b49}.facts dd{margin:0;overflow-wrap:anywhere}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.metric{background:#eef4ec;border-radius:9px;padding:12px}.metric strong{display:block;font-size:22px;color:#234b35}
.decision{display:inline-block;padding:5px 10px;border-radius:999px;background:#dcefe0;color:#1f5a38;font-weight:800}
.responses{display:flex;flex-wrap:wrap;gap:8px;list-style:none;padding:0}.response{display:inline-flex;width:34px;height:34px;align-items:center;justify-content:center;border-radius:50%;font-weight:900}
.correct{background:#dcefe0;color:#176638}.incorrect{background:#fbe1dd;color:#a52a22}
.btn{display:inline-block;background:#2f6f4e;color:#fff;text-decoration:none;padding:8px 12px;border-radius:8px;font-weight:700}
@media(max-width:720px){.grid{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}.top{align-items:flex-start;flex-direction:column}}
</style>
<main class="page">
  <div class="top">
    <div><h1>Adaptive Engine Debug</h1><p class="muted">Owner-only, read-only routing explanation.</p></div>
    <a class="btn" href="{{ url_for('owner_adaptive_debug') }}">All Active Attempts</a>
  </div>

  <section class="card">
    <h2>Routing Level Attempt</h2>
    {% if routing_attempt %}
      <dl class="facts">
        <dt>Attempt ID</dt><dd>{{ routing_attempt['attempt_id'] }}</dd>
        <dt>Student</dt><dd>{{ student_name }} ({{ student_id }})</dd>
        <dt>Standard</dt><dd>{{ standard_id }}</dd>
        <dt>Objective</dt><dd>{{ routing_attempt['objective_text'] or routing_attempt['objective_id'] }}</dd>
        <dt>Routing Level</dt><dd>{{ routing_attempt['level'] }}</dd>
      </dl>
    {% else %}
      <p>No active routing-level attempt exists for this student and standard.</p>
    {% endif %}
  </section>

  <div class="grid">
    <section class="card">
      <h2>Cumulative Evidence</h2>
      <div class="metrics">
        <div class="metric"><strong>{{ response_count }}</strong>Responses</div>
        <div class="metric"><strong>{{ correct_count }}</strong>Correct</div>
        <div class="metric"><strong>{{ incorrect_count }}</strong>Incorrect</div>
        <div class="metric"><strong>{{ '%.1f%%'|format(accuracy * 100) }}</strong>Accuracy</div>
      </div>
      <dl class="facts" style="margin-top:16px">
        <dt>Minimum Evidence</dt><dd>{{ minimum_evidence }}</dd>
        <dt>Met?</dt><dd>{{ 'Yes' if minimum_met else 'No' }}</dd>
      </dl>
    </section>

    <section class="card">
      <h2>Current Decision</h2>
      <p><span class="decision">{{ decision }}</span></p>
      <dl class="facts"><dt>Reason</dt><dd><code>{{ reason }}</code></dd></dl>
    </section>
  </div>

  <section class="card">
    <h2>Deliveries</h2>
    <div class="metrics">
      <div class="metric"><strong>{{ deliveries['active_count'] or 0 }}</strong>Active</div>
      <div class="metric"><strong>{{ deliveries['consumed_count'] or 0 }}</strong>Consumed</div>
      <div class="metric"><strong>{{ deliveries['invalidated_count'] or 0 }}</strong>Invalidated</div>
    </div>
  </section>

  <section class="card">
    <h2>Recent Responses</h2>
    {% if recent_responses %}
      <ol class="responses" aria-label="Recent accepted responses, newest first">
      {% for response in recent_responses %}
        <li class="response {{ 'correct' if response['correct'] else 'incorrect' }}"
            aria-label="{{ 'Correct' if response['correct'] else 'Incorrect' }}"
            title="{{ response['question_id'] }}">{{ '✓' if response['correct'] else '✕' }}</li>
      {% endfor %}
      </ol>
    {% else %}
      <p class="muted">No accepted responses in this routing-level attempt.</p>
    {% endif %}
  </section>
</main>
        """,
        student_id=student_id,
        standard_id=standard_id,
        student_name=(
            f"{student['first_name'] or ''} {student['last_name'] or ''}".strip()
            or student_id
        ),
        routing_attempt=routing_attempt,
        response_count=response_count,
        correct_count=correct_count,
        incorrect_count=incorrect_count,
        accuracy=accuracy,
        minimum_evidence=ae.MIN_EVIDENCE,
        minimum_met=minimum_met,
        decision=decision,
        reason=reason,
        deliveries=deliveries,
        recent_responses=recent_responses,
    )

    # Basic info
    student = conn.execute(
        "SELECT * FROM students WHERE student_id=?",
        (student_id,),
    ).fetchone()

    standard = conn.execute(
        "SELECT * FROM standards WHERE standard_id=?",
        (standard_id,),
    ).fetchone()

    # Progress state for this student/standard
    state = conn.execute(
        """
        SELECT *
        FROM progress_state
        WHERE student_id = ? AND standard_id = ?
        """,
        (student_id, standard_id),
    ).fetchone()

    # Recent responses for this student/standard
    responses = conn.execute(
        """
        SELECT r.*, q.objective_id
        FROM responses r
        LEFT JOIN questions q ON q.question_id = r.question_id
        WHERE r.student_id = ? AND r.standard_id = ?
        ORDER BY r.ts DESC
        LIMIT 50
        """,
        (student_id, standard_id),
    ).fetchall()

    # Recent attempts for objectives under this standard
    attempts = conn.execute(
        """
        SELECT a.*, q.objective_id
        FROM attempts a
        LEFT JOIN questions q ON q.question_id = a.question_id
        WHERE a.student_id = ?
          AND q.objective_id IN (
              SELECT objective_id FROM objectives WHERE standard_id = ?
          )
        ORDER BY a.timestamp DESC
        LIMIT 50
        """,
        (student_id, standard_id),
    ).fetchall()

    # Remediation / progression links for this standard
    rem_links = conn.execute(
        "SELECT * FROM remediation_links WHERE standard_id = ?",
        (standard_id,),
    ).fetchall()

    prog_links = conn.execute(
        "SELECT * FROM progression_links WHERE standard_id = ?",
        (standard_id,),
    ).fetchall()

    # Origin links & notifications for this student/standard
    origin_links = conn.execute(
        """
        SELECT *
        FROM origin_links
        WHERE student_id = ? AND rem_standard_id = ?
        ORDER BY ts DESC
        LIMIT 50
        """,
        (student_id, standard_id),
    ).fetchall()

    notifications = conn.execute(
        """
        SELECT *
        FROM notifications
        WHERE student_id = ? AND standard_id = ?
        ORDER BY ts DESC
        LIMIT 50
        """,
        (student_id, standard_id),
    ).fetchall()

    def datetime_format(value):
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(value)))
        except Exception:
            return str(value)

    html = """
    <!doctype html>
    <title>Engine Debug - {{ student_id }} / {{ standard_id }}</title>
    <style>
      body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#f3f4f6;}
      .card{background:#fff;border-radius:10px;border:1px solid #e5e7eb;padding:16px 20px;margin-bottom:16px;}
      h1,h2,h3{margin-top:0;}
      table{border-collapse:collapse;width:100%;margin-top:8px;}
      th,td{border:1px solid #e5e7eb;padding:6px 8px;font-size:12px;vertical-align:top;}
      th{background:#f9fafb;text-align:left;}
      .pill{display:inline-block;padding:2px 6px;border-radius:999px;font-size:11px;}
      .pill-state{background:#e0f2fe;color:#1d4ed8;}
      .pill-err{background:#fee2e2;color:#991b1b;}
      .ts{font-size:11px;color:#6b7280;}
      .grid{display:grid;grid-template-columns:1.1fr 1.2fr;gap:16px;}
      .small{font-size:12px;color:#6b7280;}
      a.btn{display:inline-block;padding:6px 10px;border-radius:8px;
            background:#2563eb;color:#fff;text-decoration:none;font-size:13px;}
    </style>

    <div class="card">
      <h1>Engine Debug</h1>
      <p class="small">
        Student ID: <strong>{{ student_id }}</strong><br>
        Standard ID: <strong>{{ standard_id }}</strong><br>
        {% if student %}
          Name (if stored): {{ (student['first_name'] or '') ~ ' ' ~ (student['last_name'] or '') }}
          &nbsp; | &nbsp; Grade: {{ student['grade'] or '—' }} &nbsp; Period: {{ student['class_period'] or '—' }}
        {% endif %}
        {% if standard %}
          <br>Core idea: {{ standard['core_idea'] or '—' }},
          Grade band: {{ standard['grade_band'] or '—' }}
        {% endif %}
      </p>
      <p><a href="{{ url_for('index') }}" class="btn">⬅ Back to dashboard</a></p>
    </div>

    <div class="grid">
      <div class="card">
        <h2>Progress State</h2>
        {% if state %}
          <p><span class="pill pill-state">{{ state['status'] }}</span></p>
          <table>
            <tr>
              <th>Current level</th>
              <th>Rolling avg</th>
              <th>Locked</th>
              <th>Locked reason</th>
              <th>Last update</th>
            </tr>
            <tr>
              <td>{{ state['current_level'] }}</td>
              <td>{{ '%.2f'|format(state['rolling_avg']) }}</td>
              <td>{{ state['locked'] }}</td>
              <td>{{ state['locked_reason'] or '' }}</td>
              <td class="ts">{{ datetime_format(state['last_update']) }}</td>
            </tr>
          </table>
        {% else %}
          <p class="small"><em>No progress_state row found for this student/standard yet.</em></p>
        {% endif %}

        <h3>Remediation / Progression Links</h3>
        <h4>Remediation</h4>
        {% if rem_links %}
          <table>
            <tr><th>ID</th><th>Standard</th><th>Lower standard</th></tr>
            {% for r in rem_links %}
              <tr>
                <td>{{ r['id'] }}</td>
                <td>{{ r['standard_id'] }}</td>
                <td>{{ r['lower_standard_id'] }}</td>
              </tr>
            {% endfor %}
          </table>
        {% else %}
          <p class="small"><em>No remediation_links rows for this standard.</em></p>
        {% endif %}

        <h4>Progression</h4>
        {% if prog_links %}
          <table>
            <tr><th>ID</th><th>From</th><th>Next</th></tr>
            {% for r in prog_links %}
              <tr>
                <td>{{ r['id'] }}</td>
                <td>{{ r['standard_id'] }}</td>
                <td>{{ r['next_standard_id'] }}</td>
              </tr>
            {% endfor %}
          </table>
        {% else %}
          <p class="small"><em>No progression_links rows for this standard.</em></p>
        {% endif %}
      </div>

      <div class="card">
        <h2>Notifications & Origins</h2>

        <h3>Notifications (student + standard)</h3>
        {% if notifications %}
          <table>
            <tr>
              <th>Time</th><th>Event</th><th>Details</th>
            </tr>
            {% for n in notifications %}
              <tr>
                <td class="ts">{{ datetime_format(n['ts']) }}</td>
                <td>{{ n['event'] }}</td>
                <td>{{ n['details'] }}</td>
              </tr>
            {% endfor %}
          </table>
        {% else %}
          <p class="small"><em>No notifications logged yet for this student/standard.</em></p>
        {% endif %}

        <h3>Origin Links (how student got here)</h3>
        {% if origin_links %}
          <table>
            <tr>
              <th>Time</th><th>From standard</th><th>From level</th><th>Rem standard</th>
            </tr>
            {% for o in origin_links %}
              <tr>
                <td class="ts">{{ datetime_format(o['ts']) }}</td>
                <td>{{ o['from_standard_id'] }}</td>
                <td>{{ o['from_level'] }}</td>
                <td>{{ o['rem_standard_id'] }}</td>
              </tr>
            {% endfor %}
          </table>
        {% else %}
          <p class="small"><em>No origin_links rows for this student/standard yet.</em></p>
        {% endif %}
      </div>
    </div>

    <div class="card">
      <h2>Recent Responses (standard-level)</h2>
      {% if responses %}
        <table>
          <tr>
            <th>Time</th>
            <th>Level</th>
            <th>Objective</th>
            <th>Question</th>
            <th>Correct</th>
          </tr>
          {% for r in responses %}
            <tr>
              <td class="ts">{{ datetime_format(r['ts']) }}</td>
              <td>{{ r['level'] }}</td>
              <td>{{ r['objective_id'] or '—' }}</td>
              <td>{{ r['question_id'] }}</td>
              <td>
                {% if r['correct'] %}
                  ✅
                {% else %}
                  ❌
                {% endif %}
              </td>
            </tr>
          {% endfor %}
        </table>
      {% else %}
        <p class="small"><em>No responses found yet for this student/standard.</em></p>
      {% endif %}
    </div>

    <div class="card">
      <h2>Recent Attempts (question-level, objectives under this standard)</h2>
      {% if attempts %}
        <table>
          <tr>
            <th>Time</th>
            <th>Objective</th>
            <th>Question</th>
            <th>Response</th>
            <th>Correct</th>
          </tr>
          {% for a in attempts %}
            <tr>
              <td class="ts">{{ datetime_format(a['timestamp']) }}</td>
              <td>{{ a['objective_id'] or '—' }}</td>
              <td>{{ a['question_id'] }}</td>
              <td>{{ a['response'] }}</td>
              <td>
                {% if a['is_correct'] %}
                  ✅
                {% else %}
                  ❌
                {% endif %}
              </td>
            </tr>
          {% endfor %}
        </table>
      {% else %}
        <p class="small"><em>No attempts found yet for this student on objectives under this standard.</em></p>
      {% endif %}
    </div>
    """

    return render_template_string(
        html,
        student_id=student_id,
        standard_id=standard_id,
        student=student,
        standard=standard,
        state=state,
        responses=responses,
        attempts=attempts,
        rem_links=rem_links,
        prog_links=prog_links,
        origin_links=origin_links,
        notifications=notifications,
        datetime_format=datetime_format,
    )

# ---------- Global error handler ----------
@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e

    conn = get_conn()
    ts = int(time.time())
    path = request.path
    user_id = session.get("user_id")
    role = session.get("role")
    tb_str = traceback.format_exc()

    # Try to log to the DB (but don't crash if logging fails)
    try:
        conn.execute(
            """
            INSERT INTO error_logs (ts, level, path, user_id, role, message, traceback)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                "ERROR",
                path,
                str(user_id) if user_id is not None else None,
                role,
                str(e),
                tb_str,
            ),
        )
        conn.commit()
    except Exception:
        pass

    # Log to file
    logger.error(
        "Exception at %s (user_id=%s role=%s): %s\n%s",
        path,
        user_id,
        role,
        e,
        tb_str,
    )

    # In debug mode, still let Flask show the full debugger
    if app.debug:
        raise e

    # Friendly error page for users
    return (
        render_template_string(
            """
            <!doctype html>
            <title>Something went wrong</title>
            <style>
              body{font-family:Arial, Helvetica, sans-serif;margin:24px;}
              .card{max-width:600px;margin:80px auto;padding:24px;border-radius:10px;
                    border:1px solid #e5e7eb;background:#f9fafb;}
              .small{font-size:12px;color:#6b7280;margin-top:12px;}
            </style>
            <div class="card">
              <h1>Something went wrong</h1>
              <p>We hit an unexpected error. It has been logged so it can be fixed.</p>
              <p>If this keeps happening, please tell your teacher.</p>
              <p class="small">Path: {{ path }}</p>
            </div>
            """,
            path=path,
        ),
        500,
    )


# ---------- Error log viewer (teacher) ----------
@app.route("/errors")
@require_teacher
def error_list():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, ts, level, path, user_id, role, message
        FROM error_logs
        ORDER BY ts DESC
        LIMIT 100
        """
    ).fetchall()

    html = """
    <!doctype html>
    <title>Error Log</title>
    <style>
      body{font-family:Arial, Helvetica, sans-serif;margin:24px;}
      table{border-collapse:collapse;width:100%;}
      th,td{border:1px solid #e5e7eb;padding:8px;font-size:13px;vertical-align:top;}
      th{background:#f3f4f6;text-align:left;}
      .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;}
      .pill-err{background:#fee2e2;color:#991b1b;}
      .ts{font-size:11px;color:#6b7280;}
      a.btn{display:inline-block;margin-bottom:12px;padding:6px 10px;
            border-radius:8px;background:#2563eb;color:#fff;text-decoration:none;}
    </style>

    <h1>Recent Errors</h1>
    <p class="ts">Showing up to the 100 most recent errors.</p>

    {% with msgs = get_flashed_messages() %}
      {% if msgs %}
        <div style="background:#e7f7ee;border:1px solid #a8e0bf;color:#0f6b3a;
                    padding:10px 12px;border-radius:8px;margin:10px 0;">
          {% for m in msgs %}
            <div>✅ {{ m }}</div>
          {% endfor %}
        </div>
      {% endif %}
    {% endwith %}

    <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;">
      <a href="{{ url_for('index') }}" class="btn">⬅ Back to dashboard</a>
      <form method="post"
            action="{{ url_for('clear_errors') }}"
            onsubmit="return confirm('Clear all logged errors? This cannot be undone.');">
        <button type="submit" class="btn" style="background:#dc2626;">
          🗑 Clear all errors
        </button>
      </form>
    </div>

    {% if rows %}
      <table>
        <tr>
          <th>Time</th>
          <th>Level</th>
          <th>Path</th>
          <th>User</th>
          <th>Message</th>
        </tr>
        {% for r in rows %}
          <tr>
            <td class="ts">{{ datetime_format(r['ts']) }}</td>
            <td><span class="pill pill-err">{{ r['level'] }}</span></td>
            <td>{{ r['path'] or '' }}</td>
            <td>
              ID: {{ r['user_id'] or '—' }}<br>
              Role: {{ r['role'] or '—' }}
            </td>
            <td>{{ r['message'] }}</td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <p><em>No errors have been logged yet. 🎉</em></p>
    {% endif %}
    """

    def datetime_format(value):
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(value)))
        except Exception:
            return str(value)

    return render_template_string(
        html,
        rows=rows,
        datetime_format=datetime_format,
    )

@app.post("/errors/clear")
@require_teacher
def clear_errors():
    """
    Delete all rows from error_logs. Teacher-only.
    """
    conn = get_conn()
    conn.execute("DELETE FROM error_logs")
    conn.commit()
    flash("All logged errors have been cleared.")
    return redirect(url_for("error_list"))

@app.route('/favicon.ico')
def favicon():
    return redirect(url_for('static', filename='favicon.ico'))

@app.get("/robots.txt")
def robots_txt():
    return app.response_class(
        "User-agent: *\nDisallow:\n",
        mimetype="text/plain",
    )

print(app.url_map)

if __name__ == "__main__":
    app.run(debug=True)
