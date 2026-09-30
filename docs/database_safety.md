# RootED Database Safety

Production databases may contain protected student data. Development, automated
tests, and Codex sessions must use newly created synthetic databases only. Never
copy a production database, backup, export, journal, WAL file, or historical log
into this repository.

## Runtime environment

`ROOTED_ENV` is mandatory. Its accepted values are `development`, `test`, and
`production`. Missing or invalid values fail closed; `RENDER` does not imply a
runtime environment.

- `development` and `test` require an explicit `NGSS_DB`. The resolved path must
  visibly contain `dev`, `test`, `synthetic`, `temp`, or `tmp`. A file named
  `ngss.db`, the repository's `data/ngss.db`, and paths containing `prod`,
  `production`, or `live` are rejected before SQLite is opened.
- `production` requires an explicit `NGSS_DB` and rejects paths with
  development/test/synthetic markers.

RootED no longer loads the repository `.env` automatically from application
configuration. Supply runtime settings explicitly through the process or an
approved launcher. Do not inspect an existing database merely to decide whether
it is safe; validate environment and the resolved target path first.

## Initialization and tooling

Importing `app_core.dashboard_v5` does not open or initialize SQLite. The module
entry point initializes explicitly immediately before serving, while WSGI
deployments initialize at the first request boundary for compatibility.

Migration and maintenance utilities must receive an explicit database path or
an already-open connection. They must not hard-code `data/ngss.db`, fall back to
`NGSS_DB`, create a database accidentally, or perform work when imported. CLI
tools that modify an existing database should resolve it with `strict=True`
before connecting.

`data/ngss.db` is prohibited for development and tests. The future development
database will be synthetic and explicitly created, but Phase 1 does not create
it. Existing production databases must remain outside the repository and must
never be copied into development.
