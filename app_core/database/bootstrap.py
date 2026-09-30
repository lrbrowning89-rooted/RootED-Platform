"""Create-new-only bootstrap for an empty synthetic development database."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
import uuid

from app_core.database_safety import (
    DatabaseSafetyError,
    resolve_rooted_environment,
    validate_database_target,
)
from .inspection import table_columns, table_names, unique_column_sets
from .metadata import (
    insert_database_metadata,
    read_database_metadata,
    update_database_content_release,
)
from .schema import (
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    apply_canonical_schema,
    canonical_schema_checksum,
)
from migrations.install_launch_content import (
    CONTENT_RELEASE,
    DEFAULT_PACKAGE_VERSION,
    LAUNCH_OBJECTIVE_IDS,
    LAUNCH_STANDARD_IDS,
    install_launch_content,
    load_package,
    verify_launch_content_release,
)
from migrations.standard_links import (
    seed_mvp_standard_links,
    verify_mvp_standard_links,
)


BOOTSTRAP_VERSION = "1"
CREATED_BY_TOOL = f"rooted-canonical-development-bootstrap/{BOOTSTRAP_VERSION}"
EMPTY_TABLES = (
    "users", "students", "attempts", "responses", "progress_state",
    "routing_level_attempts", "student_objective_state",
    "student_growth_progress", "student_question_deliveries",
    "class_sections", "class_enrollments", "assignments",
    "assignment_recipients", "user_auth_identities", "user_platform_roles",
    "user_instructional_authorizations", "model_assets",
)


class DevelopmentBootstrapError(RuntimeError):
    """Raised when safe development database creation cannot complete."""


def _validated_target(
    database: str | os.PathLike[str], environ: Mapping[str, str]
) -> Path:
    rooted_env = resolve_rooted_environment(environ)
    if rooted_env != "development":
        raise DatabaseSafetyError(
            "development database bootstrap requires ROOTED_ENV=development"
        )
    target = validate_database_target(database, rooted_env=rooted_env)
    configured = environ.get("NGSS_DB")
    if configured is not None:
        configured_target = validate_database_target(configured, rooted_env=rooted_env)
        if configured_target != target:
            raise DatabaseSafetyError(
                "NGSS_DB and --database must resolve to the same target"
            )
    if not target.parent.is_dir():
        raise DevelopmentBootstrapError(
            f"database parent directory must already exist: {target.parent}"
        )
    if target.exists():
        raise DevelopmentBootstrapError(
            f"database target already exists and will not be opened: {target}"
        )
    return target


def _reserve_working_file(target: Path) -> Path:
    for _ in range(20):
        candidate = target.with_name(
            f"{target.name}.bootstrap-{secrets.token_hex(12)}.tmp"
        )
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            continue
        os.close(descriptor)
        return candidate
    raise DevelopmentBootstrapError("could not reserve a unique bootstrap working file")


def verify_development_database(connection: sqlite3.Connection) -> None:
    """Verify the structural, empty, synthetic development database contract."""
    tables = table_names(connection)
    if tables != REQUIRED_TABLES:
        raise DevelopmentBootstrapError(
            f"canonical table manifest mismatch: expected={sorted(REQUIRED_TABLES)}, actual={sorted(tables)}"
        )
    if len(connection.execute("SELECT * FROM rooted_database_metadata").fetchall()) != 1:
        raise DevelopmentBootstrapError("exactly one metadata row is required")
    metadata = read_database_metadata(connection)
    expected = {
        "singleton_id": 1,
        "schema_version": SCHEMA_VERSION,
        "environment": "development",
        "data_classification": "synthetic",
        "canonical_schema_checksum": canonical_schema_checksum(),
        "created_by_tool": CREATED_BY_TOOL,
        "content_release": CONTENT_RELEASE,
    }
    if metadata is None or any(metadata.get(key) != value for key, value in expected.items()):
        raise DevelopmentBootstrapError("database metadata does not match development bootstrap identity")
    if not metadata.get("database_identifier"):
        raise DevelopmentBootstrapError("database identifier is missing")
    question_columns = {row[1] for row in table_columns(connection, "questions")}
    if "reading_level" not in question_columns:
        raise DevelopmentBootstrapError("questions.reading_level is missing")
    required_unique = ("from_standard_id", "to_standard_id", "link_type")
    if required_unique not in unique_column_sets(connection, "standard_links"):
        raise DevelopmentBootstrapError("standard_links canonical uniqueness is missing")
    expected_counts = {
        "standards": len(LAUNCH_STANDARD_IDS),
        "objectives": len(LAUNCH_OBJECTIVE_IDS),
        "questions": 147,
        "question_metadata": 147,
        "content_release_log": 1,
        "standard_links": 4,
    }
    for table, expected_count in expected_counts.items():
        actual_count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if actual_count != expected_count:
            raise DevelopmentBootstrapError(
                f"unexpected {table} count: expected={expected_count}, actual={actual_count}"
            )
    if connection.execute(
        "SELECT COUNT(*) FROM standards WHERE standard_id='4-LS1-1'"
    ).fetchone()[0]:
        raise DevelopmentBootstrapError("4-LS1-1 curriculum must not be fabricated")
    for table in EMPTY_TABLES:
        if connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != 0:
            raise DevelopmentBootstrapError(f"bootstrap table must be empty: {table}")
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise DevelopmentBootstrapError("SQLite quick_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise DevelopmentBootstrapError("SQLite foreign_key_check found violations")


def _finalize_no_overwrite(working: Path, target: Path) -> None:
    if os.name != "nt":
        raise DevelopmentBootstrapError(
            "race-safe no-overwrite finalization is currently supported only on Windows"
        )
    os.rename(working, target)


def create_development_database(
    database: str | os.PathLike[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Create and atomically finalize a new empty synthetic development DB."""
    source_environment = os.environ if environ is None else environ
    target = _validated_target(database, source_environment)
    package = load_package()
    working: Path | None = None
    connection: sqlite3.Connection | None = None
    finalized = False
    original_error: BaseException | None = None
    try:
        working = _reserve_working_file(target)
        connection = sqlite3.connect(str(working))
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise DevelopmentBootstrapError("could not enable SQLite foreign keys")
        apply_canonical_schema(connection)
        insert_database_metadata(
            connection,
            environment="development",
            data_classification="synthetic",
            database_identifier=uuid.uuid4().hex,
            created_at=int(time.time()),
            created_by_tool=CREATED_BY_TOOL,
            canonical_schema_checksum=canonical_schema_checksum(),
        )
        metadata = read_database_metadata(connection)
        content_result = install_launch_content(
            connection,
            package=package,
            package_version=DEFAULT_PACKAGE_VERSION,
            database_identifier=metadata["database_identifier"],
        )
        seed_mvp_standard_links(connection)
        update_database_content_release(connection, content_result["content_release"])
        verify_launch_content_release(
            connection, package, package_version=DEFAULT_PACKAGE_VERSION
        )
        verify_mvp_standard_links(connection)
        verify_development_database(connection)
        connection.commit()
        connection.close()
        connection = None
        if target.exists():
            raise DevelopmentBootstrapError(
                f"database target appeared before finalization and will not be overwritten: {target}"
            )
        _finalize_no_overwrite(working, target)
        finalized = True
        return target
    except BaseException as exc:
        original_error = exc
        raise
    finally:
        cleanup_errors: list[str] = []
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:
                cleanup_errors.append(f"connection close failed: {exc}")
        if not finalized and working is not None and working.exists():
            try:
                working.unlink()
            except Exception as exc:
                cleanup_errors.append(f"working-file removal failed: {exc}")
        if cleanup_errors:
            message = "; ".join(cleanup_errors)
            if original_error is None:
                raise DevelopmentBootstrapError(message)
            original_error.add_note(f"bootstrap cleanup error: {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "create-development-database",
        help="create a new empty synthetic development database",
    )
    create.add_argument("--database", required=True, help="explicit new database path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        target = create_development_database(args.database)
    except (DatabaseSafetyError, DevelopmentBootstrapError, OSError, sqlite3.Error) as exc:
        print(f"development database bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(f"created empty synthetic development database: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
