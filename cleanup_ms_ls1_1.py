import sqlite3

DB_PATH = "data/ngss.db"
STD = "MS-LS1-1"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# Delete questions for objectives in this standard
cur.execute("""
DELETE FROM questions
WHERE objective_id IN (
  SELECT objective_id
  FROM objectives
  WHERE standard_id = ?
);
""", (STD,))

# Delete objectives for this standard
cur.execute("""
DELETE FROM objectives
WHERE standard_id = ?;
""", (STD,))

conn.commit()
conn.close()

print(f"Deleted all objectives + questions for {STD}")