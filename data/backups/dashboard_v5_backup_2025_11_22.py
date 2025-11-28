from flask import (
    Flask,
    request,
    render_template_string,
    make_response,
    session,
    redirect,
    url_for,
    flash,
    get_flashed_messages,
)
import sqlite3, time, csv, io, uuid, os
from functools import wraps

from werkzeug.security import generate_password_hash, check_password_hash

from app_core.config import FERPA_ENFORCED, DEFAULT_PII_MODE, SECRET_KEY
from app_core import adaptive_engine as ae

# ---------- App metadata ----------
APP_VERSION = "v0.5 – Rolling-7 Engine Active"
APP_NAME = "Adaptive NGSS Platform"

# ---------- Database setup ----------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("NGSS_DB", os.path.join(BASE_DIR, "data", "ngss.db"))

# ---------- Flask app setup ----------
app = Flask(__name__)
app.secret_key = SECRET_KEY

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

    # Users
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
          id                INTEGER PRIMARY KEY AUTOINCREMENT,
          username          TEXT UNIQUE NOT NULL,
          password_hash     TEXT NOT NULL,
          role              TEXT NOT NULL CHECK(role IN ('teacher', 'student')),
          linked_student_id TEXT,
          is_active         INTEGER NOT NULL DEFAULT 1,
          FOREIGN KEY(linked_student_id) REFERENCES students(student_id) ON DELETE SET NULL
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


# One-time schema boot
with sqlite3.connect(DB) as _conn_boot:
    _conn_boot.row_factory = sqlite3.Row
    ensure_schema(_conn_boot)


# ---------- Helpers ----------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


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


def rolling_avg(conn, sid, oid, n=5):
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


def attempt_hist(conn, sid, oid, m=5):
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
    hist = attempt_hist(conn, sid, oid, 5)
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
        avg = rolling_avg(conn, sid, r["objective_id"], 5)
        if avg >= mastery:
            mastered.append(r["objective_id"])
    return len(mastered) / total, mastered


def first_objective_in_band(conn, core, band):
    r = conn.execute(
        """
        SELECT o.objective_id
        FROM objectives o
        JOIN standards s ON s.standard_id=o.standard_id
        WHERE s.core_idea=? AND s.grade_band=?
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
    mastery, practice = get_config(conn)
    meta = conn.execute(
        """
        SELECT s.core_idea AS core, s.grade_band AS band
        FROM objectives o
        JOIN standards s ON s.standard_id=o.standard_id
        WHERE o.objective_id=?
        """,
        (oid,),
    ).fetchone()
    core = meta["core"] if meta else None
    band = meta["band"] if meta else None

    # Cross-band promotion
    if core and band:
        rule = conn.execute(
            """
            SELECT to_band, promote_threshold
            FROM progression_rules
            WHERE core_idea=? AND from_band=?
            LIMIT 1
            """,
            (core, band),
        ).fetchone()
        if rule:
            frac, _ = mastered_fraction_in_band(conn, sid, core, band, mastery)
            if frac >= float(rule["promote_threshold"]):
                nxt = first_objective_in_band(conn, core, rule["to_band"])
                if nxt:
                    return frac, nxt, f'promote_{band}_to_{rule["to_band"]}', False

    avg = rolling_avg(conn, sid, oid)
    adv, rem = lookup_map(conn, oid)

    if frustration_active(conn, sid, oid):
        return avg, (rem or oid), "remediation_frustration_trigger", True
    if avg >= mastery:
        return avg, (adv or oid), ("advance" if adv else "mastered"), False
    if avg >= practice:
        return avg, oid, "practice", False
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
        JOIN standards s ON s.standard_id=o.standard_id
        ORDER BY s.core_idea, s.grade_band, o.order_in_band, o.objective_id
        """
    ).fetchall()


def get_students(conn, period=None):
    if period and period != "ALL":
        return conn.execute(
            """
            SELECT student_id, first_name, last_name, grade, class_period
            FROM students
            WHERE class_period=?
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
        "SELECT question_id FROM questions WHERE objective_id=? ORDER BY question_id LIMIT 1",
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
    ts = int(time.time())
    attempt_id = f"A{uuid.uuid4().hex}"

    conn.execute(
        """
        INSERT OR REPLACE INTO attempts
          (attempt_id, student_id, question_id, timestamp, response, is_correct, time_seconds, skills_missed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (attempt_id, student_id, qid, ts, f"Quick:{result}", is_correct, 0, "[]"),
    )
    conn.commit()

    std_row = conn.execute(
        """
        SELECT s.standard_id
        FROM objectives o
        JOIN standards s ON s.standard_id=o.standard_id
        WHERE o.objective_id=?
        """,
        (objective_id,),
    ).fetchone()
    std_id = std_row["standard_id"] if std_row else "UNKNOWN"
    lvl = get_level_for_objective(conn, objective_id)

    conn.execute(
        """
        INSERT INTO responses (student_id, standard_id, level, question_id, correct, ts)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (student_id, std_id, lvl, qid, is_correct, ts),
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
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            flash(f"Welcome, {user['username']}!")
            if user["role"] == "teacher":
                return redirect(url_for("index"))
            else:
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
      <button type="submit">Login</button>
    </form>
    <p style="font-size:12px;color:#666;">
      Don’t have an account? Contact your teacher to be added.
    </p>
    """
    return render_template_string(login_html)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("login"))


# ---------- Teacher dashboard ----------
@app.route("/", methods=["GET", "POST"])
@require_teacher
def index():
    conn = get_conn()
    msg = ""

    engine_checks = check_engine_tables(conn)
    engine_ok = all(engine_checks.values()) if engine_checks else False

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
            flash("No question available for that objective. Please add a question first.")
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
        ts = int(time.time())
        attempt_id = f"A{ts}"

        conn.execute(
            """
            INSERT OR REPLACE INTO attempts
              (attempt_id, student_id, question_id, timestamp, response, is_correct, time_seconds, skills_missed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (attempt_id, student_id, qid, ts, (resp or f"Quick:{correct}"), correct, 0, "[]"),
        )
        conn.commit()

        std_row = conn.execute(
            """
            SELECT s.standard_id
            FROM objectives o
            JOIN standards s ON s.standard_id=o.standard_id
            JOIN questions q ON q.objective_id=o.objective_id
            WHERE q.question_id=?
            """,
            (qid,),
        ).fetchone()
        std_id = std_row["standard_id"] if std_row else "UNKNOWN"
        lvl = get_level_for_objective(conn, objective_id)

        conn.execute(
            """
            INSERT INTO responses (student_id, standard_id, level, question_id, correct, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (student_id, std_id, lvl, qid, correct, ts),
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
    students = get_students(
        conn, selected_period if selected_period != "ALL" else None
    )
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
        try:
            avg, nxt, reason, fr = decide_next(conn, active_student, o["objective_id"])
        except TypeError:
            avg, nxt, reason = decide_next(conn, active_student, o["objective_id"])
            fr = 0
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
    if class_obj and students:
        for s in students:
            try:
                avg, nxt, reason, fr = decide_next(conn, s["student_id"], class_obj)
            except TypeError:
                avg, nxt, reason = decide_next(conn, s["student_id"], class_obj)
                fr = 0
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
                )
            )

    period_opts = ["ALL"] + sorted(
        {str(s["class_period"]) for s in get_students(conn)} - {None}
    )

    class_agg_rows = []
    mastery, practice = get_config(conn)
    for o in objs:
        per_student = (
            [
                rolling_avg(conn, s["student_id"], o["objective_id"], 5)
                for s in students
            ]
            if students
            else []
        )
        class_avg = (
            round(sum(per_student) / len(per_student), 2) if per_student else 0.0
        )
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
    <p style="font-size:13px;color:#555;margin:2px 0 0 0;">🧑‍🏫 Logged in as <strong>{{ session.get('username', 'teacher') }}</strong></p>
  </div>
  <form action="{{ url_for('logout') }}" method="get" style="margin:0;">
    <button type="submit" class="btn" style="background:#dc2626;">Logout</button>
  </form>
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
      <th>Rolling Avg (last 5)</th><th>Next Node</th><th>Reason</th>
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
      <th>Class Avg (last 5)</th>
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
        <th>Student</th><th>Name</th><th>Grade</th><th>Period</th>
        <th>Rolling Avg (last 5)</th><th>Next Node</th><th>Reason</th><th>Quick Score</th>
      </tr>
      {% for sid, name, grade, period, avg, nxt, reason, fr in class_rows %}
      <tr>
        <td>{{sid}}</td><td>{{name}}</td><td>{{grade or ""}}</td><td>{{period or ""}}</td>
        <td>{% if avg>=0.9 %}<span class="ok">{{avg}}</span>{% elif avg>=0.7 %}<span class="warn">{{avg}}</span>{% else %}<span class="bad">{{avg}}</span>{% endif %}</td>
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
        all_users=all_users,
    )


# ---------- Student view ----------
@app.route("/student", methods=["GET", "POST"])
@require_student
def student_view():
    conn = get_conn()
    feedback = None

    students = get_students(conn)
    objs = get_objectives(conn)

    student_id = request.values.get("student_id")
    objective_id = request.values.get("objective_id")

    if not student_id and students:
        student_id = students[0]["student_id"]
    if not objective_id and objs:
        objective_id = objs[0]["objective_id"]

    if request.method == "POST" and request.form.get("action") == "answer":
        student_id = request.form.get("student_id") or student_id
        objective_id = request.form.get("objective_id") or objective_id
        qid = request.form.get("question_id")
        resp = request.form.get("response")

        if not qid and objective_id:
            qid = get_any_question_id(conn, objective_id)

        if qid:
            row = conn.execute(
                "SELECT answer_key FROM questions WHERE question_id=?", (qid,)
            ).fetchone()
            correct = 1 if row and row["answer_key"] == resp else 0
            ts = int(time.time())
            attempt_id = f"ST{ts}"

            conn.execute(
                """
                INSERT OR REPLACE INTO attempts
                  (attempt_id, student_id, question_id, timestamp, response, is_correct, time_seconds, skills_missed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (attempt_id, student_id, qid, ts, resp, correct, 0, "[]"),
            )

            std_row = conn.execute(
                """
                SELECT s.standard_id, o.objective_id
                FROM objectives o
                JOIN standards s ON s.standard_id=o.standard_id
                JOIN questions q ON q.objective_id=o.objective_id
                WHERE q.question_id=?
                """,
                (qid,),
            ).fetchone()
            std_id = std_row["standard_id"] if std_row else "UNKNOWN"
            obj_id_for_level = std_row["objective_id"] if std_row else objective_id
            lvl = get_level_for_objective(conn, obj_id_for_level)

            conn.execute(
                """
                INSERT INTO responses (student_id, standard_id, level, question_id, correct, ts)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (student_id, std_id, lvl, qid, correct, ts),
            )
            conn.commit()

            engine_decision = None
            engine_summary = None
            if std_id != "UNKNOWN":
                engine_decision = ae.process_after_response(student_id, std_id)
                if isinstance(engine_decision, dict):
                    action = engine_decision.get("action")
                    level = engine_decision.get("level")
                    std = engine_decision.get("standard")
                    reason = engine_decision.get("reason")
                    parts = []
                    if action:
                        parts.append(action)
                    if std:
                        parts.append(std)
                    if level is not None:
                        parts.append(f"Level {level}")
                    if reason:
                        parts.append(f"({reason})")
                    engine_summary = " – ".join(parts)
                elif engine_decision is not None:
                    engine_summary = str(engine_decision)

            avg, nxt, reason, fr = decide_next(conn, student_id, objective_id)

            feedback = {
                "correct": bool(correct),
                "avg": round(avg, 2),
                "reason": reason,
                "frustrated": fr,
                "next_obj": nxt,
                "engine_summary": engine_summary,
            }

            objective_id = nxt
        else:
            feedback = {"error": "No question found for this objective."}

    current_question = None
    if objective_id:
        qrows = get_questions_for_objective(conn, objective_id)
        current_question = qrows[0] if qrows else None

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
      <h2 style="margin:0;">Student Practice</h2>
      <p style="font-size:13px;color:#555;margin:2px 0 0 0;">👩‍🎓 Logged in as <strong>{{ session.get('username', 'student') }}</strong></p>
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
      <p>Rolling avg for this objective: <strong>{{feedback.avg}}</strong> (reason: {{feedback.reason}})</p>
      {% if feedback.frustrated %}
        <p class="bad"><em>The system has flagged possible frustration on this objective.</em></p>
      {% endif %}
      <p>Next objective: <strong>{{feedback.next_obj}}</strong></p>

      {% if feedback.engine_summary %}
        <p style="font-size:13px;color:#555;">
          Engine (standard-level): {{ feedback.engine_summary }}
        </p>
      {% endif %}
      <hr>
    {% endif %}
  {% endif %}

  {% if current_question %}
    <h3>Objective: {{objective_id}}</h3>
    <p>{{current_question['stem']}}</p>
    <form method="post">
      <input type="hidden" name="action" value="answer">
      <input type="hidden" name="student_id" value="{{student_id}}">
      <input type="hidden" name="objective_id" value="{{objective_id}}">
      <input type="hidden" name="question_id" value="{{current_question['question_id']}}">

      <div class="choice">
        <label><input type="radio" name="response" value="A" required> A. {{current_question['choice_a']}}</label>
      </div>
      <div class="choice">
        <label><input type="radio" name="response" value="B"> B. {{current_question['choice_b']}}</label>
      </div>
      <div class="choice">
        <label><input type="radio" name="response" value="C"> C. {{current_question['choice_c']}}</label>
      </div>
      <div class="choice">
        <label><input type="radio" name="response" value="D"> D. {{current_question['choice_d']}}</label>
      </div>

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
    students = get_students(
        conn, selected_period if selected_period != "ALL" else None
    )
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
            "rolling_avg_last5",
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


if __name__ == "__main__":
    app.run(debug=True)
