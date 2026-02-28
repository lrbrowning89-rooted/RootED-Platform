import os
import sqlite3
import time

# Match the same DB as dashboard_v5.py
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]  # RootED/
DB_PATH = os.environ.get("NGSS_DB", str(PROJECT_ROOT / "data" / "ngss.db"))

MASTERY = 0.90        # ≥90% to advance
SUPER_MASTERY = 0.95  # ≥95% on remediation to return to same level
REMEDIATE = 0.70      # <70% triggers remediation
ROLL_N = 7            # rolling window of last 7 responses


# -------------- DB helpers --------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# -------------- Response logging & rolling avg --------------

def log_response(student_id: str, standard_id: str, level: int,
                 question_id: str, correct: bool) -> None:
    """Call this right after you auto-grade a question."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO responses (student_id, standard_id, level, question_id, correct, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (student_id, standard_id, level, question_id, 1 if correct else 0, int(time.time()))
        )


def get_last_n_responses(student_id: str, standard_id: str,
                         level: int, n: int = ROLL_N):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT correct FROM responses
            WHERE student_id = ? AND standard_id = ? AND level = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (student_id, standard_id, level, n)
        ).fetchall()
    return [r["correct"] for r in rows]


def rolling7_avg(student_id: str, standard_id: str, level: int) -> float:
    rows = get_last_n_responses(student_id, standard_id, level, ROLL_N)
    if not rows:
        return 0.0
    return sum(rows) / len(rows)


# -------------- Progress state --------------

def get_state(student_id: str, standard_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT current_level, status, rolling_avg, locked, locked_reason
            FROM progress_state
            WHERE student_id = ? AND standard_id = ?
            """,
            (student_id, standard_id)
        ).fetchone()

    if row:
        return {
            "level": row["current_level"],
            "status": row["status"],
            "rolling_avg": row["rolling_avg"],
            "locked": bool(row["locked"]),
            "locked_reason": row["locked_reason"],
        }

    # default state if not present
    return {
        "level": 1,
        "status": "practicing",
        "rolling_avg": 0.0,
        "locked": False,
        "locked_reason": None,
    }


def set_state(student_id: str, standard_id: str,
              level: int, status: str, rolling_avg: float) -> None:
    now = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE progress_state
            SET current_level = ?, status = ?, rolling_avg = ?, last_update = ?
            WHERE student_id = ? AND standard_id = ?
            """,
            (level, status, rolling_avg, now, student_id, standard_id)
        )
        if cur.rowcount == 0:
            conn.execute(
                """
                INSERT INTO progress_state
                  (student_id, standard_id, current_level, status, rolling_avg, locked, locked_reason, last_update)
                VALUES (?, ?, ?, ?, ?, 0, NULL, ?)
                """,
                (student_id, standard_id, level, status, rolling_avg, now)
            )


def set_locked(student_id: str, standard_id: str, locked: bool, reason: str | None = None) -> None:
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE progress_state
            SET locked = ?, locked_reason = ?, last_update = ?
            WHERE student_id = ? AND standard_id = ?
            """,
            (1 if locked else 0, reason, now, student_id, standard_id)
        )


# -------------- Progression & remediation mapping --------------

def lower_band_of(standard_id: str) -> str | None:
    """Map MS → 3–5, 3–5 → K–2, etc. Returns None if no lower standard exists."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT lower_standard_id
            FROM remediation_links
            WHERE standard_id = ?
            """,
            (standard_id,)
        ).fetchone()
    return row["lower_standard_id"] if row else None


def next_up_from(standard_id: str) -> tuple[str, int]:
    """
    Continuous progression:
    get the next standard after Level 3 mastery (same band or HS/beyond).
    Always starts the next one at Level 1.
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT next_standard_id
            FROM progression_links
            WHERE standard_id = ?
            """,
            (standard_id,)
        ).fetchone()
    if not row:
        # if you ever have a "top of graph" standard, you can choose to loop or stay
        return standard_id, 1
    return row["next_standard_id"], 1


# -------------- Origin links (for returns after remediation) --------------

def remember_origin(student_id: str, rem_standard_id: str,
                    from_standard_id: str, from_level: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO origin_links (student_id, rem_standard_id, from_standard_id, from_level, ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (student_id, rem_standard_id, from_standard_id, from_level, int(time.time()))
        )


def recall_origin(student_id: str, rem_standard_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT from_standard_id, from_level
            FROM origin_links
            WHERE student_id = ? AND rem_standard_id = ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (student_id, rem_standard_id)
        ).fetchone()
    if not row:
        return None
    return {"from_std": row["from_standard_id"], "from_level": row["from_level"]}


def clear_origin(student_id: str, rem_standard_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            DELETE FROM origin_links
            WHERE student_id = ? AND rem_standard_id = ?
            """,
            (student_id, rem_standard_id)
        )


# -------------- Notifications --------------

def create_notification(student_id: str, standard_id: str,
                        event: str, details: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notifications (student_id, standard_id, event, details, ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (student_id, standard_id, event, details, int(time.time()))
        )


def notify_drop(student_id: str, from_std: str, from_lvl: int,
                to_std: str, to_lvl: int, avg: float) -> None:
    details = f"⬇️ Dropped from {from_std} L{from_lvl} → {to_std} L{to_lvl} (Rolling-7={avg:.0%})"
    create_notification(student_id, from_std, "drop", details)


def notify_lock(student_id: str, standard_id: str) -> None:
    details = f"🔒 Locked: {standard_id} (Mini-lesson not mastered)"
    create_notification(student_id, standard_id, "lock", details)


# -------------- Mini-lesson stub --------------

def mini_lesson_mastered(student_id: str, standard_id: str) -> float:
    """
    Stub: replace with your real mini-lesson scoring logic.
    Should return a score between 0 and 1, like 0.92 for 92%.
    """
    # TODO: hook this into your mini-lesson results table
    return 0.0


# -------------- Core engine: call after each graded response --------------

def lock_standard(student_id: str, standard_id: str) -> None:
    set_locked(student_id, standard_id, True, reason="mini_lesson_fail")
    notify_lock(student_id, standard_id)


def process_after_response(student_id: str, standard_id: str) -> dict:
    """
    Call this AFTER log_response().
    It:
      - Reads current state
      - Computes rolling-7 avg
      - Decides: advance, continue, or drop
      - Handles mini-lesson if no lower band exists
    Returns a dict telling your app what to do next.
    """
    state = get_state(student_id, standard_id)
    level = state["level"]

    if state["locked"]:
        return {
            "action": "locked",
            "standard": standard_id,
            "level": level,
            "reason": state["locked_reason"],
        }

    avg = rolling7_avg(student_id, standard_id, level)

    # ---- Advance (mastery) ----
    if avg >= MASTERY:
        if level < 3:
            new_level = level + 1
            set_state(student_id, standard_id, new_level, "practicing", avg)
            return {
                "action": "serve_practice",
                "standard": standard_id,
                "level": new_level,
                "avg": avg,
            }

        # Finished Level 3 → move to next standard in progression
        next_std, next_lvl = next_up_from(standard_id)
        set_state(student_id, next_std, next_lvl, "practicing", 0.0)
        return {
            "action": "advance_standard",
            "from": (standard_id, level),
            "to": (next_std, next_lvl),
        }

    # ---- Middle band: keep practicing ----
    if REMEDIATE <= avg < MASTERY:
        set_state(student_id, standard_id, level, "practicing", avg)
        return {
            "action": "serve_practice",
            "standard": standard_id,
            "level": level,
            "avg": avg,
        }

    # ---- Remediation (<70%) ----
    lower_std = lower_band_of(standard_id)
    if lower_std:
        # Drop mapping: L1→L1, L2→L2, L3→L2
        drop_to_level = 1 if level == 1 else 2
        remember_origin(student_id, lower_std, standard_id, level)
        notify_drop(student_id, standard_id, level, lower_std, drop_to_level, avg)
        set_state(student_id, lower_std, drop_to_level, "practicing", 0.0)
        return {
            "action": "remediate_lower_band",
            "standard": lower_std,
            "level": drop_to_level,
        }

    # No lower standard → Mini-lesson path
    mini_score = mini_lesson_mastered(student_id, standard_id)
    if mini_score >= MASTERY:
        origin = recall_origin(student_id, standard_id)
        if origin:
            clear_origin(student_id, standard_id)
            set_state(student_id, origin["from_std"], origin["from_level"], "practicing", 0.0)
            return {
                "action": "mini_pass_return",
                "to": (origin["from_std"], origin["from_level"]),
            }
        # No origin saved; just resume this standard
        set_state(student_id, standard_id, level, "practicing", 0.0)
        return {
            "action": "mini_pass_resume",
            "standard": standard_id,
            "level": level,
        }

    # Mini-lesson not mastered → lock
    lock_standard(student_id, standard_id)
    return {"action": "locked", "standard": standard_id, "reason": "mini_lesson_fail"}


# -------------- When lower-band remediation is mastered --------------

def on_remediation_mastered(student_id: str, rem_standard_id: str,
                            rem_level: int, mastery_avg: float) -> dict:
    """
    Call this when the student masters the LOWER-band remediation target.
    - If mastery_avg ≥ 95% → return to same level on original standard.
    - If 90–94% → restart the original standard at Level 1.
    """
    origin = recall_origin(student_id, rem_standard_id)
    if not origin:
        # Fallback: just advance within lower band
        next_std, next_lvl = next_up_from(rem_standard_id)
        set_state(student_id, next_std, next_lvl, "practicing", 0.0)
        return {
            "action": "advance_from_lower",
            "to": (next_std, next_lvl),
        }

    target_std = origin["from_std"]
    if mastery_avg >= SUPER_MASTERY:
        target_level = origin["from_level"]
    else:
        target_level = 1  # restart track at recall

    clear_origin(student_id, rem_standard_id)
    set_state(student_id, target_std, target_level, "practicing", 0.0)

    return {
        "action": "return_to_original",
        "to": (target_std, target_level),
        "strong_return": mastery_avg >= SUPER_MASTERY,
    }
