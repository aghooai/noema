# Deployment

What a deployment consists of, how the images are built, how the processes are configured, and how they start
and stop. The reference topology is the compose `app` profile (`deploy/compose/docker-compose.yml`), which every
QA stack ran. Production infrastructure (Kubernetes, AWS accounts, load balancer, WAF) is not part of Phase 1.

## 1. Images

| Image | Dockerfile | Contents | Runs as |
|---|---|---|---|
| `noema-api` | `deploy/docker/api.Dockerfile` | Debian 13 slim with the python-build-standalone CPython 3.12 that uv installs in `/opt/python` (Wave F; the dev venv's interpreter), the locked runtime dependencies (`uv sync --frozen --no-dev`, **no media group**), the `noema` package; no tests, no dev tools, no pip | uid 10001:10002; `EXPOSE 8000 9464`; CMD `uvicorn --factory noema.api.app:create_app --loop uvloop --http httptools --host 0.0.0.0 --port 8000 --no-access-log --timeout-graceful-shutdown 35` |
| `noema-worker` | `deploy/docker/worker.Dockerfile` | `FROM noema-api` (the same venv); CMD `python -m noema.entrypoints.jobs --kinds labeling.*,session.delete,retention.sweep` | uid 10001:10002 (the uid sandboxd accepts on the spool socket) |
| `noema-media` | `deploy/docker/media-sandbox.Dockerfile` (target `media-sandbox`) | nsjail built from a pinned tag, Tesseract 5 + eng, libmagic, qpdf, the `media` dependency group, Docling models baked in; CMD `python -I -m noema.media.sandbox.sandboxd` | sandboxd as container root; each job in nsjail |
| `noema-media-test` | same, target `media-test` | `noema-media` plus the dev and fuzz groups and the tests (`make test-media`) | tests only |

Build and scan (Makefile targets; `IMAGE_TAG` defaults to `dev`):

```bash
make images IMAGE_TAG=<tag>       # noema-api, noema-worker, noema-media
make image-scan IMAGE_TAG=<tag>   # trivy JSON + CycloneDX SBOM per image; fails on a CRITICAL not waived
```

Waivers live in `deploy/docker/.trivyignore.yaml` (one entry per CVE with a statement and a review date; an expired
entry no longer waives). On the final Wave E3 tree `make image-scan` exited 0 with 51 waived ids and 0 unwaived, SBOMs
of 155 / 155 / 331 components ([reports/qa/wave-e-summary.md](../../reports/qa/wave-e-summary.md) SE13). The Wave F
api image (python-build-standalone interpreter, 372 MB) scanned 0 CRITICAL and 43 HIGH, all waived
([reports/perf/sizing.md](../../reports/perf/sizing.md) §8). The base images are pinned by digest in the
Dockerfiles; read them there, not here.

## 2. The compose `app` profile

`make e2e-up` starts it in its own project (default `noema-e2e`) and seeds it; do not use `make up PROFILES=app`
(the stack would not be bootstrapped). Services:

| Service | Image | Settings that matter |
|---|---|---|
| `api-1`, `api-2` | `noema-api` | one 2-vCPU "pod" each (`cpus: 2`), `WEB_CONCURRENCY=2`, `NOEMA_MAX_TURNS_PER_PROCESS=120`, port 8000, metrics 9464 and 9465, healthcheck `/readyz`, stop grace 45 s |
| `jobs` | `noema-worker` | the jobs CMD; healthcheck `/metrics` on 9464; stop grace 35 s |
| `media-worker` | `noema-worker` | `--kinds media.process,file.purge`; `/spool` volume shared with media-sandbox; 1 GiB, 256 pids; stop grace 35 s |
| `media-sandbox` | `noema-media` | `network_mode: none`, `cap_drop: ALL` plus `SYS_ADMIN SETUID SETGID CHOWN DAC_OVERRIDE FOWNER KILL`, seccomp `deploy/sandbox/docker-seccomp.json`, read-only root, 6 GiB, 2 CPUs, 1,024 pids |
| `postgres`, `valkey`, `moto` | `postgres:16-alpine`, `valkey/valkey:7.2-alpine`, `motoserver/moto` | the base services (moto stands in for STS, Secrets Manager, S3 and IAM) |
| `mockbedrock` | `noema-api` | the wire-level Bedrock mock from `tools/mockbedrock` (dev/test only) |
| `toxiproxy` | `ghcr.io/shopify/toxiproxy:2.9.0` | fault routing (`make qa-fault`) |

Every app container runs `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, `init: true` and a 256 MiB
`/tmp` tmpfs. The stack's AWS keys are fake and reach only moto and the mock. The pieces a production deployment
replaces: moto by real STS, Secrets Manager and S3 in the platform account; the mock by Bedrock in the tenant
accounts; and an edge (TLS, WAF) in front of the api, which Phase 1 does not have (spec-09 §1).

## 3. Process environment

Parsed once at startup by `composition.process_settings` into `ProcessSettings` (`src/noema/core/settings.py`;
spec-05 §5). An invalid value stops the process with a `StartupError`.

| Variable | Default | Staging / prod |
|---|---|---|
| `NOEMA_ENV` | - (required) | `staging` or `prod`; every environment gate keys on it |
| `NOEMA_REDIS_URL` | dev/test: `NOEMA_TEST_REDIS_URL`, else `redis://127.0.0.1:26379/0` | **required**; `redis://`, `rediss://` or `unix://` |
| `NOEMA_DB_DSN_REF` | `noema/db-dsn` | the Secrets Manager name of the `noema_app` DSN |
| `NOEMA_S3_BUCKET` | `noema-<env>` | the object bucket |
| `NOEMA_POD_ID` | the host name | the pod's identity in leases and owners; with `WEB_CONCURRENCY` > 1 each worker appends `-<pid>` |
| `NOEMA_METRICS_PORT` | unset (no `/metrics`) | set it (compose: 9464); worker *i* serves on port + *i* |
| `NOEMA_EXPECTED_PODS` | 1 | the number of api pods: the Redis-down local bucket divisor is this x `WEB_CONCURRENCY` |
| `NOEMA_TRUSTED_PROXIES` | empty | CIDRs of the proxies whose `X-Forwarded-For` is trusted for `ip_allowlist` |
| `NOEMA_DRAIN_DELAY_S` | 5 (0 in test); 0-120 | SIGTERM to uvicorn's own shutdown, with `/readyz` 503 meanwhile |
| `WEB_CONCURRENCY` | 1; 1-64 | **2** on api pods of 2 vCPU; never on jobs or media-worker |
| `NOEMA_MAX_TURNS_PER_PROCESS` | 120; 0 = off | per-process cap (overload guard, not the SLO; [sizing.md](sizing.md)) |
| `NOEMA_REDIS_MAX_CONNECTIONS` | 256; 8-10,000 | the process's one Redis pool, pub/sub included |
| `NOEMA_TOKEN_ESTIMATOR_TABLE` | unset (the shipped table) | absolute path to an estimator refit JSON; unreadable or invalid stops startup |
| `NOEMA_CREDENTIALS_BROKER` | `sts` | `sts` only (`ambient` is dev/test, the live smoke) |
| `NOEMA_BREAKER_TTFT_RULE` | `on` | `on` only (`off` is dev/test, the mock stacks) |
| `NOEMA_SANDBOX_BACKEND` | `remote` | `remote` (media-worker; `bwrap` is dev/test, `inproc_rlimit` test only) |
| `NOEMA_SPOOL_DIR`, `NOEMA_SANDBOX_SOCKET` | `/spool`, `<spool>/sandboxd.sock` | absolute paths shared by media-worker and media-sandbox |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT` | unset: no span export, spans not recorded | set to export traces (OTLP/HTTP); budget ~15-30 µs CPU per span ([modules/integration.md](../modules/integration.md) §9) |

Refused outside dev/test (startup fails): a non-empty `NOEMA_AWS_ENDPOINT_OVERRIDES`, `NOEMA_DEV_API_KEY`,
`NOEMA_CREDENTIALS_BROKER=ambient`, `NOEMA_BREAKER_TTFT_RULE=off`, `NOEMA_SANDBOX_BACKEND=bwrap`.
`NOEMA_CONTRACT_VALIDATE=1` is refused in prod. Tests only: `NOEMA_LIVE`, `NOEMA_AGENT_ID`, `NOEMA_TEST_*`.

Not read by the services: `NOEMA_OPERATOR_KEY` and `NOEMA_CLI_DSN` (`noema-admin`), `NOEMA_DB_OWNER_DSN`
(migrations, `noema-admin bootstrap`), `NOEMA_PG_ADMIN_DSN` and `NOEMA_DB_NAME` (`make db-bootstrap`).
The platform clients (STS, Secrets Manager, S3) use the container's default AWS credential chain, i.e. the platform
workload role, in `us-east-1`. Bedrock calls are signed only with the tenant's STS credentials and never with the
default chain (ADR-001 §4).

## 4. Secrets

| Secret (Secrets Manager, platform account) | Format | Read by | Rotation |
|---|---|---|---|
| `noema/db-dsn` (name in `NOEMA_DB_DSN_REF`) | the `noema_app` DSN | every service process | a running pool keeps its DSN: restart the pods after a rotation ([modules/integration.md](../modules/integration.md) §9) |
| `noema/api-key-pepper` | `{"version": <n>, "pepper": "<at least 32 chars>"}` | api (keys), `noema-admin` | the current and the previous version are loaded; keys verified with the previous version are re-hashed on use (spec-09 §2) |
| `noema/input-digest-pepper` | random bytes, at least 32 (spec-06 §1.2): a SecretString is used as its UTF-8 bytes, a SecretBinary as is; the dev bootstrap writes 64 hex characters (`secrets.token_hex(32)`) | decision (per-tenant HMAC key of the input digests, L2 cache keys) | do not rotate in Phase 1: there is no procedure, and a new value changes every digest, so existing L2 entries stop matching and expire by TTL |
| `platform/tenants/<tenant_id>/external-id` | `aghoo-<tenant_id>-<32 base62>` | the STS broker (api, jobs) | `noema-admin tenants external-id rotate` / `promote` ([tenant-onboarding.md](tenant-onboarding.md) §4) |

Database role passwords (`noema_owner`, `noema_app`, `noema_cli`) are set when `make db-bootstrap` creates or
repairs the roles, from `NOEMA_DB_OWNER_PASSWORD`, `NOEMA_DB_APP_PASSWORD` and `NOEMA_DB_CLI_PASSWORD` (required
outside dev and test; dev-only defaults there). Operator keys (`op_...`) and API keys (`ak_...`) are
printed once by `noema-admin` and stored only as hashes. `tools/bootstrap_dev.py` seeds all of the above into moto
for dev and test stacks only.

## 5. Database setup and migrations

Alembic runs as `noema_owner`, never as the app; production is forward-only. Revisions:

| Revision | Wave | Contents |
|---|---|---|
| `0001_initial` | B | the spec-03 schema: schemas, tables, indexes, RLS policies, grants, definer functions |
| `0002_c2a_requests` | C2a | `derivative.facts`, the per-signature `labeling_run_open` index and `kept_items` indexes, `jobs.finish` returning text, `ops.retention_page`, `admin.config_body` (file-less `config approve`), `admin.rehash_operator`. Code from C2a on refuses to run against a 0001 database (`finish_job` raises) |
| `0003_operator_proof` | E | the database verifies the operator key per connection, so the two-person rules need two keys; no table or grant changes. Keep statement logging with parameters off (the proof is a bind parameter) |

```bash
make db-bootstrap   # roles and database (superuser DSN: DB_ADMIN_DSN or NOEMA_PG_ADMIN_DSN; dev passwords in dev/test)
make db-migrate     # alembic upgrade head as noema_owner (DB_OWNER_DSN or NOEMA_DB_OWNER_DSN)
make db-hash        # the migration hash (names the test template database)
```

Both targets default to the dev compose Postgres on 127.0.0.1:25432; override `DB_PG_PORT`, `DB_NAME`,
`DB_ADMIN_DSN` or `DB_OWNER_DSN` for another database. Verified on a stack's database:
`make db-migrate DB_PG_PORT=35132` (already at head: exit 0, `alembic_version` = `0003_operator_proof`). The order
for a release is in [upgrades-and-migrations.md](upgrades-and-migrations.md).

## 6. First start of an environment

1. Create the platform secrets (section 4) and the bucket `noema-<env>`; in staging and prod one KMS key per
   tenant, alias `alias/noema-<env>-<tenant_id>`.
2. `make db-bootstrap` and `make db-migrate` against the environment's database.
3. Start the processes. Until a config version is active, `/readyz` answers 503 and authenticated requests get a
   retryable 500 `internal_error`.
4. `noema-admin bootstrap --operator <name>` (one time, `NOEMA_DB_OWNER_DSN`), then a second operator, the price
   catalog, the config (propose and approve by two operators), tenants, projects and keys:
   [configuration.md](configuration.md). Pods load the activated config within 5 s.
5. Onboard each tenant account: [tenant-onboarding.md](tenant-onboarding.md).

`tools/bootstrap_dev.py` performs steps 1, 2 and 4 for a dev or test compose stack (`make e2e-bootstrap`).

## 7. Health, readiness and metrics

| Endpoint | Port | Behavior |
|---|---|---|
| `GET /healthz` | 8000 | 200 while the event loop answers |
| `GET /readyz` | 8000 | 200 when a config snapshot is loaded and the process is not draining; else 503, no body (it discloses no dependency state; a degraded Redis or Postgres does not make the pod unready) |
| `GET /metrics` | `NOEMA_METRICS_PORT` (+ worker index) | Prometheus text; scrape every worker port of an api pod (9464 and 9465 in compose) |

The jobs and media-worker containers have no HTTP API; their liveness is their `/metrics` endpoint. Alerts and
dashboards: [observability.md](observability.md).

## 8. Graceful shutdown

On SIGTERM an api process (spec-11 §7):
1. answers `/readyz` 503 at once and starts the drain hooks with a 30 s grace;
2. stops accepting `NOEMA_DRAIN_DELAY_S` (5 s) later (uvicorn's own shutdown);
3. lets running turns continue up to 30 s, then cancels them with a retryable `all_routes_unavailable` and gives
   them up to 5 s to finalize;
4. flushes the outbox (up to 10 s), stops the background tasks, closes the pools and exits.

Set the orchestrator's termination grace above that: compose uses 45 s for the api and 35 s for the workers, and
the image runs uvicorn with `--timeout-graceful-shutdown 35`. The jobs and media-worker processes stop claiming,
give running handlers 10 s, flush the outbox and exit; unfinished jobs are re-queued by the reaper (lock lapse,
2 minutes). Evidence: e2e `tests/e2e/suite/test_shutdown.py` (a SIGTERM during a turn: `/readyz` 503 at once, the
turn streams to its end and is stored, usage flushed; 52/52 e2e on the final tree,
[reports/qa/wave-e-summary.md](../../reports/qa/wave-e-summary.md) section 4). An idle api container restarted with
`docker compose restart` took 8.7 s and was ready about 1 s after it was started again (measured while writing this
document).

## 9. Datastores in production

Phase 1 ships no datastore infrastructure and the design sets no RPO or RTO: the deployment owner sets both. The
compose stack is not a model for durability: its Postgres is a single container and its Valkey runs
`--save '' --appendonly no --maxmemory-policy noeviction` (`deploy/compose/docker-compose.yml`). Nothing in this
section was run in Phase 1 except `make db-migrate`.

**Postgres is the system of record** ([architecture.md](../architecture.md) section 5): tenant data, usage lines,
audit, config versions, keys, operators, calibrators and the job queue.

| Requirement | Why |
|---|---|
| Multi-AZ, 2 vCPU / 8 GiB to start (design 08 §9) | a failover is a Postgres outage: `store: true` turns get a retryable 500 until it ends ([runbooks.md](runbooks.md#postgres-down)) |
| `max_connections` above 40 per api pod plus the workers (320 at 8 pods, [sizing.md](sizing.md)) | each api process holds a pool of `DB_POOL_MAX` 20 |
| Automated backups with point-in-time recovery, retention per your RPO | there is no other copy of the ledger and the audit trail |
| A restore drill before go-live, after each Postgres major upgrade, and on a fixed cadence (for example quarterly) | an untested restore is not a backup |

Restore (outline; your provider's tooling does steps 1 and 2):
1. Stop or scale the api, jobs and media-worker processes to 0, so nothing writes to the old instance.
2. Restore the snapshot or point in time to a new instance; an instance-level restore includes the roles
   `noema_owner`, `noema_app` and `noema_cli`.
3. `make db-migrate` against it (`DB_OWNER_DSN`): a no-op at head, required if the restore point predates the
   running release's last revision; then check that `alembic_version` matches the release (section 5).
4. Point `noema/db-dsn` (and the `NOEMA_CLI_DSN` / `NOEMA_DB_OWNER_DSN` of the operators) at the new instance and
   start the processes: a running pool keeps its DSN (section 4).
5. Check: `/readyz` 200, `noema-admin config list` shows the active version ([configuration.md](configuration.md)),
   and the outbox backlog drains ([runbooks.md](runbooks.md#noema-outbox-redis-backlog)). Writes after the restore
   point are lost; outbox records still in Valkey drain into the restored database or dead-letter after 10
   deliveries (`ops.outbox_dead_letter`). Not exercised in Phase 1.

**Valkey / Redis holds coordination state, and the outbox during a Postgres outage**: GCRA buckets and stream
slots, turn owners, session queues, the L2 decision cache, pub/sub, and, while Postgres is unavailable, the outbox
streams `outbox:<0..7>` with the usage lines, audit events and turn finalizes (architecture section 5).

| Requirement | Why |
|---|---|
| `maxmemory-policy noeviction` (managed services often default to `volatile-lru` or `allkeys-lru`) | an eviction policy silently drops keys: a GCRA bucket (admits past its limit), a turn finalize marker (TTL 1 day), or with `allkeys-*` an outbox stream (lost billing and audit records). Under `noeviction` a full Valkey refuses writes with an OOM error instead (not exercised in Phase 1): alert on its memory before that |
| `maxmemory` with headroom over the caches plus the outbox backlog of the longest Postgres outage you plan to ride out; 1 GiB to start (design 08 §9); alert on the server's `used_memory` | not measured in Phase 1; Noema exports no Valkey memory metric |
| A replica with automatic failover (design 08 §9), and AOF persistence (`appendonly yes`) | without persistence a Valkey restart during a Postgres outage loses the outbox records it holds |

Runbooks: [postgres-down](runbooks.md#postgres-down), [redis-down](runbooks.md#redis-down).
