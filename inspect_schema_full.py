import sqlite3

DB = "data/ngss.db"
TABLES = ["progress_state", "attempts", "responses"]

conn = sqlite3.connect(DB)

for t in TABLES:
    print(f"\n=== {t} ===")
    cols = conn.execute("PRAGMA table_info(%s);" % t).fetchall()
    if not cols:
        print("(no columns returned - table missing?)")
        continue
    for c in cols:
        print(f"- {c[1]} ({c[2]}) pk={c[5]} notnull={c[3]} default={c[4]}")

conn.close()