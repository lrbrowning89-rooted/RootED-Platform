import sqlite3
from datetime import datetime

DB = "data/ngss.db"
STD = "MS-LS1-1"

# Put your test student_id here after Step 2
STUDENT_ID = "S1"

def ts_to_str(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print(f"\n=== progress_state for {STUDENT_ID} / {STD} ===")
ps = conn.execute("""
SELECT student_id, standard_id, current_level, status, rolling_avg, locked, locked_reason, last_update
FROM progress_state
WHERE student_id = ? AND standard_id = ?;
""", (STUDENT_ID, STD)).fetchone()

if not ps:
    print("No progress_state row found yet. (Start the standard in the app and answer 1 question, then run again.)")
else:
    print(dict(ps))
    if ps["last_update"] is not None:
        print("last_update_readable:", ts_to_str(ps["last_update"]))

print(f"\n=== last 15 attempts (joined to objective/order) ===")
rows = conn.execute("""
SELECT a.timestamp, a.question_id, a.is_correct,
       o.objective_id, o.order_in_band
FROM attempts a
JOIN questions q ON CAST(q.question_id AS TEXT) = a.question_id
JOIN objectives o ON o.objective_id = q.objective_id
WHERE a.student_id = ?
  AND o.standard_id = ?
ORDER BY a.timestamp DESC
LIMIT 15;
""", (STUDENT_ID, STD)).fetchall()

if not rows:
    print("No attempts found yet for this student + standard.")
else:
    for r in rows:
        print({
            "ts": ts_to_str(r["timestamp"]),
            "question_id": r["question_id"],
            "correct": r["is_correct"],
            "objective_id": r["objective_id"],
            "order_in_band": r["order_in_band"],
        })

print(f"\n=== Rolling-7 computed from last 7 attempts (this standard) ===")
last7 = conn.execute("""
SELECT a.is_correct
FROM attempts a
JOIN questions q ON CAST(q.question_id AS TEXT) = a.question_id
JOIN objectives o ON o.objective_id = q.objective_id
WHERE a.student_id = ?
  AND o.standard_id = ?
ORDER BY a.timestamp DESC
LIMIT 7;
""", (STUDENT_ID, STD)).fetchall()

if last7:
    vals = [int(r["is_correct"]) for r in last7]
    pct = (sum(vals) / len(vals)) * 100.0
    print(f"last7 = {vals}  ->  {sum(vals)}/{len(vals)} = {pct:.1f}%")
else:
    print("Not enough attempts yet to compute Rolling-7.")

conn.close()