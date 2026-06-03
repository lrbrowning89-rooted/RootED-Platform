import sqlite3

STD = "MS-LS1-1"
DB = "data/ngss.db"

conn = sqlite3.connect(DB)

print("Objective ordering:")
rows = conn.execute("""
SELECT objective_id, order_in_band
FROM objectives
WHERE standard_id = ?
ORDER BY order_in_band;
""", (STD,)).fetchall()
print(rows)

print("\nQuestions per objective:")
rows = conn.execute("""
SELECT o.objective_id, COUNT(*)
FROM objectives o
JOIN questions q ON q.objective_id = o.objective_id
WHERE o.standard_id = ?
GROUP BY o.objective_id
ORDER BY o.order_in_band;
""", (STD,)).fetchall()
print(rows)

print("\nTotal questions:")
total = conn.execute("""
SELECT COUNT(*)
FROM questions q
JOIN objectives o ON q.objective_id = o.objective_id
WHERE o.standard_id = ?;
""", (STD,)).fetchone()[0]
print(total)

conn.close()