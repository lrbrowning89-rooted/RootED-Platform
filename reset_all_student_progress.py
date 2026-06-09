import sqlite3

conn = sqlite3.connect("data/ngss.db")

for table in (
    "attempts",
    "responses",
    "progress_state",
    "origin_links",
    "notifications",
    "student_objective_state",
):
    conn.execute(f"DELETE FROM {table}")

conn.commit()
conn.close()

print("Reset all student practice/progress/objective state")