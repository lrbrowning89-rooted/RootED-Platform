import sqlite3
import unittest
from unittest.mock import patch

import app_core.database.schema as schema_module
from app_core.database.inspection import (
    foreign_keys,
    index_names,
    metadata_schema_version,
    table_columns,
    table_names,
    unique_column_sets,
)

from app_core.database.metadata import (
    DatabaseMetadataError,
    insert_database_metadata,
    read_database_metadata,
    validate_database_metadata,
)
from app_core.database.schema import (
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    SchemaApplicationError,
    SchemaCompatibilityError,
    apply_canonical_schema,
    canonical_schema_checksum,
    canonical_schema_sql,
)


class CanonicalSchemaTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        apply_canonical_schema(self.connection)

    def tearDown(self):
        self.connection.close()

    def table_names(self):
        return {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}

    def columns(self, table):
        return {row[1]: row for row in self.connection.execute(
            f"PRAGMA table_info({table})"
        )}

    def index_names(self):
        return {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}

    def test_schema_version_is_one(self):
        self.assertEqual(SCHEMA_VERSION, 1)

    def test_manifest_matches_application_tables(self):
        self.assertEqual(self.table_names() - {"sqlite_sequence"}, REQUIRED_TABLES)

    def test_foreign_keys_are_enabled(self):
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_fresh_schema_passes_foreign_key_check(self):
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_schema_is_idempotent(self):
        apply_canonical_schema(self.connection)
        self.assertEqual(self.table_names() - {"sqlite_sequence"}, REQUIRED_TABLES)

    def test_foreign_keys_must_be_enabled_by_caller(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        with self.assertRaisesRegex(SchemaApplicationError, "foreign key enforcement"):
            apply_canonical_schema(connection)
        self.assertEqual(table_names(connection), frozenset())

    def test_nonblank_incompatible_database_fails_without_repair(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("CREATE TABLE incompatible(value TEXT)")
        with self.assertRaises(SchemaCompatibilityError):
            apply_canonical_schema(connection)
        self.assertEqual(table_names(connection), frozenset({"incompatible"}))

    def test_mismatched_canonical_table_fails_without_repair(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("CREATE TABLE config(wrong_column TEXT)")
        with self.assertRaises(SchemaCompatibilityError):
            apply_canonical_schema(connection)
        self.assertEqual([row[1] for row in table_columns(connection, "config")], ["wrong_column"])

    def test_failure_rolls_back_all_schema_objects(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA foreign_keys = ON")
        statements = schema_module._statements(canonical_schema_sql())
        injected = statements[:5] + ("CREATE TABLE broken(",) + statements[5:]
        with patch.object(schema_module, "_statements", return_value=injected):
            with self.assertRaises(SchemaApplicationError):
                apply_canonical_schema(connection)
        self.assertEqual(table_names(connection), frozenset())

    def test_executor_preserves_caller_transaction(self):
        self.connection.execute("BEGIN")
        self.connection.execute("INSERT INTO config(key,value) VALUES ('caller','owned')")
        apply_canonical_schema(self.connection)
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM config").fetchone()[0], 0)

    def test_api_reads_the_canonical_resource(self):
        self.assertIn("rooted_database_metadata", canonical_schema_sql())

    def test_checksum_is_deterministic_sha256(self):
        first = canonical_schema_checksum()
        self.assertEqual(first, canonical_schema_checksum())
        self.assertEqual(len(first), 64)
        int(first, 16)

    def test_checksum_changes_with_schema_source(self):
        source = canonical_schema_sql()
        original_checksum = canonical_schema_checksum()
        with patch.object(schema_module, "canonical_schema_sql", return_value=source + "\n-- changed"):
            self.assertNotEqual(original_checksum, schema_module.canonical_schema_checksum())

    def test_questions_include_nullable_reading_level(self):
        self.assertEqual(self.columns("questions")["reading_level"][3], 0)

    def test_important_runtime_columns_exist(self):
        expected = {
            "users": {"id", "username", "account_role", "linked_student_id", "sso_subject"},
            "responses": {"student_id", "standard_id", "question_id", "routing_level_attempt_id"},
            "student_question_deliveries": {"submission_token", "growth_attempt_id", "sequence_number"},
            "assignments": {"teacher_user_id", "class_id", "target_type", "assign_at"},
            "question_metadata": {"question_id", "model_id", "source_artifact", "imported_at"},
        }
        for table, columns in expected.items():
            self.assertTrue(columns <= set(self.columns(table)), table)

    def test_compatibility_tables_exist(self):
        self.assertTrue({"skills", "question_skills", "frustration_events"} <= self.table_names())

    def test_progress_state_omits_historical_graph_columns(self):
        self.assertTrue({
            "current_standard_id", "origin_standard_id", "active_route_type"
        }.isdisjoint(self.columns("progress_state")))

    def test_migration_history_tables_are_absent(self):
        self.assertTrue({
            "migration_log", "archived_questions", "archived_objectives"
        }.isdisjoint(self.table_names()))

    def test_content_release_log_is_present(self):
        self.assertIn("content_release_log", self.table_names())

    def test_standard_links_has_expected_uniqueness(self):
        sql = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='standard_links'"
        ).fetchone()[0]
        self.assertIn("UNIQUE(from_standard_id,to_standard_id,link_type)", sql)
        self.assertIn(
            ("from_standard_id", "to_standard_id", "link_type"),
            unique_column_sets(self.connection, "standard_links"),
        )

    def test_standard_links_has_no_foreign_keys(self):
        self.assertEqual(self.connection.execute(
            "PRAGMA foreign_key_list(standard_links)"
        ).fetchall(), [])
        self.assertEqual(foreign_keys(self.connection, "standard_links"), ())

    def test_standard_links_matches_connection_helper_columns(self):
        self.assertTrue({
            "id", "from_standard_id", "to_standard_id", "link_type",
            "source", "priority", "notes", "created_at",
        } <= set(self.columns("standard_links")))

    def test_canonical_indexes_are_present(self):
        self.assertTrue({
            "idx_progress_state_unique", "idx_responses_routing_level_attempt",
            "idx_student_growth_active", "idx_student_question_delivery_active",
            "uq_user_platform_roles_active",
            "idx_users_sso",
        } <= self.index_names())
        self.assertEqual(index_names(self.connection), self.index_names() - {
            name for name in self.index_names() if name.startswith("sqlite_autoindex_")
        })

    def test_legacy_progress_index_name_is_absent(self):
        self.assertNotIn("idx_state_unique", self.index_names())

    def test_metadata_is_not_automatically_inserted(self):
        self.assertIsNone(read_database_metadata(self.connection))
        self.assertIsNone(metadata_schema_version(self.connection))

    def test_metadata_structure_is_exact(self):
        self.assertEqual(tuple(self.columns("rooted_database_metadata")), (
            "singleton_id", "schema_version", "environment", "data_classification",
            "database_identifier", "content_release", "synthetic_fixture_version",
            "created_at", "created_by_tool", "canonical_schema_checksum",
        ))

    def test_schema_application_seeds_no_content_or_users(self):
        for table in ("users", "students", "standards", "objectives", "questions", "standard_links"):
            self.assertEqual(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_metadata_insert_and_read(self):
        insert_database_metadata(
            self.connection, environment="test", data_classification="synthetic",
            database_identifier="canonical-memory-test", created_at=123,
            synthetic_fixture_version="fixture-1",
        )
        metadata = read_database_metadata(self.connection)
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual(metadata["database_identifier"], "canonical-memory-test")
        self.assertEqual(metadata_schema_version(self.connection), 1)

    def test_inspection_helpers_report_canonical_structure(self):
        self.assertEqual(table_names(self.connection), REQUIRED_TABLES)
        self.assertIn("reading_level", {row[1] for row in table_columns(self.connection, "questions")})

    def test_metadata_is_singleton(self):
        insert_database_metadata(
            self.connection, environment="test", data_classification="synthetic",
            database_identifier="one", created_at=1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            insert_database_metadata(
                self.connection, environment="test", data_classification="synthetic",
                database_identifier="two", created_at=2,
            )

    def test_invalid_environment_is_rejected_by_helper(self):
        with self.assertRaises(DatabaseMetadataError):
            insert_database_metadata(
                self.connection, environment="staging", data_classification="synthetic",
                database_identifier="bad", created_at=1,
            )

    def test_invalid_environment_is_rejected_by_sql(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO rooted_database_metadata "
                "(singleton_id,schema_version,environment,data_classification,database_identifier,created_at) "
                "VALUES (1,1,'staging','synthetic','bad',1)"
            )

    def test_invalid_classification_is_rejected(self):
        with self.assertRaises(DatabaseMetadataError):
            insert_database_metadata(
                self.connection, environment="test", data_classification="unknown",
                database_identifier="bad", created_at=1,
            )

    def test_wrong_schema_version_is_rejected(self):
        with self.assertRaises(DatabaseMetadataError):
            validate_database_metadata({
                "singleton_id": 1, "schema_version": 2, "environment": "test",
                "data_classification": "synthetic", "database_identifier": "bad",
                "created_at": 1,
            })

    def test_blank_identifier_is_rejected(self):
        with self.assertRaises(DatabaseMetadataError):
            insert_database_metadata(
                self.connection, environment="test", data_classification="synthetic",
                database_identifier="  ", created_at=1,
            )

    def test_non_integer_created_at_is_rejected(self):
        with self.assertRaises(DatabaseMetadataError):
            insert_database_metadata(
                self.connection, environment="test", data_classification="synthetic",
                database_identifier="bad", created_at="now",
            )

    def test_foreign_key_enforcement_is_effective(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO questions(question_id,objective_id) VALUES ('q','missing')"
            )

    def test_standard_links_allows_future_standard_targets(self):
        self.connection.execute(
            "INSERT INTO standard_links(from_standard_id,to_standard_id,link_type,created_at) "
            "VALUES ('MS-LS1-1','5-LS1-1','remediation',1)"
        )
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM standard_links"
        ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
