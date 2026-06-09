import sqlite3
import json
from collections import Counter

conn = sqlite3.connect("data/ngss.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT q.question_id, q.objective_id, q.stem, m.difficulty, m.identifier_tags, m.model_id
    FROM questions q
    LEFT JOIN question_metadata m ON m.question_id = q.question_id
    WHERE q.objective_id = 'MS-LS1-2B'
    ORDER BY q.question_id
""").fetchall()

print("QUESTION COUNT:", len(rows))

difficulty = Counter(r["difficulty"] for r in rows)
print("DIFFICULTY DISTRIBUTION:", dict(difficulty))

identifier_counts = Counter()
for r in rows:
    tags = json.loads(r["identifier_tags"] or "[]")
    identifier_counts.update(tags)
print("IDENTIFIER COVERAGE:", dict(identifier_counts))

model_count = sum(1 for r in rows if r["model_id"])
print("MODEL ID COUNT:", model_count)

print("\nFIRST 3 QUESTIONS:")
for r in rows[:3]:
    print(r["question_id"], "|", r["difficulty"], "|", r["stem"])

print("\nOBJECTIVE:")
obj = conn.execute("""
    SELECT objective_id, standard_id, objective_text, order_in_band
    FROM objectives
    WHERE objective_id='MS-LS1-2B'
""").fetchone()
print(dict(obj) if obj else None)

conn.close()