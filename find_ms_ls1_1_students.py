import sqlite3

DB = "data/ngss.db"
STD = "MS-LS1-1"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
SELECT a.student_id, COUNT(*) AS n_attempts, MAX(a.timestamp) AS last_ts
FROM attempts a
JOIN questions q ON CAST(q.question_id AS TEXT) = a.question_id
JOIN objectives o ON o.objective_id = q.objective_id
WHERE o.standard_id = ?
GROUP BY a.student_id
ORDER BY last_ts DESC;
""", (STD,)).fetchall()

print(f"Students with attempts in {STD}:")
if not rows:
    print("NONE YET")
else:
    for r in rows:
        print(dict(r))

conn.close()