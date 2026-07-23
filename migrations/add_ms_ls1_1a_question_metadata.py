import json
import os
import shutil
import sqlite3
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "ngss.db"
JSON_PATH = ROOT / "data" / "imports" / "MS-LS1-1A.json"
BACKUP_DIR = ROOT / "data" / "backups"

OBJECTIVE_ID = "MS-LS1-1A"
STANDARD_ID = "MS-LS1-1"
SOURCE_ARTIFACT = "MS-LS1-1A.json"
EXPECTED_QUESTION_COUNT = 21

METADATA_COLUMNS = (
    "question_id",
    "difficulty",
    "question_type",
    "explanation",
    "identifier_tags",
    "remediation_tags",
    "model_id",
    "source_artifact",
)


def normalize_question_type(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lower().replace(" ", "_")


def load_questions(json_path: Path = JSON_PATH) -> list[dict]:
    pack = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(pack, dict):
        raise ValueError(f"{json_path.name} must be an object")
    if pack.get("standard_id") != STANDARD_ID:
        raise ValueError(f"{json_path.name} standard_id is not {STANDARD_ID}")
    if pack.get("objective_id") != OBJECTIVE_ID:
        raise ValueError(f"{json_path.name} objective_id is not {OBJECTIVE_ID}")

    questions = pack.get("questions", [])
    if len(questions) != EXPECTED_QUESTION_COUNT:
        raise ValueError(
            f"{json_path.name} must contain {EXPECTED_QUESTION_COUNT} questions"
        )

    question_ids = [q.get("question_id") for q in questions]
    if len(set(question_ids)) != EXPECTED_QUESTION_COUNT:
        raise ValueError(f"{json_path.name} contains duplicate question IDs")
    return questions


def ensure_question_metadata_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS question_metadata (
            question_id TEXT PRIMARY KEY,
            difficulty TEXT,
            question_type TEXT,
            explanation TEXT,
            identifier_tags TEXT,
            remediation_tags TEXT,
            model_id TEXT,
            source_artifact TEXT,
            imported_at INTEGER
        )
        """
    )


def metadata_from_question(question: dict) -> dict:
    return {
        "question_id": question["question_id"],
        "difficulty": question.get("difficulty"),
        "question_type": normalize_question_type(question.get("question_type")),
        "explanation": question.get("explanation"),
        "identifier_tags": json.dumps(question.get("identifier_tags", [])),
        "remediation_tags": json.dumps(question.get("remediation_tags", [])),
        "model_id": question.get("model_id"),
        "source_artifact": SOURCE_ARTIFACT,
    }


def validate_source_matches_database(
    conn: sqlite3.Connection, questions: list[dict]
) -> None:
    for question in questions:
        row = conn.execute(
            """
            SELECT question_id, objective_id, stem, choice_a, choice_b, choice_c,
                   choice_d, answer_key
            FROM questions
            WHERE question_id = ?
            """,
            (question["question_id"],),
        ).fetchone()
        if row is None:
            raise ValueError(f"Missing live question {question['question_id']}")
        if row["objective_id"] != OBJECTIVE_ID:
            raise ValueError(
                f"{question['question_id']} belongs to {row['objective_id']}, "
                f"not {OBJECTIVE_ID}"
            )
        if row["stem"] != question.get("prompt"):
            raise ValueError(f"Stem mismatch for {question['question_id']}")

        choices = question.get("answer_choices", {})
        for letter, column in (
            ("A", "choice_a"),
            ("B", "choice_b"),
            ("C", "choice_c"),
            ("D", "choice_d"),
        ):
            if row[column] != choices.get(letter):
                raise ValueError(f"Choice {letter} mismatch for {question['question_id']}")
        if row["answer_key"] != question.get("correct_answer"):
            raise ValueError(f"Answer key mismatch for {question['question_id']}")


def backup_database(db_path: Path, backup_dir: Path = BACKUP_DIR) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"ngss_before_ms_ls1_1a_metadata_{timestamp}.db"
    shutil.copy2(db_path, backup_path)
    return backup_path


def insert_metadata(
    conn: sqlite3.Connection, questions: list[dict], imported_at: int | None = None
) -> dict:
    imported_at = imported_at or int(time.time())
    inserted = 0
    skipped = 0
    conflicts = []

    for question in questions:
        intended = metadata_from_question(question)
        existing = conn.execute(
            "SELECT * FROM question_metadata WHERE question_id = ?",
            (intended["question_id"],),
        ).fetchone()

        if existing is not None:
            mismatched_columns = [
                column
                for column in METADATA_COLUMNS
                if existing[column] != intended[column]
            ]
            if mismatched_columns:
                conflicts.append((intended["question_id"], mismatched_columns))
            else:
                skipped += 1
            continue

        conn.execute(
            """
            INSERT INTO question_metadata
              (question_id, difficulty, question_type, explanation,
               identifier_tags, remediation_tags, model_id, source_artifact,
               imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intended["question_id"],
                intended["difficulty"],
                intended["question_type"],
                intended["explanation"],
                intended["identifier_tags"],
                intended["remediation_tags"],
                intended["model_id"],
                intended["source_artifact"],
                imported_at,
            ),
        )
        inserted += 1

    if conflicts:
        details = "; ".join(
            f"{question_id}: {', '.join(columns)}"
            for question_id, columns in conflicts
        )
        raise ValueError(f"Existing metadata conflicts detected: {details}")

    return {"inserted": inserted, "updated": 0, "skipped": skipped}


def run(
    db_path: Path | str | None = None,
    json_path: Path | str = JSON_PATH,
    create_backup: bool = True,
) -> dict:
    db_path = Path(db_path or os.environ.get("NGSS_DB", DEFAULT_DB))
    json_path = Path(json_path)
    questions = load_questions(json_path)
    backup_path = backup_database(db_path) if create_backup else None

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_question_metadata_table(conn)
        validate_source_matches_database(conn, questions)
        result = insert_metadata(conn, questions)
        conn.commit()

    result["backup_path"] = str(backup_path) if backup_path else None
    return result


if __name__ == "__main__":
    summary = run()
    print(f"Backup: {summary['backup_path']}")
    print(f"Inserted metadata rows: {summary['inserted']}")
    print(f"Updated metadata rows: {summary['updated']}")
    print(f"Skipped existing valid rows: {summary['skipped']}")
    print("MS-LS1-1A question metadata migration complete")
