import sqlite3

DB = "data/ngss.db"
TABLES = ["progress_state", "attempts", "responses"]

conn = sqlite3.connect(DB)

for t in TABLES:
    print(f"\n=== {t} ===")
    cols = conn.execute(f"PRAGMA table_info({t});").fetchall()
    for c in cols:
        # (cid, name, type, notnull, dflt_value, pk)
        print(f"- {c[1]} ({c[2]}) pk={c[5]} notnull={c[3]} default={c[4]}")

conn.close()