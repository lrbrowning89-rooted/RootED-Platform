import sqlite3
from datetime import datetime

conn = sqlite3.connect("data/ngss.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("""
SELECT student_id, question_id, is_correct, timestamp
FROM attempts
ORDER BY timestamp DESC
LIMIT 20;
""").fetchall()

print("Last 20 attempts:")
for r in rows:
    ts = datetime.fromtimestamp(r["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
    print(ts, r["student_id"], r["question_id"], r["is_correct"])

conn.close()