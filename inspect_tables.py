import sqlite3

conn = sqlite3.connect("data/ngss.db")

rows = conn.execute("""
SELECT name
FROM sqlite_master
WHERE type='table'
ORDER BY name;
""").fetchall()

print("Tables in DB:")
for r in rows:
    print("-", r[0])

conn.close()