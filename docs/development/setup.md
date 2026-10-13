# Development setup

## 1. Toolchain

| Tool | Version | Why |
|---|---|---|
| Linux x86_64 | - | the media image, nsjail and the fuzz group (`atheris`) are x86_64 Linux |
| Docker with Compose v2 | Compose with `up --wait` and profiles | every stack; image builds are the only networked build step |
| `uv` | 0.9 or later (`pyproject.toml` `required-version`) | the Python toolchain; it provides the managed CPython 3.12 (`.python-version`) |
| Node.js | 22 or later (`package.json` engines; the UI's dev proxy needs 22.18 for type stripping) | contract tooling, the TypeScript SDK, the Web UI |
| `jq` | any | the examples that read a stack's state file |

Versions on the build host while these docs were written: uv 0.9.10, Node 24.4.0, npm 11.4.2, Docker 25.0.8,
Docker Compose 5.3.1.

## 2. Dependencies

```bash
make sync       # uv sync --frozen --group media: the locked dev + media groups into .venv (exact)
make sync-dev   # only the dev group (drops the heavy media parsers)
make npm-ci     # npm ci --offline --ignore-scripts: the locked JS toolchain into node_modules
```

Every tool runs through `uv run --frozen --no-sync` (the Makefile's `RUN`), so a target never re-syncs the venv.
Do not run a plain `uv sync`: it drops the media group. `make lock` re-resolves `uv.lock` and is for the platform
role only. `make npm-ci` installs from the npm cache only (`--offline`): on a host with an empty cache it fails with
`ENOTCACHED` ("cache mode is 'only-if-cached' but no cached response is available"; reproduced 2026-10-12 with an
empty `--cache` in a temporary directory), so run `npm ci --ignore-scripts` online once first (not run here).
Verified: `uv sync --frozen --group media --dry-run` reported "Would make no changes" on the current venv;
`make npm-ci` was not run (it replaces `node_modules`).

## 3. The dev services

```bash
cp .env.example .env    # optional: override ports and dev settings (never real credentials)
make up                 # postgres, valkey, moto (+ PROFILES, default toxiproxy) as compose project noema-dev
make services-ps        # status
make down               # stop every profile (volumes kept)
```

Host ports (127.0.0.1 only): Postgres 25432 (`postgres` / `noema-dev-only`), Valkey 26379, moto 25000, the host
toxiproxy API 28474. Tests use these shared services but isolate themselves: one cloned database, one Redis key
namespace and one set of moto resources per pytest session ([testing.md](testing.md) section 4). For a dev
database: `make db-bootstrap db-migrate` (dev role passwords; DSNs in `.env.example`).

## 4. A full local stack

`make e2e-up` builds the images (skip with `E2E_BUILD=0`), starts the base services, toxiproxy and the Bedrock wire
mock, seeds the stack with `tools/bootstrap_dev.py`, starts the `app` profile (two api pods of two processes, jobs,
media-worker, media-sandbox) and waits until every service is healthy. The quickstart in
[README.md](../../README.md) sends a turn to it.

| Variable | Default | Meaning |
|---|---|---|
| `E2E_PROJECT` | `noema-e2e` | the compose project; one per stack |
| `E2E_PORT_BASE` | 28100 (0 = ephemeral) | api-1 +1, api-2 +2, metrics +11..+14, postgres +32, moto +50, toxiproxy +74, valkey +79, mockbedrock +80 |
| `E2E_BUILD` | 1 | 0 reuses the images (`NOEMA_API_IMAGE`, `NOEMA_WORKER_IMAGE`, `NOEMA_MEDIA_IMAGE` pick other tags) |
| `E2E_ENV` | `dev` | the stack's `NOEMA_ENV` (keep `dev` for two replicas) |
| `E2E_VALIDATE` | empty | 1 turns on the server-side contract validator |
| `E2E_BREAKER_TTFT` | `off` | the breakers' p95-TTFT rule (off on the mock, whose scenarios change latencies 30x) |
| `E2E_STATE` | `~/.noema/stacks/<project>.json` | the state file (0600): endpoints, operator keys, tenants, projects, principals and API keys |

`python -m tools.bootstrap_dev --help` lists the bootstrap's options (`--compose-project`, `--env {dev,test}`,
`--db-host`, `--state`); `make e2e-bootstrap` re-runs it on a running stack (idempotent). It seeds tenants `t_acme`
and `t_beta`, projects `main` and `second`, principals `developer`, `developer2`, `tenant_admin`, `reviewer`,
`end_user`, `auditor` (and `developer` in `second`), one `ak_test_` key each, two operators, a fake price catalog and
the config (proposed by one operator, approved by the other). `make e2e-ps`, `make e2e-logs [SERVICE=api-1]` and
`make e2e-down` (deletes the volumes) manage it.

## 5. A host-run API against a stack

The bootstrap also writes `<project>.env` next to the state file, the environment of a host process against the stack (endpoint overrides
on the published moto and mock ports, the host DSN secret, Valkey, fake AWS keys):

```bash
set -a; . ~/.noema/stacks/noema-e2e.env; set +a
make api-serve API_PORT=28199     # uvicorn on 127.0.0.1:28199 with the real composition
```

Verified with a stack on port base 35100 (`API_PORT=35199`): `/healthz` and `/readyz` 200, a `stream: false` turn 200.

## 6. Web UI and SDKs

- UI (`web/`, [modules/ui.md](../modules/ui.md)): `make ui-check` (tsc, vitest, build), `make ui-serve` (the SPA and
  the dev cookie proxy on 127.0.0.1:5180; needs `NOEMA_ENV=dev`, `NOEMA_UI_API_URL` and
  `NOEMA_UI_API_KEYS=alias=ak_test_...[,...]`; test keys only), `make ui-e2e
  [UI_STACK_STATE=~/.noema/stacks/<project>.json]` (Playwright on the system Chrome). Not run while writing this page.
- SDKs (`sdk/typescript` = `@aghoo/sdk`, `sdk/python` = `aghoo`, [modules/sdk.md](../modules/sdk.md)):
  `make sdk-check` (both: contract sync, typecheck, lint, tests without network).

## 7. Contracts and formatting

- The contracts in `src/noema/contracts/_vendor` are a byte copy of the design repo's `contracts/`;
  `make contracts-check` fails on drift. Only the platform role runs `make contracts-sync contracts-generate`.
- `make lint [MOD=<name>]` (ruff check and format check), `make format MOD=<name>` (only on paths you own),
  `make typecheck [MOD=<name>]`, `make typecheck-tests`, `make importlint`.
