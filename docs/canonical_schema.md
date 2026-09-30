# Canonical database schema

The authoritative fresh-database schema is
`app_core/database/canonical_schema.sql`. The former `app_core/schema.sql` path
is a non-executable deprecation pointer and must not be used to create a
database.

`app_core.database.schema.apply_canonical_schema(connection)` applies the SQL
only to an already-open SQLite connection. It does not select a path, read the
environment, open a connection, seed content, run historical migrations, or
insert database identity metadata.

Schema version 1 defines the current runtime tables, the empty compatibility
tables `skills`, `question_skills`, and `frustration_events`, and the singleton
`rooted_database_metadata` identity table. Identity metadata is inserted
separately through connection-only helpers in `app_core.database.metadata`;
schema application never invents an environment, classification, release, or
database identifier.

`content_release_log` is the content-package ledger. Historical migration and
archive tables are not part of a fresh canonical database.

## Executor contract

The caller must open the SQLite connection and enable `PRAGMA foreign_keys`
before calling `apply_canonical_schema`. The executor accepts either a blank
database or a database whose tables and explicit indexes exactly match the
canonical source. Any other nonblank structure raises
`SchemaCompatibilityError`; the executor does not migrate, repair, drop, or
overwrite it.

Schema statements run individually inside a SQLite savepoint. If a statement
or post-application structural check fails, the executor rolls back to that
savepoint and raises `SchemaApplicationError`. An enclosing caller transaction
is neither committed nor rolled back. When no outer transaction exists,
releasing the successful savepoint makes the schema durable on that connection.

`canonical_schema_checksum()` returns the lowercase SHA-256 digest of the exact
UTF-8 authoritative SQL source. It does not inspect a database or write the
metadata row.

`app_core.database.inspection` contains small read-only helpers for tables,
columns, explicit indexes, unique column sets, foreign keys, and the optional
singleton metadata schema version. Every helper requires an already-open
connection.

## Development bootstrap CLI

Phase 2C provides a create-new-only command with no default target:

```text
python -m app_core.database.bootstrap create-development-database --database <explicit-path>
```

The process environment must contain `ROOTED_ENV=development`. If `NGSS_DB`
is present, it must resolve to the same target as `--database`. The existing
Phase 1 development-path policy is applied before SQLite is opened.

The final target and its parent are never created implicitly: the parent must
already exist and the target must not. The command reserves a unique sibling
working file, creates and verifies the empty canonical database there, commits
its single synthetic-development metadata row, closes the connection, rechecks
the final target, and uses Windows no-overwrite rename semantics to finalize.
Any pre-finalization failure removes only that invocation's working file.

The tool deliberately cannot create production, protected, or user-bearing
databases; its only content is the approved launch package and MVP graph.
Race-safe finalization currently fails closed on
non-Windows platforms until an equivalent no-overwrite primitive is added.

## Launch content and MVP graph

The development bootstrap installs the approved `rooted-launch-content`
release `2026-07-27` through the connection-only core in
`migrations.install_launch_content`. The seven declared curriculum JSON files
remain the source of truth. Installation inserts only absent launch IDs,
treats an exact reinstall as a no-op, and fails closed on partial or conflicting
content. It does not commit the caller's transaction.

The bootstrap then seeds exactly four canonical graph rows through
`migrations.standard_links`, updates the singleton metadata `content_release`,
and verifies content, graph, operational emptiness, SQLite integrity, and
foreign keys before finalization.

Two graph rows reference `4-LS1-1`, including the approved remediation route.
That standard intentionally has no launch curriculum or deliverable questions
yet. The graph reference is retained without foreign keys; bootstrap must not
fabricate elementary standards, objectives, or questions to satisfy it.

Launch metadata may retain non-null `model_id` references. Phase 2D does not
seed or require `model_assets`; model-asset catalog installation remains a
separate content concern.
