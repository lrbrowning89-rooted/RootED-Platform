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
            COUNT(DISTINCT o.objective_id) AS objective_count,
            COUNT(DISTINCT q.question_id) AS question_count
        FROM standards s
        LEFT JOIN objectives o ON o.standard_id = s.standard_id
        LEFT JOIN questions q ON q.objective_id = o.objective_id
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


def get_progress_by_standard(conn, student_id):
    if not student_id:
        return {}
    rows = conn.execute(
        """
        SELECT standard_id, current_level, status, rolling_avg, locked, last_update
        FROM progress_state
        WHERE student_id = ?
        ORDER BY last_update DESC
        """,
        (student_id,),
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


def get_latest_student_progress(conn, student_id):
    return conn.execute(
        """
        SELECT ps.student_id,
               ps.standard_id,
               ps.current_level,
               ps.status,
               ps.rolling_avg,
               ps.locked,
               ps.locked_reason,
               ps.last_update,
               s.core_idea,
               s.grade_band
        FROM progress_state ps
        LEFT JOIN standards s ON s.standard_id = ps.standard_id
        WHERE ps.student_id = ?
        ORDER BY ps.last_update DESC
        LIMIT 1
        """,
        (student_id,),
    ).fetchone()


def get_active_objective_for_student(conn, student_id, standard_id):
    if not student_id or not standard_id:
        return None
    try:
        return conn.execute(
            """
            SELECT sos.current_objective_id AS objective_id,
                   o.objective_text
            FROM student_objective_state sos
            LEFT JOIN objectives o ON o.objective_id = sos.current_objective_id
            WHERE sos.student_id = ?
              AND sos.standard_id = ?
              AND sos.status = 'active'
            LIMIT 1
            """,
            (student_id, standard_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


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
        avg = float(row_get(progress, "rolling_avg", 0.0))
        status = (row_get(progress, "status", "") or "").lower()
        locked = bool(row_get(progress, "locked", 0))
        frustrated = frustration_active(conn, sid, objective_id) if objective_id else False

        not_started = (
            counts["attempts"] == 0
            and counts["responses"] == 0
            and counts["progress_rows"] == 0
        )
        mastered = status in ("mastered", "completed", "complete") or (
            response_count >= 7 and avg >= mastery
        )
        low_average = counts["responses"] > 0 and avg < practice
        no_recent_progress = counts["progress_rows"] > 0 and response_count == 0
        needs_help = bool(locked or frustrated or low_average or no_recent_progress)
        progressing = bool(progress and not mastered and not needs_help)

        reasons = []
        if locked:
            reasons.append(row_get(progress, "locked_reason", "locked") or "locked")
        if frustrated:
            reasons.append("frustration signal")
        if low_average:
            reasons.append(f"rolling avg {round(avg * 100)}%")
        if no_recent_progress:
            reasons.append("no responses at current level")
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
    period_filter = ""
    params = []
    if selected_period and selected_period != "ALL":
        period_filter = "WHERE st.class_period = ?"
        params.append(selected_period)

    return conn.execute(
        f"""
        SELECT ps.standard_id,
               COALESCE(s.core_idea, '') AS core_idea,
               COALESCE(s.grade_band, '') AS grade_band,
               COUNT(DISTINCT ps.student_id) AS student_count,
               SUM(CASE WHEN ps.locked = 1 THEN 1 ELSE 0 END) AS locked_count,
               SUM(CASE WHEN ps.status IN ('mastered', 'completed', 'complete') THEN 1 ELSE 0 END) AS completed_count,
               ROUND(AVG(ps.rolling_avg), 2) AS avg_progress,
               COUNT(DISTINCT o.objective_id) AS objective_count,
               COUNT(DISTINCT q.question_id) AS question_count
        FROM progress_state ps
        JOIN students st ON st.student_id = ps.student_id
        LEFT JOIN standards s ON s.standard_id = ps.standard_id
        LEFT JOIN objectives o ON o.standard_id = ps.standard_id
        LEFT JOIN questions q ON q.objective_id = o.objective_id
        {period_filter}
        GROUP BY ps.standard_id, s.core_idea, s.grade_band
        ORDER BY student_count DESC, ps.standard_id
        """,
        params,
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


def get_preview_question(conn, question_id):
    if not question_id:
        return None
    return conn.execute(
        """
        SELECT q.question_id,
               q.objective_id,
               q.stem,
               q.choice_a,
               q.choice_b,
               q.choice_c,
               q.choice_d,
               COALESCE(o.standard_id, '') AS standard_id,
               COALESCE(o.objective_text, '') AS objective_text
        FROM questions q
        LEFT JOIN objectives o ON o.objective_id = q.objective_id
        WHERE q.question_id = ?
        LIMIT 1
        """,
        (question_id,),
    ).fetchone()


def get_first_preview_question_id(conn, objective_id=None):
    if objective_id:
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

    row = conn.execute(
        """
        SELECT question_id
        FROM questions
        ORDER BY question_id
        LIMIT 1
        """
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
  input,select{padding:6px 8px;border:1px solid #cbd5e1;border-radius:6px}
  .muted{font-size:13px;color:#555}
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
    <a class="btn btn-muted" href="{{ url_for('index') }}">Back to Dashboard</a>
  </div>

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
            {{ (q['stem'][:80] if q['stem'] else q['question_id']) }}{{ ('...' if q['stem'] and (q['stem']|length)>80 else '') }} ({{ q['question_id'] }})
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
    <h3>
      Objective: {{ current_question['objective_id'] }}
      <span class="muted">({{ current_question['standard_id'] }})</span>
    </h3>
    <p>{{ current_question['stem'] }}</p>
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
  {% else %}
    <p><em>No questions are available to preview yet.</em></p>
  {% endif %}
</div>
    """
    return render_template_string(
        html,
        objs=objs,
        qrows=qrows,
        selected_objective_id=selected_objective_id,
        selected_question_id=selected_question_id,
        current_question=current_question,
        current_model_asset=current_model_asset,
    )


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
          width:100%;
          border:none;
          min-height:46px;
          padding:11px 14px;
          border-radius:8px;
          cursor:pointer;
          font-size:15px;
          font-weight:750;
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

              <div class="divider"><span>OR</span></div>

              <p class="sso-copy">Sign in with your school account:</p>
              <form method="get" action="{{ url_for('sso_login', provider='google') }}" class="sso-form">
                <button type="submit" class="btn-sso-google">Continue with Google</button>
              </form>
              <form method="get" action="{{ url_for('sso_login', provider='microsoft') }}" class="sso-form">
                <button type="submit" class="btn-sso-ms">Continue with Microsoft</button>
              </form>
            </div>
          </aside>
        </section>
      </main>
    </body>
    </html>
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
    preview_objective_id = request.values.get("preview_objective_id", class_obj)
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
    <p class="section-note">Uses existing locked status, frustration signal, low Rolling-7 average, and missing current-level responses.</p>
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
              Rolling avg: {{ (row.rolling_avg * 100)|round|int }}%
              {% if row.objective_id %} | Objective {{ row.objective_id }}{% endif %}
              {% if row.last_activity %} | Last activity {{ format_ts(row.last_activity) }}{% endif %}
            </div>
            {% if row.standard_id %}
              <p style="margin:10px 0 0;">
                <a class="btn-mini" style="background:#4b5563;color:#fff;text-decoration:none;"
                   href="{{ url_for('engine_debug', student_id=row.student_id, standard_id=row.standard_id) }}"
                   target="_blank">View Details</a>
              </p>
            {% endif %}
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
            <td>{{ ((row["avg_progress"] or 0) * 100)|round|int }}%</td>
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
    <p class="section-note">Students with no attempts, responses, or progress state.</p>
    {% if not_started_rows %}
      <table>
        <tr><th>Student</th><th>Grade</th><th>Period</th><th>Next Click</th></tr>
        {% for row in not_started_rows %}
          <tr>
            <td><strong>{{ row.name }}</strong><br><span style="font-size:12px;color:#6b7280;">{{ row.student_id }}</span></td>
            <td>{{ row.grade or "" }}</td>
            <td>{{ row.period or "" }}</td>
            <td><a class="btn-mini" style="background:#2f6f4e;color:#fff;text-decoration:none;" href="{{ url_for('student_view', student_id=row.student_id) }}" target="_blank">View Student</a></td>
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
        <tr><th>Student</th><th>Status</th><th>Standard</th><th>Rolling Avg</th></tr>
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
            <td>{{ (row.rolling_avg * 100)|round|int }}%</td>
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
                  {{ (q['stem'][:90] if q['stem'] else q['question_id']) }}{{ ('...' if q['stem'] and (q['stem']|length)>90 else '') }} ({{q['question_id']}})
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
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 10px 0;">
            {% for q in qrows %}
              <a class="btn-mini"
                 style="background:#4b5563;color:#fff;text-decoration:none;"
                 href="{{ url_for('teacher_question_preview', question_id=q['question_id']) }}"
                 target="_blank">Preview {{ q['question_id'] }}</a>
            {% endfor %}
          </div>
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
        return "MS-LS1-1B"
    
    def get_first_objective_for_standard(std):
        row = conn.execute(
            """
SELECT o.objective_id
FROM objectives o
WHERE o.standard_id = ?
  AND EXISTS (
      SELECT 1
      FROM questions q
      WHERE q.objective_id = o.objective_id
  )
ORDER BY o.order_in_band, o.objective_id
LIMIT 1
            """,
            (std,),
        ).fetchone()
        return row["objective_id"] if row else None

    def get_next_objective_for_standard(std, current_objective_id):
        current = conn.execute(
            """
            SELECT order_in_band
            FROM objectives
            WHERE objective_id = ?
            """,
            (current_objective_id,),
        ).fetchone()

        if not current:
            return None

        row = conn.execute(
            """
SELECT o.objective_id
FROM objectives o
WHERE o.standard_id = ?
  AND o.order_in_band > ?
  AND EXISTS (
      SELECT 1
      FROM questions q
      WHERE q.objective_id = o.objective_id
  )
ORDER BY o.order_in_band, o.objective_id
LIMIT 1
            """,
            (std, current["order_in_band"]),
        ).fetchone()

        return row["objective_id"] if row else None

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
        conn.commit()

    def get_engine_target():
        """
        Choose the student's current standard/objective from progress_state.
        MS-LS1-1 uses the approved A/B mapper; unexpected higher levels fall back to B.
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


            saved_obj = conn.execute(
                """
                SELECT current_objective_id
                FROM student_objective_state
                WHERE student_id = ?
                  AND standard_id = ?
                  AND status = 'active'
                """,
                (student_id, std),
            ).fetchone()

            if saved_obj:
                return std, level, saved_obj["current_objective_id"], ps

            first_obj = get_first_objective_for_standard(std)
            if first_obj:
                set_student_objective(student_id, std, first_obj)

            return std, level, first_obj, ps

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
                    FROM responses r
                    JOIN questions q ON q.question_id = r.question_id
                    WHERE r.student_id = ?
                      AND r.standard_id = ?
                      AND r.level = ?
                      AND q.objective_id = ?
                    """,
                    (student_id, std_id, lvl, objective_id),
                ).fetchone()

                recent_count = int(recent_count_row["n"] or 0) if recent_count_row else 0

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

                    if (
                        isinstance(engine_decision, dict)
                        and engine_decision.get("action") in ("next_standard_found", "standard_complete")
                    ):
                        next_objective = get_next_objective_for_standard(
                            std_id,
                            objective_id,
                        )

                        if next_objective:
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
        terminal_payload = session.get("terminal_completion_payload") or {}
        completed_standard = terminal_payload.get("standard", current_std)
        completed_reason = terminal_payload.get("reason", "standard_complete")

        html_done = """
<!doctype html>
<title>Standard Complete</title>
<style>
  body{font-family:Arial, Helvetica, sans-serif;margin:24px;background:#f3f4f6}
  .card{max-width:720px;margin:0 auto;background:#fff;border-radius:10px;padding:20px 24px;border:1px solid #e5e7eb}
  .btn{background:#2563eb;color:#fff;border:none;padding:10px 14px;border-radius:8px;cursor:pointer;text-decoration:none;display:inline-block}
  .muted{font-size:13px;color:#555}
</style>
<div class="card">
  <h2>🎉 Standard Complete!</h2>
  <p>Congratulations! You have completed all objectives for this standard.</p>
  <p>There are no additional learning pathways assigned at this time.</p>
  <p class="muted">Completed standard: <strong>{{ completed_standard }}</strong></p>
  <p class="muted">Reason: {{ completed_reason }}</p>

  <form method="post" style="margin-top:16px;">
    <input type="hidden" name="action" value="return_dashboard">
    <button class="btn" type="submit">Return to Student Dashboard</button>
  </form>
</div>
        """
        return render_template_string(
            html_done,
            completed_standard=completed_standard,
            completed_reason=completed_reason,
        )

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
        standard["rolling_avg"] = row_get(standard_progress, "rolling_avg", None)
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
    response_count = get_response_count_for_level(conn, student_id, current_std, current_level)
    rolling_avg = float(row_get(progress_row, "rolling_avg", 0.0) or 0.0)
    progress_percent = max(0, min(100, round(rolling_avg * 100)))
    progress_status = row_get(progress_row, "status", "not started")
    response_goal = 7
    response_percent = max(0, min(100, round((response_count / response_goal) * 100)))
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
  .stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}
  .stat{background:rgba(255,255,255,.72);border:1px solid rgba(47,111,78,.13);border-radius:8px;padding:12px}
  .stat strong{display:block;font-size:22px;color:#1f3d2f;margin-top:4px}
  .progress{height:12px;background:#e1e7e2;border-radius:999px;overflow:hidden;margin:10px 0 6px}
  .bar{height:100%;background:linear-gradient(90deg,var(--leaf),#6b9b58);width:{{ progress_percent }}%}
  .response-bar{height:100%;background:linear-gradient(90deg,var(--gold),#d7aa58);width:{{ response_percent }}%}
  .cta-panel{display:flex;flex-direction:column;justify-content:space-between;gap:16px;background:#fffefa;border:1px solid var(--line);border-radius:8px;padding:18px}
  .cta-panel h2{margin:0;color:#183629;font-size:24px}
  .cta-panel p{margin:8px 0 0;color:#4d5e55;line-height:1.5}
  .cta-button{width:100%;min-height:52px;padding:13px 16px;background:var(--leaf);color:#fff;box-shadow:0 14px 24px rgba(47,111,78,.18);font-size:16px}
  .cta-button:hover{background:var(--leaf-dark)}
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
        <div class="status-row">
          <span class="pill pill-current">{{ progress_status }}</span>
          <span class="muted">Level {{ current_level }}</span>
        </div>
        <div class="stats">
          <div class="stat">
            <span class="muted">Rolling-7 progress</span>
            <strong>{{ progress_percent }}%</strong>
            <div class="progress" aria-label="Rolling-7 progress"><div class="bar"></div></div>
          </div>
          <div class="stat">
            <span class="muted">Responses at this level</span>
            <strong>{{ response_count }} / 7</strong>
            <div class="progress" aria-label="Responses collected"><div class="response-bar"></div></div>
          </div>
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
              <span>{{ standard.question_count }} questions</span>
              {% if standard.level %}
                <span>Level {{ standard.level }}</span>
              {% endif %}
            </div>
            {% if standard.question_count == 0 %}
              <p class="muted">Questions are not available for this standard yet.</p>
            {% elif standard.rolling_avg is not none %}
              <p class="muted">Latest Rolling-7 average: {{ (standard.rolling_avg * 100)|round|int }}%</p>
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
            progress_percent=progress_percent,
            response_percent=response_percent,
            progress_status=progress_status,
            response_count=response_count,
        )

    locked_review = session.get("locked_payload") if session.get("current_mode") == "locked" else None
    current_question = None
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
  .model-asset{margin:14px 0 16px 0;padding:12px;border:1px solid #d7dee8;border-radius:8px;background:#f8fafc}
  .model-asset-title{margin:0 0 8px 0;font-weight:700;color:#1f2937}
  .model-asset-caption{margin:8px 0 0 0;font-size:13px;color:#555;line-height:1.4}
  .model-asset img{display:block;max-width:100%;height:auto;margin:0 auto;border-radius:6px}
  .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
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
        current_model_asset=current_model_asset,
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
