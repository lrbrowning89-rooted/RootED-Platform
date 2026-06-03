import sqlite3

conn = sqlite3.connect("data/ngss.db")

rows = conn.execute("""
SELECT student_id, MAX(timestamp) AS last_ts, COUNT(*) AS attempts
FROM attempts
GROUP BY student_id
ORDER BY last_ts DESC
LIMIT 10;
""").fetchall()

print("Most recent student_ids in attempts:")
for r in rows:
    print(r)

conn.close()