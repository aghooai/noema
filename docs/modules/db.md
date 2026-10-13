# noema.db: schema, roles, units of work, repositories (owners: db-schema, db-repos; frozen after Wave B)

Spec: spec-03 (DDL, RLS, definer functions, retention, tests), spec-02 §7 / §7.1 (the `Database`, `UnitOfWork`,
repo and `SystemCalls` Protocols). Everything here is frozen; changes go through `.orchestration/requests/`.

## Layout

| Module | Contents |
|---|---|
| `alembic/` | revisions `0001_initial`, `0002_c2a_requests` and `0003_operator_proof`, each executing `sql/<revision>/*.sql` in file-name order; `runner.py`: `upgrade`, `downgrade` (dev/test only; `run_downgrade_scripts(revision_dir)` runs `sql/<revision>/down/*.sql`), `migration_hash`, the `bootstrap`/`migrate`/`hash` CLI |
| `engine.py` | `Engine` / `PoolSettings` (an asyncpg pool for one login role, role-checked on connect), `connect`, provisioning (`ensure_roles`, `create_database`, `secure_database`) |
| `uow.py` | `PgDatabase[U]` (implements `Database`), `Tx`, `Scope`, `scope_for`; every transaction starts with `set_config('app.tenant_id' / 'app.project_id', $1, true)` |
| `system.py` | `PgSystemCalls` (implements `SystemCalls`, the definer functions granted to `noema_app`), `PgAdminCalls` and `AdminError` (the `noema-admin` functions, `noema_cli`) |
| `repos/` | one `Pg*Repo` per spec-02 §7.1 Protocol, `PgUnitOfWork`, `make_unit_of_work`; the only SQL of the app. `IDEM_INLINE_MAX_BYTES` (1 MiB) and `idempotency_body_key(tenant, record)` |

Roles: `noema_owner` (migrations), `noema_app` (the app; no BYPASSRLS, FORCE RLS on every tenant table),
`noema_cli` (operator CLI, admin functions only), `noema_definer` (owns the SECURITY DEFINER functions;
`search_path = pg_catalog, pg_temp`).

## Using it from a domain module

- Transactions: `async with services.db.uow(principal) as uow:` (request scope; tenant-wide for tenant_admin/auditor),
  `services.db.tenant_uow(tenant_id, project_id=None)` (jobs, sweepers, labeling), `services.db.system_uow()`
  (definer functions only). All three raise `NoemaError(internal_error)` (retryable) when Postgres is down.
  Wave E: the processes' `noema_app` pool (`engine.app_pool_settings`, used by composition) bounds every statement,
  so a hung Postgres (a failover or partition without RST, a row-lock wait) fails fast with the same retryable
  error instead of waiting for the kernel's TCP timeout: the driver's command timeout `STATEMENT_TIMEOUT_S` = 10 s,
  and `statement_timeout` 10 s / `lock_timeout` 5 s on the server. `Engine.acquire` maps them (asyncpg's own
  `TimeoutError`, `QueryCanceledError`, `LockNotAvailableError`); a caller's own `TimeoutError` still propagates.
- Wave E: `PgIdempotencyRepo.delete_expired(record_id) -> bool` (beyond the frozen Protocol; the retention
  sweeper's delete): deletes the record only while `expires_at < now()`, so a record a new request took over since
  the sweeper's page read (same id) is kept.
- Rows are `Mapping[str, object]` with spec-03 column names; jsonb = Python JSON values, arrays = lists,
  timestamps = UTC datetimes. `get` returns None for missing, deleted or invisible rows.
- B4 additions: `SessionRepo.delete` (hard delete; turns first), `IdempotencyRepo.take_over(..., resource_id=)`
  (conditional: exactly one racing taker wins), `SystemCalls.outbox_dead(..., attempts=)`,
  `SystemCalls.set_calibrator_status` / `bump_epoch` (the labeling drift job; refusals raise `AdminError` with
  `reason` forbidden / not_found / invalid_argument), `noema.derivative.text_layer_pages int[]` (page_images only).
- Time: repositories compare leases and expiries with Postgres `now()`; `IdempotencyRepo.insert_in_flight` takes
  absolute timestamps from the store's clock.

## Tests

- Fixtures (`tests/fixtures/db.py`, a pytest plugin of the root conftest, so every package has them): `pg_template`
  (the migrated template per migration hash, under an advisory lock), `pg_database` (a per-session clone, dropped at
  exit), `pg_app_engine` / `pg_db` (as `noema_app`), `pg_admin` (superuser, past RLS), `pg_cli`, `pg_owner`,
  `new_tenant_id()`, `seed_tenant()`. From inside an event loop use `await ensure_template(admin_dsn)`.
- Conformance: the shared `database` suite (`tests/contract_internal/test_repos.py`, `test_session_repo.py`,
  `test_uow_rls.py`) runs on the fake and on Postgres (`tests/integration/db_repos/conformance_impls.py`). Its
  `AgingClock` moves the database's lease and expiry columns into the past when the suite advances virtual time:
  the supported pattern for any real kit (e.g. ops-ledger's IdempotencyStore) that drives Postgres leases with a
  `FakeClock`.
- Commands: `make test-db` (schema/RLS/definer tests), `make test MOD=db_repos`, `make db-bootstrap db-migrate`
  (dev database; variables in `.env.example`), `make db-reap-test`.

## Revision 0002 (`0002_c2a_requests`, Wave C2a change requests)

Every environment must run `make db-migrate` before deploying code from C2a on (`PgSystemCalls.finish_job` raises
`RuntimeError` against a 0001 database, where `jobs.finish` still returns a boolean). Test templates rebuild by
migration hash. Contents (spec-03 §2/§4, marked "rev 0002"):
- `noema.derivative.facts jsonb` with `CHECK derivative_facts_closed` (pdf_sanitized only:
  `{"active_content": [...]}`); read back as `DerivativeRef.active_content` by `noema.media.pipeline`.
- `labeling_run_open` (one running run per tenant, profile, version, question name **and signature**),
  `labeling_run_by_definition`, `label_item_kept` (`LabelRepo.open_run(..., question_signature)`, `kept_items`;
  `label_item_by_decision` serves `items_for_decision`).
- `jobs.finish(...) → text` (the new state, NULL when the worker no longer held the job): `SystemCalls.finish_job`
  returns `FinishedJobState | None`.
- `ops.retention_page(limit, after)` (keyset on `(expires_at, kind, id, tenant_id)`): `SystemCalls.retention_page`,
  `RetentionCandidate` = `(tenant_id, kind, id, expires_at)`.
- `admin.config_body(op, version)` (file-less `noema-admin config approve`) and `admin.rehash_operator(op, hash,
  pepper_version)` (operator keys after a pepper rotation), both `noema_cli` (`PgAdminCalls`).
- Test fixtures (C2a): a clone's comment records its creator (`created_at`, `host`, `pid`, `pidns`);
  `stale_clone(meta, now=, host=, pidns=)` drops a clone with no comment, older than 6 h, or made on this host in this
  PID namespace by a process that is gone (a pid in another namespace is never judged dead).

## Revision 0003 (`0003_operator_proof`, Wave E edge fix: operator identity proven to the database)

`admin._operator` (run by every operator-facing admin.* function) used to check only that `p_operator_id` named a
live operator; the key was verified inside the noema-admin client, so anyone holding the shared `noema_cli` DSN
could propose as operator A and approve as B (operator ids are readable in audit rows and `list_config_versions`),
mint keys, or re-enroll after a revocation by naming a live id. Now the connection must carry the proof in its
session setting `noema.operator_key` (`{"v": pepper version, "p": pepper, "k": key_id, "s": secret}`, set by
`PgAdminCalls.prove_operator` after the CLI's own check): `_operator` recomputes `hmac-sha256:v<v>:<hex>` with the
new internal helper `admin._hmac_sha256` (RFC 2104 on the built-in `sha256()`) and compares it with
`platform.operator.key_hash`. A session setting is private to its connection, so one connection acts only as the
operator whose secret it holds; the two-person rules (config approval, calibrator activation) now need two keys.
No table, column or granted function changes (IMPL-010, spec-03 §4). The proof reaches the server as a bind
parameter: keep statement logging with parameters (`log_statement=all`) off on production databases, as for every
secret-bearing call. Test helpers: `tests/integration/db/_admin.py` (`operator()` with a real key hash, `login()`,
`Acting`).

## Known limits

- `admin.upsert_platform_calibrator` checks only the scope (`platform`, no `tenant_id`), the `cal_` id and the status
  (`fitted | accepted | rejected`); the jsonb columns (`member_t`, `pooled`, `holdout`, `acceptance`,
  `reliability`) are stored as given. A caller must pass a contract-valid artifact (what
  `noema.labeling.artifact.artifact_row` produces), or listCalibrators would serve an invalid `CalibratorInfo` and
  the registry would skip the row. `test_platform_artifacts_go_through_admin_only` stores a real fitted artifact and
  validates what listCalibrators serves from it. (C2b: full `CalibratorInfo` validation in PL/pgSQL was rejected;
  no Phase 1 code path imports platform artifacts.)
