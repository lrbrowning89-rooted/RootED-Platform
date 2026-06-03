import sqlite3

conn = sqlite3.connect("data/ngss.db")
conn.row_factory = sqlite3.Row

print("=== questions schema ===")
cols = conn.execute("PRAGMA table_info(questions);").fetchall()
for c in cols:
    print(f"- {c['name']} ({c['type']}) pk={c['pk']} notnull={c['notnull']} default={c['dflt_value']}")

print("\n=== first 10 MS-LS1-1 questions ===")
rows = conn.execute("""
SELECT q.question_id, q.objective_id, q.stem
FROM questions q
JOIN objectives o ON o.objective_id = q.objective_id
WHERE o.standard_id = 'MS-LS1-1'
ORDER BY q.question_id
LIMIT 10
""").fetchall()

for r in rows:
    print(dict(r))

conn.close()