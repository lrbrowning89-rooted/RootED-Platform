"""Install RootED's approved launch content into one explicit SQLite database.

This command is content-only. It never falls back to NGSS_DB or data/ngss.db.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE_DIR = ROOT / "data" / "imports"
PACKAGE_NAME = "rooted-launch-content"
DEFAULT_PACKAGE_VERSION = "2026-07-27"
PACKAGE_FILES = (
    "MS-LS1-1A.json",
    "MS-LS1-1B.json",
    "MS-LS1-2A.json",
    "MS-LS1-2B.json",
    "MS-LS1-3A.json",
    "MS-LS1-3B.json",
    "MS-LS1-3C.json",
)
LAUNCH_STANDARD_IDS = ("MS-LS1-1", "MS-LS1-2", "MS-LS1-3")
LAUNCH_OBJECTIVE_IDS = (
    "MS-LS1-1A",
    "MS-LS1-1B",
    "MS-LS1-2A",
    "MS-LS1-2B",
    "MS-LS1-3A",
    "MS-LS1-3B",
    "MS-LS1-3C",
)
STANDARD_DATA = {
    "MS-LS1-1": ("Structure and Function", "LS1.A", "MS"),
    "MS-LS1-2": ("Cells as Systems", "LS1", "MS"),
    "MS-LS1-3": ("Interacting Body Systems", "LS1", "MS"),
}
OBJECTIVE_DEFAULTS = {
    "MS-LS1-1A": (
        "Define a cell and explain why cells are considered the basic unit of life.",
        "Cells as the Basic Unit of Life",
        None,
        1,
    ),
    "MS-LS1-1B": (
        "Use evidence to explain how observations of cells support cell theory.",
        "Evidence for Cell Theory",
        "Students use evidence from microscope observations, investigations, "
        "and models to explain how observations of cells support cell theory.",
        2,
    ),
    "MS-LS1-2A": (
        "Explain how cell structures perform functions that support the cell.",
        "Cell Structures and Functions",
        None,
        1,
    ),
    "MS-LS1-2B": (
        "Explain how cell parts work together as a system.",
        "Cell as a System",
        None,
        2,
    ),
    "MS-LS1-3A": (
        "Explain how cells, tissues, organs, and organ systems are organized.",
        "Levels of Biological Organization",
        None,
        1,
    ),
    "MS-LS1-3B": (
        "Identify major organ systems and describe their primary functions.",
        "Organ Systems and Their Functions",
        None,
        2,
    ),
    "MS-LS1-3C": (
        "Explain how body systems interact to support the organism.",
        "Body-System Interactions",
        None,
        3,
    ),
}
OBJECTIVE_STANDARD_IDS = {
    objective_id: objective_id[:-1] for objective_id in LAUNCH_OBJECTIVE_IDS
}
REQUIRED_TABLES = ("standards", "objectives", "questions")
PROTECTED_TABLES = (
    "users",
    "user_auth_identities",
    "user_platform_roles",
    "user_instructional_authorizations",
    "class_sections",
    "class_enrollments",
    "responses",
    "progress_state",
    "student_growth_progress",
    "student_objective_state",
    "routing_level_attempts",
    "assignments",
    "assignment_recipients",
)


class LaunchContentError(RuntimeError):
    pass


@dataclass(frozen=True)
class LaunchPackage:
    standards: dict[str, dict]
    objectives: dict[str, dict]
    questions: dict[str, dict]
    metadata: dict[str, dict]
    checksum: str


def normalize_question_type(value: object) -> str | None:
    if value is None:
        return None
    return str(value).strip().lower().replace(" ", "_")


def canonical_json(value: object) -> str:
    return json.dumps(value if value is not None else [], separators=(",", ":"))


def resolve_database(path_value: str | Path | None) -> Path:
    if not path_value:
        raise LaunchContentError("--database is required")
    path = Path(path_value).expanduser()
    try:
        path = path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise LaunchContentError(f"Database does not exist: {path}") from exc
    if not path.is_file():
        raise LaunchContentError(f"Database is not a file: {path}")
    if not path.parent.exists():
        raise LaunchContentError(f"Database parent directory does not exist: {path.parent}")
    try:
        with path.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                raise LaunchContentError(f"Target is not a valid SQLite database: {path}")
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise LaunchContentError(f"SQLite quick_check failed: {result}")
    except sqlite3.Error as exc:
        raise LaunchContentError(f"Cannot open SQLite database {path}: {exc}") from exc
    return path


def _choices_and_key(question: dict, source_name: str) -> tuple[str, str, str, str, str]:
    choices = question.get("answer_choices")
    if isinstance(choices, dict):
        if set(choices) != {"A", "B", "C", "D"}:
            raise LaunchContentError(
                f"{source_name}: {question.get('question_id')} must have choices A-D"
            )
        ordered = [choices[letter] for letter in "ABCD"]
    elif isinstance(choices, list) and len(choices) == 4:
        ordered = choices
    else:
        raise LaunchContentError(
            f"{source_name}: {question.get('question_id')} has invalid answer_choices"
        )
    if not all(isinstance(value, str) and value.strip() for value in ordered):
        raise LaunchContentError(
            f"{source_name}: {question.get('question_id')} has an empty answer choice"
        )
    correct = question.get("correct_answer")
    if correct in ("A", "B", "C", "D"):
        answer_key = correct
    else:
        matches = [
            letter for letter, text in zip("ABCD", ordered) if text == correct
        ]
        if len(matches) != 1:
            raise LaunchContentError(
                f"{source_name}: {question.get('question_id')} has invalid correct_answer"
            )
        answer_key = matches[0]
    return (*ordered, answer_key)


def load_package(package_dir: Path | str = DEFAULT_PACKAGE_DIR) -> LaunchPackage:
    package_dir = Path(package_dir).expanduser().resolve()
    paths = [package_dir / name for name in PACKAGE_FILES]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise LaunchContentError("Missing package file(s): " + ", ".join(missing))

    digest = hashlib.sha256()
    standards: dict[str, dict] = {}
    objectives: dict[str, dict] = {}
    questions: dict[str, dict] = {}
    metadata: dict[str, dict] = {}

    for path in paths:
        raw = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(raw)
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, list):
            source_questions = payload
            standard_id = source_questions[0].get("standard_id") if source_questions else None
            objective_id = source_questions[0].get("objective_id") if source_questions else None
        elif isinstance(payload, dict):
            source_questions = payload.get("questions")
            standard_id = payload.get("standard_id")
            objective_id = payload.get("objective_id")
        else:
            raise LaunchContentError(f"{path.name}: package must be an object or array")

        if standard_id not in LAUNCH_STANDARD_IDS:
            raise LaunchContentError(f"{path.name}: unexpected standard_id {standard_id}")
        if objective_id not in LAUNCH_OBJECTIVE_IDS:
            raise LaunchContentError(f"{path.name}: unexpected objective_id {objective_id}")
        if OBJECTIVE_STANDARD_IDS[objective_id] != standard_id:
            raise LaunchContentError(
                f"{path.name}: objective {objective_id} references missing standard "
                f"{standard_id}"
            )
        if objective_id in objectives:
            raise LaunchContentError(f"Duplicate objective ID: {objective_id}")
        if not isinstance(source_questions, list) or len(source_questions) != 21:
            raise LaunchContentError(f"{path.name}: expected exactly 21 questions")

        standard_name, core_idea, grade_band = STANDARD_DATA[standard_id]
        if standard_id in standards and standards[standard_id] != {
            "standard_id": standard_id,
            "standard_name": standard_name,
            "core_idea": core_idea,
            "grade_band": grade_band,
        }:
            raise LaunchContentError(f"Duplicate standard ID conflict: {standard_id}")
        standards[standard_id] = {
            "standard_id": standard_id,
            "standard_name": standard_name,
            "core_idea": core_idea,
            "grade_band": grade_band,
        }

        text, display_name, student_description, order_in_band = OBJECTIVE_DEFAULTS[
            objective_id
        ]
        objectives[objective_id] = {
            "objective_id": objective_id,
            "standard_id": standard_id,
            "objective_text": text,
            "display_name": display_name,
            "student_description": student_description,
            "order_in_band": order_in_band,
        }

        for question in source_questions:
            required = ("question_id", "standard_id", "objective_id", "prompt")
            missing_fields = [
                field for field in required
                if not isinstance(question.get(field), str) or not question[field].strip()
            ]
            if missing_fields:
                raise LaunchContentError(
                    f"{path.name}: question missing required fields {missing_fields}"
                )
            question_id = question["question_id"]
            if question_id in questions:
                raise LaunchContentError(f"Duplicate question ID: {question_id}")
            if question["standard_id"] != standard_id:
                raise LaunchContentError(
                    f"{path.name}: {question_id} references missing standard "
                    f"{question['standard_id']}"
                )
            if question["objective_id"] != objective_id:
                raise LaunchContentError(
                    f"{path.name}: {question_id} references missing objective "
                    f"{question['objective_id']}"
                )
            choice_a, choice_b, choice_c, choice_d, answer_key = _choices_and_key(
                question, path.name
            )
            questions[question_id] = {
                "question_id": question_id,
                "objective_id": objective_id,
                "stem": question["prompt"].strip(),
                "choice_a": choice_a,
                "choice_b": choice_b,
                "choice_c": choice_c,
                "choice_d": choice_d,
                "answer_key": answer_key,
            }
            metadata[question_id] = {
                "question_id": question_id,
                "difficulty": question.get("difficulty"),
                "question_type": normalize_question_type(question.get("question_type")),
                "explanation": question.get("explanation"),
                "identifier_tags": canonical_json(question.get("identifier_tags", [])),
                "remediation_tags": canonical_json(question.get("remediation_tags", [])),
                "model_id": question.get("model_id"),
                "source_artifact": path.name,
            }

    if set(standards) != set(LAUNCH_STANDARD_IDS):
        raise LaunchContentError(
            f"Package must contain exactly {len(LAUNCH_STANDARD_IDS)} launch standards"
        )
    if set(objectives) != set(LAUNCH_OBJECTIVE_IDS):
        raise LaunchContentError(
            f"Package must contain exactly {len(LAUNCH_OBJECTIVE_IDS)} launch objectives"
        )
    if len(questions) != 147:
        raise LaunchContentError(
            f"Package must contain exactly 147 launch questions, found {len(questions)}"
        )
    return LaunchPackage(standards, objectives, questions, metadata, digest.hexdigest())


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def require_schema(conn: sqlite3.Connection) -> None:
    missing = [name for name in REQUIRED_TABLES if not table_exists(conn, name)]
    if missing:
        raise LaunchContentError("Required table(s) missing: " + ", ".join(missing))
    required_columns = {
        "standards": {"standard_id", "standard_name", "core_idea", "grade_band"},
        "objectives": {
            "objective_id", "standard_id", "objective_text", "display_name",
            "student_description", "order_in_band",
        },
        "questions": {
            "question_id", "objective_id", "stem", "choice_a", "choice_b",
            "choice_c", "choice_d", "answer_key",
        },
    }
    for table, expected in required_columns.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing_columns = expected - actual
        if missing_columns:
            raise LaunchContentError(
                f"{table} missing required columns: {sorted(missing_columns)}"
            )


def ensure_support_tables(conn: sqlite3.Connection) -> None:
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS content_release_log (
          package_name TEXT NOT NULL,
          package_version TEXT NOT NULL,
          source_checksum TEXT NOT NULL,
          applied_at INTEGER NOT NULL,
          database_identifier TEXT NOT NULL,
          inserted_count INTEGER NOT NULL,
          updated_count INTEGER NOT NULL,
          unchanged_count INTEGER NOT NULL,
          verification_result TEXT NOT NULL,
          PRIMARY KEY(package_name,package_version)
        )
        """
    )


def row_state(
    conn: sqlite3.Connection,
    table: str,
    id_column: str,
    values: dict,
    comparable_columns: tuple[str, ...],
) -> tuple[str, sqlite3.Row | None]:
    existing = conn.execute(
        f"SELECT * FROM {table} WHERE {id_column}=?", (values[id_column],)
    ).fetchone()
    if existing is None:
        return "inserted", None
    if all(existing[column] == values[column] for column in comparable_columns):
        return "unchanged", existing
    return "updated", existing


def upsert_package(
    conn: sqlite3.Connection,
    package: LaunchPackage,
    *,
    imported_at: int,
    force_failure_after: int | None = None,
) -> dict[str, int]:
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "rejected": 0}
    writes = 0

    def fail_if_requested() -> None:
        nonlocal writes
        writes += 1
        if force_failure_after is not None and writes >= force_failure_after:
            raise LaunchContentError("Forced mid-import failure")

    for values in package.standards.values():
        columns = ("standard_name", "core_idea", "grade_band")
        state, _ = row_state(conn, "standards", "standard_id", values, columns)
        conn.execute(
            """
            INSERT INTO standards(standard_id,standard_name,core_idea,grade_band)
            VALUES (:standard_id,:standard_name,:core_idea,:grade_band)
            ON CONFLICT(standard_id) DO UPDATE SET
              standard_name=excluded.standard_name,
              core_idea=excluded.core_idea,
              grade_band=excluded.grade_band
            """,
            values,
        )
        counts[state] += 1
        fail_if_requested()

    for values in package.objectives.values():
        existing = conn.execute(
            "SELECT standard_id FROM objectives WHERE objective_id=?",
            (values["objective_id"],),
        ).fetchone()
        if existing and existing["standard_id"] != values["standard_id"]:
            counts["rejected"] += 1
            raise LaunchContentError(
                f"Objective conflict: {values['objective_id']} belongs to "
                f"{existing['standard_id']}"
            )
        columns = (
            "standard_id", "objective_text", "display_name",
            "student_description", "order_in_band",
        )
        state, _ = row_state(conn, "objectives", "objective_id", values, columns)
        conn.execute(
            """
            INSERT INTO objectives
              (objective_id,standard_id,objective_text,display_name,
               student_description,order_in_band)
            VALUES
              (:objective_id,:standard_id,:objective_text,:display_name,
               :student_description,:order_in_band)
            ON CONFLICT(objective_id) DO UPDATE SET
              standard_id=excluded.standard_id,
              objective_text=excluded.objective_text,
              display_name=excluded.display_name,
              student_description=excluded.student_description,
              order_in_band=excluded.order_in_band
            """,
            values,
        )
        counts[state] += 1
        fail_if_requested()

    question_columns = (
        "objective_id", "stem", "choice_a", "choice_b", "choice_c",
        "choice_d", "answer_key",
    )
    for values in package.questions.values():
        existing = conn.execute(
            "SELECT objective_id FROM questions WHERE question_id=?",
            (values["question_id"],),
        ).fetchone()
        if existing and existing["objective_id"] != values["objective_id"]:
            counts["rejected"] += 1
            raise LaunchContentError(
                f"Question conflict: {values['question_id']} belongs to "
                f"{existing['objective_id']}"
            )
        state, _ = row_state(
            conn, "questions", "question_id", values, question_columns
        )
        conn.execute(
            """
            INSERT INTO questions
              (question_id,objective_id,stem,choice_a,choice_b,choice_c,
               choice_d,answer_key)
            VALUES
              (:question_id,:objective_id,:stem,:choice_a,:choice_b,:choice_c,
               :choice_d,:answer_key)
            ON CONFLICT(question_id) DO UPDATE SET
              objective_id=excluded.objective_id,
              stem=excluded.stem,
              choice_a=excluded.choice_a,
              choice_b=excluded.choice_b,
              choice_c=excluded.choice_c,
              choice_d=excluded.choice_d,
              answer_key=excluded.answer_key
            """,
            values,
        )
        counts[state] += 1
        fail_if_requested()

    metadata_columns = (
        "difficulty", "question_type", "explanation", "identifier_tags",
        "remediation_tags", "model_id", "source_artifact",
    )
    for values in package.metadata.values():
        state, _ = row_state(
            conn, "question_metadata", "question_id", values, metadata_columns
        )
        conn.execute(
            """
            INSERT INTO question_metadata
              (question_id,difficulty,question_type,explanation,identifier_tags,
               remediation_tags,model_id,source_artifact,imported_at)
            VALUES
              (:question_id,:difficulty,:question_type,:explanation,
               :identifier_tags,:remediation_tags,:model_id,:source_artifact,
               :imported_at)
            ON CONFLICT(question_id) DO UPDATE SET
              difficulty=excluded.difficulty,
              question_type=excluded.question_type,
              explanation=excluded.explanation,
              identifier_tags=excluded.identifier_tags,
              remediation_tags=excluded.remediation_tags,
              model_id=excluded.model_id,
              source_artifact=excluded.source_artifact
            """,
            {**values, "imported_at": imported_at},
        )
        counts[state] += 1
        fail_if_requested()
    return counts


def verify_content(conn: sqlite3.Connection, package: LaunchPackage) -> dict:
    require_schema(conn)
    if not table_exists(conn, "question_metadata"):
        raise LaunchContentError("question_metadata table is missing")
    for table, id_column, expected in (
        ("standards", "standard_id", package.standards),
        ("objectives", "objective_id", package.objectives),
        ("questions", "question_id", package.questions),
        ("question_metadata", "question_id", package.metadata),
    ):
        placeholders = ",".join("?" for _ in expected)
        found = {
            row[0] for row in conn.execute(
                f"SELECT {id_column} FROM {table} "
                f"WHERE {id_column} IN ({placeholders})",
                tuple(expected),
            )
        }
        missing = set(expected) - found
        if missing:
            raise LaunchContentError(
                f"{table} is missing launch IDs: {sorted(missing)}"
            )
    comparisons = (
        ("standards", "standard_id", package.standards,
         ("standard_name", "core_idea", "grade_band")),
        ("objectives", "objective_id", package.objectives,
         ("standard_id", "objective_text", "display_name",
          "student_description", "order_in_band")),
        ("questions", "question_id", package.questions,
         ("objective_id", "stem", "choice_a", "choice_b", "choice_c",
          "choice_d", "answer_key")),
        ("question_metadata", "question_id", package.metadata,
         ("difficulty", "question_type", "explanation", "identifier_tags",
          "remediation_tags", "model_id", "source_artifact")),
    )
    for table, id_column, expected, columns in comparisons:
        for stable_id, values in expected.items():
            state, _ = row_state(
                conn, table, id_column, values, tuple(columns)
            )
            if state != "unchanged":
                raise LaunchContentError(
                    f"{table} content mismatch for {stable_id}"
                )
    for question_id, values in package.questions.items():
        row = conn.execute(
            "SELECT objective_id FROM questions WHERE question_id=?", (question_id,)
        ).fetchone()
        if row["objective_id"] != values["objective_id"]:
            raise LaunchContentError(f"Question relationship mismatch: {question_id}")
    for objective_id, values in package.objectives.items():
        row = conn.execute(
            "SELECT standard_id FROM objectives WHERE objective_id=?", (objective_id,)
        ).fetchone()
        if row["standard_id"] != values["standard_id"]:
            raise LaunchContentError(f"Objective relationship mismatch: {objective_id}")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise LaunchContentError(
            f"Foreign-key check found {len(violations)} unresolved violation(s)"
        )
    standard_placeholders = ",".join("?" for _ in LAUNCH_STANDARD_IDS)
    objective_placeholders = ",".join("?" for _ in LAUNCH_OBJECTIVE_IDS)
    catalog_rows = conn.execute(
        f"""
        SELECT s.standard_id,COUNT(DISTINCT q.question_id)
        FROM standards s
        JOIN objectives o ON o.standard_id=s.standard_id
          AND o.objective_id IN ({objective_placeholders})
        JOIN questions q ON q.objective_id=o.objective_id
        WHERE s.standard_id IN ({standard_placeholders})
        GROUP BY s.standard_id
        """,
        (*LAUNCH_OBJECTIVE_IDS, *LAUNCH_STANDARD_IDS),
    ).fetchall()
    preview_count = conn.execute(
        f"""
        SELECT COUNT(*) FROM objectives o JOIN standards s
          ON s.standard_id=o.standard_id
        WHERE o.objective_id IN ({objective_placeholders})
        """,
        LAUNCH_OBJECTIVE_IDS,
    ).fetchone()[0]
    if len(catalog_rows) != 3:
        raise LaunchContentError("Assignment catalog would not expose 3 standards")
    if preview_count != 7:
        raise LaunchContentError("Question Preview would not expose 7 objectives")
    model_ids = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT model_id FROM question_metadata "
            "WHERE model_id IS NOT NULL AND TRIM(model_id)<>''"
        )
    }
    model_assets_status = (
        "present" if table_exists(conn, "model_assets") else "not required; table absent"
    )
    return {
        "standards": 3,
        "objectives": 7,
        "questions": 147,
        "metadata": 147,
        "assignment_catalog_standards": len(catalog_rows),
        "question_preview_objectives": preview_count,
        "foreign_key_violations": 0,
        "referenced_model_ids": len(model_ids),
        "model_assets": model_assets_status,
    }


def protected_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in PROTECTED_TABLES if table_exists(conn, table)
    }


def run(
    database: str | Path,
    *,
    dry_run: bool = False,
    verify_only: bool = False,
    package_dir: str | Path = DEFAULT_PACKAGE_DIR,
    package_version: str = DEFAULT_PACKAGE_VERSION,
    force_failure_after: int | None = None,
) -> dict:
    if dry_run and verify_only:
        raise LaunchContentError("--dry-run and --verify-only are mutually exclusive")
    db_path = resolve_database(database)
    package = load_package(package_dir)
    uri = f"{db_path.as_uri()}?mode={'ro' if verify_only else 'rw'}"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        require_schema(conn)
        before_protected = protected_counts(conn)
        if verify_only:
            verification = verify_content(conn, package)
            return {
                "mode": "verify-only",
                "database": str(db_path),
                "package_version": package_version,
                "checksum": package.checksum,
                "counts": {"inserted": 0, "updated": 0, "unchanged": 0, "rejected": 0},
                "verification": verification,
            }

        conn.execute("BEGIN IMMEDIATE")
        try:
            ensure_support_tables(conn)
            existing_release = conn.execute(
                """
                SELECT source_checksum FROM content_release_log
                WHERE package_name=? AND package_version=?
                """,
                (PACKAGE_NAME, package_version),
            ).fetchone()
            if existing_release and existing_release["source_checksum"] != package.checksum:
                raise LaunchContentError(
                    "Package version already exists with a different checksum"
                )
            counts = upsert_package(
                conn,
                package,
                imported_at=int(time.time()),
                force_failure_after=force_failure_after,
            )
            verification = verify_content(conn, package)
            if protected_counts(conn) != before_protected:
                raise LaunchContentError("Protected table counts changed")
            if not existing_release:
                conn.execute(
                    """
                    INSERT INTO content_release_log
                      (package_name,package_version,source_checksum,applied_at,
                       database_identifier,inserted_count,updated_count,
                       unchanged_count,verification_result)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        PACKAGE_NAME,
                        package_version,
                        package.checksum,
                        int(time.time()),
                        str(db_path),
                        counts["inserted"],
                        counts["updated"],
                        counts["unchanged"],
                        "ok",
                    ),
                )
            if dry_run:
                conn.rollback()
                mode = "dry-run"
            else:
                conn.commit()
                mode = "import"
            return {
                "mode": mode,
                "database": str(db_path),
                "package_version": package_version,
                "checksum": package.checksum,
                "counts": counts,
                "verification": verification,
                "release_log": "unchanged" if existing_release else "inserted",
            }
        except Exception:
            conn.rollback()
            raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the approved RootED launch content package."
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--package-version", default=DEFAULT_PACKAGE_VERSION)
    parser.add_argument("--package-dir", default=str(DEFAULT_PACKAGE_DIR))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(
            args.database,
            dry_run=args.dry_run,
            verify_only=args.verify_only,
            package_dir=args.package_dir,
            package_version=args.package_version,
        )
    except (LaunchContentError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.dry_run:
        print("DRY RUN — no changes committed")
    elif args.verify_only:
        print("VERIFY ONLY — no changes made")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
