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
)
import sqlite3
import time
import csv
import io
import uuid
import os
import logging
from logging.handlers import RotatingFileHandler
import traceback
from functools import wraps

from werkzeug.security import generate_password_hash, check_password_hash
from authlib.integrations.flask_client import OAuth

from app_core.config import FERPA_ENFORCED, DEFAULT_PII_MODE, SECRET_KEY
from app_core import adaptive_engine as ae

# ---------- App metadata ----------
APP_VERSION = "v0.5 – Rolling-7 Engine Active"
APP_NAME = "Adaptive NGSS Platform"

# ---------- Database setup ----------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("NGSS_DB", os.path.join(BASE_DIR, "data", "ngss.db"))
print("APP_DB =", DB)

# ---------- Flask app setup ----------
app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = SECRET_KEY

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

# Microsoft SSO (OIDC via common tenant) – can leave env vars empty for now
oauth.register(
    name="microsoft",
    client_id=os.environ.get("MS_CLIENT_ID"),
    client_secret=os.environ.get("MS_CLIENT_SECRET"),
    server_metadata_url="https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ---------- Auth decorators ----------
def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session or "role" not in session:
            flash("Please log in first.")
            return redirect(url_for("login"))
        if session.get("role") not in ("teacher", "student"):
            session.clear()
            flash("Session error. Please log in again.")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


def require_teacher(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session or "role" not in session:
            flash("Please log in as a teacher.")
            return redirect(url_for("login"))
        if session.get("role") != "teacher":
            flash("Teacher access only.")
            if session.get("role") == "student":
                return redirect(url_for("student_view"))
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


def require_student(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session or "role" not in session:
            flash("Please log in as a student.")
            return redirect(url_for("login"))
        if session.get("role") != "student":
            flash("Student access only.")
            if session.get("role") == "teacher":
                return redirect(url_for("index"))
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


# ---------- Schema ----------
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
          order_in_band  INTEGER,
          FOREIGN KEY(standard_id) REFERENCES standards(standard_id)
        )
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
    rows = conn.execute(
        """
        SELECT o.objective_id
        FROM objectives o
        JOIN standards s ON s.standard_id = o.standard_id
        WHERE s.core_idea = ? AND s.grade_band = ?
        ORDER BY o.order_in_band, o.objective_id
        """,
        (core, band),
    ).fetchall()
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
    r = conn.execute(
        """
        SELECT o.objective_id
        FROM objectives o
        JOIN standards s ON s.standard_id = o.standard_id
        WHERE s.core_idea = ? AND s.grade_band = ?
        ORDER BY o.order_in_band, o.objective_id
        LIMIT 1
        """,
        (core, band),
    ).fetchone()
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
    return conn.execute(
        """
        SELECT
            o.objective_id,
            o.objective_text,
            o.standard_id,
            s.core_idea,
            s.grade_band
        FROM objectives o
        JOIN standards s ON s.standard_id = o.standard_id
        ORDER BY s.core_idea, s.grade_band, o.order_in_band, o.objective_id
        """
    ).fetchall()


def get_available_standards(conn):
    return conn.execute(
        """
        SELECT
            s.standard_id,
            s.core_idea,
            s.grade_band,
            COUNT(o.objective_id) AS objective_count
        FROM standards s
        LEFT JOIN objectives o ON o.standard_id = s.standard_id
        GROUP BY s.standard_id, s.core_idea, s.grade_band
        ORDER BY s.core_idea, s.grade_band, s.standard_id
        """
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


def row_get(row, key, default=None):
    if not row or key not in row.keys():
        return default
    value = row[key]
    return default if value is None else value


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

def get_or_create_sso_user(
    conn,
    provider: str,
    subject: str,
    email: str | None = None,
    default_role: str = "student",
    linked_student_id: str | None = None,
):
    """
    Look up or create a user for an SSO identity.

    provider: e.g. "google", "microsoft", "clever", or "dev"
    subject:  stable unique id from the SSO provider (sub claim)
    """
    if not provider or not subject:
        return None

    # 1) Check if a user already exists for this SSO identity
    row = conn.execute(
        """
        SELECT *
        FROM users
        WHERE sso_provider = ? AND sso_subject = ?
        LIMIT 1
        """,
        (provider, subject),
    ).fetchone()

    now_ts = int(time.time())

    if row:
        # Respect disabled accounts
        try:
            is_active = int(row["is_active"])
        except Exception:
            is_active = 1

        if is_active != 1:
            return None

        # Soft update email / last_login_ts
        conn.execute(
            """
            UPDATE users
            SET sso_email = COALESCE(?, sso_email),
                last_login_ts = ?
            WHERE id = ?
            """,
            (email, now_ts, row["id"]),
        )
        conn.commit()

        # Re-fetch to pick up changes
        return conn.execute(
            "SELECT * FROM users WHERE id = ?", (row["id"],)
        ).fetchone()

    # 2) Create a new user, using email as a base username if available
    base_username = email.split("@")[0] if email and "@" in email else subject
    username = base_username
    suffix = 1
    while conn.execute(
        "SELECT 1 FROM users WHERE username = ?",
        (username,),
    ).fetchone():
        suffix += 1
        username = f"{base_username}{suffix}"

    # Generate a random password (not actually used for SSO logins)
    random_pw = uuid.uuid4().hex
    pw_hash = generate_password_hash(random_pw)

    conn.execute(
        """
        INSERT INTO users
          (username, password_hash, role, linked_student_id,
           is_active, sso_provider, sso_subject, sso_email, last_login_ts)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (
            username,
            pw_hash,
            default_role,
            linked_student_id,
            provider,
            subject,
            email,
            now_ts,
        ),
    )
    conn.commit()

    return conn.execute(
        "SELECT * FROM users WHERE sso_provider = ? AND sso_subject = ? LIMIT 1",
        (provider, subject),
    ).fetchone()


def get_questions_for_objective(conn, oid):
    if not oid:
        return []
    return conn.execute(
        """
        SELECT question_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key
        FROM questions
        WHERE TRIM(UPPER(objective_id)) = TRIM(UPPER(?))
        ORDER BY question_id
        """,
        (oid,),
    ).fetchall()


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


def get_any_question_id(conn, objective_id):
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
    return f"Q-{objective_id}-quick"

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

    # Insert into responses
    conn.execute(
        """
        INSERT INTO responses (student_id, standard_id, level, question_id, correct, ts)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (student_id, std_id, lvl, question_id, is_correct, ts),
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
                order_val = r.get("order_in_band") or ""
                try:
                    order_val = int(order_val) if order_val != "" else None
                except ValueError:
                    order_val = None
                if not oid or not sid:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO objectives
                      (objective_id, standard_id, objective_text, order_in_band)
                    VALUES (?, ?, ?, ?)
                    """,
                    (oid, sid, text, order_val),
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
            session["role"] = user["role"]
            flash(f"Welcome, {user['username']}!")
            if user["role"] == "teacher":
                session["current_mode"] = "question"
                return redirect(url_for("index"))
            else:
                session["current_mode"] = "home"
                session["locked_payload"] = None
                return redirect(url_for("student_view"))
        else:
            flash("Invalid username or password.")

    login_html = """
    <!doctype html>
    <title>Login</title>
    <style>
      body{font-family:Arial, Helvetica, sans-serif;margin:24px;}
      .flash-box{
        background:#e7f7ee;border:1px solid #a8e0bf;color:#0f6b3a;
        padding:10px 12px;border-radius:8px;margin:10px 0;font-size:14px;
      }
      .btn{
        background:#2563eb;color:#fff;border:none;padding:8px 12px;
        border-radius:8px;cursor:pointer;font-size:14px;
      }
      .btn-sso-google{
        background:#ea4335;color:#fff;border:none;padding:8px 12px;
        border-radius:8px;cursor:pointer;font-size:14px;
      }
      .btn-sso-ms{
        background:#0078d4;color:#fff;border:none;padding:8px 12px;
        border-radius:8px;cursor:pointer;font-size:14px;
      }
      .divider{
        margin:16px 0;
        text-align:center;
        font-size:12px;
        color:#6b7280;
      }
      .divider span{
        background:#fff;
        padding:0 8px;
      }
      .divider:before,
      .divider:after{
        content:"";
        display:inline-block;
        width:40%;
        border-top:1px solid #e5e7eb;
        transform:translateY(-0.35em);
      }
      .divider:before{margin-right:8px;}
      .divider:after{margin-left:8px;}
    </style>

    {% with msgs = get_flashed_messages() %}
      {% if msgs %}
        <div class="flash-box">
          {% for m in msgs %}
            <div>✅ {{ m }}</div>
          {% endfor %}
        </div>
      {% endif %}
    {% endwith %}

    <h2>Login</h2>
    <form method="post" style="max-width:300px;">
      <label>Username<br><input name="username" required></label><br><br>
      <label>Password<br><input type="password" name="password" required></label><br><br>
      <button type="submit" class="btn">Login</button>
    </form>
    <p style="font-size:12px;color:#666;margin-top:4px;">
      Don’t have an account? Contact your teacher to be added.
    </p>

    <div class="divider"><span>OR</span></div>

    <div style="max-width:300px;">
      <p style="font-size:13px;color:#4b5563;margin-bottom:8px;">
        Sign in with your school account:
      </p>
      <form method="get" action="{{ url_for('sso_login', provider='google') }}" style="margin-bottom:8px;">
        <button type="submit" class="btn-sso-google">Continue with Google</button>
      </form>
      <form method="get" action="{{ url_for('sso_login', provider='microsoft') }}">
        <button type="submit" class="btn-sso-ms">Continue with Microsoft</button>
      </form>
    </div>
    """
    return render_template_string(login_html)


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

    client = oauth.create_client(provider)
    if not client:
        flash(f"SSO provider '{provider}' is not configured.")
        return redirect(url_for("login"))

    try:
        token = client.authorize_access_token()
    except Exception as e:
        flash(f"SSO login failed: {e}")
        return redirect(url_for("login"))

    # Try to get user info / ID token
    userinfo = None
    sub = None
    email = None

    # For OIDC providers, Authlib can parse the ID token:
    try:
        id_token = client.parse_id_token(token)
    except Exception:
        id_token = None

    if id_token:
        # OIDC-compliant: subject + email come from ID token
        sub = id_token.get("sub")
        email = id_token.get("email")
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
    # Decide default role based on email
    default_role = "student"
    if email and email.lower() == "lrbrowning89@gmail.com".lower():
        default_role = "teacher"

    user = get_or_create_sso_user(
        conn,
        provider=provider,
        subject=str(sub),
        email=email,
        default_role=default_role,
        linked_student_id=None,
    )

    if not user:
        flash("This SSO account is disabled or could not be created.")
        return redirect(url_for("login"))

    # Write login info to session (same as local login)
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]

    flash(f"Welcome, {user['username']} (SSO via {provider})!")

    if user["role"] == "teacher":
        session["current_mode"] = "question"
        return redirect(url_for("index"))
    else:
        session["current_mode"] = "home"
        session["locked_payload"] = None
        return redirect(url_for("student_view"))


# ---------- Teacher dashboard ----------
@app.route("/", methods=["GET", "POST"])
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

    # Save attempt (teacher-driven)
    if request.method == "POST" and request.form.get("action") == "save_attempt":
        student_id = request.form.get("student_id") or "S1"
        objective_id = request.form.get("objective_id")
        qid = request.form.get("question_id")
        resp = request.form.get("response")

        if not qid and objective_id:
            qid = get_any_question_id(conn, objective_id)

        keep_obj = request.values.get("class_objective") or request.values.get(
            "objective_id"
        )
        if not keep_obj:
            row_first = conn.execute(
                "SELECT objective_id FROM objectives ORDER BY objective_id LIMIT 1"
            ).fetchone()
            keep_obj = row_first["objective_id"] if row_first else None
        keep_period = request.values.get("period", "ALL")

        if not qid:
            flash(
                "No question available for that objective. Please add a question first."
            )
            return redirect(
                url_for(
                    "index",
                    class_objective=keep_obj,
                    period=keep_period,
                    student_id=student_id,
                )
            )

        row = conn.execute(
            "SELECT answer_key FROM questions WHERE question_id=?", (qid,)
        ).fetchone()
        correct = 1 if row and row["answer_key"] == resp else 0

        std_id, lvl, _ = record_attempt_and_response(
            conn,
            student_id=student_id,
            question_id=qid,
            response=(resp or f"Quick:{correct}"),
            is_correct=correct,
            objective_id=objective_id,
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
    class_obj = request.values.get(
        "class_objective", objs[0]["objective_id"] if objs else None
    )

    current_obj_for_questions = request.values.get("objective_id", class_obj)
    qrows = (
        get_questions_for_objective(conn, current_obj_for_questions)
        if current_obj_for_questions
        else []
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
<title>Adaptive NGSS - Teacher Dashboard</title>
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
  <div>
    <h1 style="margin:0;">Adaptive NGSS Teacher Dashboard</h1>
    <p style="font-size:13px;color:#555;margin:2px 0 0 0;">
      🧑‍🏫 Logged in as <strong>{{ session.get('username', 'teacher') }}</strong>
    </p>
  </div>
  <div style="display:flex;gap:8px;align-items:center;">
    <a href="{{ url_for('error_list') }}"
       class="btn"
       style="text-decoration:none;background:#4b5563;">
      View Error Log
    </a>

    <a href="{{ url_for('diagnostic_home') }}"
       class="btn"
       style="text-decoration:none;background:#7c3aed;">
      Diagnostic Arena
    </a>

    <form action="{{ url_for('logout') }}" method="get" style="margin:0;">
      <button type="submit" class="btn" style="background:#dc2626;">Logout</button>
    </form>

<form action="{{ url_for('student_view') }}" method="get" target="_blank" style="margin:0;margin-left:8px;">
  <button type="submit" class="btn" style="background:#4b5563;">Student Mode</button>
</form>

  </div>
</div>
<style>
  body{font-family:Arial, Helvetica, sans-serif;margin:24px}
  table{border-collapse:collapse;width:100%}
  th,td{border:1px solid #ddd;padding:8px;vertical-align:top}
  th{background:#f4f6f8;text-align:left}
  .ok{color:#0a0}.warn{color:#b58900}.bad{color:#c00}
  .card{border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin:16px 0;background:#fff}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .btn{background:#2563eb;color:#fff;border:none;padding:8px 12px;border-radius:8px;cursor:pointer}
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
            </td>
          </tr>
        {% endfor %}
      </table>
    {% else %}
      <p><em>No users in the system yet.</em></p>
    {% endif %}
  </div>

  <div class="card">
    <h2>Record an Attempt</h2>
    {% if not objs %}
      <p>No objectives found. Run the importer first.</p>
    {% else %}
      <form method="get" class="toolbar" style="margin:0 0 8px 0">
        <input type="hidden" name="student_id" value="{{active_student}}">
        <input type="hidden" name="class_objective" value="{{class_obj}}">
        <input type="hidden" name="period" value="{{selected_period}}">
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
        <input type="hidden" name="objective_id" value="{{ request.values.get('objective_id', class_obj) }}">

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
          <p><em>Showing questions for objective: {{ request.values.get('objective_id', class_obj) }}</em></p>
          <label>Question
            <select name="question_id">
              {% for q in qrows %}
                <option value="{{q['question_id']}}">
                  {{q['question_id']}} - {{ (q['stem'][:60] if q['stem'] else '') }}{{ ('...' if q['stem'] and (q['stem']|length)>60 else '') }}
                </option>
              {% endfor %}
            </select>
          </label>
          <label>Response
            <select name="response"><option>A</option><option>B</option><option>C</option><option>D</option></select>
          </label>
          <p><button class="btn" type="submit">Save Attempt</button></p>
        {% else %}
          <p>No questions for this objective.</p>
        {% endif %}
      </form>
    {% endif %}
  </div>
</div> <!-- end grid -->

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

<div class="card">
  <div class="headerbar">
    <h2 style="margin:0">Objectives — Rolling Avg & Next Node</h2>
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
</div>

<div class="card">
  <h2>Class Aggregate — Rolling Avg & Next Node (Period: {{selected_period}})</h2>
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
</div>

<div class="card">
  <h2>Class View</h2>
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
        <th>Engine</th>
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
            {% if std_id %}
              <a
                href="{{ url_for('engine_debug', student_id=sid, standard_id=std_id) }}"
                target="_blank"
                class="btn-mini"
                style="background:#4b5563;color:#fff;text-decoration:none;"
              >
                🧪
              </a>
            {% else %}
              <span style="font-size:11px;color:#9ca3af;">n/a</span>
            {% endif %}
          </td>
        </tr>
      {% endfor %}
    </table>
  {% endif %}
</div>
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
        qrows=qrows,
        period_opts=period_opts,
        selected_period=selected_period,
        pii_mode=pii_mode,
        display_name=display_name,
        mastery=mastery,
        practice=practice,
        APP_NAME=APP_NAME,
        APP_VERSION=APP_VERSION,
        db_name=db_name,
        db_time=db_time,
        engine_ok=engine_ok,
        engine_checks=engine_checks,
        recent_errors=recent_errors,
        all_users=all_users,
    )


# ---------- Student view ----------
@app.route("/student", methods=["GET", "POST"])
@require_login
def student_view():
    conn = get_conn()
    feedback = None

    role = session.get("role")
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

    def ms_ls1_1_objective_for_level(level: int) -> str:
        if level == 1:
            return "MS-LS1-1A"
        if level == 2:
            return "MS-LS1-1B"
        return "MS-LS1-1C"

    def get_engine_target():
        """
        Choose the student's current standard/objective from progress_state.
        MS-LS1-1 still uses the temporary A/B/C mapper.
        Other standards use the first available objective for that standard.
        If no content exists yet, objective_id stays None.
        """
        ps = conn.execute(
            """
            SELECT standard_id, current_level, status, locked, locked_reason
            FROM progress_state
            WHERE student_id = ?
            ORDER BY last_update DESC
            LIMIT 1
            """,
            (student_id,),
        ).fetchone()

        if ps:
            std = ps["standard_id"]
            level = int(ps["current_level"] or 1)

            if std == "MS-LS1-1" and ps["status"] == "completed":
                return "MS-LS1-1", level, None, ps

            if std == "MS-LS1-1":
                return "MS-LS1-1", level, ms_ls1_1_objective_for_level(level), ps

            obj = conn.execute(
                """
                SELECT objective_id
                FROM objectives
                WHERE standard_id = ?
                ORDER BY order_in_band, objective_id
                LIMIT 1
                """,
                (std,),
            ).fetchone()

            return std, level, obj["objective_id"] if obj else None, ps

        return "MS-LS1-1", 1, "MS-LS1-1A", ps

    current_std, current_level, objective_id, progress_row = get_engine_target()

    if role == "teacher":
        requested_objective = request.values.get("objective_id")
        if requested_objective:
            objective_id = requested_objective
            std_row = conn.execute(
                "SELECT standard_id FROM objectives WHERE objective_id=?",
                (objective_id,),
            ).fetchone()
            current_std = std_row["standard_id"] if std_row else current_std
            current_level = get_level_for_objective(conn, objective_id)

    # Completion page for the temporary MS-LS1-1 walkthrough.
    if progress_row and progress_row["standard_id"] == "MS-LS1-1" and progress_row["status"] == "completed":
        html_done = """
<!doctype html>
<title>MS-LS1-1 Complete</title>
<style>
  body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#f3f4f6}
  .card{max-width:700px;margin:0 auto 16px auto;background:#fff;border-radius:10px;padding:16px 20px;border:1px solid #e5e7eb}
  .btn{background:#2563eb;color:#fff;border:none;padding:8px 12px;border-radius:8px;cursor:pointer;text-decoration:none;display:inline-block}
</style>
<div class="card">
  <h2>✅ MS-LS1-1 Complete</h2>
  <p>You completed the MS-LS1-1 walkthrough.</p>
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

    # ---------- Handle Continue Learning from student home ----------
    if request.method == "POST" and request.form.get("action") == "continue_learning":
        session["current_mode"] = "question"
        session["locked_payload"] = None
        return redirect(url_for("student_view"))

    # ---------- Handle answer submission ----------
    if request.method == "POST" and request.form.get("action") == "answer":
        if role == "teacher":
            student_id = request.form.get("student_id") or student_id
            objective_id = request.form.get("objective_id") or objective_id

        qid = request.form.get("question_id")
        resp = request.form.get("response")

        if not qid and objective_id:
            qid = get_any_question_id(conn, objective_id)

        if qid:
            row = conn.execute(
                "SELECT answer_key FROM questions WHERE question_id=?",
                (qid,),
            ).fetchone()
            correct = 1 if row and row["answer_key"] == resp else 0

            std_row = conn.execute(
                "SELECT standard_id FROM objectives WHERE objective_id=?",
                (objective_id,),
            ).fetchone()
            current_std_id = std_row["standard_id"] if std_row else current_std

            level_override = current_level if role == "student" else None

            std_id, lvl, ts = record_attempt_and_response(
                conn,
                student_id=student_id,
                question_id=qid,
                response=resp,
                is_correct=correct,
                objective_id=objective_id,
                level_override=level_override,
                attempt_prefix="ST",
            )
            conn.commit()

            engine_decision = None
            engine_summary = None

            if std_id != "UNKNOWN":
                recent_count_row = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM responses
                    WHERE student_id = ? AND standard_id = ? AND level = ?
                    """,
                    (student_id, std_id, lvl),
                ).fetchone()
                recent_count = int(recent_count_row["n"] or 0) if recent_count_row else 0

                # True Rolling-7 gate: do not route/lock/advance until 7 responses exist at this standard+level.
                if recent_count < 7:
                    avg_now = ae.rolling7_avg(student_id, std_id, lvl)
                    ae.set_state(student_id, std_id, lvl, "practicing", avg_now)
                    engine_decision = {
                        "status": "question",
                        "action": "collect_rolling7",
                        "standard": std_id,
                        "level": lvl,
                        "avg": avg_now,
                        "reason": f"collecting_data_{recent_count}_of_7",
                    }
                else:
                    engine_decision = ae.process_after_response(student_id, std_id)

                    # Temporary MS-LS1-1 completion fallback: the engine currently loops to itself when progression_links is empty.
                    if (
                        isinstance(engine_decision, dict)
                        and engine_decision.get("action") == "advance_standard"
                        and engine_decision.get("from") == ("MS-LS1-1", 3)
                        and engine_decision.get("to") == ("MS-LS1-1", 1)
                    ):
                        now = int(time.time())
                        conn.execute(
                            """
                            INSERT INTO progress_state
                              (student_id, standard_id, current_level, status, rolling_avg, locked, locked_reason, last_update)
                            VALUES (?, ?, ?, ?, ?, 0, NULL, ?)
                            ON CONFLICT(student_id, standard_id) DO UPDATE SET
                              current_level=excluded.current_level,
                              status=excluded.status,
                              rolling_avg=excluded.rolling_avg,
                              locked=0,
                              locked_reason=NULL,
                              last_update=excluded.last_update
                            """,
                            (student_id, "MS-LS1-1", 3, "completed", 1.0, now),
                        )
                        conn.execute(
                            """
                            INSERT INTO notifications (student_id, standard_id, event, details, ts)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (student_id, "MS-LS1-1", "complete", "✅ Completed MS-LS1-1 walkthrough", now),
                        )
                        conn.commit()
                        engine_decision = {
                            "status": "completed",
                            "action": "complete_standard",
                            "standard": "MS-LS1-1",
                            "level": 3,
                            "reason": "ms_ls1_1_final_objective_mastered",
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
                elif isinstance(engine_decision, dict) and engine_decision.get("status") == "completed":
                    session["current_mode"] = "completed"
                    session["locked_payload"] = None
                else:
                    session["current_mode"] = "question"
                    session["locked_payload"] = None

            feedback = {
                "correct": bool(correct),
                "engine_summary": engine_summary,
                "engine_decision": engine_decision,
            }

            # Re-read engine target after response processing.
            current_std, current_level, objective_id, progress_row = get_engine_target()
        else:
            feedback = {"error": "No question found for this objective."}

    if session.get("current_mode") == "completed":
        return redirect(url_for("student_view"))

    available_standards = get_available_standards(conn)
    available_standard_cards = [
        {
            "standard_id": row_get(standard, "standard_id", "Unknown standard"),
            "core_idea": row_get(standard, "core_idea", "Science"),
            "grade_band": row_get(standard, "grade_band", "Available"),
            "objective_count": row_get(standard, "objective_count", 0),
        }
        for standard in available_standards
    ]
    current_standard_meta = get_standard_meta(conn, current_std)
    response_count = get_response_count_for_level(conn, student_id, current_std, current_level)
    rolling_avg = float(row_get(progress_row, "rolling_avg", 0.0) or 0.0)
    progress_percent = max(0, min(100, round(rolling_avg * 100)))
    progress_status = row_get(progress_row, "status", "not started")
    current_core_idea = row_get(current_standard_meta, "core_idea", "Science")
    current_grade_band = row_get(current_standard_meta, "grade_band", "Current band")

    if role == "student" and session.get("current_mode") == "home":
        home_html = """
<!doctype html>
<title>Adaptive NGSS - Student Home</title>
<style>
  body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#f3f4f6;color:#111827}
  .shell{max-width:900px;margin:0 auto}
  .card{background:#fff;border-radius:10px;padding:18px 22px;border:1px solid #e5e7eb;margin-bottom:16px}
  .header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:12px}
  .btn{background:#2563eb;color:#fff;border:none;padding:10px 14px;border-radius:8px;cursor:pointer;text-decoration:none;display:inline-block;font-weight:700}
  .btn-danger{background:#dc2626}
  .muted{font-size:13px;color:#4b5563}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .metric{border:1px solid #e5e7eb;border-radius:8px;padding:12px;background:#f9fafb}
  .metric strong{display:block;font-size:22px;margin-top:4px}
  .progress{height:12px;background:#e5e7eb;border-radius:999px;overflow:hidden;margin:10px 0 4px}
  .bar{height:100%;background:#16a34a;width:{{ progress_percent }}%}
  table{width:100%;border-collapse:collapse;margin-top:8px}
  th,td{text-align:left;border-bottom:1px solid #e5e7eb;padding:9px 8px;font-size:14px;vertical-align:top}
  th{font-size:12px;color:#4b5563;text-transform:uppercase;letter-spacing:.04em}
  .pill{display:inline-block;padding:3px 7px;border-radius:999px;background:#dbeafe;color:#1d4ed8;font-size:12px;font-weight:700}
  @media(max-width:700px){body{margin:14px}.grid{grid-template-columns:1fr}.header{display:block}.logout{margin-top:10px}}
</style>

<div class="shell">
  <div class="card">
    <div class="header">
      <div>
        <h2 style="margin:0;">Student Home</h2>
        <p class="muted" style="margin:4px 0 0 0;">
          Logged in as <strong>{{ session.get('username', 'student') }}</strong>
        </p>
      </div>
      <form class="logout" action="{{ url_for('logout') }}" method="get" style="margin:0;">
        <button type="submit" class="btn btn-danger">Logout</button>
      </form>
    </div>

    {% with msgs = get_flashed_messages() %}
      {% if msgs %}
        <div style="background:#e7f7ee;border:1px solid #a8e0bf;color:#0f6b3a;padding:10px 12px;border-radius:8px;margin:10px 0;font-size:14px;">
          {% for m in msgs %}
            <div>{{ m }}</div>
          {% endfor %}
        </div>
      {% endif %}
    {% endwith %}

    <div class="grid">
      <div class="metric">
        <span class="muted">Current assigned standard</span>
        <strong>{{ current_std }}</strong>
        {% if current_standard_meta %}
          <div class="muted">{{ current_core_idea }} | {{ current_grade_band }}</div>
        {% endif %}
      </div>
      <div class="metric">
        <span class="muted">Current progress</span>
        <strong>Level {{ current_level }}</strong>
        <div class="progress" aria-label="Current progress"><div class="bar"></div></div>
        <div class="muted">{{ progress_percent }}% Rolling-7 average | {{ response_count }} of 7 responses at this level | {{ progress_status }}</div>
      </div>
    </div>

    <form method="post" style="margin:16px 0 0 0;">
      <input type="hidden" name="action" value="continue_learning">
      <button class="btn" type="submit">Continue Learning</button>
    </form>
  </div>

  <div class="card">
    <h3 style="margin:0 0 8px 0;">Available Standards</h3>
    <table>
      <thead>
        <tr>
          <th>Standard</th>
          <th>Core Idea</th>
          <th>Grade Band</th>
          <th>Objectives</th>
        </tr>
      </thead>
      <tbody>
        {% for standard in available_standards %}
          <tr>
            <td>
              <strong>{{ standard.standard_id }}</strong>
              {% if standard.standard_id == current_std %}
                <span class="pill">Current</span>
              {% endif %}
            </td>
            <td>{{ standard.core_idea }}</td>
            <td>{{ standard.grade_band }}</td>
            <td>{{ standard.objective_count }}</td>
          </tr>
        {% else %}
          <tr><td colspan="4"><em>No standards are available yet.</em></td></tr>
        {% endfor %}
      </tbody>
    </table>
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
            progress_percent=progress_percent,
            progress_status=progress_status,
            response_count=response_count,
        )

    locked_review = session.get("locked_payload") if session.get("current_mode") == "locked" else None
    current_question = None

    if not locked_review and objective_id:
        qrows = get_questions_for_objective(conn, objective_id)

        if qrows:
            total = len(qrows)
            if total >= 6:
                level_1_rows = qrows[:3]
                level_2_rows = qrows[3:5]
                level_3_rows = qrows[5:]
            elif total >= 3:
                level_1_rows = qrows[:1]
                level_2_rows = qrows[1:2]
                level_3_rows = qrows[2:]
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

    student_html = """
<!doctype html>
<title>Adaptive NGSS - Student Practice</title>
<style>
  body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#f3f4f6}
  .card{max-width:700px;margin:0 auto 16px auto;background:#fff;
        border-radius:10px;padding:16px 20px;border:1px solid #e5e7eb}
  .header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
  .btn{background:#2563eb;color:#fff;border:none;padding:8px 12px;border-radius:8px;cursor:pointer}
  .choice{margin:4px 0;}
  .ok{color:#16a34a}
  .bad{color:#dc2626}
  input,select{padding:6px 8px;border:1px solid #cbd5e1;border-radius:6px}
  .toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
</style>

<div class="card">
  <div class="header">
    <div>
      <h2 style="margin:0;">Student Practice TEST-16</h2>
      <p style="font-size:13px;color:#555;margin:2px 0 0 0;">
        👩‍🎓 Logged in as <strong>{{ session.get('username', 'student') }}</strong>
        | role = <strong>{{ session.get('role') }}</strong>
      </p>
    </div>
    <form action="{{ url_for('logout') }}" method="get" style="margin:0;">
      <button type="submit" class="btn" style="background:#dc2626;">Logout</button>
    </form>
  </div>

  {% with msgs = get_flashed_messages() %}
    {% if msgs %}
      <div style="background:#e7f7ee;border:1px solid #a8e0bf;color:#0f6b3a;padding:10px 12px;border-radius:8px;margin:10px 0;font-size:14px;">
        {% for m in msgs %}
          <div>✅ {{ m }}</div>
        {% endfor %}
      </div>
    {% endif %}
  {% endwith %}

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

      {% if feedback.engine_summary %}
        <p style="font-size:13px;color:#555;">
          Engine (standard-level): {{ feedback.engine_summary }}
        </p>
      {% endif %}
      <p style="font-size:13px;color:#555;">
        Debug target: objective={{ objective_id }}
      </p>
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
    <h3>
      Objective: {{ objective_id }}
      {% if session.get('role') == 'student' %}
        <span style="font-size:13px;color:#666;">(engine-selected, level {{ current_level }})</span>
      {% endif %}
    </h3>
    <p>{{current_question['stem']}}</p>
    <form method="post">
      <input type="hidden" name="action" value="answer">
      <input type="hidden" name="student_id" value="{{student_id}}">
      <input type="hidden" name="objective_id" value="{{objective_id}}">
      <input type="hidden" name="question_id" value="{{current_question['question_id']}}">

      <div class="choice"><label><input type="radio" name="response" value="A" required> A. {{current_question['choice_a']}}</label></div>
      <div class="choice"><label><input type="radio" name="response" value="B"> B. {{current_question['choice_b']}}</label></div>
      <div class="choice"><label><input type="radio" name="response" value="C"> C. {{current_question['choice_c']}}</label></div>
      <div class="choice"><label><input type="radio" name="response" value="D"> D. {{current_question['choice_d']}}</label></div>

      <p><button class="btn" type="submit">Submit Answer</button></p>
    </form>
  {% else %}
    <p><em>No questions are available for this objective yet.</em></p>
  {% endif %}
</div>
    """
    return render_template_string(
        student_html,
        students=students,
        objs=objs,
        student_id=student_id,
        objective_id=objective_id,
        current_question=current_question,
        feedback=feedback,
        locked_review=locked_review,
        current_level=current_level,
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
      <h2>🧪 Diagnostic Session</h2>
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
      <h2 style="margin-top:0;">🧪 Diagnostic Arena (MVP)</h2>
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
      <h2 style="margin-top:0;">🧪 Diagnostic Session</h2>
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
    conn = get_conn()
    if request.method == "GET":
        flash("User account management is on the Teacher Dashboard.")
        return redirect(url_for("index"))

    action = request.form.get("action")
    user_id = request.form.get("user_id")

    if not action or not user_id:
        flash("Missing user action or id.")
        return redirect(url_for("index"))

    if action == "reset_pw":
        new_pw = request.form.get("new_password", "").strip()
        if not new_pw:
            flash("New password is required.")
        else:
            pw_hash = generate_password_hash(new_pw)
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?", (pw_hash, user_id)
            )
            conn.commit()
            flash("Password updated.")
    elif action == "toggle_active":
        conn.execute(
            """
            UPDATE users
            SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
            WHERE id=?
            """,
            (user_id,),
        )
        conn.commit()
        flash("User status updated.")
    else:
        flash("Unknown user action.")

    return redirect(url_for("index"))


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

# ---------- Engine debug viewer ----------
@app.route("/engine_debug/<student_id>/<standard_id>")
@require_teacher
def engine_debug(student_id, standard_id):
    conn = get_conn()

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

print(app.url_map)

if __name__ == "__main__":
    app.run(debug=True)
