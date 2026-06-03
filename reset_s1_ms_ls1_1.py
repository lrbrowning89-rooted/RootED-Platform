import sqlite3

DB = "data/ngss.db"
STUDENT = "S1"
STANDARD = "MS-LS1-1"

conn = sqlite3.connect(DB)
cur = conn.cursor()

# Delete responses for this student + standard
cur.execute("""
DELETE FROM responses
WHERE student_id = ?
  AND standard_id = ?;
""", (STUDENT, STANDARD))

# Delete attempts tied to this standard for this student
cur.execute("""
DELETE FROM attempts
WHERE student_id = ?
  AND question_id IN (
      SELECT q.question_id
      FROM questions q
      JOIN objectives o ON o.objective_id = q.objective_id
      WHERE o.standard_id = ?
  );
""", (STUDENT, STANDARD))

# Delete progress_state row for this student + standard
cur.execute("""
DELETE FROM progress_state
WHERE student_id = ?
  AND standard_id = ?;
""", (STUDENT, STANDARD))

# Delete notifications for this student + standard if present
try:
    cur.execute("""
    DELETE FROM notifications
    WHERE student_id = ?
      AND standard_id = ?;
    """, (STUDENT, STANDARD))
except Exception:
    pass

conn.commit()
conn.close()

print(f"Reset complete for {STUDENT} / {STANDARD}")