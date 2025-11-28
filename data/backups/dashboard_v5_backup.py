from flask import Flask, request, render_template_string, make_response, session, redirect, url_for, flash, get_flashed_messages
import sqlite3, time, csv, io, uuid
from config import FERPA_ENFORCED, DEFAULT_PII_MODE, SECRET_KEY
import os  # ← new import for path handling

# ---------- Database setup ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("NGSS_DB", os.path.join(BASE_DIR, "ngss.db"))

# ---------- Flask app setup ----------
app = Flask(__name__)
app.secret_key = SECRET_KEY

# ---------- DEFINE ensure_schema BEFORE calling it ----------
def ensure_schema(conn):
    # Config for thresholds etc.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
          key   TEXT PRIMARY KEY,
          value TEXT NOT NULL
        )
    """)

    # Students roster
    conn.execute("""
        CREATE TABLE IF NOT EXISTS students (
          student_id   TEXT PRIMARY KEY,
          first_name   TEXT,
          last_name    TEXT,
          grade        INTEGER,
          class_period TEXT
        )
    """)

    # NGSS standards (core idea + grade band)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS standards (
          standard_id TEXT PRIMARY KEY,
          core_idea   TEXT,
          grade_band  TEXT
        )
    """)

    # Objectives (nodes) tied to standards
    conn.execute("""
        CREATE TABLE IF NOT EXISTS objectives (
          objective_id   TEXT PRIMARY KEY,
          standard_id    TEXT NOT NULL,
          objective_text TEXT,
          order_in_band  INTEGER,
          FOREIGN KEY(standard_id) REFERENCES standards(standard_id)
        )
    """)

    # Questions tied to objectives
    conn.execute("""
        CREATE TABLE IF NOT EXISTS questions (
          question_id TEXT PRIMARY KEY,
          objective_id TEXT NOT NULL,
          stem        TEXT,
          choice_a    TEXT,
          choice_b    TEXT,
          choice_c    TEXT,
          choice_d    TEXT,
          answer_key  TEXT,
          FOREIGN KEY(objective_id) REFERENCES objectives(objective_id)
        )
    """)

    # Attempts (fine-grained) – this is what rolling_avg/attempt_hist use
    conn.execute("""
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
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_attempts_student_obj_ts
        ON attempts(student_id, question_id, timestamp DESC)
    """)

    # Optional explicit map for advance/remediation if you want to override graph
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lists (
          objective_id  TEXT PRIMARY KEY,
          advance_to    TEXT,
          remediation_to TEXT
        )
    """)

    # Graph-style paths between objectives (fallback for lookup_map)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paths (
          id        INTEGER PRIMARY KEY AUTOINCREMENT,
          from_node TEXT NOT NULL,
          to_node   TEXT NOT NULL,
          note      TEXT
        )
    """)

    # Cross-band progression rules (e.g., MS → HS once band is mastered)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS progression_rules (
          id               INTEGER PRIMARY KEY AUTOINCREMENT,
          core_idea        TEXT NOT NULL,
          from_band        TEXT NOT NULL,
          to_band          TEXT NOT NULL,
          promote_threshold REAL NOT NULL
        )
    """)

    # Optional mapping of objective → level (1/2/3)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS objective_levels (
          objective_id TEXT PRIMARY KEY,
          level        INTEGER NOT NULL
        )
    """)

    # Standard-level responses for “rolling 7” / mastery tracking
    conn.execute("""
        CREATE TABLE IF NOT EXISTS responses (
          id          INTEGER PRIMARY KEY,
          student_id  TEXT NOT NULL,
          standard_id TEXT NOT NULL,   -- e.g., "MS-LS1-1"
          level       INTEGER NOT NULL DEFAULT 1,  -- 1/2/3 for now
          question_id TEXT NOT NULL,
          correct     INTEGER NOT NULL,           -- 1 or 0
          ts          INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_responses_student_std_lvl_ts
        ON responses(student_id, standard_id, level, ts DESC)
    """)

    conn.commit()


# ---------- One-time schema check/create ----------
with sqlite3.connect(DB) as _conn_boot:
    _conn_boot.row_factory = sqlite3.Row
    ensure_schema(_conn_boot)


def get_conn():
    c=sqlite3.connect(DB)
    c.row_factory=sqlite3.Row
    return c

def get_config(conn):
    try:
        rows = conn.execute('SELECT key,value FROM config').fetchall()
    except sqlite3.OperationalError:
        # config table not present yet → sane defaults
        return 0.9, 0.7
    cfg = {}
    for r in rows:
        try:
            cfg[r['key']] = float(r['value'])
        except Exception:
            pass
    return cfg.get('mastery_threshold', 0.9), cfg.get('practice_lower', 0.7)

def set_config(conn, mastery, practice):
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('mastery_threshold', ?)", (str(mastery),))
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('practice_lower', ?)", (str(practice),))
    conn.commit()

def rolling_avg(conn, sid, oid, n=5):
    rows=conn.execute(
        'SELECT a.is_correct FROM attempts a '
        'JOIN questions q ON q.question_id=a.question_id '
        'WHERE a.student_id=? AND q.objective_id=? '
        'ORDER BY a.timestamp DESC LIMIT ?', (sid,oid,n)
    ).fetchall()
    if not rows:
        return 0.0
    total=len(rows)
    correct=sum(1 for r in rows if int(r['is_correct'])==1)
    return correct/total

def attempt_hist(conn, sid, oid, m=5):
    rows=conn.execute(
        'SELECT a.is_correct FROM attempts a '
        'JOIN questions q ON q.question_id=a.question_id '
        'WHERE a.student_id=? AND q.objective_id=? '
        'ORDER BY a.timestamp DESC LIMIT ?', (sid,oid,m)
    ).fetchall()
    return [int(r['is_correct']) for r in rows]

def frustration_active(conn, sid, oid):
    hist=attempt_hist(conn, sid, oid, 5)
    return len(hist)==5 and all(v==0 for v in hist)

def lookup_map(conn, oid):
    r=conn.execute('SELECT advance_to,remediation_to FROM lists WHERE objective_id=?',(oid,)).fetchone()
    if r:
        return r['advance_to'], r['remediation_to']
    a=conn.execute("SELECT to_node FROM paths WHERE from_node=? AND note LIKE '%Advance%'", (oid,)).fetchone()
    m=conn.execute("SELECT to_node FROM paths WHERE from_node=? AND note LIKE '%Remed%'", (oid,)).fetchone()
    return (a['to_node'] if a else None), (m['to_node'] if m else None)

def mastered_fraction_in_band(conn, sid, core, band, mastery=0.9):
    rows=conn.execute(
        'SELECT o.objective_id FROM objectives o '
        'JOIN standards s ON s.standard_id=o.standard_id '
        'WHERE s.core_idea=? AND s.grade_band=? '
        'ORDER BY o.order_in_band, o.objective_id', (core,band)
    ).fetchall()
    if not rows:
        return 0.0, []
    ms=[]; tot=len(rows)
    for r in rows:
        avg=rolling_avg(conn, sid, r['objective_id'], 5)
        if avg>=mastery:
            ms.append(r['objective_id'])
    return (len(ms)/tot), ms

def first_objective_in_band(conn, core, band):
    r=conn.execute(
        'SELECT o.objective_id FROM objectives o '
        'JOIN standards s ON s.standard_id=o.standard_id '
        'WHERE s.core_idea=? AND s.grade_band=? '
        'ORDER BY o.order_in_band, o.objective_id LIMIT 1', (core,band)
    ).fetchone()
    return r['objective_id'] if r else None

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
        (sid, first, last, grade, period)
    )
    conn.commit()

def decide_next(conn, sid, oid):
    mastery, practice = get_config(conn)
    meta=conn.execute(
        'SELECT s.core_idea core, s.grade_band band '
        'FROM objectives o JOIN standards s ON s.standard_id=o.standard_id '
        'WHERE o.objective_id=?', (oid,)
    ).fetchone()
    core=meta['core'] if meta else None
    band=meta['band'] if meta else None

    # Cross-band promotion
    if core and band:
        rule=conn.execute(
            'SELECT to_band, promote_threshold FROM progression_rules '
            'WHERE core_idea=? AND from_band=? LIMIT 1', (core,band)
        ).fetchone()
        if rule:
            frac,_=mastered_fraction_in_band(conn, sid, core, band, mastery)
            if frac >= float(rule['promote_threshold']):
                nxt=first_objective_in_band(conn, core, rule['to_band'])
                if nxt:
                    return frac, nxt, f'promote_{band}_to_{rule["to_band"]}', False


    avg=rolling_avg(conn, sid, oid)
    adv, rem = lookup_map(conn, oid)

    if frustration_active(conn, sid, oid):
        return avg, (rem or oid), 'remediation_frustration_trigger', True
    if avg >= mastery:
        # advance if mapping exists; otherwise mark mastered and stay here
        return avg, (adv or oid), ('advance' if adv else 'mastered'), False
    if avg >= practice:
        return avg, oid, 'practice', False
    return avg, (rem or oid), 'remediation', False

def get_objectives(conn):
    return conn.execute("""
        SELECT 
            o.objective_id,
            o.objective_text,
            o.standard_id,
            s.core_idea,
            s.grade_band
        FROM objectives o
        JOIN standards s ON s.standard_id = o.standard_id
        ORDER BY s.core_idea, s.grade_band, o.order_in_band, o.objective_id
    """).fetchall()

def get_students(conn, period=None):
    if period and period!='ALL':
        return conn.execute('SELECT student_id,first_name,last_name,grade,class_period FROM students WHERE class_period=? ORDER BY student_id',(period,)).fetchall()
    return conn.execute('SELECT student_id,first_name,last_name,grade,class_period FROM students ORDER BY student_id').fetchall()

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
        (oid,)
    ).fetchall()


# ----- FERPA-safe name helpers -----
def mask_name(first, last):
    fi = (first or '').strip()[:1].upper()
    li = (last  or '').strip()[:1].upper()
    initials = (fi + li).strip()
    return initials or ""  # empty string if no name parts

def current_pii_mode():
    if FERPA_ENFORCED:
        return 'masked'
    return session.get("pii_mode", DEFAULT_PII_MODE)

def display_name(s: sqlite3.Row) -> str:
    """Return FERPA-safe display name based on current mode."""
    first = (s['first_name'] or '').strip()
    last  = (s['last_name'] or '').strip()

    if current_pii_mode() == 'full':
        full_name = f"{first} {last}".strip()
        return full_name or s['student_id']

    # masked mode: show initials if we have them, else fall back to ID
    fi = first[:1].upper() if first else ''
    li = last[:1].upper() if last else ''
    initials = (fi + li).strip()
    return initials or s['student_id']


@app.route('/toggle_names', methods=['POST'])
def toggle_names():
    # read the selection from the dropdown
    mode = request.form.get('pii_mode')
    # accept only valid values
    if mode in ('masked', 'full'):
        session['pii_mode'] = mode
    # go back to the main page after changing the mode
    return redirect(url_for('index'))

def get_any_question_id(conn, objective_id):
    """Return any question_id for the objective; fall back to a dummy id if none exist."""
    row = conn.execute(
        "SELECT question_id FROM questions WHERE objective_id=? ORDER BY question_id LIMIT 1",
        (objective_id,)
    ).fetchone()
    if row:
        return row["question_id"]
    # fallback: create a throwaway id that won't break inserts (no FK enforced)
    return f"Q-{objective_id}-quick"

def get_level_for_objective(conn, objective_id):
    """
    Return level (1/2/3) for the given objective_id.
    If you don't have a mapping table yet, default to 1.
    Optional schema: objective_levels(objective_id TEXT PRIMARY KEY, level INTEGER)
    """
    try:
        row = conn.execute(
            "SELECT level FROM objective_levels WHERE objective_id=?",
            (objective_id,)
        ).fetchone()
        if row:
            lvl = int(row['level'])
            if lvl in (1, 2, 3):
                return lvl
    except sqlite3.OperationalError:
        pass
    return 1

@app.post("/quick_attempt")
def quick_attempt():
    conn = get_conn()
    student_id = request.form.get("student_id")
    objective_id = request.form.get("objective_id")
    result = request.form.get("result")  # "correct" or "incorrect"

    # Preserve filters when we bounce back to the page
    class_objective = request.form.get("class_objective", objective_id or "")
    period = request.form.get("period", "ALL")
    active_student = request.form.get("active_student", student_id or "S1")

    if not (student_id and objective_id and result in ("correct", "incorrect")):
        flash("Missing student/objective/result for quick score.")
        return redirect(url_for("index", class_objective=class_objective, period=period, student_id=active_student))

    qid = get_any_question_id(conn, objective_id)
    is_correct = 1 if result == "correct" else 0
    ts = int(time.time())
    attempt_id = f"A{uuid.uuid4().hex}"

    # Save to attempts
    conn.execute(
        "INSERT OR REPLACE INTO attempts (attempt_id, student_id, question_id, timestamp, response, is_correct, time_seconds, skills_missed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (attempt_id, student_id, qid, ts, f"Quick:{result}", is_correct, 0, "[]")
    )
    conn.commit()

    # --- ALSO log to responses (for rolling-7) ---
    std_row = conn.execute(
        "SELECT s.standard_id FROM objectives o "
        "JOIN standards s ON s.standard_id=o.standard_id "
        "WHERE o.objective_id=?",
        (objective_id,)
    ).fetchone()
    std_id = std_row["standard_id"] if std_row else "UNKNOWN"
    lvl = get_level_for_objective(conn, objective_id)

    conn.execute(
        "INSERT INTO responses (student_id, standard_id, level, question_id, correct, ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (student_id, std_id, lvl, qid, is_correct, ts)  # level=1 for now
    )
    conn.commit()

    flash(f"Quick score saved for {student_id} on {objective_id} ({'correct' if is_correct else 'incorrect'}).")
    return redirect(url_for("index", class_objective=class_objective, period=period, student_id=active_student))

@app.post("/import_csv")
def import_csv():
    """
    Import standards, objectives, or questions from a CSV file.
    Expected headers:
      standards:  standard_id, core_idea, grade_band
      objectives: objective_id, standard_id, objective_text, order_in_band
      questions:  question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key
    """
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
        # Read the uploaded file as text and wrap it for csv.DictReader
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
                    INSERT OR REPLACE INTO objectives (objective_id, standard_id, objective_text, order_in_band)
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

@app.route('/update_config', methods=['POST'])
def update_config():
    conn = get_conn()
    mastery = request.form.get('mastery_threshold', '0.9')
    practice = request.form.get('practice_lower', '0.7')
    set_config(conn, mastery, practice)
    flash(f"Config updated — Mastery: {mastery}, Practice: {practice}")
    return redirect(url_for('index'))

@app.route('/', methods=['GET', 'POST'])
def index():
    conn = get_conn()
    msg = ""

    # Add/update student
    if request.method == 'POST' and request.form.get('action') == 'save_student':
        sid = request.form.get('student_id', '').strip()
        first = request.form.get('first_name', '').strip()
        last = request.form.get('last_name', '').strip()
        grade = request.form.get('grade', '').strip()
        period = request.form.get('class_period', '').strip()
        if sid:
            try:
                gval = int(grade) if grade else None
            except Exception:
                gval = None
            upsert_student(conn, sid, first, last, gval, period)
            msg = f"Saved student {sid}"
        else:
            msg = "Student ID is required."

    # Save attempt  ← NOTE: this whole block must be INSIDE index()
    # Save attempt  ← NOTE: this whole block must be INSIDE index()
    if request.method == 'POST' and request.form.get('action') == 'save_attempt':
        student_id   = request.form.get('student_id') or 'S1'
        objective_id = request.form.get('objective_id')  # keep the selected objective
        qid          = request.form.get('question_id')
        resp         = request.form.get('response')

        # If no question_id came through, pick any valid question for that objective
        if not qid and objective_id:
            qid = get_any_question_id(conn, objective_id)

        # Preserve current filters for redirect WITHOUT referencing class_obj yet
        keep_obj = request.values.get('class_objective') or request.values.get('objective_id')
        if not keep_obj:
            row_first = conn.execute("SELECT objective_id FROM objectives ORDER BY objective_id LIMIT 1").fetchone()
            keep_obj = row_first['objective_id'] if row_first else None
        keep_period = request.values.get('period', 'ALL')

        if not qid:
            # Still no question? Don't insert; show a friendly message.
            flash("No question available for that objective. Please add a question first.")
            return redirect(url_for("index", class_objective=keep_obj, period=keep_period, student_id=student_id))
        else:
            # Grade correctness (only if the question exists with an answer_key)
            row = conn.execute(
                'SELECT answer_key FROM questions WHERE question_id=?',
                (qid,)
            ).fetchone()
            correct = 1 if row and row['answer_key'] == resp else 0

            ts = int(time.time())
            attempt_id = f'A{ts}'

            # Save to attempts
            conn.execute(
                'INSERT OR REPLACE INTO attempts (attempt_id, student_id, question_id, timestamp, response, is_correct, time_seconds, skills_missed) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (attempt_id, student_id, qid, ts, (resp or f"Quick:{correct}"), correct, 0, '[]')
            )
            conn.commit()

            # --- ALSO log to responses (for rolling-7) ---
            std_row = conn.execute(
                "SELECT s.standard_id FROM objectives o "
                "JOIN standards s ON s.standard_id=o.standard_id "
                "JOIN questions q ON q.objective_id=o.objective_id "
                "WHERE q.question_id=?",
                (qid,)
            ).fetchone()
            std_id = std_row["standard_id"] if std_row else "UNKNOWN"

            # Use the same objective_id level mapping as quick_attempt
            lvl = get_level_for_objective(conn, objective_id)

            conn.execute(
                "INSERT INTO responses (student_id, standard_id, level, question_id, correct, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (student_id, std_id, lvl, qid, correct, ts)
            )
            conn.commit()


            flash(f"Saved attempt for {qid}. Correct={bool(correct)}")
            return redirect(url_for("index", class_objective=keep_obj, period=keep_period, student_id=student_id))
    # ===== Data for panels (still inside index) =====
    objs = get_objectives(conn)

    # Filters
    selected_period = request.values.get('period', 'ALL')
    students = get_students(conn, selected_period if selected_period != 'ALL' else None)
    active_student = request.values.get('student_id', students[0]['student_id'] if students else 'S1')
    class_obj = request.values.get('class_objective', objs[0]['objective_id'] if objs else None)

    # The attempt panel should show questions for the dropdown selection if present,
    # otherwise fall back to the class objective.
    current_obj_for_questions = request.values.get('objective_id', class_obj)
    qrows = get_questions_for_objective(conn, current_obj_for_questions) if current_obj_for_questions else []

    # Current name-visibility mode (for template + display)
    pii_mode = current_pii_mode()

    # Single-student table
    single_table = []
    for o in objs:
        try:
            avg, nxt, reason, fr = decide_next(conn, active_student, o['objective_id'])
        except TypeError:
            avg, nxt, reason = decide_next(conn, active_student, o['objective_id'])
            fr = 0

        core = o['core_idea'] if 'core_idea' in o.keys() else None
        band = o['grade_band'] if 'grade_band' in o.keys() else None

        single_table.append((
            o['objective_id'],
            o['standard_id'],
            core,
            band,
            o['objective_text'],
            round(avg, 2),
            nxt,
            reason,
            fr
        ))

    # Class view rows
    class_rows = []
    if class_obj and students:
        for s in students:
            try:
                avg, nxt, reason, fr = decide_next(conn, s['student_id'], class_obj)
            except TypeError:
                avg, nxt, reason = decide_next(conn, s['student_id'], class_obj)
                fr = 0
            full_name = display_name(s)  # FERPA-safe name
            class_rows.append((
                s['student_id'], full_name, s['grade'], s['class_period'],
                round(avg, 2), nxt, reason, fr
            ))

    # Period dropdown options (compute regardless of student count)
    period_opts = ['ALL'] + sorted({str(s['class_period']) for s in get_students(conn)} - {None})

    # Class aggregate (per objective across the currently filtered students)
    class_agg_rows = []
    mastery, practice = get_config(conn)
    for o in objs:
        per_student = [
            rolling_avg(conn, s['student_id'], o['objective_id'], 5)
            for s in students
        ] if students else []
        class_avg = round(sum(per_student) / len(per_student), 2) if per_student else 0.0

        adv, rem = lookup_map(conn, o['objective_id'])
        if class_avg >= mastery and adv:
            nxt, reason = adv, 'advance(class)'
        elif class_avg >= practice:
            nxt, reason = o['objective_id'], 'practice(class)'
        else:
            nxt, reason = (rem or o['objective_id']), 'remediation(class)'

        fr_count = sum(
            1 for s in (students or []) if frustration_active(conn, s['student_id'], o['objective_id'])
        )

        class_agg_rows.append((
            o['objective_id'],
            o['standard_id'],
            o['objective_text'],
            class_avg,
            nxt,
            reason,
            fr_count
        ))

    # ---- HTML (template) ----
    html = '''
<!doctype html>
<title>Adaptive NGSS - Teacher Dashboard</title>
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
  .toolbar{display:flex;gap:12px;align-items:center}
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
  </div>
{% endif %}

<div class="grid">
<!-- Config Panel -->
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
  <!-- Add/Update Student -->
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

  <!-- Record Attempt -->
  <div class="card">
    <h2>Record an Attempt</h2>
    {% if not objs %}
      <p>No objectives found. Run the importer first.</p>
    {% else %}

      <!-- Objective picker (standalone GET; not nested) -->
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

      <!-- Attempt POST form (single, not nested) -->
      <form method="post">
        <input type="hidden" name="action" value="save_attempt">
        <!-- carry selected objective into POST -->
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
                <option value="{{q['question_id']}}">{{q['question_id']}} - {{ (q['stem'][:60] if q['stem'] else '') }}{{ ('...' if q['stem'] and (q['stem']|length)>60 else '') }}</option>
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

  </div>  <!-- end of Record Attempt card -->
  
  <!-- NEW: Import CSV card -->
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

  <!-- Maintenance / Data Tools -->
  <div class="card">
    <h2>Maintenance / Data Tools</h2>
    <p style="font-size:14px; color:#555;">
      Use these tools to back up or reset practice data.
      <br><strong>Note:</strong> Reset will delete all attempts and responses but will keep students, standards, objectives, and questions.
    </p>
    <form method="post" action="/admin_action" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
      <button class="btn" type="submit" name="action" value="export_attempts">
        ⬇ Download attempts backup (CSV)
      </button>
      <button class="btn" type="submit" name="action" value="reset_practice"
              onclick="return confirm('This will delete ALL attempts and responses. Students and content will stay. Are you sure?');">
        🧹 Clear all attempts/responses
      </button>
    </form>
  </div>

<!-- Single student view -->
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

<!-- Class aggregate view -->
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

<!-- Class view -->
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
    '''

    # Final render (inside the function)
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
        practice=practice
    )

@app.route('/student', methods=['GET', 'POST'])
def student_view():
    conn = get_conn()
    feedback = None

    # Load students and objectives for dropdowns
    students = get_students(conn)
    objs = get_objectives(conn)

    # Which student/objective are we on?
    student_id = request.values.get('student_id')
    objective_id = request.values.get('objective_id')

    # Default to first student / first objective if none chosen yet
    if not student_id and students:
        student_id = students[0]['student_id']
    if not objective_id and objs:
        objective_id = objs[0]['objective_id']

    # Handle a submitted answer
    if request.method == 'POST' and request.form.get('action') == 'answer':
        student_id = request.form.get('student_id') or student_id
        objective_id = request.form.get('objective_id') or objective_id
        qid = request.form.get('question_id')
        resp = request.form.get('response')

        # If no question id, try to pick any question for this objective
        if not qid and objective_id:
            qid = get_any_question_id(conn, objective_id)

        if qid:
            # Grade correctness
            row = conn.execute(
                'SELECT answer_key FROM questions WHERE question_id=?',
                (qid,)
            ).fetchone()
            correct = 1 if row and row['answer_key'] == resp else 0

            ts = int(time.time())
            attempt_id = f'ST{ts}'

            # Log to attempts
            conn.execute(
                'INSERT OR REPLACE INTO attempts (attempt_id, student_id, question_id, timestamp, response, is_correct, time_seconds, skills_missed) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (attempt_id, student_id, qid, ts, resp, correct, 0, "[]")
            )

            # Also log to responses (for rolling-7 style tracking)
            std_row = conn.execute(
                "SELECT s.standard_id, o.objective_id FROM objectives o "
                "JOIN standards s ON s.standard_id=o.standard_id "
                "JOIN questions q ON q.objective_id=o.objective_id "
                "WHERE q.question_id=?",
                (qid,)
            ).fetchone()
            std_id = std_row["standard_id"] if std_row else "UNKNOWN"
            obj_id_for_level = std_row["objective_id"] if std_row else objective_id
            lvl = get_level_for_objective(conn, obj_id_for_level)

            conn.execute(
                "INSERT INTO responses (student_id, standard_id, level, question_id, correct, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (student_id, std_id, lvl, qid, correct, ts)
            )
            conn.commit()

            # Use your existing decide_next() to pick the next objective
            avg, nxt, reason, fr = decide_next(conn, student_id, objective_id)

            feedback = {
                "correct": bool(correct),
                "avg": round(avg, 2),
                "reason": reason,
                "frustrated": fr,
                "next_obj": nxt
            }

            # Move student to the next objective
            objective_id = nxt
        else:
            feedback = {"error": "No question found for this objective."}

    # After possibly updating objective_id, grab the current question
    current_question = None
    if objective_id:
        qrows = get_questions_for_objective(conn, objective_id)
        current_question = qrows[0] if qrows else None

    # Simple student-facing template
    student_html = '''
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
  a{color:#2563eb;text-decoration:none}
</style>

<div class="card">
  <div class="header">
    <h2 style="margin:0;">Student Practice</h2>
    <a href="/">⬅ Back to Teacher Dashboard</a>
  </div>

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
    '''

    return render_template_string(
        student_html,
        students=students,
        objs=objs,
        student_id=student_id,
        objective_id=objective_id,
        current_question=current_question,
        feedback=feedback
    )

@app.route('/admin_action', methods=['POST'])
def admin_action():
    conn = get_conn()
    action = request.form.get('action')

    if action == 'export_attempts':
        # Build a CSV backup of all attempts
        rows = conn.execute(
            """
            SELECT attempt_id, student_id, question_id, timestamp, response, is_correct, time_seconds, skills_missed
            FROM attempts
            ORDER BY timestamp DESC
            """
        ).fetchall()

        output = io.StringIO()
        w = csv.writer(output)
        w.writerow([
            "attempt_id", "student_id", "question_id",
            "timestamp", "response", "is_correct",
            "time_seconds", "skills_missed"
        ])
        for r in rows:
            w.writerow([
                r["attempt_id"],
                r["student_id"],
                r["question_id"],
                r["timestamp"],
                r["response"],
                r["is_correct"],
                r["time_seconds"],
                r["skills_missed"],
            ])

        resp = make_response(output.getvalue())
        resp.headers["Content-Type"] = "text/csv"
        resp.headers["Content-Disposition"] = 'attachment; filename="attempts_backup.csv"'
        return resp

    elif action == 'reset_practice':
        # Delete all attempts + responses, but keep roster, standards, objectives, questions
        conn.execute("DELETE FROM attempts")
        conn.execute("DELETE FROM responses")
        conn.commit()
        flash("All attempts and responses have been cleared. Students and content were kept.")
        return redirect(url_for("index"))

    else:
        flash("Unknown admin action.")
        return redirect(url_for("index"))

@app.route('/export_csv')
def export_csv():
    conn=get_conn()
    selected_period=request.args.get('period','ALL')
    students=get_students(conn, selected_period if selected_period!='ALL' else None)
    objs=get_objectives(conn)

    output=io.StringIO(); w=csv.writer(output)
    w.writerow(['student_id','name','grade','period','objective_id','objective_text','rolling_avg_last5','next_node','reason','frustration_active'])

    for s in students:
        name=f"{s['first_name'] or ''} {s['last_name'] or ''}".strip()
        for o in objs:
            avg,nxt,reason,fr=decide_next(conn, s['student_id'], o['objective_id'])
            w.writerow([s['student_id'],name,s['grade'] or '',s['class_period'] or '',o['objective_id'],o['objective_text'],round(avg,2),nxt,reason,'TRUE' if fr else 'FALSE'])

    resp=make_response(output.getvalue())
    resp.headers['Content-Type']='text/csv'
    resp.headers['Content-Disposition']=f'attachment; filename=class_progress_period_{selected_period}.csv'
    return resp

if __name__=='__main__':
    app.run(debug=True)
