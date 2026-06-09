import sqlite3

conn = sqlite3.connect("data/ngss.db")
conn.row_factory = sqlite3.Row

for std in ("MS-LS1-2", "MS-LS1-3"):
    print("\n==============================")
    print("STANDARD:", std)
    print("==============================")

    objectives = conn.execute("""
        SELECT objective_id, objective_text, order_in_band
        FROM objectives
        WHERE standard_id = ?
        ORDER BY order_in_band, objective_id
    """, (std,)).fetchall()

    print("OBJECTIVE COUNT:", len(objectives))

    for obj in objectives:
        print(f"\nOBJECTIVE: {obj['objective_id']}")
        print("TEXT:", obj["objective_text"])
        print("ORDER:", obj["order_in_band"])

        qs = conn.execute("""
            SELECT question_id, stem
            FROM questions
            WHERE objective_id = ?
            ORDER BY question_id
        """, (obj["objective_id"],)).fetchall()

        print("QUESTION COUNT:", len(qs))
        for q in qs:
            print(f"  {q['question_id']}: {q['stem']}")

conn.close()