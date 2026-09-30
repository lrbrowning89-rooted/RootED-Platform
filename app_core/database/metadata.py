"""Connection-only helpers for the singleton RootED database identity row."""

from collections.abc import Mapping
import sqlite3
from typing import Any

from .schema import SCHEMA_VERSION

METADATA_ROW_ID = 1
VALID_ENVIRONMENTS = frozenset({"development", "test", "production"})
VALID_DATA_CLASSIFICATIONS = frozenset({"synthetic", "protected"})


class DatabaseMetadataError(ValueError):
    """Raised when database identity metadata is missing or invalid."""


def read_database_metadata(connection: sqlite3.Connection) -> dict[str, Any] | None:
    """Read the singleton metadata row from an existing connection."""
    cursor = connection.execute(
        "SELECT * FROM rooted_database_metadata WHERE singleton_id = ?",
        (METADATA_ROW_ID,),
    )
    row = cursor.fetchone()
    return None if row is None else dict(zip((item[0] for item in cursor.description), row))


def validate_database_metadata(metadata: Mapping[str, Any]) -> None:
    """Validate a metadata mapping against the canonical identity contract."""
    if metadata.get("singleton_id") != METADATA_ROW_ID:
        raise DatabaseMetadataError("metadata singleton_id must be 1")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise DatabaseMetadataError(f"metadata schema_version must be {SCHEMA_VERSION}")
    if metadata.get("environment") not in VALID_ENVIRONMENTS:
        raise DatabaseMetadataError("invalid metadata environment")
    if metadata.get("data_classification") not in VALID_DATA_CLASSIFICATIONS:
        raise DatabaseMetadataError("invalid metadata data_classification")
    identifier = metadata.get("database_identifier")
    if not isinstance(identifier, str) or not identifier.strip():
        raise DatabaseMetadataError("database_identifier must be non-empty")
    if not isinstance(metadata.get("created_at"), int):
        raise DatabaseMetadataError("created_at must be an integer timestamp")


def insert_database_metadata(
    connection: sqlite3.Connection,
    *, environment: str, data_classification: str, database_identifier: str,
    created_at: int, content_release: str | None = None,
    synthetic_fixture_version: str | None = None,
    created_by_tool: str | None = None,
    canonical_schema_checksum: str | None = None,
) -> None:
    """Insert the singleton identity row into an already-open database."""
    values = (
        METADATA_ROW_ID, SCHEMA_VERSION, environment, data_classification,
        database_identifier, content_release, synthetic_fixture_version,
        created_at, created_by_tool, canonical_schema_checksum,
    )
    validate_database_metadata(dict(zip((
        "singleton_id", "schema_version", "environment", "data_classification",
        "database_identifier", "content_release", "synthetic_fixture_version",
        "created_at", "created_by_tool", "canonical_schema_checksum",
    ), values)))
    connection.execute(
        """INSERT INTO rooted_database_metadata (
          singleton_id, schema_version, environment, data_classification,
          database_identifier, content_release, synthetic_fixture_version,
          created_at, created_by_tool, canonical_schema_checksum
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        values,
    )


def update_database_content_release(
    connection: sqlite3.Connection, content_release: str
) -> None:
    """Set content release on the existing singleton metadata row."""
    if not isinstance(content_release, str) or not content_release.strip():
        raise DatabaseMetadataError("content_release must be non-empty")
    cursor = connection.execute(
        "UPDATE rooted_database_metadata SET content_release=? WHERE singleton_id=?",
        (content_release, METADATA_ROW_ID),
    )
    if cursor.rowcount != 1:
        raise DatabaseMetadataError("singleton database metadata row is missing")
