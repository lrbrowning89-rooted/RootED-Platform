import sqlite3

DB = "data/ngss.db"
STD = "MS-LS1-1"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print("=== objective_levels schema ===")
cols = conn.execute("PRAGMA table_info(objective_levels);").fetchall()
for c in cols:
    print(f"- {c['name']} ({c['type']}) pk={c['pk']} notnull={c['notnull']} default={c['dflt_value']}")

print("\n=== objective_levels rows for MS-LS1-1 ===")
rows = conn.execute("""
SELECT *
FROM objective_levels
WHERE standard_id = ?
ORDER BY level;
""", (STD,)).fetchall()

if not rows:
    print("NONE")
else:
    for r in rows:
        print(dict(r))

print("\n=== objectives for MS-LS1-1 (source of truth) ===")
rows = conn.execute("""
SELECT objective_id, order_in_band
FROM objectives
WHERE standard_id = ?
ORDER BY order_in_band;
""", (STD,)).fetchall()
print([tuple(r) for r in rows])

conn.close()