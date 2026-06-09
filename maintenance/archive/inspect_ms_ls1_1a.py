import sqlite3

conn = sqlite3.connect("data/ngss.db")
conn.row_factory = sqlite3.Row

print("=== OBJECTIVES ===")
for r in conn.execute("""
    SELECT objective_id, standard_id, objective_text, order_in_band
    FROM objectives
    WHERE objective_id = 'MS-LS1-1A'
       OR objective_id LIKE 'MS-LS1-1A%'
"""):
    print(dict(r))

print("\n=== QUESTIONS ===")
questions = conn.execute("""
    SELECT question_id, objective_id, stem
    FROM questions
    WHERE objective_id = 'MS-LS1-1A'
    ORDER BY question_id
""").fetchall()

print("QUESTION COUNT:", len(questions))

for q in questions:
    print(f"{q['question_id']} | {q['stem']}")

conn.close()