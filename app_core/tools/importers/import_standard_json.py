import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_json(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(data, dict), "Root JSON must be an object.")
    return data


def validate_payload(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    require("standard" in payload and isinstance(payload["standard"], dict), "Missing object: standard")
    std = payload["standard"]
    for k in ["standard_id", "core_idea", "grade_band"]:
        require(k in std and isinstance(std[k], str) and std[k].strip(), f"standard.{k} must be a non-empty string")

    require("objectives" in payload and isinstance(payload["objectives"], list), "Missing array: objectives")
    objectives = payload["objectives"]

    for i, obj in enumerate(objectives):
        require(isinstance(obj, dict), f"objectives[{i}] must be an object")
        for k in ["objective_id", "objective_text", "display_name", "order_in_band"]:
            require(k in obj, f"objectives[{i}].{k} is required")
        require(isinstance(obj["objective_id"], str) and obj["objective_id"].strip(), f"objectives[{i}].objective_id must be a string")
        require(isinstance(obj["objective_text"], str) and obj["objective_text"].strip(), f"objectives[{i}].objective_text must be a string")
        require(isinstance(obj["display_name"], str) and obj["display_name"].strip(), f"objectives[{i}].display_name must be a non-empty string")
        require(isinstance(obj["order_in_band"], int), f"objectives[{i}].order_in_band must be an int")

        if "questions" in obj:
            require(isinstance(obj["questions"], list), f"objectives[{i}].questions must be an array if present")
            for j, q in enumerate(obj["questions"]):
                require(isinstance(q, dict), f"objectives[{i}].questions[{j}] must be an object")
                for k in ["stem", "choice_a", "choice_b", "choice_c", "choice_d", "answer_key", "reading_level"]:
                    require(k in q, f"objectives[{i}].questions[{j}].{k} is required")
                require(isinstance(q["stem"], str) and q["stem"].strip(), f"questions[{j}].stem must be a string")
                for ck in ["choice_a", "choice_b", "choice_c", "choice_d"]:
                    require(isinstance(q[ck], str) and q[ck].strip(), f"questions[{j}].{ck} must be a string")
                require(isinstance(q["answer_key"], str) and q["answer_key"].strip(), f"questions[{j}].answer_key must be a string")
                require(q["answer_key"].strip().upper() in {"A", "B", "C", "D"}, f"questions[{j}].answer_key must be A/B/C/D")
                require(isinstance(q["reading_level"], int), f"questions[{j}].reading_level must be an int")

    return std, objectives


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1;", (name,)
    ).fetchone()
    return row is not None


def import_standard(conn: sqlite3.Connection, std: Dict[str, Any], objectives: List[Dict[str, Any]], dry_run: bool) -> Dict[str, int]:
    # Safety: only write to these 3 tables
    for t in ["standards", "objectives", "questions"]:
        require(table_exists(conn, t), f"Required table missing: {t}")

    standard_id = std["standard_id"].strip()
    core_idea = std["core_idea"].strip()
    grade_band = std["grade_band"].strip()

    counts = {"standards_written": 0, "objectives_written": 0, "questions_written": 0}

    # We do NOT delete anything here. Deletion/replacement was a separate manual step you already did safely.
    if dry_run:
        print(f"[DRY RUN] Would UPDATE standards for standard_id={standard_id}")
    else:
        conn.execute(
            """
            UPDATE standards
            SET core_idea = ?, grade_band = ?
            WHERE standard_id = ?;
            """,
            (core_idea, grade_band, standard_id),
        )
        counts["standards_written"] += 1

    # Insert objectives + questions
    for obj in objectives:
        objective_id = obj["objective_id"].strip()
        objective_text = obj["objective_text"].strip()
        display_name = obj["display_name"].strip()
        student_description = (obj.get("student_description") or "").strip() or None
        order_in_band = int(obj["order_in_band"])

        if dry_run:
            print(f"[DRY RUN] Would INSERT/REPLACE objective_id={objective_id} (standard_id={standard_id})")
        else:
            conn.execute(
                """
                INSERT OR REPLACE INTO objectives
                  (objective_id, standard_id, objective_text, display_name,
                   student_description, order_in_band)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (objective_id, standard_id, objective_text, display_name,
                 student_description, order_in_band),
            )
            counts["objectives_written"] += 1

        questions = obj.get("questions", [])
        for j, q in enumerate(questions, start=1):
            stem = q["stem"].strip()
            choice_a = q["choice_a"].strip()
            choice_b = q["choice_b"].strip()
            choice_c = q["choice_c"].strip()
            choice_d = q["choice_d"].strip()
            answer_key = q["answer_key"].strip().upper()
            reading_level = int(q["reading_level"])

            question_id = f"Q_{objective_id}_{j}"

            if dry_run:
                print(f"[DRY RUN] Would INSERT question_id={question_id} for objective_id={objective_id}: {stem[:50]}...")
            else:
                conn.execute(
                    """
                    INSERT INTO questions (
                        question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key, reading_level
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (question_id, objective_id, stem, choice_a, choice_b, choice_c, choice_d, answer_key, reading_level),
                )
                counts["questions_written"] += 1

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Import one NGSS standard JSON into existing RootED tables.")
    parser.add_argument("json_path", type=str, help="Path to JSON file (e.g., data/imports/ms-ls1-1.json)")
    parser.add_argument("--db", type=str, default="data/ngss.db", help="Path to SQLite db (default: data/ngss.db)")
    parser.add_argument("--dry-run", action="store_true", help="Validate + print actions, but do not write to the DB.")
    args = parser.parse_args()

    json_path = Path(args.json_path)
    db_path = Path(args.db)

    require(json_path.exists(), f"JSON file not found: {json_path}")
    require(db_path.exists(), f"DB file not found: {db_path}")

    payload = load_json(json_path)
    std, objectives = validate_payload(payload)

    conn = connect_db(db_path)
    try:
        if args.dry_run:
            counts = import_standard(conn, std, objectives, dry_run=True)
            print("[DRY RUN] Complete.", counts)
        else:
            conn.execute("BEGIN;")
            counts = import_standard(conn, std, objectives, dry_run=False)
            conn.commit()
            print("Import complete.", counts)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
