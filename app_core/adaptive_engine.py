import os
import sqlite3
import time

print("LOADED adaptive_engine from:", __file__)

# Match the same DB as dashboard_v5.py
from app_core.config import resolve_database_path

DB_PATH = resolve_database_path()

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


def response_count(student_id: str, standard_id: str, level: int) -> int:
    """Return how many responses exist for this student/standard/level."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM responses
            WHERE student_id = ? AND standard_id = ? AND level = ?
            """,
            (student_id, standard_id, level)
        ).fetchone()
    return int(row["n"] or 0)


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
            SET current_level = ?, status = ?, rolling_avg = ?, locked = 0, locked_reason = NULL, last_update = ?
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


def next_up_from(standard_id: str) -> tuple[str | None, int | None]:
    """
    Look up the next standard from standard_links.
    Only progression links are used here.
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT to_standard_id
            FROM standard_links
            WHERE from_standard_id = ?
              AND link_type = 'progression'
            ORDER BY priority ASC, id ASC
            LIMIT 1
            """,
            (standard_id,)
        ).fetchone()

    if not row:
        return None, None

    return row["to_standard_id"], 1


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
    Temporary validation stub.
    Returns 0.0 so the engine does not auto-pass mini-lessons during Step 16 testing.
    """
    return 0.0


# -------------- Core engine: call after each graded response --------------

def lock_standard(student_id: str, standard_id: str) -> None:
    set_locked(student_id, standard_id, True, reason="mini_lesson_fail")
    notify_lock(student_id, standard_id)

def build_mini_lesson_payload(standard_id: str, objective_id: str | None = None, level: int = 1) -> dict:
    """
    Build a simple review payload for the student UI.
    This is intentionally basic for now so we can get the review flow working first.
    """
    objective_text = ""

    # Try to pull objective text if your project has a helper for that later.
    # For now, keep it safe and simple.
    if objective_id:
        objective_text = objective_id

    if objective_text:
        summary = f"Review this concept before continuing: {objective_text}"
    else:
        summary = "Review this concept before continuing."

    return {
        "title": "Quick Review",
        "objective_text": objective_text,
        "summary": summary,
        "key_points": [],
        "standard_id": standard_id,
        "objective_id": objective_id,
        "level": level,
    }

def process_after_response(student_id: str, standard_id: str) -> dict:
    print("RUNNING process_after_response", student_id, standard_id)
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

    avg = rolling7_avg(student_id, standard_id, level)
    count = response_count(student_id, standard_id, level)

    # Temporary MVP safeguard:
    # Do not advance, remediate, or lock until a full Rolling-7 window exists.
    # This prevents a single correct/incorrect answer from triggering routing.
    if count < ROLL_N:
        set_state(student_id, standard_id, level, "practicing", avg)
        return {
            "status": "question",
            "action": "collecting_rolling7",
            "standard": standard_id,
            "level": level,
            "avg": avg,
            "count": count,
                           "standard": standard_id,
 "needed": ROLL_N,
            "reason": "insufficient_rolling7_data",
        }

    if state["locked"]:
        mini_lesson = build_mini_lesson_payload(
            standard_id=standard_id,
            objective_id=None,
            level=level,
        )
        return {
            "status": "locked",
            "action": "locked",
            "standard": standard_id,
            "level": level,
            "reason": state["locked_reason"],
            "mini_lesson": mini_lesson,
        }

    # ---- Advance (mastery) ----
    if avg >= MASTERY:
        max_level = 3

        if level < max_level:
            new_level = level + 1
            set_state(student_id, standard_id, new_level, "practicing", avg)
            return {
                "status": "question",
                "standard": standard_id,
                "level": new_level,
                "avg": avg,
                "reason": "mastery_advance",
            }

        # Finished final level → look for next standard in standard_links
        next_std, next_lvl = next_up_from(standard_id)

        if next_std:
            set_state(student_id, next_std, next_lvl, "practicing", 0.0)
            return {
                "status": "question",
                "action": "next_standard_found",
                "from": (standard_id, level),
                "to": (next_std, next_lvl),
                "standard": next_std,
                "level": next_lvl,
                "reason": "progression_link_found",
            }

        return {
            "status": "complete",
            "action": "standard_complete",
            "standard": standard_id,
            "level": level,
            "reason": "no_progression_link_found",
        }

    # ---- Middle band: keep practicing ----
    if REMEDIATE <= avg < MASTERY:
        set_state(student_id, standard_id, level, "practicing", avg)
        return {
            "status": "question",
            "action": "serve_practice",
            "standard": standard_id,
            "level": level,
            "avg": avg,
            "reason": "middle_band_continue",
        }

    # ---- Remediation (<70%) ----
    # Same-standard level drop
    if level > 1:
        drop_to_level = level - 1
        set_state(student_id, standard_id, drop_to_level, "practicing", 0.0)
        return {
            "status": "question",
            "action": "drop_level",
            "standard": standard_id,
            "level": drop_to_level,
            "avg": avg,
            "reason": "below_remediation_threshold",
        }

    # At Level 1, try lower-band remediation
    lower_std = lower_band_of(standard_id)
    if lower_std:
        remember_origin(student_id, lower_std, standard_id, level)
        notify_drop(student_id, standard_id, level, lower_std, 1, avg)
        set_state(student_id, lower_std, 1, "practicing", 0.0)
        return {
            "status": "question",
            "action": "remediate_lower_band",
            "standard": lower_std,
            "level": 1,
            "avg": avg,
            "reason": "below_remediation_threshold",
        }

    # No lower standard → Mini-lesson path
    mini_score = mini_lesson_mastered(student_id, standard_id)
    if mini_score >= MASTERY:
        origin = recall_origin(student_id, standard_id)
        if origin:
            clear_origin(student_id, standard_id)
            set_state(student_id, origin["from_std"], origin["from_level"], "practicing", 0.0)
            return {
                "status": "question",
                "action": "mini_pass_return",
                "to": (origin["from_std"], origin["from_level"]),
            }
        # No origin saved; just resume this standard
        set_state(student_id, standard_id, level, "practicing", 0.0)
        return {
            "status": "question",
            "action": "mini_pass_resume",
            "standard": standard_id,
            "level": level,
        }

    # Mini-lesson not mastered → lock
    lock_standard(student_id, standard_id)

    mini_lesson = build_mini_lesson_payload(
        standard_id=standard_id,
        objective_id=None,
        level=level,
    )

    return {
        "status": "locked",
        "action": "locked",
        "standard": standard_id,
        "level": level,
        "avg": avg,
        "reason": "mini_lesson_fail",
        "mini_lesson": mini_lesson,
    }


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
