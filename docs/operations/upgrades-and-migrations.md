# Upgrades and migrations

Phase 1 is release 0.1.0 ([CHANGELOG.md](../../CHANGELOG.md)). There is no earlier production release to upgrade
from; this page sets the procedure every later release follows and records the schema history.

## 1. What versions exist

| Item | Version | Where |
|---|---|---|
| Package | 0.1.0 | `pyproject.toml` |
| API version header | `Aghoo-Version: 2026-10-01` | contract revision 6 (`src/noema/contracts/_vendor`, a byte copy of the design contracts) |
| Database schema | `0003_operator_proof` (head) | `src/noema/db/alembic/versions/` |
| Platform config | integer `version`, one `active` at a time | `noema-admin config list` |
| Price catalog | integer version, pinned as `db:<n>` | `noema-admin prices import` output |

## 2. Release order (expand, migrate, contract)

1. **Build and scan** the images under a new tag: `make images IMAGE_TAG=<tag>`, then
   `make image-scan IMAGE_TAG=<tag>` (fails on an unwaived CRITICAL; review the waivers' dates).
2. **Migrate** the database before any process of the new release starts: `make db-migrate` with the target's
   `DB_OWNER_DSN` (runs as `noema_owner`; `alembic upgrade head`). Revisions are additive; a destructive change
   (drop, rename, type narrowing, NOT NULL on an existing column) waits one release, so the previous code keeps
   working on the new schema (spec-03 §7). Data backfills run as jobs, not in migrations.
3. **Roll the api pods** one at a time. Each pod drains on SIGTERM: `/readyz` 503 first, then 30 s for running
   turns, the outbox flush, exit ([deployment.md](deployment.md) section 8). Wait for `/readyz` 200 on the new pod
   before the next.
4. **Roll jobs and media-worker** (running handlers get 10 s; unfinished jobs are re-queued by the reaper).
   media-sandbox has no state beyond its spool.
5. **Config last**: a config that uses a new field or rule is proposed and approved only after every pod runs the
   code that understands it (a pod that cannot load a version keeps its current snapshot and logs an ERROR).

Process variables (`WEB_CONCURRENCY`, `NOEMA_MAX_TURNS_PER_PROCESS`, `NOEMA_REDIS_MAX_CONNECTIONS`,
`NOEMA_TOKEN_ESTIMATOR_TABLE`, ...) are read at startup only: change them by rolling the pods.

## 3. Schema history

| Revision | Requirement |
|---|---|
| `0001_initial` | the base schema |
| `0002_c2a_requests` | **required** by code from Wave C2a on: against a 0001 database `PgSystemCalls.finish_job` raises (`jobs.finish` still returned a boolean). Adds `derivative.facts`, labeling indexes, `ops.retention_page`, `admin.config_body`, `admin.rehash_operator` ([modules/db.md](../modules/db.md)) |
| `0003_operator_proof` | the database verifies the operator key on each `noema_cli` connection; no table, column or grant change. Keep statement logging with parameters off on production databases |

`make db-hash` prints the hash of the revision files; the test template database is named after it, so tests
rebuild their template whenever a revision changes.

Downgrades exist for CI and tests only (`noema.db.alembic.runner.downgrade`, the `down/*.sql` scripts of 0002 and 0003);
production is forward-only. To undo a release, roll the code back (the schema stays, being additive) and fix
forward.

## 4. Config, prices and estimator changes

| Change | Procedure |
|---|---|
| Config | `config validate`, `propose` (operator A), `approve` (operator B); undo with `config rollback <n>` plus a second approval ([configuration.md](configuration.md) section 3) |
| Prices | `prices import <file>` (a new catalog version), then a config version with `platform.cost.prices_ref: "db:<n>"`; undo by pinning the previous `db:<n>` |
| Token estimator refit | `uv run --frozen --no-sync python -m noema.ops.cost.estimator_fit tests/cassettes/live-*/*.json > fit.json` (run: exit 0, a table for the 7 model aliases plus a per-alias error report), put the file on the pods, set `NOEMA_TOKEN_ESTIMATOR_TABLE` to its absolute path and roll them (an unreadable or invalid file stops startup; the rollout was not run) |
| Calibrators | `calibrators approve`, then `activate` by another operator; pods see it within 5 s |

## 5. Secret rotation

| Secret | Procedure |
|---|---|
| `noema/api-key-pepper` | put a new value `{"version": <n+1>, "pepper": ...}`; Secrets Manager keeps the old one as `AWSPREVIOUS`. Pods reload the ring every 300 s; a key verified with the previous pepper is re-hashed on use. Only the current and the previous versions are loaded, so a key unused across two rotations stops working |
| `noema/db-dsn` | new `noema_app` password in Postgres and the secret, then roll every process (a running pool keeps its DSN) |
| ExternalIds | `tenants external-id rotate` / `promote` around the trust-policy changes ([tenant-onboarding.md](tenant-onboarding.md) section 4) |
| Operator keys | `operators create` a new operator, then `operators revoke <operator_id>` the old one |

## 6. Contracts

The contracts are vendored byte for byte from the design repo; `make contracts-check` (part of `make check`) fails
when the copy, the bundle or the generated models drift. A contract change is made in the design repo and pulled
with `make contracts-sync contracts-generate` by the platform role only ([CONTRIBUTING.md](../../CONTRIBUTING.md)).
Clients pin `Aghoo-Version`; additive changes keep it.
