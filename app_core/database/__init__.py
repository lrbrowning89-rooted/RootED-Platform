"""Connection-only helpers for RootED's canonical SQLite schema."""

from .schema import (
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    SchemaApplicationError,
    SchemaCompatibilityError,
    apply_canonical_schema,
    canonical_schema_checksum,
)

__all__ = [
    "REQUIRED_TABLES", "SCHEMA_VERSION", "SchemaApplicationError",
    "SchemaCompatibilityError", "apply_canonical_schema",
    "canonical_schema_checksum",
]
