import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_path import add_db_argument, print_database_path, resolve_database_path


parser = argparse.ArgumentParser(description="Inspect MS-LS1-1A content in the RootED database.")
add_db_argument(parser)
args = parser.parse_args()
db_path = resolve_database_path(args.db)

print_database_path(db_path)

conn = sqlite3.connect(db_path)
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
