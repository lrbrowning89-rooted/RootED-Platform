# tests/test_engine.py
import sqlite3
import os
import sys
import time

# Allow Python to find app_core imports
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'app_core'))

import adaptive_engine as ae

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'ngss.db')

def test_log_and_rolling_avg():
    """Basic sanity test for log_response and rolling7_avg"""
    conn = sqlite3.connect(DB_PATH)
    student_id = "TST01"
    standard_id = "MS-LS1-1"
    level = 1

    # Clear out any old test data
    conn.execute("DELETE FROM responses WHERE student_id=?", (student_id,))
    conn.commit()

    # Log 7 alternating responses (4 correct, 3 incorrect)
    for i in range(7):
        ae.log_response(student_id, standard_id, level, f"Q{i}", correct=(i % 2 == 0))
        time.sleep(0.1)

    avg = ae.rolling7_avg(student_id, standard_id, level)
    assert 0.55 < avg < 0.65, f"Rolling-7 average unexpected: {avg}"
    print(f"✅ Rolling-7 test passed, avg={avg:.2f}")

if __name__ == "__main__":
    test_log_and_rolling_avg()
    print("All tests passed!")
