# infra: images, compose profiles, media test runner (Wave B2)

Owner: infra (then the integrator). Paths: `deploy/**`, `make/infra.mk`, `tests/integration/infra/**`.
Sources of truth: ADR-002 (sandbox), spec-01 §4 (processes), spec-10 §1 (test layers and isolation).

## 1. Images (`make images`; tag `IMAGE_TAG=dev`)

| Image | Dockerfile (target) | Contents | User | Size | Cold build |
|---|---|---|---|---|---|
| `noema-api:dev` | `deploy/docker/api.Dockerfile` | Debian 13 slim + CPython 3.12.15 from python-build-standalone in `/opt/python` (Wave F, IMPL-089); `/opt/noema/venv` = `uv sync --frozen --no-dev` + the noema wheel; **no media group** (importing PIL, pikepdf, docling, torch, magika, magic, markdown_it, ... fails); CMD uvicorn `--factory` with `--timeout-graceful-shutdown 35` (C2b) | 10001:10002 | 372 MB | 20 s (2026-10-10; not re-measured on the Wave F base) |
| `noema-worker:dev` | `deploy/docker/worker.Dockerfile` | `FROM noema-api` (identical venv); CMD `python -m noema.entrypoints.jobs --kinds labeling.*,session.delete,retention.sweep` | 10001:10002 | 372 MB (shared layers) | < 1 s |
| `noema-media:dev` | `deploy/docker/media-sandbox.Dockerfile` (`media-sandbox`) | Debian 12; nsjail 3.6 (bundled kafel); tesseract 5.3 + eng; libmagic1 + libmagic-mgc; qpdf 11.3; `/opt/noema-sandbox/venv` = runtime deps + `media` group + noema wheel (2.0 GB, torch CPU); Docling models in `/opt/docling-models` (layout heron + TableFormer, 669 MB); `deploy/sandbox/*` in `/etc/noema-sandbox/`; CMD `python -I -m noema.media.sandbox.sandboxd` | root (sandboxd) | 2.89 GB | 112 s |
| `noema-media-test:dev` | same file (`media-test`) | `noema-media` + dev and fuzz groups (pytest, atheris, ...) + the current `src/` (re-installed as a wheel) + `tests/`, `conftest.py`, `pyproject.toml` in `/opt/noema-test` | root | 3.37 GB | 128 s |

Cold = `docker build --no-cache` with a warm uv download cache on this host (2026-10-10). The media build's long
steps: the locked venv (44 s), nsjail (apt 27 s + clone/build 6 s, in parallel), the Docling models (9 s), the image
export (43 s). A source-only change re-runs only the last `noema-media-test` layers.

- Base images are pinned by digest. api and worker: `debian:trixie-slim@sha256:a29215f6...`, `ghcr.io/astral-sh/uv:0.13.0@sha256:cdc60931...` and `PYTHON_VERSION=3.12.15` (Wave F, IMPL-089). media: `python:3.12-slim-bookworm@sha256:34386ef0...`, `ghcr.io/astral-sh/uv:0.9.10@sha256:29bd4509...`. nsjail by tag and commit.
- Every build context is the repo root, filtered by an allowlist next to each Dockerfile (`<name>.Dockerfile.dockerignore`): `.venv`, `.git` and caches never enter an image.
- Python: the api and worker images run the python-build-standalone CPython that `uv python install` puts in `/opt/python` (checksum-verified by uv; Tcl/Tk, the GUI modules, pip, the headers and the shared libpython removed), then sync with `UV_PYTHON_DOWNLOADS=never`. Its statically linked OpenSSL, SQLite, zlib and other libraries are invisible to trivy's Debian scan: a CVE in them needs a `PYTHON_VERSION` / `UV_IMAGE` bump (reports/perf/sizing.md §7 risk 6, §8). The media images run `/usr/local/bin/python3` of their base image (`--python /usr/local/bin/python3`, `UV_PYTHON_DOWNLOADS=never`). Bytecode is precompiled (`UV_COMPILE_BYTECODE=1`) because every root filesystem is read-only.
- Media image process environment: `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `DOCLING_ARTIFACTS_PATH=/opt/docling-models`, `TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata`, `NOEMA_SANDBOX_POLICY=/etc/noema-sandbox/seccomp.kafel`. Jails do not inherit it (`keep_env: false`): pass the ADR-002 §3 `envar` lines.

## 2. Compose (`deploy/compose/docker-compose.yml`, project `noema-dev`)

| Profile | Services | Use |
|---|---|---|
| (none) | postgres, valkey, moto | the B1 base (`make services-up`) |
| `toxiproxy` | `toxiproxy-host` (host network, API `127.0.0.1:28474`) | fault injection from host-run tests (§4) |
| `media` | `media-sandbox` | sandboxd development, `make test-media`, `make sandbox-selftest` |
| `app` | `api-1`, `api-2`, `jobs`, `media-worker`, `media-sandbox`, `mockbedrock`, `toxiproxy` | the e2e/security/load/UI stacks (`make e2e-up`, docs/modules/integration.md) |
| `fault` | `toxiproxy` (compose network; proxies `postgres` :15432, `valkey` :16379, `moto` :15000 from `deploy/compose/toxiproxy.json`) | the spec-10 §4 fault layer (also in `app` since C2b) |

- `make up [PROFILES="toxiproxy media"]` (default `toxiproxy`) starts the base services plus the profiles and waits until healthy; `make down` stops every profile (volumes kept). For the `app` profile use `make e2e-up` instead (its own project, ports and bootstrap, docs/modules/integration.md §7): `make up PROFILES=app` would run the app in `noema-dev` against the shared services, unseeded.
- All host ports bind `127.0.0.1`. Profiled services publish **ephemeral** host ports (`NOEMA_*_PORT` default `0`): read them with `docker compose -p <project> -f deploy/compose/docker-compose.yml port <service> <container port>`.
- **Isolated stacks** (spec-10 §1.1, `-p noema-<layer>-<RUN>`): set every port variable to 0 so the base services do not collide with `noema-dev`, e.g.
  `NOEMA_PG_PORT=0 NOEMA_VALKEY_PORT=0 NOEMA_MOTO_PORT=0 docker compose -p noema-e2e-$RUN -f deploy/compose/docker-compose.yml --profile app up -d --wait`, and `down -v` in a finalizer. `tests/integration/infra/helpers.py` (`Compose`, `PORT_VARIABLES`, `project_name`) and the `isolated_compose` fixture do exactly this.
- Image overrides: `NOEMA_API_IMAGE`, `NOEMA_WORKER_IMAGE`, `NOEMA_MEDIA_IMAGE`; `NOEMA_STACK_ENV` (default `dev` since C2b/D; `test` gives each process its own Redis namespace and no `/metrics`, so the two api replicas share nothing and the worker healthchecks fail) is the `NOEMA_ENV` of the profiled containers. `NOEMA_CONTRACT_VALIDATE` is passed through from the caller (`1` or unset, IMPL-041).

### 2.1 Container settings (ADR-002 §2)

| Service | Network | Capabilities | Other |
|---|---|---|---|
| `media-sandbox` | `network_mode: none` | drop ALL + `SYS_ADMIN, SETUID, SETGID, CHOWN, DAC_OVERRIDE, FOWNER, KILL` | outer seccomp `deploy/sandbox/docker-seccomp.json` (Docker default + `pivot_root`), `no-new-privileges`, read-only root, tmpfs `/tmp`, `/spool` = volume `media-spool`, 6 GiB / 2 CPUs / 1024 pids, env `NOEMA_ENV` only; healthy once `/spool/sandboxd.sock` is a socket |
| `media-worker` | compose network | drop ALL | uid 10001:10002, read-only root, `/spool`, 1 GiB / 256 pids; waits for a healthy `media-sandbox` |
| `api-1`, `api-2`, `jobs`, `mockbedrock` | compose network | drop ALL | uid 10001:10002, read-only root, `no-new-privileges`, `init: true`; hostnames `api-1`/`api-2`/`jobs`/`media-worker` (the pod ids); api healthcheck `GET /readyz` on :8000, workers `GET /metrics` on :9464; stop grace 45 s (api) / 35 s (workers: 10 s handlers + 10 s flush + 10 s cancelled worker), spec-11 §7 |

`media-spool` is a tmpfs volume (`size=4g,uid=10001,gid=10002,mode=0711`). The app containers get the spec-05 §5
variables, endpoint overrides for all five keys (moto, `http://mockbedrock:8080`, Mantle `.../v1`) and fake AWS keys
that only work against moto. Why each sandbox privilege is needed (and which are not): `deploy/sandbox/PRIVILEGES.md`.

## 3. Media tests (media-pipeline, media-negotiation, qa-security)

- `make test-media` builds `noema-media-test` from the working tree and runs
  `pytest tests/unit/media_pipeline tests/integration/media_pipeline tests/integration/infra/media_image` (the ones that exist)
  inside it, through `docker compose run media-sandbox` in a throwaway project: **no network**, the sandbox
  capabilities and seccomp profile, read-only root, as container root (so tests can spawn nsjail). Narrow it with
  `MEDIA_TESTS="tests/unit/media_pipeline/test_x.py"` and `PYTEST_ARGS='-k ...'`; `XDIST=n` works. Exit 5 (no tests) counts as success.
- `/codes/aghooai/probes/fixtures` is mounted read-only at the same path (spec-07 MED-02: `number.png` → "4471", `invoice.pdf`).
- Gate image-only tests with `pytest.mark.skipif(os.environ.get("NOEMA_MEDIA_IMAGE") != "1", ...)` (set only in `noema-media-test`), so `make test MOD=media_pipeline` on the host skips them.
- The root filesystem is read-only: write under `tmp_path` or `/tmp`; `HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis` and `PYTHONDONTWRITEBYTECODE=1` are preset, and the runner passes `-p no:cacheprovider`.
- Jails see the venv at `/opt/noema-sandbox/venv` (noema installed as a wheel, so `python -I -m noema.media.extractors.entry` works with only `/opt/noema-sandbox` bind-mounted) and the models at `/opt/docling-models`.
- **NsjailRunner template**: start from `deploy/sandbox/selftest.py: jail_config()` (a working ADR-002 §3 jail) and the corrections in PRIVILEGES.md (tmpfs flags as fields; `rlimit_nproc_type`/`rlimit_stack_type: VALUE`; policy via `--seccomp_policy $NOEMA_SANDBOX_POLICY`). Docling converts `invoice.pdf` inside such a jail with `rlimit_as_type: INF` and no `/proc`.
- `make sandbox-selftest` runs ADR-002 §2 checks 1-10 (SEC-SBX-01) in `noema-media` under the compose settings; `selftest.sh --json` gives a machine-readable report.

## 4. Fault injection from host-run tests (toxiproxy-host)

One host-network toxiproxy is shared by every agent (`make up` starts it). Create a proxy named after your
`noema_run_id`, listening on `127.0.0.1:0`; toxiproxy returns the bound port. Never touch another run's proxy.

```python
api = httpx.Client(base_url=f"http://127.0.0.1:{os.environ.get('NOEMA_TOXIPROXY_PORT', '28474')}")
api.post("/proxies", json={"name": f"{noema_run_id}-pg", "listen": "127.0.0.1:0",
                          "upstream": "127.0.0.1:25432", "enabled": True}).raise_for_status()
port = int(api.get(f"/proxies/{noema_run_id}-pg").json()["listen"].rsplit(":", 1)[1])
# ... connect through 127.0.0.1:{port}; cut it:   api.post(f"/proxies/{name}", json={"enabled": False})
# ... add latency: api.post(f"/proxies/{name}/toxics", json={"type": "latency", "attributes": {"latency": 300}})
api.delete(f"/proxies/{noema_run_id}-pg")   # in a finalizer
```

Skip (not fail) when `GET /version` is unreachable, as `tests/integration/infra/test_toxiproxy.py` does.

## 5. Integrator (Wave D) and QA (Wave E)

- Since C2b the `app` profile runs: the composition wires real components only (no `tests.fakes` in the images),
  the entrypoints exist, and `tools/bootstrap_dev.py` seeds moto (bucket, `noema/db-dsn`, `noema/db-dsn-host`, the
  peppers, ExternalIds, invoke roles) and Postgres (roles, migration, operators, config, tenants, keys). Use
  `make e2e-up` / `e2e-bootstrap` / `e2e-smoke` / `e2e-down` (`make/e2e.mk`, docs/modules/integration.md): its own
  project `noema-e2e`, fixed ports from `E2E_PORT_BASE` (28100), `NOEMA_STACK_ENV=dev`.
- `x-noema-app` sets `NOEMA_REDIS_URL` (`${NOEMA_STACK_REDIS_URL:-redis://valkey:6379/0}`), the moto endpoint
  overrides (`${NOEMA_STACK_MOTO_URL:-http://moto:5000}`), `NOEMA_S3_BUCKET`, `NOEMA_DB_DSN_REF=noema/db-dsn`,
  `NOEMA_DRAIN_DELAY_S` and depends on a healthy toxiproxy too. `mockbedrock` mounts `tools/`, `tests/fakes/` and
  `deploy/compose/mockbedrock/` read-only and starts with `--scenario .../${NOEMA_MOCK_SCENARIO:-default.yaml}`; its
  healthcheck is the O(1) `GET /_mock/requests?limit=0`. The media-worker runs `--kinds media.process,file.purge`.
- Fault runs: `NOEMA_STACK_REDIS_URL=redis://toxiproxy:16379/0`, `NOEMA_STACK_MOTO_URL=http://toxiproxy:15000` and
  the bootstrap `--db-host toxiproxy:15432` (`make e2e-up E2E_DB_HOST=toxiproxy:15432`); toggle the proxies with the
  API published on `docker compose port toxiproxy 8474` (28174 for the default e2e stack).
- SEC-SBX-02 needs `network_mode: none` from `docker inspect` (asserted in compose config by `tests/integration/infra/test_compose.py`); trivy/SBOM scans take the three images above.

## 6. Tests (`make test-integration MOD=infra`, `make test-media`)

| File | What | Needs |
|---|---|---|
| `test_compose.py` | profiles, loopback-only and ephemeral ports, ADR-002 §2 settings, the spool volume, endpoint overrides, the outer seccomp profile; C2b: the app processes (kinds, pod ids, worker grace, toxiproxy dependency), the mock's scenario mount, the fault routing variables | docker CLI |
| `test_toxiproxy.py` | per-run proxy on the host toxiproxy (cut, restore, latency); the `fault` toxiproxy in an isolated project | `make up`; docker |
| `test_images.py` | no media parsers or dev tools in noema-api; uid 10001; worker = api venv; media image offline env, sandbox files, wheel install; C2b: the graceful-shutdown timeout, the real composition builds in the api/worker images without `tests` | `make images` |
| `test_sandbox_privileges.py` | the PRIVILEGES.md ladder: compose settings pass; Docker-default seccomp, or no SYS_ADMIN/SETUID/SETGID, fail; unconfined and no-KILL pass | `make images` |
| `media_image/test_media_toolchain.py` | in the image: tesseract → 4471, pikepdf + qpdf, pypdfium2, Docling → Markdown offline, HEIC via pillow-heif, libmagic + Magika, nsjail in a network namespace, the selftest | `make test-media` |
