"""Safely apply RootED's canonical schema to an open SQLite connection."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sqlite3

SCHEMA_VERSION = 1

REQUIRED_TABLES = frozenset({
    "access_authorization_audit_log", "account_lifecycle_audit_log",
    "assignment_recipients", "assignments", "attempts",
    "authorization_lifecycle_audit_log", "class_enrollments",
    "class_membership_audit_log", "class_sections", "config",
    "content_release_log", "diagnostic_items", "diagnostic_responses",
    "diagnostic_sessions", "error_logs", "frustration_events",
    "impersonated_action_audit", "impersonation_audit", "lists",
    "model_assets", "notifications", "objective_levels", "objectives",
    "origin_links", "paths", "progress_state", "progression_links",
    "progression_rules", "question_flag_events", "question_flags",
    "question_metadata", "question_skills", "questions", "remediation_links",
    "responses", "rooted_database_metadata", "routing_level_attempts", "skills",
    "standard_links", "standards", "student_growth_progress",
    "student_objective_state", "student_question_deliveries", "students",
    "teacher_email_authorizations", "user_auth_identities",
    "user_instructional_authorizations", "user_platform_roles", "users",
})

_SCHEMA_PATH = Path(__file__).with_name("canonical_schema.sql")
_CREATE_OBJECT = re.compile(
    r"^CREATE\s+(?:(UNIQUE)\s+)?(TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\s+([^\s(]+)",
    re.IGNORECASE,
)


class SchemaApplicationError(RuntimeError):
    """Base error for canonical schema application failures."""


class SchemaCompatibilityError(SchemaApplicationError):
    """Raised when a nonblank database is not exactly canonical."""


def canonical_schema_sql() -> str:
    """Return the authoritative SQL source without opening a database."""
    return _SCHEMA_PATH.read_text(encoding="utf-8")


def canonical_schema_checksum() -> str:
    """Return the SHA-256 checksum of the authoritative UTF-8 SQL source."""
    return hashlib.sha256(canonical_schema_sql().encode("utf-8")).hexdigest()


def _statements(source: str) -> tuple[str, ...]:
    statements: list[str] = []
    pending = ""
    for line in source.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                statements.append(statement)
            pending = ""
    if pending.strip():
        raise SchemaApplicationError("canonical schema contains an incomplete statement")
    return tuple(statements)


def _normalized_sql(sql: str) -> str:
    normalized = re.sub(
        r"\bIF\s+NOT\s+EXISTS\b", "", sql.strip().rstrip(";"),
        flags=re.IGNORECASE,
    )
    return " ".join(normalized.split()).lower()


def _expected_objects() -> dict[tuple[str, str], str]:
    expected: dict[tuple[str, str], str] = {}
    for statement in _statements(canonical_schema_sql()):
        match = _CREATE_OBJECT.match(statement)
        if match:
            object_type = match.group(2).lower()
            name = match.group(3).strip('"`[]')
            expected[(object_type, name)] = _normalized_sql(statement)
    return expected


def _actual_objects(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {
        (object_type, name): _normalized_sql(sql)
        for object_type, name, sql in connection.execute(
            """SELECT type, name, sql FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%'
                 AND type IN ('table','index','view','trigger')"""
        )
        if sql is not None
    }


def _assert_foreign_keys_enabled(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SchemaApplicationError(
            "foreign key enforcement must be enabled by the caller before schema application"
        )


def _assert_blank_or_canonical(connection: sqlite3.Connection) -> None:
    actual = _actual_objects(connection)
    if not actual:
        return
    expected = _expected_objects()
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        mismatched = sorted(
            key for key in set(actual) & set(expected)
            if actual[key] != expected[key]
        )
        raise SchemaCompatibilityError(
            "nonblank database is not the canonical schema "
            f"(missing={missing}, unexpected={unexpected}, mismatched={mismatched})"
        )


def apply_canonical_schema(connection: sqlite3.Connection) -> None:
    """Apply schema atomically to a blank or already-canonical open connection.

    The caller must enable ``PRAGMA foreign_keys`` before calling. A nonblank
    database must match the canonical tables and explicit indexes exactly.
    Work is enclosed in a savepoint, so failure rolls back this call without
    committing or rolling back the caller's surrounding transaction.
    """
    _assert_foreign_keys_enabled(connection)
    _assert_blank_or_canonical(connection)
    statements = tuple(
        statement for statement in _statements(canonical_schema_sql())
        if not statement.upper().startswith("PRAGMA FOREIGN_KEYS")
    )
    savepoint = "rooted_canonical_schema_application"
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        for statement in statements:
            connection.execute(statement)
        _assert_blank_or_canonical(connection)
    except Exception as exc:
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        if isinstance(exc, SchemaApplicationError):
            raise
        raise SchemaApplicationError("canonical schema application failed") from exc
    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
