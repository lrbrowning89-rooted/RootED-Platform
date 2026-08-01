#!/usr/bin/env python3
"""Combined read-only audit of RootED question model assets.

The audit deliberately treats the database catalog, filesystem artwork, and
Git working tree as independent evidence. It never writes to any of them.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "ngss.db"
DEFAULT_STATIC_ROOT = ROOT / "app_core" / "static"
DEFAULT_LIBRARY_ROOT = ROOT / "visual_library" / "model_assets"
SUPPORTED_EXTENSIONS = {".png", ".svg"}
EXPECTED_MODEL_ASSET_COLUMNS = {
    "model_id", "asset_type", "src", "alt_text", "title", "caption",
    "created_at", "updated_at",
}


@dataclass(frozen=True)
class FileEvidence:
    runtime: Path | None
    approved: Path | None
    export: Path | None
    alternatives: tuple[Path, ...]

    @property
    def known_paths(self) -> tuple[Path, ...]:
        return tuple(path for path in (self.approved, self.export, self.runtime) if path)


@dataclass(frozen=True)
class AuditRow:
    model_id: str
    question_ids: tuple[str, ...]
    catalog_present: bool
    catalog_src: str
    artwork_status: str
    path_status: str
    git_status: str
    hash_status: str
    git_details: tuple[tuple[Path, str], ...]
    hash_details: tuple[tuple[Path, str], ...]
    files: FileEvidence
    warnings: tuple[str, ...]


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(db_path.resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone() is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def normalize_static_filename(src: str | None) -> str | None:
    value = (src or "").strip().replace("\\", "/")
    if not value or value.startswith(("http://", "https://", "//")):
        return None
    for prefix in ("/static/", "static/", "app_core/static/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.lstrip("/")
    normalized = os.path.normpath(value).replace("\\", "/")
    if normalized in {".", ".."} or normalized.startswith("../"):
        return None
    return normalized


def safe_runtime_path(src: str | None, static_root: Path) -> Path | None:
    normalized = normalize_static_filename(src)
    if not normalized:
        return None
    candidate = (static_root.resolve() / normalized).resolve()
    try:
        candidate.relative_to(static_root.resolve())
    except ValueError:
        return None
    return candidate


def inspect_static_file(src: str | None, static_root: Path) -> tuple[str, list[str]]:
    """Compatibility helper retained for callers of the original audit."""
    candidate = safe_runtime_path(src, static_root)
    if candidate is None:
        return "invalid-path" if src else "n/a", ["src is not a safe static path"] if src else []
    if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return "unsupported-extension", [f"unsupported extension: {candidate.suffix or '(none)'}"]
    if not candidate.is_file():
        return "missing", ["static file missing"]
    return "exists", []


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_same_stem(directory: Path, stem: str) -> tuple[Path, ...]:
    if not directory.is_dir():
        return tuple()
    return tuple(sorted(
        path.resolve() for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS and path.stem == stem
    ))


def find_library_copy(library_root: Path, stage: str, filename: str) -> Path | None:
    if not library_root.is_dir():
        return None
    matches = sorted(
        path.resolve() for path in library_root.glob(f"**/{stage}/{filename}") if path.is_file()
    )
    return matches[0] if matches else None


def discover_files(src: str, static_root: Path, library_root: Path) -> FileEvidence:
    expected = safe_runtime_path(src, static_root)
    filename = expected.name if expected else Path(src).name
    stem = Path(filename).stem
    runtime_dir = expected.parent if expected else static_root / "model_assets"
    runtime_matches = find_same_stem(runtime_dir, stem)
    exact_runtime = expected if expected and expected.is_file() else None
    runtime = exact_runtime or (runtime_matches[0] if runtime_matches else None)

    approved = find_library_copy(library_root, "05_approved", filename)
    export = find_library_copy(library_root, "06_exports", filename)
    alternatives: set[Path] = set(runtime_matches)
    if not approved or not export:
        for stage, current in (("05_approved", approved), ("06_exports", export)):
            if current:
                continue
            stage_matches = sorted(
                path.resolve() for path in library_root.glob(f"**/{stage}/{stem}.*")
                if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
            )
            if stage_matches:
                if stage == "05_approved":
                    approved = stage_matches[0]
                else:
                    export = stage_matches[0]
                alternatives.update(stage_matches)
    alternatives.update(path for path in (approved, export, runtime) if path)
    return FileEvidence(runtime, approved, export, tuple(sorted(alternatives)))


def git_file_statuses(repo_root: Path, paths: tuple[Path, ...]) -> dict[Path, str]:
    existing = tuple(dict.fromkeys(path.resolve() for path in paths if path.is_file()))
    if not existing:
        return {}
    relative = [path.relative_to(repo_root.resolve()).as_posix() for path in existing]
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", *relative],
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    changed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        code, name = line[:2], line[3:]
        changed[name.replace("\\", "/")] = "untracked" if code == "??" else "modified"
    statuses: dict[Path, str] = {}
    for path, rel in zip(existing, relative):
        statuses[path] = changed.get(rel, "tracked-clean")
    return statuses


def summarize_git(statuses: dict[Path, str]) -> str:
    values = set(statuses.values())
    if not values:
        return "no-files"
    if "untracked" in values:
        return "not-ready:untracked"
    if "modified" in values:
        return "not-ready:modified"
    return "ready:tracked-clean"


def compare_hashes(paths: tuple[Path, ...]) -> str:
    existing = tuple(dict.fromkeys(path for path in paths if path.is_file()))
    if len(existing) < 2:
        return "not-comparable"
    return "match" if len({sha256(path) for path in existing}) == 1 else "mismatch"


def get_question_links(conn: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    if not table_exists(conn, "question_metadata"):
        return {}
    rows = conn.execute(
        """SELECT TRIM(model_id) AS model_id, question_id
           FROM question_metadata
           WHERE model_id IS NOT NULL AND TRIM(model_id) <> ''
           ORDER BY model_id, question_id"""
    ).fetchall()
    links: dict[str, list[str]] = {}
    for row in rows:
        links.setdefault(row["model_id"], []).append(row["question_id"])
    return {model_id: tuple(ids) for model_id, ids in links.items()}


def get_catalog(conn: sqlite3.Connection) -> tuple[dict[str, dict[str, str]], list[str]]:
    if not table_exists(conn, "model_assets"):
        return {}, ["missing model_assets table"]
    columns = table_columns(conn, "model_assets")
    warnings = []
    missing = sorted(EXPECTED_MODEL_ASSET_COLUMNS - columns)
    if missing:
        warnings.append("model_assets missing approved columns: " + ", ".join(missing))
    if not {"model_id", "src"}.issubset(columns):
        return {}, warnings + ["model_assets missing model_id or src"]
    rows = conn.execute("SELECT model_id, src FROM model_assets ORDER BY model_id").fetchall()
    return {
        (row["model_id"] or "").strip(): {"src": (row["src"] or "").strip()}
        for row in rows if (row["model_id"] or "").strip()
    }, warnings


def build_audit(db_path: Path, static_root: Path, library_root: Path, repo_root: Path = ROOT) -> tuple[list[AuditRow], list[str]]:
    with connect_readonly(db_path) as conn:
        links = get_question_links(conn)
        catalog, warnings = get_catalog(conn)
    model_ids = sorted(set(links) | set(catalog))
    rows: list[AuditRow] = []
    for model_id in model_ids:
        asset = catalog.get(model_id)
        src = asset["src"] if asset else ""
        files = discover_files(src, static_root, library_root) if src else FileEvidence(None, None, None, tuple())
        expected = safe_runtime_path(src, static_root) if src else None
        exact = bool(expected and expected.is_file())
        artwork = "exists" if files.known_paths else "missing"
        if exact:
            path_status = "agrees"
        elif files.runtime:
            path_status = f"mismatch:catalog-{expected.suffix.lower() or 'none'}/file-{files.runtime.suffix.lower()}"
        elif files.approved or files.export:
            path_status = "runtime-missing:library-copy-exists"
        else:
            path_status = "missing"
        git_statuses = git_file_statuses(repo_root, files.known_paths)
        hash_details = tuple((path, sha256(path)) for path in files.known_paths if path.is_file())
        hash_status = compare_hashes(files.known_paths)
        row_warnings = []
        if path_status.startswith("mismatch:"):
            row_warnings.append("artwork exists under the same basename with a different extension")
        if hash_status == "mismatch":
            row_warnings.append("approved/export/runtime copies differ by SHA-256")
        rows.append(AuditRow(
            model_id=model_id,
            question_ids=links.get(model_id, tuple()),
            catalog_present=asset is not None,
            catalog_src=src,
            artwork_status=artwork,
            path_status=path_status,
            git_status=summarize_git(git_statuses),
            hash_status=hash_status,
            git_details=tuple(git_statuses.items()),
            hash_details=hash_details,
            files=files,
            warnings=tuple(row_warnings),
        ))
    return rows, warnings


def relative_display(path: Path | None, repo_root: Path = ROOT) -> str:
    if not path:
        return "n/a"
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def print_report(db_path: Path, static_root: Path, library_root: Path = DEFAULT_LIBRARY_ROOT) -> int:
    print("RootED Combined Model Asset Audit")
    print("=================================")
    print(f"Database: {db_path}")
    print(f"Static root: {static_root}")
    print(f"Visual library: {library_root}")
    print("Mode: read-only")
    print()
    if not db_path.exists():
        print(f"ERROR: database file not found: {db_path}")
        return 2
    rows, warnings = build_audit(db_path, static_root, library_root)
    print("Summary")
    print("-------")
    print(f"Models audited: {len(rows)}")
    print(f"Artwork exists: {sum(row.artwork_status == 'exists' for row in rows)}")
    print(f"Catalog integrated: {sum(row.catalog_present for row in rows)}")
    print(f"Question linked: {sum(bool(row.question_ids) for row in rows)}")
    print(f"Path agrees: {sum(row.path_status == 'agrees' for row in rows)}")
    print(f"Path mismatch: {sum(row.path_status.startswith('mismatch:') for row in rows)}")
    print(f"Git ready: {sum(row.git_status == 'ready:tracked-clean' for row in rows)}")
    print(f"Hash match: {sum(row.hash_status == 'match' for row in rows)}")
    print(f"Hash mismatch: {sum(row.hash_status == 'mismatch' for row in rows)}")
    print()
    print("Inventory")
    print("---------")
    for row in rows:
        print(f"- model_id: {row.model_id}")
        print(f"  artwork_existence: {row.artwork_status}")
        print(f"  catalog_integration: {'present' if row.catalog_present else 'missing'}")
        print(f"  question_linkage: {'linked (' + str(len(row.question_ids)) + ')' if row.question_ids else 'unlinked'}")
        print(f"  question_ids: {', '.join(row.question_ids) or 'n/a'}")
        print(f"  path_agreement: {row.path_status}")
        print(f"  catalog_src: {row.catalog_src or 'n/a'}")
        print(f"  runtime: {relative_display(row.files.runtime)}")
        print(f"  approved: {relative_display(row.files.approved)}")
        print(f"  export: {relative_display(row.files.export)}")
        print(f"  git_readiness: {row.git_status}")
        for path, status in row.git_details:
            print(f"  git_file: {relative_display(path)} = {status}")
        print(f"  copy_hashes: {row.hash_status}")
        for path, digest in row.hash_details:
            print(f"  sha256: {relative_display(path)} = {digest}")
        for warning in row.warnings:
            print(f"  WARN: {warning}")
        print()
    if warnings:
        print("Schema warnings")
        print("---------------")
        for warning in warnings:
            print(f"- {warning}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--static-root", default=str(DEFAULT_STATIC_ROOT))
    parser.add_argument("--library-root", default=str(DEFAULT_LIBRARY_ROOT))
    args = parser.parse_args()
    return print_report(Path(args.db), Path(args.static_root), Path(args.library_root))


if __name__ == "__main__":
    raise SystemExit(main())
