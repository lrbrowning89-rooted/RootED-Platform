#!/usr/bin/env python3
"""
Read-only Visual Library Manager report for RootED model assets.

The report opens the SQLite database in read-only mode, joins question metadata
to objectives, standards, and model_assets, then checks referenced files under
the Flask static directory. It does not write to the database or static files.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "ngss.db"
DEFAULT_STATIC_ROOT = ROOT / "app_core" / "static"
MODEL_ASSET_STATIC_DIR = "model_assets"
SUPPORTED_EXTENSIONS = {".png", ".svg"}
EXPECTED_MODEL_ASSET_COLUMNS = {
    "model_id",
    "asset_type",
    "src",
    "alt_text",
    "title",
    "caption",
    "created_at",
    "updated_at",
}


@dataclass(frozen=True)
class AssetStatus:
    status: str
    normalized_path: str
    exists: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class QuestionVisualRow:
    question_id: str
    objective_id: str
    standard_id: str
    model_id: str
    title: str
    asset_type: str
    static_file_exists: bool
    alt_text_present: bool
    caption_present: bool

    @property
    def is_complete(self) -> bool:
        return (
            bool(self.model_id)
            and bool(self.title)
            and bool(self.asset_type)
            and self.static_file_exists
            and self.alt_text_present
            and self.caption_present
        )


@dataclass(frozen=True)
class AssetUsageRow:
    model_id: str
    title: str
    asset_type: str
    question_count: int
    static_file_exists: bool
    alt_text_present: bool
    caption_present: bool

    @property
    def is_complete(self) -> bool:
        return (
            bool(self.model_id)
            and bool(self.title)
            and bool(self.asset_type)
            and self.static_file_exists
            and self.alt_text_present
            and self.caption_present
        )


@dataclass(frozen=True)
class CoverageRow:
    group_id: str
    total_questions: int
    questions_with_model: int
    complete_visual_questions: int

    @property
    def coverage_percent(self) -> float:
        if self.total_questions == 0:
            return 0.0
        return (self.complete_visual_questions / self.total_questions) * 100


@dataclass(frozen=True)
class VisualLibraryReport:
    db_path: Path
    static_root: Path
    question_rows: tuple[QuestionVisualRow, ...]
    asset_usage_rows: tuple[AssetUsageRow, ...]
    orphaned_model_assets: tuple[str, ...]
    orphaned_static_files: tuple[str, ...]
    coverage_by_objective: tuple[CoverageRow, ...]
    coverage_by_standard: tuple[CoverageRow, ...]
    completion_percent: float
    warnings: tuple[str, ...]


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    db_path = db_path.resolve()
    uri = f"file:{quote(str(db_path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def validate_required_tables(conn: sqlite3.Connection) -> list[str]:
    warnings: list[str] = []
    required_tables = {
        "questions",
        "objectives",
        "standards",
        "question_metadata",
        "model_assets",
    }
    for table_name in sorted(required_tables):
        if not table_exists(conn, table_name):
            warnings.append(f"missing required table: {table_name}")
    return warnings


def normalize_static_filename(src: str | None) -> str:
    src = (src or "").strip().replace("\\", "/")
    if not src or src.startswith(("http://", "https://", "//")):
        return ""
    if src.startswith("/static/"):
        src = src[len("/static/") :]
    elif src.startswith("static/"):
        src = src[len("static/") :]
    elif src.startswith("app_core/static/"):
        src = src[len("app_core/static/") :]
    else:
        src = src.lstrip("/")

    normalized = os.path.normpath(src).replace("\\", "/")
    if normalized == "." or normalized == ".." or normalized.startswith("../"):
        return ""
    return normalized


def inspect_static_file(src: str | None, static_root: Path) -> AssetStatus:
    warnings: list[str] = []
    if not src:
        return AssetStatus("missing-src", "", False, tuple(warnings))

    stripped = src.strip()
    if stripped.startswith(("http://", "https://", "//")):
        warnings.append("remote src is not supported by the current image MVP")
        return AssetStatus("unsupported-remote", "", False, tuple(warnings))

    normalized_path = normalize_static_filename(stripped)
    if not normalized_path:
        warnings.append("src is not a safe static path")
        return AssetStatus("invalid-path", "", False, tuple(warnings))

    extension = Path(normalized_path).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        warnings.append(f"unsupported extension: {extension or '(none)'}")
        return AssetStatus("unsupported-extension", normalized_path, False, tuple(warnings))

    static_root = static_root.resolve()
    candidate = (static_root / normalized_path).resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError:
        warnings.append("src resolves outside static root")
        return AssetStatus("invalid-path", normalized_path, False, tuple(warnings))

    if not candidate.is_file():
        warnings.append("static file missing")
        return AssetStatus("missing", normalized_path, False, tuple(warnings))

    return AssetStatus("exists", normalized_path, True, tuple(warnings))


def yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def percent(part: int, whole: int) -> float:
    if whole == 0:
        return 0.0
    return (part / whole) * 100


def get_assets(conn: sqlite3.Connection) -> tuple[dict[str, sqlite3.Row], list[str]]:
    warnings: list[str] = []
    if not table_exists(conn, "model_assets"):
        return {}, ["missing model_assets table"]

    columns = table_columns(conn, "model_assets")
    missing_expected = sorted(EXPECTED_MODEL_ASSET_COLUMNS - columns)
    if missing_expected:
        warnings.append(
            "model_assets missing approved column(s): "
            + ", ".join(missing_expected)
        )

    rows = conn.execute(
        """
        SELECT model_id,
               asset_type,
               src,
               alt_text,
               title,
               caption
        FROM model_assets
        WHERE model_id IS NOT NULL
          AND TRIM(model_id) <> ''
        ORDER BY model_id
        """
    ).fetchall()
    return {row["model_id"].strip(): row for row in rows}, warnings


def get_question_visual_rows(
    conn: sqlite3.Connection,
    assets_by_model_id: dict[str, sqlite3.Row],
    static_root: Path,
) -> tuple[list[QuestionVisualRow], list[str]]:
    warnings: list[str] = []
    if not table_exists(conn, "question_metadata"):
        return [], ["missing question_metadata table"]

    rows = conn.execute(
        """
        SELECT qm.question_id,
               q.objective_id,
               COALESCE(o.standard_id, '') AS standard_id,
               TRIM(qm.model_id) AS model_id
        FROM question_metadata qm
        LEFT JOIN questions q
          ON q.question_id = qm.question_id
        LEFT JOIN objectives o
          ON o.objective_id = q.objective_id
        WHERE qm.model_id IS NOT NULL
          AND TRIM(qm.model_id) <> ''
        ORDER BY COALESCE(o.standard_id, ''),
                 COALESCE(q.objective_id, ''),
                 q.question_id
        """
    ).fetchall()

    question_rows: list[QuestionVisualRow] = []
    for row in rows:
        model_id = row["model_id"]
        asset = assets_by_model_id.get(model_id)
        status = inspect_static_file(asset["src"] if asset else None, static_root)
        warnings.extend(f"{model_id}: {warning}" for warning in status.warnings)

        question_rows.append(
            QuestionVisualRow(
                question_id=row["question_id"] or "",
                objective_id=row["objective_id"] or "",
                standard_id=row["standard_id"] or "",
                model_id=model_id,
                title=(asset["title"] if asset else "") or "",
                asset_type=(asset["asset_type"] if asset else "") or "",
                static_file_exists=status.exists,
                alt_text_present=bool(((asset["alt_text"] if asset else "") or "").strip()),
                caption_present=bool(((asset["caption"] if asset else "") or "").strip()),
            )
        )

        if not asset:
            warnings.append(f"{model_id}: missing model_assets row")

    return question_rows, warnings


def get_asset_usage_rows(
    assets_by_model_id: dict[str, sqlite3.Row],
    model_usage: Counter[str],
    static_root: Path,
) -> tuple[list[AssetUsageRow], list[str]]:
    warnings: list[str] = []
    rows: list[AssetUsageRow] = []
    for model_id in sorted(model_usage):
        asset = assets_by_model_id.get(model_id)
        status = inspect_static_file(asset["src"] if asset else None, static_root)
        warnings.extend(f"{model_id}: {warning}" for warning in status.warnings)
        rows.append(
            AssetUsageRow(
                model_id=model_id,
                title=(asset["title"] if asset else "") or "",
                asset_type=(asset["asset_type"] if asset else "") or "",
                question_count=model_usage[model_id],
                static_file_exists=status.exists,
                alt_text_present=bool(((asset["alt_text"] if asset else "") or "").strip()),
                caption_present=bool(((asset["caption"] if asset else "") or "").strip()),
            )
        )
    return rows, warnings


def get_orphaned_model_assets(
    assets_by_model_id: dict[str, sqlite3.Row],
    model_usage: Counter[str],
) -> tuple[str, ...]:
    return tuple(
        model_id
        for model_id in sorted(assets_by_model_id)
        if model_usage[model_id] == 0
    )


def get_referenced_static_paths(
    assets_by_model_id: dict[str, sqlite3.Row],
) -> set[str]:
    referenced: set[str] = set()
    for asset in assets_by_model_id.values():
        normalized = normalize_static_filename(asset["src"])
        if normalized:
            referenced.add(normalized)
    return referenced


def get_orphaned_static_files(
    static_root: Path,
    referenced_static_paths: set[str],
) -> tuple[str, ...]:
    asset_dir = static_root / MODEL_ASSET_STATIC_DIR
    if not asset_dir.is_dir():
        return tuple()

    orphaned: list[str] = []
    for file_path in sorted(asset_dir.rglob("*")):
        if not file_path.is_file():
            continue
        relative_path = file_path.relative_to(static_root).as_posix()
        if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if relative_path not in referenced_static_paths:
            orphaned.append(relative_path)
    return tuple(orphaned)


def get_coverage_rows(
    conn: sqlite3.Connection,
    question_rows: list[QuestionVisualRow],
    group_column: str,
) -> tuple[CoverageRow, ...]:
    total_rows = conn.execute(
        f"""
        SELECT COALESCE(o.{group_column}, '') AS group_id,
               COUNT(q.question_id) AS total_questions
        FROM questions q
        LEFT JOIN objectives o
          ON o.objective_id = q.objective_id
        GROUP BY COALESCE(o.{group_column}, '')
        ORDER BY COALESCE(o.{group_column}, '')
        """
    ).fetchall()

    model_counts: dict[str, int] = defaultdict(int)
    complete_counts: dict[str, int] = defaultdict(int)
    for row in question_rows:
        group_id = row.objective_id if group_column == "objective_id" else row.standard_id
        model_counts[group_id] += 1
        if row.is_complete:
            complete_counts[group_id] += 1

    return tuple(
        CoverageRow(
            group_id=row["group_id"] or "(unmapped)",
            total_questions=int(row["total_questions"] or 0),
            questions_with_model=model_counts[row["group_id"] or ""],
            complete_visual_questions=complete_counts[row["group_id"] or ""],
        )
        for row in total_rows
    )


def build_report(db_path: Path, static_root: Path) -> VisualLibraryReport:
    warnings: list[str] = []
    with connect_readonly(db_path) as conn:
        warnings.extend(validate_required_tables(conn))
        assets_by_model_id, asset_warnings = get_assets(conn)
        warnings.extend(asset_warnings)
        question_rows, question_warnings = get_question_visual_rows(
            conn,
            assets_by_model_id,
            static_root,
        )
        warnings.extend(question_warnings)
        model_usage = Counter(row.model_id for row in question_rows)
        asset_usage_rows, usage_warnings = get_asset_usage_rows(
            assets_by_model_id,
            model_usage,
            static_root,
        )
        warnings.extend(usage_warnings)
        coverage_by_objective = get_coverage_rows(
            conn,
            question_rows,
            "objective_id",
        )
        coverage_by_standard = get_coverage_rows(
            conn,
            question_rows,
            "standard_id",
        )

    completion_count = sum(1 for row in question_rows if row.is_complete)
    completion_percent = percent(completion_count, len(question_rows))
    orphaned_model_assets = get_orphaned_model_assets(assets_by_model_id, model_usage)
    orphaned_static_files = get_orphaned_static_files(
        static_root,
        get_referenced_static_paths(assets_by_model_id),
    )

    return VisualLibraryReport(
        db_path=db_path,
        static_root=static_root,
        question_rows=tuple(question_rows),
        asset_usage_rows=tuple(asset_usage_rows),
        orphaned_model_assets=orphaned_model_assets,
        orphaned_static_files=orphaned_static_files,
        coverage_by_objective=coverage_by_objective,
        coverage_by_standard=coverage_by_standard,
        completion_percent=completion_percent,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) if rows else len(header)
        for index, header in enumerate(headers)
    ]
    print(" | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def print_report(report: VisualLibraryReport) -> None:
    print("RootED Visual Library Manager Report")
    print("=" * 36)
    print(f"Database: {report.db_path}")
    print(f"Static root: {report.static_root}")
    print("Mode: read-only")
    print()

    total_referenced_questions = len(report.question_rows)
    complete_questions = sum(1 for row in report.question_rows if row.is_complete)
    print("Summary")
    print("-------")
    print(f"Questions referencing model_id: {total_referenced_questions}")
    print(f"Distinct referenced models: {len(report.asset_usage_rows)}")
    print(f"Complete referenced question visuals: {complete_questions}")
    print(f"Overall production visual completion: {report.completion_percent:.1f}%")
    print(f"Orphaned model_assets rows: {len(report.orphaned_model_assets)}")
    print(f"Orphaned static files: {len(report.orphaned_static_files)}")
    print()

    print("Question Visual Inventory")
    print("-------------------------")
    print_table(
        [
            "Standard",
            "Objective",
            "Question ID",
            "Model ID",
            "Model Asset Title",
            "Asset Type",
            "Static File Exists",
            "Alt Text Present",
            "Caption Present",
        ],
        [
            [
                row.standard_id or "(unmapped)",
                row.objective_id or "(unmapped)",
                row.question_id,
                row.model_id,
                row.title or "(missing)",
                row.asset_type or "(missing)",
                yes_no(row.static_file_exists),
                yes_no(row.alt_text_present),
                yes_no(row.caption_present),
            ]
            for row in report.question_rows
        ],
    )
    print()

    print("Model Usage")
    print("-----------")
    print_table(
        [
            "Model ID",
            "Model Asset Title",
            "Asset Type",
            "Questions Using Model",
            "Static File Exists",
            "Alt Text Present",
            "Caption Present",
        ],
        [
            [
                row.model_id,
                row.title or "(missing)",
                row.asset_type or "(missing)",
                str(row.question_count),
                yes_no(row.static_file_exists),
                yes_no(row.alt_text_present),
                yes_no(row.caption_present),
            ]
            for row in report.asset_usage_rows
        ],
    )
    print()

    print("Orphaned model_assets Rows")
    print("--------------------------")
    if report.orphaned_model_assets:
        for model_id in report.orphaned_model_assets:
            print(f"- {model_id}")
    else:
        print("None")
    print()

    print("Orphaned Static Files")
    print("---------------------")
    if report.orphaned_static_files:
        for static_file in report.orphaned_static_files:
            print(f"- {static_file}")
    else:
        print("None")
    print()

    print("Coverage by Objective")
    print("---------------------")
    print_table(
        [
            "Objective",
            "Total Questions",
            "Questions With Model",
            "Complete Visual Questions",
            "Completion",
        ],
        [
            [
                row.group_id,
                str(row.total_questions),
                str(row.questions_with_model),
                str(row.complete_visual_questions),
                f"{row.coverage_percent:.1f}%",
            ]
            for row in report.coverage_by_objective
        ],
    )
    print()

    print("Coverage by Standard")
    print("--------------------")
    print_table(
        [
            "Standard",
            "Total Questions",
            "Questions With Model",
            "Complete Visual Questions",
            "Completion",
        ],
        [
            [
                row.group_id,
                str(row.total_questions),
                str(row.questions_with_model),
                str(row.complete_visual_questions),
                f"{row.coverage_percent:.1f}%",
            ]
            for row in report.coverage_by_standard
        ],
    )
    print()

    print("Warnings")
    print("--------")
    if report.warnings:
        for warning in report.warnings:
            print(f"- {warning}")
    else:
        print("None")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a read-only RootED Visual Library Manager report."
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help=f"Path to SQLite database. Default: {DEFAULT_DB}",
    )
    parser.add_argument(
        "--static-root",
        default=str(DEFAULT_STATIC_ROOT),
        help=f"Path to Flask static root. Default: {DEFAULT_STATIC_ROOT}",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    static_root = Path(args.static_root)
    if not db_path.exists():
        print(f"ERROR: database file not found: {db_path}")
        return 2
    if not static_root.exists():
        print(f"ERROR: static root not found: {static_root}")
        return 2

    report = build_report(db_path, static_root)
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
