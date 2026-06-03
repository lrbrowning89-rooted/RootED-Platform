import sqlite3

conn = sqlite3.connect("data/ngss.db")

rows = conn.execute("""
SELECT objective_id, order_in_band
FROM objectives
WHERE standard_id = 'MS-LS1-1'
ORDER BY order_in_band;
""").fetchall()

print("Objective Ordering:", rows)

count = conn.execute("""
SELECT COUNT(*)
FROM questions q
JOIN objectives o ON q.objective_id = o.objective_id
WHERE o.standard_id = 'MS-LS1-1';
""").fetchone()[0]

print("Total Questions:", count)
conn.close()