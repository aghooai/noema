# Wave E2: supply-chain-infra fixer (defect 29 / SE13, defect 24, sprint B F5)

Role: supply-chain-infra fixer, 2026-10-11. No live Bedrock call (the wire mock only), no AWS resource (moto only), no
git write. Own stacks only: `noema-e2sc` (e2e, port base 30800), `noema-e2sc-fault` (30700), `noema-e2sc-perf`
(30932/30950/30979, load api 30990, mocks 30981-30982); image tag `e2sc`; trivy cache `~/.noema/e2-supply/`. All torn
down at the end (section 6).

## 1. Items

| Item | Fix | Regression test (fails before the fix) | Command | Result |
|---|---|---|---|---|
| Defect 29 / SE13: trivy api/worker 2 CRITICAL + 55 HIGH, media 4 + 94, unwaived | api/worker base moved to `python:3.12-slim-trixie` (Debian 13.7) + `apt-get upgrade` in the runtime stage; media kept on the latest bookworm point release (12.15) + `apt-get upgrade` (trixie's libmagic changes a media-pipeline verdict, section 2.2); one waiver per remaining CVE in `deploy/docker/.trivyignore.yaml` (justification + review date 2027-01-09); new gate `make image-scan` (trivy JSON + CycloneDX SBOM per image, fails on a CRITICAL that is not waived) | `tests/integration/infra/test_image_waivers.py` (2: one CVE + review date + statement per line; the SE13 parser waives every id until its date, none after): 2 failed without the file, 2 passed with it. Gate: `make image-scan` exit 2 with no waivers, with expired waivers, and on the old bookworm images; exit 0 on the new ones | `make image-scan IMAGE_TAG=e2sc IMAGE_SCAN_OUT=reports/qa/e2`; `make qa-security-sca QA_SEC_TAG=e2sc QA_SEC_REPORTS=reports/qa/e2/security`; `python -m tests.security.sca_gate reports/qa/e2/security deploy/docker/.trivyignore.yaml` | api/worker 0 CRITICAL + 44 HIGH (8 ids), media 4 + 94 (51 ids), **all waived, 0 open**; gate exit 0; **SE13 PASS** with the waiver file (pip-audit 0/243, npm 0/0). As wired today `qa-security-sca` still reads `reports/security/waivers.md` (absent): SE13 FAIL there until qa-security passes the file (section 5) |
| SBOMs | `make image-scan` writes CycloneDX per image | - | as above | `reports/qa/e2/sbom/noema-{api,worker,media}.cdx.json`: CycloneDX 1.7, 155 / 155 / 331 components |
| Defect 24: helpers read only metrics port 9464 per replica (counter deltas miss the 2nd worker) | bootstrap records `NOEMA_METRICS_PORT .. + WEB_CONCURRENCY - 1` per replica from the running container's environment (`endpoints.metrics` = one list per replica; an unpublished worker port is a bootstrap error); `kit.metric_samples` reads every worker of the replica (so `stackctl.metric_total`, `circuit_states`, `multipod.tripped`, `test_cache_isolation.mismatches` see all processes unchanged); the SE12 capture writes `metrics-<replica>-<worker>.txt`; `make/e2e.mk` pins the 2nd worker ports (+13/+14, section 3.2) | `tests/integration/infra/test_metrics_endpoints.py` (4, fake docker CLI + 4 local /metrics servers): 4 failed before, 4 passed after | `pytest tests/integration/infra/test_metrics_endpoints.py`; live: SEC-ISO-02 x 8, `make qa-fault` F2/F10, security stack tests, `make e2e` on 2 workers | live SEC-ISO-02 counter test: **1/8 passed with the old 9464-only view, 8/8 with every worker**; F2 PASS on 2 workers; e2e 52/52 |
| Sprint B F5: `run.py --api-image` limited to one process | image mode runs `--api-procs N` in the container: a pod launcher (`POD_LAUNCHER`, `python -c` in the image) shares one listening socket among N `tests.load.serve` processes with `WEB_CONCURRENCY=N`, forwards SIGTERM/SIGINT, per-process lag/GC files `out/api/api-<i>.{lag,gc}`; the runner waits for every metrics port, samples the N processes, scrapes every port | `tests/load/test_run_api_image.py` (2: the CLI accepts `--api-image` + `--api-procs 2`; the launcher shares one socket, passes the serve args and forwards SIGTERM to both processes): 2 failed before, 2 passed after | live: `python -m tests.load.run --state ~/.noema/stacks/noema-e2sc-perf.json --api-image noema-api:e2sc --api-procs 2 --plan 20:30 --warmup-s 15 --settle-s 5 --api-port 30990 --mock-port 30981 --stack-cpus ''` | exit 0: 90 turns, 0 errors; `api_procs` 2 in every sample; both processes served turns (independent counters on 30991 / 30992); both lag + GC files flushed at stop; container removed |

## 2. Defect 29 (SE13) in detail

### 2.1 Base evaluation (trivy 0.75.0, DB of 2026-10-11, HIGH/CRITICAL, every one without a fixed version)

| Image | Debian | CRITICAL | HIGH | Distinct ids |
|---|---|---|---|---|
| `python:3.12-slim-bookworm` (the pinned digest `34386ef0...` IS the latest bookworm tag) | 12.15 | 2 | 55 | 17 |
| `python:3.12-slim-trixie@sha256:a6e34c59...` (2026-10-06) | 13.7 | 0 | 44 | 8 |
| `noema-api` / `noema-worker`, before (`:qalead`) | 12.15 | 2 | 55 | 17 |
| `noema-api` / `noema-worker`, after (`:e2sc`, trixie + upgrade) | 13.7 | **0** | 44 | 8 |
| `noema-media`, before (`:qalead`) | 12.15 | 4 | 94 | 51 |
| `noema-media` on trixie (evaluated, not adopted) | 13.7 | 1 | 79 | 37 |
| `noema-media`, after (`:e2sc`, bookworm 12.15 + upgrade) | 12.15 | 4 | 94 | 51 |

`apt-get upgrade` found nothing to upgrade in either base today (the digests are 5 days old); it stays in the build so
point updates between digest bumps are picked up. CPython is 3.12.15 in both bases.

### 2.2 Why the media image stays on bookworm

On trixie `make test-media IMAGE_TAG=e2sc` gave 1 failed / 304 passed:
`tests/unit/media_pipeline/test_detect.py::test_text_naming_the_pdf_header_is_not_a_polyglot` (`Rejected(type_mismatch)`).
Cause, measured with python-magic in both images: libmagic 5.46 (trixie) reports `application/pdf` for `%PDF-` up to byte
1023, 5.44 (bookworm) only up to about byte 255. So a Markdown note quoting `%PDF-1.7` within its first KiB, which
media-pipeline documents as accepted (`src/noema/media/extractors/detect.py` docstring), would be rejected. That is
media-pipeline's code and test, not mine, so the media image keeps bookworm. With bookworm: test-media 305 passed, 8
skipped (as before). The move to trixie (about 15 HIGH and 3 CRITICAL fewer) is a follow-up once media-pipeline adopts
the 1 KiB window (section 5).

### 2.3 Waivers (`deploy/docker/.trivyignore.yaml`, 51 entries, review date 2027-01-09)

One entry per CVE id: `{id, expired_at, statement}` on one line, readable by trivy (`--ignorefile`, an expired entry
stops waiving) and by `tests.security.sca_gate.waived()`. The statements rest on facts I checked in the images:
- api/worker (8 ids: ncurses infocmp, systemd-homed, libacl, 4 x util-linux mount/nsenter, perl Archive::Tar): local
  tools or privileged callers that no Noema process runs. uid 10001, no-new-privileges (SUID void), cap_drop ALL,
  read-only root.
- media (43 more): `ldd` shows the system libxml2, libcurl, libldap, gnutls, libtiff reached only by the jailed
  `tesseract` child, which reads a Pillow-written PNG on stdin (`ocr.py`, `raster` = image/png) and its read-only
  Debian tessdata. glib, harfbuzz, expat, X11 and Xrender are linked only by pango/cairo/fontconfig for `text2image`
  (a training tool that never runs). Python's pyexpat bundles its own expat. Pillow bundles its own libtiff. nsjail
  does not link libmount. No venv extension links the flagged system libraries. There is no network (network_mode
  none, clone_newnet). No Noema code uses SQLite. MiniZip is not in zlib1g. tiffcrop, the curl CLI and python3-libxml2
  are not installed.
- Check: every HIGH/CRITICAL id of the three images is waived and no entry is unused (51 = 51).

### 2.4 Gate and image checks

| Check | Command | Result |
|---|---|---|
| gate, new images | `make image-scan IMAGE_TAG=e2sc IMAGE_SCAN_OUT=reports/qa/e2 IMAGE_SCAN_CACHE=~/.noema/e2-supply/trivy-cache` | exit 0; JSON lists the waived findings with their statements (`ExperimentalModifiedFindings`) |
| gate, no waivers | same with `IMAGE_WAIVERS=<empty list>` | exit 2: `CVE-2026-6653` (media) not waived |
| gate, expired waivers | same with every `expired_at` set to 2026-10-10 | exit 2 (same CRITICAL) |
| gate, old images | `IMAGE_TAG=qalead` with the waivers | exit 2: `CVE-2023-45853`, `CVE-2025-7458` (api, worker), `CVE-2026-58016` (media) |
| image tests on `:e2sc` | `tests/integration/infra/{test_images,test_sandbox_privileges}.py` with the tag changed, run from a gitignored copy (`reports/tmp/`, removed) | 14 passed (uid 10001, no media parsers, worker = api venv, composition builds, sandbox ladder) |
| media toolchain | `make test-media IMAGE_TAG=e2sc` | 305 passed, 8 skipped |
| sandbox | `make sandbox-selftest IMAGE_TAG=e2sc` | checks 1-10 PASS |
| upload fuzz (SE9) | `make qa-security-fuzz QA_SEC_TAG=e2sc` | 6 passed (660 s): 415 jail runs, 0 escaped, egress EAFNOSUPPORT, interfaces `lo`; one atheris pdf.sanitize crash input contained (jail: `rejected corrupt`) |
| app on the new images | `make e2e E2E_PROJECT=noema-e2sc E2E_PORT_BASE=30800 IMAGE_TAG=e2sc E2E_BUILD=0` (2 workers per api pod) | 52 passed (156.7 s) |

## 3. Defect 24 in detail

### 3.1 Change

- `tools/bootstrap_dev.py`: `service_env()` reads a running service's container environment (`docker compose ps -q`,
  `docker inspect`). `discover()` records, per api replica, `NOEMA_METRICS_PORT .. + WEB_CONCURRENCY - 1`
  (composition.serve_metrics: each uvicorn worker serves the first free port) and raises `BootstrapError` when a worker
  port is not published (the endpoint count is replicas x workers, or the bootstrap fails). `endpoints()` builds the
  state file's `endpoints`: **`metrics` is now one list per replica**, e.g. `[["…:30811", "…:30813"], ["…:30812",
  "…:30814"]]`.
- `tests/e2e/suite/kit.py`: `metrics_urls(stack, replica)` (it also reads a state file written before this change, one
  URL per replica); `metric_samples()` returns the samples of every worker in worker order. Sums are replica totals
  (counters), and the first sample of a label set is worker 0's (gauges, as before).
  `tests/fault/stackctl.py` and `tests/security/stack/test_cache_isolation.py` needed no edit: they go through
  `metric_samples`.
- `tests/security/stack/capture.py`: the SE12 capture writes every worker of every replica (`metrics-<r>-<w>.txt`).
- `make/e2e.mk` (**outside my file list, see 3.2**): `NOEMA_API1_METRICS2_PORT` / `NOEMA_API2_METRICS2_PORT` = base +13
  / +14.

### 3.2 The e2e.mk edit (out of my list, flagged)

First `make e2e` with the change: 32 passed, **20 errors** after `test_shutdown`. That test `docker start`s api-1
again, and the 2nd worker's port 9465 was published on an ephemeral host port (e2e.mk pinned only 9464), so it moved
(measured 33370 -> 33373) and every later `heal_circuits` scrape got `ConnectError`. Before this change the helpers
read only the pinned 9464, so this is a regression my fix would cause in `make e2e` and in `qa-security` (which runs
`make e2e` first). The alternatives in my paths were a reconnect-and-rediscover workaround in the kit, or reading
fewer workers (which is defect 24 itself). So I added the two port variables, next to the existing ones, and updated
the port comment. After that: `make e2e` 52/52, recorded ports 30811/30813 and 30812/30814. The integrator (owner)
may move or reword it.

### 3.3 Live evidence (own stacks, 2 uvicorn workers per api pod, images `:e2sc`)

| Check | Command | Result |
|---|---|---|
| SEC-ISO-02 counter (engines finding 23's flaky half) | `pytest tests/security/stack/test_cache_isolation.py::test_forged_cross_tenant_cancel_emits_a_security_event -m e2e`, 8 runs each, state with the old one-URL-per-replica `metrics` vs the new state | old: **1 passed, 7 failed** (`assert 0.0 > 0.0`: the event counted on port 9465); new: **8 passed** |
| security stack tests | `pytest tests/security/stack -m e2e --deselect tests/security/stack/test_ui_csp.py` (no UI proxy) | 27 passed, 2 failed, neither mine: `test_authz_matrix` (`resumeTurnEvents` 501 before V(turn), edge #22) and `test_media_injection` (an untracked file another fixer created at 21:05, datamark check). `test_cache_isolation` 3/3 and `test_zz_secrets_scan` pass; the capture holds `metrics-0-0/0-1/1-0/1-1.txt` |
| F2 / F10 counter deltas | `make qa-fault FAULT_PROJECT=noema-e2sc-fault FAULT_PORT_BASE=30700 FAULT_IMAGE_TAG=e2sc FAULT_TESTS="tests/fault/test_f02_access.py tests/fault/test_f10_redis_down.py"` | F2 3/3 PASS (`tenant_access_failures_counted` 2.0 per variant, summed over the 4 processes). F10 down/hung: the counter half passes (`admission_degraded` 3.0, i.e. 3 of 4 processes, L2 misses 255/251, 125/125 turns completed) but the test fails at `same_pod_cancel == 202` (got 500; with 2 workers a "same pod" cancel can land on the other process, which needs Redis) and at cross-pod attach 500 (edge #25). Neither comes from this change; the F10 owner has to decide what "same pod" means with 2 workers (section 5) |
| bootstrap without the app profile | `python -m tools.bootstrap_dev --compose-project noema-e2sc-perf` (base services only) | `metrics: []`, no error |

## 4. Sprint B F5 and the final checks

`tests/load/run.py` (image mode only): `POD_LAUNCHER` + `start_api_container` with `--api-procs N` (`WEB_CONCURRENCY=N`
in the env file, a wait for every metrics port, `child_pids()` of the launcher = the sampled processes), the analysis
also finds `api/api-*.lag`, the step scrape covers `--api-procs` ports, and the `--api-procs` refusal is gone. N = 1
keeps the old single-process command. Live run in section 1 (`/tmp/noema-e2sc/load/img-2procs`, raw data outside the
repo).

| Check | Command | Result |
|---|---|---|
| lint / format / types of my files | `ruff check`, `ruff format --check`, `mypy --strict` on the 7 changed or new Python files | clean |
| make check (full, final tree) | `make check` | every step green except `test-integration`: 560 passed, 21 skipped, **1 failed**, `tests/integration/api/test_schemathesis_smoke.py::…[POST /v1/files]` (`SerializationNotPossible: image/png`), caused by another fixer's in-progress contract edit (`openapi.yaml` `encoding.file.contentType`, 20:42 UTC; not my paths). Unit 3278 passed, conformance 757, my infra tests 4 + 2 + compose 21 + images 7 passed in the run; `sdk-check` (after the failed step) run separately: 192 passed |
| new tests | `pytest tests/integration/infra/test_metrics_endpoints.py tests/integration/infra/test_image_waivers.py tests/load/test_run_api_image.py tests/load/test_harness.py` | 4 + 2 + 2 + 14 passed |
| SE13 | section 1 | PASS with the waiver file |

## 5. Not fixed here / hand-offs (outside my paths)

1. **qa-security (`make/qa-security.mk`)**: SE13 in `make qa-security-sca` still reads the default
   `reports/security/waivers.md`, which does not exist, so it reports `waived ids: 0; SE13 FAIL`. One-word fix: call
   `python -m tests.security.sca_gate $(QA_SEC_REPORTS) deploy/docker/.trivyignore.yaml`, or pass
   `--ignorefile` to its trivy runs. spec-09 §9 names `reports/security/waivers.md`; the spec agent should record the
   new location (or a pointer).
2. **media-pipeline**: decide whether `detect`/`intake` accept a strict-UTF-8 Markdown note whose `%PDF-` sits within the
   first KiB when libmagic >= 5.45 says `application/pdf`, and update `test_detect.py` accordingly. After that, infra
   can move `noema-media` to trixie (measured 1 CRITICAL + 79 HIGH, 37 ids, against 4 + 94, 51 ids today; drop the 14
   bookworm-only waivers).
3. **integrator**: `docs/modules/integration.md` §5.1 ("the e2e helpers read 9464") and §6 (`endpoints.metrics` is now
   one list per replica) and §7 (ports +13/+14, i.e. 28113/28114 at the default base) need the update; also accept
   or re-home the `make/e2e.mk` edit (section 3.2).
4. **infra docs / qa-tools docs**: `docs/modules/infra.md` §1 (api/worker on `python:3.12-slim-trixie@sha256:a6e34c59...`,
   Debian 13; media stays on bookworm and why; `make image-scan`; the waiver file) and `tests/load/README.md`
   (`--api-procs` is no longer "venv only") are not in my file list.
5. **F10 owner (QA fault / api)**: with 2 workers per api pod, `same_pod_cancel` (expected 202) can reach the other
   worker of the owning container. With Redis down that needs the same relay as cross-pod, so it answered 500 in both
   variants here. Either aim the probe at the owning process, run F10 with `NOEMA_STACK_API_WORKERS=1`, or redefine
   "same pod" as the process (`pod_id` = host-pid). The cross-pod attach 500 is edge #25.
6. Waiver upkeep: review by 2027-01-09. The trivy DB changes daily, so a new CRITICAL fails `make image-scan` until it is
   fixed (`apt-get upgrade` picks up Debian fixes on rebuild) or waived with a statement.
7. Cosmetic: in image mode the sampler cannot read the container processes' fd tables (uid 10001), so `fds` is -1 per
   process and sums to -2 with 2 processes (the single-process mode already reported -1).

## 6. Files and teardown

Changed: `deploy/docker/api.Dockerfile` (trixie digest, `apt-get upgrade`), `deploy/docker/media-sandbox.Dockerfile`
(`apt-get upgrade`, why bookworm), `make/infra.mk` (`image-scan`, `IMAGE_SCAN_*`, `IMAGE_WAIVERS`),
`tools/bootstrap_dev.py`, `tests/e2e/suite/kit.py`, `tests/security/stack/capture.py`, `tests/load/run.py` (image mode),
`make/e2e.mk` (2 port variables, flagged). New: `deploy/docker/.trivyignore.yaml`,
`tests/integration/infra/test_metrics_endpoints.py`, `tests/integration/infra/test_image_waivers.py`,
`tests/load/test_run_api_image.py`. The two new tests under `tests/integration/infra` (infra-owned) and the one under
`tests/load` sit outside the brief's list, because the quality bar needs a regression test per fix. Reports:
`reports/qa/e2/sbom/noema-{api,worker,media}.cdx.json`, `reports/qa/e2/trivy-noema-{api,worker,media}.json` (with the
waived findings), `reports/qa/e2/security/` (the `qa-security-sca` inputs of the SE13 run: pip-audit, npm audit, trivy,
gitleaks), this report.

Teardown: `noema-e2sc` (`make e2e-down E2E_PORT_BASE=30800`), `noema-e2sc-fault` (`make qa-fault-down`, both the
aborted 34300 attempt and 30700), `noema-e2sc-perf` (`docker compose down -v`): no container or volume left, the
stacks' state files deleted, the load container removed by the runner. Left on purpose: images `noema-{api,worker,
media,media-test}:e2sc` (the trixie evaluation tags are removed), the trivy cache `~/.noema/e2-supply/`, raw run data in
`/tmp/noema-e2sc/`. The shared `noema-dev` services were not touched.
