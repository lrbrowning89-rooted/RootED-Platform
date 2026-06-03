import sqlite3
from datetime import datetime

conn = sqlite3.connect("data/ngss.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("""
SELECT student_id, standard_id, level, question_id, correct, ts
FROM responses
ORDER BY ts DESC
LIMIT 20;
""").fetchall()

print("Last 20 responses:")
for r in rows:
    ts = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
    print(ts, r["student_id"], r["standard_id"], "L", r["level"], "Q", r["question_id"], "C", r["correct"])

conn.close()
