import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_path import add_db_argument, print_database_path, resolve_database_path


parser = argparse.ArgumentParser(description="Audit MS-LS1-1C database rows and file references.")
add_db_argument(parser)
args = parser.parse_args()
db_path = resolve_database_path(args.db)

print_database_path(db_path)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== DATABASE AUDIT: MS-LS1-1C ===")

print("\nOBJECTIVES:")
for r in conn.execute("""
    SELECT objective_id, standard_id, objective_text, order_in_band
    FROM objectives
    WHERE objective_id LIKE '%MS-LS1-1C%'
       OR objective_id LIKE '%MSLS1_1_C%'
"""):
    print(dict(r))

print("\nQUESTIONS:")
for r in conn.execute("""
    SELECT question_id, objective_id, stem
    FROM questions
    WHERE objective_id LIKE '%MS-LS1-1C%'
       OR question_id LIKE '%MSLS1_1_C%'
       OR question_id LIKE '%MS-LS1-1C%'
    ORDER BY question_id
"""):
    print(dict(r))

print("\nPROGRESS STATE:")
for r in conn.execute("""
    SELECT *
    FROM progress_state
    WHERE standard_id='MS-LS1-1'
       OR current_standard_id='MS-LS1-1'
       OR origin_standard_id='MS-LS1-1'
"""):
    print(dict(r))

print("\nRESPONSES LEVEL 3:")
for r in conn.execute("""
    SELECT student_id, standard_id, level, question_id, correct, ts
    FROM responses
    WHERE standard_id='MS-LS1-1'
      AND level=3
    ORDER BY ts DESC
    LIMIT 20
"""):
    print(dict(r))

conn.close()

print("\n=== FILE AUDIT: MS-LS1-1C REFERENCES ===")

root = Path(".")
for path in root.rglob("*"):
    if path.is_dir():
        continue
    if any(part in {".git", "venv", "__pycache__"} for part in path.parts):
        continue
    if path.suffix.lower() not in {".py", ".json", ".csv", ".txt", ".md"}:
        continue

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue

    if "MS-LS1-1C" in text or "MSLS1_1_C" in text:
        print(path)
