# Release gate: Noema Phase 1 MVP (QA lead, 2026-10-13)

Role: release QA lead. Rules: Bedrock-only, **no live Bedrock call** (the wire mock and moto only; the live ledger is
untouched at 85 of 87), no git write command, edits only under `tests/**` and `reports/**` (section 6). Every stack
below had its own compose project and a port base **below 32768** (the host's ephemeral range is 32768-60999), and
every one was torn down at the end; `noema-dev` was kept (section 8). Raw logs: `/tmp/noema-rg/logs/` on the build
host; results files: `reports/qa/release-gate/`.

Inputs read: [../mvp-acceptance.md](../mvp-acceptance.md) §7.1 and §7.2, [wave-e-summary.md](wave-e-summary.md),
[../perf/sizing.md](../perf/sizing.md).

## 1. Gates (the returned list)

**Verdict.** Every requested gate passes on images built from the release tree (HEAD `10aa2d4` = tag `wave-f`),
with two test-harness fixes by this role (section 6). No product defect was found and no product code was changed.

Two first runs were red, and both are disclosed with their root cause:
- `qa-security`'s SCA part: PyPI was unreachable. The part was re-run green.
- `qa-fault`'s F10 [hung]: the test's own same-pod cancel probe does not hold on 2-process pods. Every spec-10
  criterion passed. After the fix, F10 passed 3 times and the full suite passed 22/22 on a fresh stack.

This meets condition 1 of the acceptance verdict ([../mvp-acceptance.md](../mvp-acceptance.md) §1: a re-gate on
the Wave F tree's own images) and closes §7.1 item 8.

The 06 §11 Phase 1 tally is **unchanged: 50 PASS, 1 FAIL (K1), 12 NOT-RUN**:
- K1 and every [P0] row need live calls or Phase 0 data, and none are allowed here.
- L1 as written at 200 streams per pod still misses p50; the sized density passes.

| Gate | Result | Evidence (section) |
|---|---|---|
| Images from the current tree (`make e2e-images image-media-test IMAGE_TAG=rgate`, `E2E_BUILD=1`) | **PASS** | 331/331 `noema/**/*.py` in the api and media images equal to the tree; CPython 3.12.15 (PBS) (§2) |
| `make check` (incl. ownership), start and end | **PASS** | all green twice; ownership: Wave F set 108/108, all tracked 1,989/1,989, end-state changes 38/38 at the final `make check` and 84/84 at 03:48 (§3, §5) |
| `make e2e` run 1 (fresh stack) | **PASS** | 52 passed, 154.9 s (§3) |
| `make e2e` run 2 (fresh stack) | **PASS** | 52 passed, 155.7 s (§3) |
| `make ui-check` | **PASS** | tsc, vitest 75/75, build, e2e helpers (§3) |
| `make ui-e2e` (fixture API and e2e stack) | **PASS** | 12/12 and 12/12, 55 responses contract-valid each (§3) |
| `make qa-contract` (Q1, Q2, Q3, X1 part) | **PASS** | 131 passed; schemathesis 6,160 + 1,735 calls, 0 failures; 0 ContractValidator violations; docs-drift 8/8, sdk-compat 65/65 (§3) |
| `make qa-security` (SE1-SE12 parts) | **PASS** | local 72; stack 52 + 12 + 31 (SE1 356/356 cells, 0 leaks); fuzz 6/6, no escape, no egress (§3.1) |
| SE13 SCA / trivy gate with the waivers and the SBOMs | **PASS** (re-run; first try FAIL on a PyPI connection error) | pip-audit 243 / 0 vulnerable, npm 0 / 0, trivy 0 unwaived ids (51 waived), SBOMs 148 / 148 / 331 (§3.1) |
| `make qa-fault` (F1-F12, S1a, S1b) | **PASS** (final run 22/22; first run 21/22, harness fix) | §3.2, §3.3, §5 |
| `make qa-gates` (L2, L3, Q8, L5, R4, Q9, SE7, M3, ownership) | **PASS** | all green; L5 p99 927.5 ms; Q9 1.0 / 0.0; M3 CER 0.0; 9 of 13 holdback cells invalid by mock lateness, bounds met (§3.4) |
| `make test-media` | **PASS** | 307 passed, 8 skipped (§3) |
| `make observability-check` | **PASS** | promtool 25 alerting + 19 recording rules, 4 test files, 84 dashboard queries, pytest 139 (§3, §5) |
| `NOEMA_OBS_RUNBOOKS=1 make observability-check` | **PASS** | same, 139 passed (§3, §5) |
| `make image-scan` | **PASS** | exit 0, 0 unwaived CRITICAL (§3) |
| C2 ramp 0 -> 1,000 in the container | **PASS** | lag p99 1.9 / 3.5 / 4.1 / 4.9 ms to 200 per pod; 0 x 5xx to 1,000 (§4.2) |
| C3 slow clients in the container | **PASS** | others 1.001 [0.997, 1.006]; 60/60 `client_too_slow`; queue 1,048,270 B <= 1 MiB (§4.4) |
| C5 flooding tenant in the container (2 runs) | **PASS** | 1.020 [1.017, 1.022], 1.031 [1.026, 1.036]; 0 transport errors (§4.3) |
| L1 confirmation at 100 streams per pod (container) | **PASS** | p50 9.21 [9.18, 9.23], p95 12.64 [12.45, 12.81] ms; slots gauge 50 + 50 (§4.1) |
| `check_docs.sh` (design repo), start and end | **PASS** | exit 0 twice (§3, §5) |
| Teardown (own stacks; `noema-dev` kept) | **PASS** | 0 containers, volumes and networks in all 7 projects (§8) |

## 2. Setup

- **Code under test.** HEAD `10aa2d4` ("Wave F: observability as code, docs set, design rev 7, acceptance
  report"). The brief describes the basis as "tag wave-e3 plus the uncommitted Wave F work"; at the start of this
  gate (2026-10-12 23:54 UTC) that work was already **committed and tagged `wave-f`** (`wave-f` = HEAD,
  `git status --porcelain` empty; `git diff --stat wave-e3 HEAD`: 108 files). Checked in the tree: the api image is
  built on python-build-standalone CPython 3.12.15 (`deploy/docker/api.Dockerfile`: `uv python install`,
  `PYTHON_VERSION=3.12.15`, uv 0.13.0), and `aghoo_turn_slots_held` is registered
  (`src/noema/telemetry/registry.py:143`) and exported every 1 s (`src/noema/api/overload.py`, `api/app.py:184`).
- **Product digest** (sha256 over the sorted per-file sha256 of `src deploy tools`, no `__pycache__`; the E2/E3
  method): `aa9568ec8e7773a0` at 23:54 UTC; **`678b7d68ced8ea33` from 23:57 UTC to the end**. The only product-path
  change is a docstring in `tools/bootstrap_dev.py` ("revision 0002" -> "`0003_operator_proof`", the §7.2 item),
  made by **another role working on the same tree during this gate**, which also edited docs only
  (`docs/IMPL-DECISIONS.md`, `docs/architecture.md`, `docs/impl-spec/OWNER-CHECKPOINT.md`, `docs/modules/infra.md`,
  `docs/modules/integration.md`). `src/` and `deploy/` did not change during the gate (watcher every 60 s,
  `/tmp/noema-rg/logs/tree-watch.log`). Those edits are not this role's and are not reviewed here.
- **Images** `noema-{api,worker,media,media-test}:rgate`, built from the current tree at 23:54-23:56 UTC with
  `make e2e-images image-media-test IMAGE_TAG=rgate` and rebuilt (cache hit) by the first `make e2e` with
  `E2E_BUILD=1 IMAGE_TAG=rgate`: api `sha256:3863d2741b72`, worker `sha256:40b5e78dc0e2`, media
  `sha256:8f27a96517b5`, media-test `sha256:0dc3f25a214d`. **Every one of the 331 `noema/**/*.py` files in the api
  and media images has the tree's sha256** (`find | sha256sum`, diffed: 0 lines). Interpreter in the api image:
  CPython 3.12.15, clang 22.1.3, `python3.12` with no `libpython` in `ldd` (static, as sizing §8 says).
- **Deployment as configured**: 2 api processes per pod (`WEB_CONCURRENCY=2`) in every stack and in every load run.
- **Own stacks** (project, port base): `noema-rg-e2e` 21100, `noema-rg-e2e2` 21200, `noema-rg-fault` 21300,
  `noema-rg-contract` 21400, `noema-rg-sec` 21500 (UI proxy 21590, Playwright 21595), `noema-rg-gates` 21600; load
  stack `noema-rg-perf` (postgres 22032, moto 22050, valkey 22079; api 22090 with metrics 22091/22092, mocks
  22081/22082); `make ui-e2e` on the fixture API at 21290 and against `noema-rg-e2e` at 21295. Images for every stack:
  `NOEMA_{API,WORKER,MEDIA}_IMAGE=noema-*:rgate` (or the target's tag variable = `rgate`), `*_BUILD=0` after the
  build above.
- **Batches** (the E3 QA lead's order): B1 in parallel = `make check`, `observability-check` x2, `check_docs.sh`,
  `ui-check`, then `e2e` #1 + `test-media` + `image-scan`, `ui-e2e` x2, `e2e` #2 + `qa-contract`; then `qa-security`,
  `qa-fault` and `qa-gates` each alone; then the load runs, serialized on a quiet host; then a final `make check`.

## 3. Suite results on the release tree

| Target (command) | When (UTC) | Result | Evidence |
|---|---|---|---|
| `NOEMA_AGENT_ID=rgate make check XDIST=4` | 23:54:59-23:57:01 | **PASS** "make check: all green" | ruff check all passed, `ruff format --check` 920 files; mypy --strict 327 src / 594 test files; import-linter 27 kept, 0 broken; forbidden-scan clean; ownership (clean tree: "no changed paths"); contracts-check 70 files identical + generated files up to date; unit 3,588 passed / 1 skipped; conformance 757 / 8 skipped; integration 573 / 21 skipped; sdk-check TS 86 + 6 skipped, Python 192 + 1 skipped |
| ownership of the Wave F change set (`tools/check_ownership.py --paths $(git diff --name-only wave-e3 HEAD)`) | 23:58 | **PASS** | 108 of 108 paths owned, exit 0 |
| `make qa-ownership-all` (every tracked path) | 23:58 | **PASS** | "tracked paths: 1989, unowned: 0" |
| final `make check` on the end-state tree | section 5 | see section 5 | - |
| `make e2e E2E_PROJECT=noema-rg-e2e E2E_PORT_BASE=21100 E2E_BUILD=1 IMAGE_TAG=rgate` (fresh stack #1) | 23:58:14-00:01:20 | **PASS** | **52 passed** in 154.9 s; api-1/api-2 `noema-api:rgate`, `WEB_CONCURRENCY=2` |
| `make e2e E2E_PROJECT=noema-rg-e2e2 E2E_PORT_BASE=21200 E2E_BUILD=0` (fresh stack #2, #1 torn down first) | 00:03:56-00:07:01 | **PASS** | **52 passed** in 155.7 s |
| `make ui-check` | 23:57:54-23:58:08 | **PASS** | `tsc --strict`, vitest **75/75** (7 files), vite build, ruff + `mypy --strict` on `web/e2e` (3 files) |
| `NOEMA_UI_PORT=21290 make ui-e2e` (fixture API) | 00:02:26-00:02:40 | **PASS** | Playwright **12 passed** (W1, W2, W4, W7, SEC-UI-01..03); 55 proxied responses contract-validated, 0 failures |
| `NOEMA_UI_PORT=21295 make ui-e2e UI_STACK_STATE=~/.noema/stacks/noema-rg-e2e.json UI_STACK_MOCK=1` (e2e stack #1) | 00:02:47-00:03:27 | **PASS** | **12 passed** (38.3 s); 55 responses validated, 0 failures |
| `make qa-contract QA_CONTRACT_PROJECT=noema-rg-contract QA_CONTRACT_PORT_BASE=21400 QA_CONTRACT_BUILD=0` | 00:03:58-00:07:12 | **PASS** (Q1, Q2, Q3, X1 part) | docs-drift 8/8; sdk-compat 65/65; stack **131 passed**: openai Python 14/14, retries 3/3, openai npm 12/12, Jev 8/8, input robustness 67/67, schemathesis stateless **6,160 calls over 25 operations, 0 failures** (only 5xx: `resumeTurnEvents` 501 x65, allowed), stateful **1,735 calls over 19 operations, 0 failures**; server-side ContractValidator violations (api-1, api-2): **0**. Tallies: `reports/qa/release-gate/contract/` |
| `make test-media IMAGE_TAG=rgate` | 23:58:16-00:01:37 | **PASS** | **307 passed, 8 skipped** (no network in the media image) |
| `make observability-check` | 23:55:08-23:55:42 | **PASS** "observability-check: all green" | promtool check rules `--lint=all`: 6 + 8 + 11 alerting, 19 recording rules; promtool test rules: 4 files SUCCESS; example config syntax valid; 84 dashboard queries parsed; pytest `tests/unit/observability` **139 passed** (the target sets `NOEMA_OBS_RUNBOOKS=1` for this step itself) |
| `NOEMA_OBS_RUNBOOKS=1 make observability-check` | 23:55:42-23:56:04 | **PASS** | identical output: 139 passed, 0 skipped (the runbook-anchor test in `test_alerts.py` ran in both forms) |
| `cd /codes/aghooai/docs/noema && ./check_docs.sh` | 23:56:04-23:56:41 | **PASS** exit 0 (37.3 s) | 11 mermaid diagrams, tsc, schema, pydantic, config, audit, openapi, bundle, prose, links: PASS |
| `make image-scan IMAGE_TAG=rgate IMAGE_SCAN_OUT=reports/qa/release-gate/image-scan` | 23:58:22-23:58:56 | **PASS** exit 0 | "CRITICAL not waived": none on api, worker, media. Counts (jq over the JSON): api and worker **0 unwaived HIGH/CRITICAL**, 43 HIGH waived (8 ids), 0 Python-package findings; media 4 CRITICAL + 94 HIGH, all waived (51 ids); unwaived only LOW/MEDIUM/UNKNOWN (api 60/58/1, media 176/165/4; media's 6 are the base image's pip 25.0.1, 5 MEDIUM + 1 LOW). CycloneDX SBOMs **148 / 148 / 331** components. Identical counts to Wave F's `:wfinfra` scan. All 51 waivers `expired_at: 2027-01-09` (valid) |

### 3.1 `make qa-security` (SE1-SE13; `QA_SEC_PROJECT=noema-rg-sec QA_SEC_PORT_BASE=21500 QA_SEC_TAG=rgate QA_SEC_BUILD=0 QA_SEC_UI_PORT=21590 QA_SEC_OUT=~/.noema/rg-security QA_SEC_REPORTS=reports/qa/release-gate/security`; 00:10:05-00:28:38)

| Part | Result | Evidence |
|---|---|---|
| `qa-security-local` | **PASS** | **72 passed** (authz oracle and golden cells, `x-noema-roles`, scanner patterns, SEC-STS-01..17 on moto, adapters on the mock): SE4 (moto) |
| `qa-security-stack` | **PASS** | `make e2e` on the security stack **52 passed**; SEC-UI Playwright **12 passed**, 55 responses validated; `tests/security/stack` **31 passed**: SE1 authz matrix **356 cells, 356 equal to the oracle, 0 leaks** (18 `resumeTurnEvents` cells included); SE3 cache isolation 3/3; SE15 edge and config 4/4; SE6 injection 11/11 (52 captured requests, 51 canaries, none unseen); SE7 media injection (44 captured requests, none unseen, 0 policy violations); SE2 RLS 2/2 (24 tables checked); SE12 trace scan (49 spans) and secrets scan (46 secret values over compose log, Redis, DB dump, mock requests and the 4 api processes' metrics); SE11 `test_ui_csp.py` 2/2. gitleaks on the capture: 3,636 `aws-access-token` (moto AKIDs in the mock's request records) + 19 `generic-api-key` (`db.sql` idempotency owner ids): the triaged false-positive classes of [security.md](security.md) / E3 |
| `qa-security-fuzz` (SE9) | **PASS** | **6 passed** in 720.7 s in the media image (network none): mutation corpus 40 per kind, `escaped: []`, egress tcp / udp_dns / tcp6 / netlink all `EAFNOSUPPORT`, interfaces `lo` only; atheris: detect, image.normalize, pdf.render, markdown.sanitize 0 findings; pdf.sanitize 1 crash input (582 B) -> **jail `rejected:corrupt`, typed** (the same class as E3) |
| `qa-security-sca` (SE13: pip-audit, npm audit, trivy with the waivers, SBOMs, gitleaks repo) | **FAIL on the first try, PASS on the re-run** | First try (00:28): **pip-audit could not reach PyPI** (`requests.exceptions.ConnectionError: RemoteDisconnected`), the target's `\|\| true` let it continue without `pip-audit.json`, and `tests.security.sca_gate` stopped with `FileNotFoundError` (fails closed; an infrastructure failure, no finding). Re-run `make qa-security-sca` with the same variables (00:31:05-00:31:27): **"waived ids: 51; SE13 PASS"**: pip-audit **243 packages, 0 vulnerable** (`torch`, `torchvision` not auditable); npm audit root 0 / sdk 0 (all severities); trivy api / worker `high_unfixed` 43, media `high_unfixed` 94 + `critical_unfixed` 4, **0 unwaived ids** against `deploy/docker/.trivyignore.yaml`; CycloneDX SBOMs in `reports/qa/release-gate/security/sbom/`: **148 / 148 / 331** components (api / worker / media). gitleaks repo (git history): 90 hits (E3: 77); the 13 new ones are in commits `055ac23` and `10aa2d4`: moto AKIDs in the committed `reports/qa/e3/qa-lead/fault-results.json`, strings (`ENV GPG_KEY=...`) in committed trivy JSON, and `ASIAEXAMPLEEXAMPLE01` in `tests/unit/adapters/test_sigv4.py`; the one `aws-amazon-bedrock-api-key-short-lived` hit is the redaction-test fixture in `tests/unit/telemetry/test_redact.py:32` (commit `671f39a`, in E3 too). No real secret |
| **`make qa-security` as one invocation** | exit 2 ("qa-security FAILED: qa-security-sca"), then the failed part re-run green | every part green on this tree; the one red was the PyPI connection |

### 3.2 `make qa-fault` (F1-F12, S1; `FAULT_PROJECT=noema-rg-fault FAULT_PORT_BASE=21300 FAULT_IMAGE_TAG=rgate FAULT_BUILD=0 FAULT_RESULTS=reports/qa/release-gate/fault-results.json`; 2 workers per api pod)

First run (00:32-01:09): **21 passed, 1 failed** in 2,175.6 s. The failure is `test_f10_redis_outage_keeps_turns_flowing[hung]`, on the test's
own **same-pod cancel** probe: it asserts 202 and got **500**. Every spec-10 F10 criterion passed in that run (section 3.3).
Root cause and harness fix: section 3.3. After the fix, F10 was re-run three times on the same stack: **2/2 passed
each time**. A final full `make qa-fault` on a fresh stack is in section 5.

| Row | Result (first full run; `fault-results.json`) |
|---|---|
| F1 | **PASS**: fallback **42/42** served (gap p95 35.7 ms, max 50.1); tenant B TTFT p95 644.1 ms (fault) vs 636.8 ms (clean) |
| F2 | **PASS**: F2a foreign / missing ExternalId / moto down: 3 x 20 `503 tenant_access_misconfigured`, not retryable, **0 mock requests**; F2b 50 typed, 0 Sol requests, tenant B 50/50; F2c members degraded, 100/100 completed |
| F3 | **PASS**: open **4.16 s**, recovered **5.23 s** after the window, 255 of 256 turns refused without a model call while open; F3b TTFT rule open 4.54 s after the slowdown, closed 0.06 s after the restore |
| F4 | **PASS**: F4a fallback **100/100**, max e2e **11.29 s** at deadline 30 s; F4b 100/100 typed, max 5.46 s |
| F5 | **PASS**: pre-commit fallback **100/100**; post-commit 100/100 `upstream_stalled`, partial text stored (20/20 checked) |
| F6 | **PASS**: pre-commit fallback **80/80**; post-commit 5 types x 20 `all_routes_unavailable` retryable, partial rendered |
| F7 | **PASS**: reasoning-only 50/50 and empty text 50/50 served by fallback, 0 committed from GLM, 0 empty answers |
| F8 | **PASS**: attempt fallback 30/30 (CRC) + 30/30 (truncation), all 200; members get `upstream_error` / `parse_error` / `missing_options`, 100% completed |
| F9 | **PASS**: 200/200 served, every answer (1,000) `unavailable` |
| F10 | **PASS by spec-10's criteria; the test's extra probe failed once** (section 3.3): down 125/125 and hung 124/124 completed during the outage, 40 + 41 after, `admission_degraded` 3 / 4, L2 misses > 0, cross-pod cancel 500, cross-pod attach 410 after 2.5 / 3.3 s; same-pod cancel 202 (down) / **500 (hung)** |
| F11 | **PASS**: `store: true` 500 `internal_error` at ingress for the 60 s; `store: false` 0 failures; 80/80 stored exactly once, one generation line each, 0 missing |
| F12 | **PASS**: per failing upstream, **rate 0.5: Sol 1.044**, Opus 1.000, GLM 1.000; **rate 0.25: Sol 1.079**, Opus 1.000, GLM 1.000 (bound 1.1) |
| S1a | **PASS**: asserted in every row: 0 problems (the F10 [hung] run reached its probe assertion after `assert_s1`) |
| S1b | **PASS**: **482 of 482** failed-first turns served (F1, F4a, F5-pre, F6-pre, F7, F8-attempt) |
| C3 (slow clients, mini form) | **PASS**: 4 slow readers stored `failed` at 47-48 s, 8 normal `completed`, normal TTFT p95 680 vs 805 ms baseline |

### 3.3 F10 same-pod cancel: root cause and the harness fix (QA lead)

- **What failed.** `cancel_probe(stack, 0)` starts a slow turn on api-1 and cancels it on api-1 while Redis is hung.
  The api-1 access log shows `POST /v1/turns/{turn_id}/cancel 500 303.208ms` at 01:01:49.164 UTC. In the `down`
  mode the same call was `202 0.654ms`. 303 ms is the Redis call timeout, so the cancel took the Redis path.
- **Why.** An api pod runs 2 processes (`WEB_CONCURRENCY=2`, IMPL-087). The running-turn registry is per process:
  `TurnService.running` in `src/noema/turns/service.py:366-400`. A cancel that reaches the process running the turn
  is answered locally (202). On the pod's other process it is not local, so the service asks Redis for the owner.
  With Redis down that raises `CoordinationDown` and returns **500 (retryable)**, as the method's docstring
  specifies. Each new connection to api-1 is accepted by whichever of its 2 processes accepts first, so a "same-pod"
  cancel can be a cross-process cancel. E2 and E3 happened to land on the owning process.
- **The spec does not require a same-pod 202.** spec-10 §4 F10 requires: >= 99.9% of turns complete, local GCRA
  buckets, L2 bypass, same-pod SSE unaffected, cross-pod attach waits then 410, and cross-pod cancel 500. The test
  added "same-pod cancel unaffected", which was written for one process per pod. Every spec-10 criterion passed
  in the failing run. **This is not a product regression and the product code is unchanged.** The semantics are
  recorded as a finding for the api and spec owners (section 7, item 1).
- **Harness fix** (`tests/fault/test_f10_redis_down.py`, owned by qa-tools / qa):
  - The same-pod probe now retries a 500 on a fresh connection, up to 8 tries (`SAME_POD_TRIES`).
  - It asserts that the last status is 202 and that every earlier one is 500: sibling-process 500s, then the
    owning process's local 202.
  - The cross-pod probe is unchanged (a single try, must be 500).
  - The module docstring now states the per-process semantics.
  - Before: `assert probes["same_pod_cancel"] == 202`.
- **After the fix**, 3 runs of `FAULT_TESTS=tests/fault/test_f10_redis_down.py` on the same stack (results in
  `reports/qa/release-gate/fault-f10-rerun-{1,2,3}.json`): 2 passed in each run, 157.5 s each. Same-pod cancel
  statuses: run 1 down **`500,500,202`**, hung `202`; run 2 `202` / `202`; run 3 `202` / `202`. The run 1 sequence
  shows the mechanism directly: two cancels reached the sibling process (500), the third reached the owner (202).
  Every run: 125/125 completed during the outage, 40/40 after, cross-pod cancel 500, cross-pod attach 410 after
  2.52-2.53 s (down) and 3.27 s (hung). The final full run (section 5) saw `500,500,202` once more, in the hung
  mode, so 3 of 9 same-pod first tries over all runs reached the sibling process.

### 3.4 `make qa-gates` (`QA_GATES_PROJECT=noema-rg-gates QA_GATES_PORT_BASE=21600 QA_GATES_TAG=rgate QA_GATES_BUILD=0 QA_GATES_RESULTS=reports/qa/release-gate/gates-results.json QA_GATES_OUT=~/.noema/rg-gates`; 01:23:44-01:37:48; mock unpinned, the default)

**PASS**, "qa-gates: all green", every part in one invocation:

| Part | Result | Evidence (`gates-results.json`) |
|---|---|---|
| `qa-gates-selftest` | **PASS** | 8 passed |
| `qa-load-holdback` (L2, L3, Q8) | **PASS**, 13 passed (580.7 s) | L2, 6 cells, n 200 each: every p95 within its bound. Tightest: 300/300 **326.4 vs 350 ms**, 900/720 **920.3 vs 950 ms**, 300/720 730.7 vs 770 ms. L3, 6 forced-restart cells: tightest 900/720 **1,650.0 vs 1,720 ms**, 300/2400 2,719.0 vs 2,800, 900/2400 3,323.0 vs 3,400; premise share 1.0 in every cell. Q8: `holdback_deadline` share 0 in every cell except where the hold cap fires before S1 (L2 900/300 and the Q8 cell: 1.0); **stored `served_spec_differs` true 200/200** in Q8. Validity: **9 of 13 cells flagged invalid by mock lateness** (p99 2.26-21.18 ms > 2 ms), each met its bound. E3 had 7 of 13 and Wave F unpinned 3 of 13: the compose mock's lateness on a shared host, [mvp-acceptance §7.1](../mvp-acceptance.md) item 10 |
| `qa-load-backup-tail` (L5) | **PASS** | DeepSeek-only, n 5,000: **p99 927.5 ms** (bound 1,500), p95 609.8; 5,050 answers `predicate:ok`; valid |
| `qa-replay` (R4) | **PASS** (meas) | 40 sessions x 5 turns, 200 served; spec restarts **46** (= the script), share 0.23; waste share of generation cost 0.401 at the synthetic agreement 0.77 (reported, P0 thresholds); idempotent replay **60/60 identical**, 0 model calls, usage unchanged |
| `qa-hier-deadline` (Q9) | **PASS** | n 200 per case: `deadline_exceeded` share **1.0 at 1,000 ms and 0.0 at 3,000 ms** for lightweight-model-decide and router-default; no other outcome |
| `qa-media-injection` (SE7) | **PASS** | 1 passed (also in `qa-security`) |
| `qa-ocr-cer` (M3) | **PASS** (meas) | in `noema-media-test:rgate`: aggregate CER **0.0** (probe fixtures + clean renders). The degraded "pangram rotated 3 deg" render reads 0.55 (word order), reported and not gated, as before |
| `qa-ownership-all` | **PASS** | tracked paths 1,989, unowned 0 |

## 4. Load gates in the container (`noema-api:rgate`, 2 processes per pod)

**Method.** The E3 QA lead's and Wave F infra-perf's method and placement, in container mode for every run:
- `--api-image noema-api:rgate --api-procs 2`: `docker run --cpuset-cpus 46-47`, uid 10001, read-only root, no
  capabilities;
- Postgres + Valkey of `noema-rg-perf` on CPUs 44-45; 8 mock processes (Converse 16-19, Mantle 40-43); drivers on
  4-15 and 28-39;
- load config v2 `sha256:b475dc16...` (the E3 / Wave F digest); v3 `sha256:916f53ef...` for C5 (the E3 C5 digest).

**Runner.** `/tmp/noema-rg/run.sh`, a copy of `reports/perf/wf-infra/load/run.sh` with this stack's ports. A run
starts only with no other `tests.load.run` alive and the 1-minute loadavg below 6, and the loadavg and foreign load
processes are recorded every 10 s (`<run>.monitor`). The only "foreign" processes in every sample are the
container's own 2 api processes (checked with `pgrep -af`), as in Wave F. The 1-minute loadavg max over every
run was 7.24 (the ramp; the E3 rule is < 8); the per-window means are in the tables. The runner, `digest.sh`,
the monitors, the step-end `/metrics` scrapes of both processes and C3's probe files are copied to
`reports/qa/release-gate/load/`.

**Isolation and provenance.**
- No suite of this role ran during a load run (the stacks of section 3 were down).
- Product digest `678b7d68ced8ea33` at the start and end of every run.
- The results' `provenance.code` reports HEAD `10aa2d4` + 4 changed paths, image id `sha256:3863d2741b72`.
- 0 contract violations in every run (each driver's summary).
- 0 tracebacks and 0 error-level lines in the api logs.

**Results.** `reports/qa/release-gate/load/<run>.json`; raw data in `/tmp/noema-rg/runs/<run>/`.

### 4.1 L1 confirmation at 100 streams per pod — `rg-l1-100.json` (`--plan 100:740 --warmup-s 60 --settle-s 30`, 01:40:59-01:54:39)

The Wave F knee measurement (125 per pod) stands, as the brief says; this run re-measures the planned density once.

| streams/pod | n | turns/s | TTFT ovh p50 [95% CI] | p95 [95% CI] | p99 | e2e ovh p50 / p95 | ingress p50 / p95 | api lag p99 / max | pod CPU | CPU ms/turn | RSS MiB | load mean (max) | valid | L1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 100 | 10,696 | 14.45 | **9.21 [9.18, 9.23]** | **12.64 [12.45, 12.81]** | 16.7 | 15.8 / 19.7 | 8.5 / 10.5 | 3.6 / 33 | 0.605 | 41.9 | 388 -> 564 | 2.9 (3.9) | yes | **PASS** |
| 100 (Wave F `wf-l1-a`, `:wfinfra`) | 10,693 | 14.45 | 9.30 [9.27, 9.32] | 13.20 [13.02, 13.43] | 20.9 | 15.9 / 22.7 | 8.5 / 11.3 | 3.8 / 42 | 0.617 | 42.7 | 562 | 4.2 (7.7) | yes | PASS |

- **Confirmed.** The release image reproduces Wave F's planned-density point: p50 0.09 ms lower and p95 0.56 ms
  lower; CPU per turn 41.9 vs 42.7 ms.
- Window validity: 100% `holdback_ms` 0 with `spec_agrees`, 15 decision calls per turn; worst driver lag p99
  0.92 ms, worst mock 0.94 ms.
- Full GC per process: 25.0 / 25.7 ms p50 every 41 s, max 35 ms.
- **`aghoo_turn_slots_held` at the step-end scrape: 50 + 50 = 100**, equal to the streams per pod.

### 4.2 C2 ramp 0 -> 1,000 streams per pod — `rg-ramp.json` (the E3 form: `--plan 50:120,100:120,150:120,200:120,250:120,300:120,400:120,600:120,800:120,1000:120 --warmup-s 60 --settle-s 30`, 01:54:40-02:20:49)

2-minute holds, shortened and labeled as spec-10 C2 allows. Pass: api event-loop lag p99 < 50 ms at every step up to
200 streams per pod; the rest is measured.

| streams/pod | n | turns/s | TTFT ovh p50 [CI] | p95 [CI] | api lag p99 / max | pod CPU | ms/turn | RSS MiB | 429 `queue_timeout` | slots held (sum) | valid | E3 venv: lag p99, TTFT p50 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 854 | 7.12 | 8.42 [8.33, 8.49] | 11.34 [10.96, 11.92] | **1.9** / 14 | 0.321 | 45.1 | 386 | 0 | 33 + 17 = 50 | yes | 2.1, 8.3 |
| 100 | 1,748 | 14.57 | 9.26 [9.18, 9.34] | 13.07 [12.76, 13.55] | **3.5** / 38 | 0.598 | 41.0 | 493 | 0 | 62 + 38 = 100 | yes | 3.4, 9.2 |
| 150 | 2,622 | 21.85 | 10.20 [10.11, 10.28] | 14.94 [14.67, 15.44] | **4.1** / 119 | 0.880 | 40.3 | 601 | 0 | 90 + 60 = 150 | yes | 4.2, 10.1 |
| 200 | 3,454 | 28.78 | 11.20 [11.13, 11.28] | 18.00 [17.50, 18.41] | **4.9** / 78 | 1.158 | 40.2 | 715 | 0 | 109 + 91 = 200 | yes | 5.1, 11.2 |
| 250 | 4,025 | 33.54 | 13.65 [13.44, 13.85] | 30.80 [29.26, 33.39] | 6.4 / 105 | 1.331 | 39.7 | 807 | 2,134 | - | yes | 6.1, 13.4 |
| 300 | 4,139 | 34.49 | 13.82 [13.67, 14.04] | 36.51 [34.15, 38.06] | 6.5 / 113 | 1.394 | 40.4 | 852 | 7,261 | - | yes | 6.7, 14.2 |
| 400 | 4,145 | 34.54 | 14.36 [14.21, 14.57] | 35.54 [33.43, 37.27] | 6.8 / 123 | 1.434 | 41.5 | 872 | 19,162 | - | yes | 6.1, 13.5 |
| 600 | 4,160 | 34.67 | 14.89 [14.71, 15.09] | 36.07 [34.23, 38.41] | 6.9 / 130 | 1.504 | 43.4 | 879 | 43,046 | - | yes | 6.6, 14.4 |
| 800 | 4,142 | 34.52 | 15.95 [15.72, 16.18] | 40.73 [39.08, 42.58] | 7.3 / 125 | 1.566 | 45.4 | 884 | 67,005 | - | yes | 7.2, 15.8 |
| 1,000 | 4,165 | 34.71 | 16.27 [16.04, 16.54] | 38.17 [36.55, 39.99] | 7.2 / 133 | 1.639 | 47.2 | 886 | 90,831 | - | yes | 6.7, 16.1 |

- **C2 = PASS in the container.** Lag p99 is **1.9 / 3.5 / 4.1 / 4.9 ms** at 50 / 100 / 150 / 200 streams per pod
  (bound 50 ms). Every window up to 1,000 streams is valid.
- **The image matches the E3 venv run** (the run that decided C2) up to 200 streams per pod: within 0.2 ms of lag
  p99 and 0.12 ms of TTFT p50. Past the cap (250-1,000) the gaps are at most 0.7 ms of lag p99 and 0.86 ms of
  TTFT p50, both at 400.
- **Capacity**: 34.5-34.7 turns/s from 300 streams (E3 venv 34.6-34.7). The first sheds come at 250 streams, the
  per-process cap 120 x 2. Past that the edge shed answers 429 `queue_timeout`.
- **Driver statuses over the whole run**: 42,041 x 200 and 285,341 x 429. **0 x 5xx, 0 transport errors** (E3: one
  `ReadError`; Wave F's driver keep-alive fix) and 0 contract violations.
- **Datastores** (the R4 metrics, `dscompact.py`, both processes):
  - Postgres: at most 2 of 20 connections in use at a step-end scrape; acquire 0.03-0.05 ms mean; unit of work
    2.26 -> 5.05 ms mean from 50 to 1,000 streams.
  - Redis: 5-7 of 256 in use; acquire 0.02 ms; command 0.27 -> 0.89 ms mean. No pool saturates.
- **`aghoo_turn_slots_held`, summed per pod**, equalled the streams per pod at every step up to 200. The split
  between the 2 processes is uneven (33 + 17 at 50, 109 + 91 at 200). From 250 streams it reads 110-120 per
  process (230-240 per pod), the per-process cap.

### 4.3 C5 fairness, one tenant flooding — `rg-c5.json`, `rg-c5-run2.json` (config v3; the E3 gated form `--plan 100:300,100:300 --warmup-s 60 --settle-s 60 --side flood:t_beta:200:1`, run twice back to back, 02:21:01-02:45:44)

Setup: the victim t_acme runs 100 streams per pod in both windows. Window 1 adds t_beta at 200 streams, 5x its
`concurrent_streams_per_project` limit of 40. Pass: victim client TTFT p95 <= 1.10 x solo, read on the bootstrap
95% CI of the ratio (`ratio.py` / `c5table.py`). The flooder's statuses and the admission counters cover both
processes.

| Run | victim p95 solo -> flood (ms) | ratio [95% CI] | victim errors | victim ovh p95 | victim ingress p95 | victim decision p95 | pod CPU solo -> flood | full GC p50 per process (flood) | flooder 200 / 429 / transport err | admission timeouts / sheds | load mean | valid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **rg-c5** | 313.4 -> 319.6 | **1.020 [1.017, 1.022]** | 0 | 18.8 | 15.3 | 73 | 0.61 -> 0.95 | 106 / 94 ms | 2,100 / 43,339 / **0** | 5,817 / 37,498 | 2.9 / 4.7 | yes |
| **rg-c5-run2** | 313.1 -> 322.9 | **1.031 [1.026, 1.036]** | 0 | 22.1 | 17.3 | 78 | 0.60 -> 0.94 | 86 / 107 ms | 2,102 / 43,993 / **0** | 5,705 / 38,253 | 3.0 / 3.8 | yes |
| E3 `e3-c5` / `-run2` (venv) | 312.9 -> 319.6 / 313.1 -> 320.1 | 1.021 [1.019, 1.026] / 1.022 [1.019, 1.026] | 0 / 0 | 19.0 / 19.1 | 16.1 / 15.7 | 75 / 76 | 0.60 -> 0.93 / 0.94 | 95 / 98, 98 / 88 | 2,110 / 43,391 / 1; 2,114 / 43,424 / 4 | 5,912 / 37,449; 5,877 / 37,511 | - | yes |

- **C5 = PASS in the container**: 1.020 and 1.031, upper CI at most 1.036, far under 1.10; 0 victim errors.
- **The keep-alive fix is confirmed under the flood**: 0 transport errors on the flooder in both runs, where E3
  saw 1 and 4 `ReadError`s. This closes the open half of [mvp-acceptance §7.1](../mvp-acceptance.md) item 8.
- Run 2's upper CI (1.036) is 0.010 above E3's (1.026), and its victim TTFT-overhead p95 under the flood is
  22.1 ms (E3 19.1). Both runs stay far inside the bound.

### 4.4 C3 slow clients — `rg-c3.json` (config v4 = the v2 shape again, `sha256:c92ea7fc...`; the E3 form `--plan 180:180,180:180,180:180 --warmup-s 60 --settle-s 120 --scenario tests/load/scenarios/slow.yaml --sse-probe --side fast:20:1 --side slow:20:2:14500`, 02:45:55-03:00:52)

Every window runs 180 normal L1 streams. Window 1 adds 20 heavy clients read at full speed (the baseline); window 2
adds the same 20 read at about 10% of the byte rate. In the container this needed the harness fix of section 6:
before it, `--sse-probe` was silently dropped with `--api-image`. The probe now writes
`/tmp/noema-rg/runs/rg-c3/api/api-{0,1}.sseprobe.json`. Stored statuses come from Postgres (`c3.py`).

| Criterion | Measured (container) | E3 (venv) | Verdict |
|---|---|---|---|
| normal TTFT p95 within 10% of the baseline | window 2 / window 1 = **1.001 [0.997, 1.006]** (323.5 vs 323.0 ms); vs window 0: 1.019 [1.015, 1.022]; 0 errors, 0 contract violations, every window valid (loadavg mean 3.8-4.7, max 6.5) | 0.998 [0.994, 1.002] | **PASS** |
| coalescing | 180 coalescing runs (93 + 87), **15,660 frames merged** (8,091 + 7,569) | 180 runs, 15,660 frames | **PASS** |
| `client_too_slow` at 30 +/- 1 s | **60 of 60** slow streams (31 + 29 per process), 30.000-30.059 s after the stream stayed blocked; 60 "client too slow" log lines | 60/60 at 30.000-30.029 s | **PASS** |
| ... and the turn ends (spec-04 §12: stored `failed`) | **60 of 60 slow turns `failed` at 64.3-64.6 s**; the 80 fast-read heavy turns `completed` at 90.3 s; 60 uvicorn "ASGI callable returned without completing response" lines (the documented cost of the bounded closing send); 0 tracebacks | 60/60 at 64.3-64.6 s | **PASS** |
| queue high-water <= 256 events / 1 MiB | **253 events, 1,048,270 B in both processes** (306 B under 1 MiB; largest frame 11,539 / 11,542 B) | 253 events, 1,048,270 B | **PASS** |

**C3 = PASS in the container**, matching E3's venv run figure for figure. Pod RSS in window 2: 2.72-2.84 GB (E3
2.76-2.82 GB; the heavy turns' sealed logs are held for the 300 s resume window).

## 5. Final checks on the end-state tree (03:03 UTC onwards)

The end-state tree is HEAD plus this role's 3 test files and reports, plus the other role's uncommitted docs edits
and the `tools/bootstrap_dev.py` docstring (product digest `678b7d68ced8ea33`, unchanged since 23:57).

| Command | When (UTC) | Result | Evidence |
|---|---|---|---|
| `NOEMA_AGENT_ID=rgate make check XDIST=4` | 03:03:01-03:05:01 | **PASS** "make check: all green" | identical counts to section 3 (ruff 920 files, mypy 327 / 594, import-linter 27 / 0, forbidden-scan clean, contracts 70 identical, unit 3,588 / 1 skipped, conformance 757 / 8, integration 573 / 21, sdk TS 86 + 6, Python 192 + 1); **ownership: 38 changed paths, all owned** (this role's `tests/fault/**`, `tests/load/**` -> qa-tools, qa; `reports/**` -> qa; the other role's `docs/**` -> wave-a / repo-docs / infra / integrator and `tools/bootstrap_dev.py` -> integrator) |
| `tools/check_ownership.py` (after the load evidence was copied to `reports/qa/release-gate/load/`) | 03:48 | **PASS** exit 0 | 84 changed paths, all owned |
| `NOEMA_OBS_RUNBOOKS=1 make observability-check` | 03:05:01-03:05:35 | **PASS** "observability-check: all green" | 139 passed |
| `cd /codes/aghooai/docs/noema && ./check_docs.sh` | 03:05:35-03:06:10 | **PASS** exit 0 (35.5 s) | re-run after the other role's edit of the design repo's 08 §6 onboarding row |
| `pytest tests/load/test_run_api_image.py tests/load/test_harness.py tests/load/test_gatekit.py` | 00:00 and 03:46 | **PASS** 23 passed (both times) | the load harness self-tests, incl. the new regression test (section 6) |
| `make qa-fault ... FAULT_RESULTS=reports/qa/release-gate/fault-results-final.json` (fresh `noema-rg-fault` stack, the fixed F10 test) | 03:06:22-03:43:06 | **PASS** | **22 passed** in 2,174.5 s (`fault-results-final.json`): F1 fallback 53/53 (gap p95 45.4 ms), tenant B TTFT p95 633.4 ms; F3 open 3.85 s, recovered 4.94 s; F3b open 8.09 s, closed 0.06 s; F4a 100/100, max 11.17 s; F5 100/100 + 100 `upstream_stalled`; F6-pre 60/60; F9 200/200; F10 down / hung 125/125 during, 40/40 after, cross-pod cancel 500, attach 410 after 2.5 / 3.3 s, **same-pod cancel `202` (down) and `500,500,202` (hung)**: the old assertion would have failed again; F11 0 missing, `store: false` 0 failures; F12 Sol 1.034 (rate 0.5) / 1.042 (rate 0.25), Opus and GLM 1.000; S1b **493 of 493** failed-first turns served |

## 6. Changes by this role (tests only; no product code changed, no product bug found)

1. **C3's SSE probe in the container** (`tests/load/run.py`).
   - Defect: `--sse-probe` was honoured only by the venv api. With `--api-image` the flag was accepted and silently
     dropped, so the queue high-water criterion of C3 could not be measured on the image.
   - Fix: a new `container_serve_args(args, n)` builds the container's `tests.load.serve` arguments, including
     `--sse-probe-out /out/api[-{i}].sseprobe.json`. `POD_LAUNCHER` substitutes `{i}` with each process's index.
   - Regression test: `tests/load/test_run_api_image.py::test_the_container_pod_gets_one_sse_probe_file_per_process`.
     It failed before the fix (`AttributeError: module 'tests.load.run' has no attribute 'container_serve_args'`)
     and passes after.
   - Smoke run with the image: `/tmp/noema-rg/runs/smoke-probe/api/api-{0,1}.sseprobe.json` were written.
   - Lint: ruff, `ruff format --check` and `mypy --strict` are clean on both files.
2. **F10's same-pod cancel probe per process** (`tests/fault/test_f10_redis_down.py`). Section 3.3 gives the
   evidence; ruff, format and `mypy --strict` are clean.

## 7. Findings and open items

| # | Finding | Evidence | Owner |
|---|---|---|---|
| 1 | **A cancel during a Redis outage can answer 500 even when the turn runs on the same pod.** Here 3 of the 9 same-pod first tries reached the sibling process. The running-turn registry is per process and a pod runs 2 processes (IMPL-087). spec-04 §12 "Failure handling" (`docs/impl-spec/04-turn-lifecycle.md:354`, "cross-pod cancel -> 500") and spec-10 F10 still speak of pods. The behaviour matches `TurnService.cancel`'s docstring (500, retryable), so a client retry can succeed. Options: say "another process" in spec-04 / spec-10, or forward cancels between a pod's processes | section 3.3: 500 in 303 ms on api-1; `500,500,202` in re-run 1 | api, spec owner |
| 2 | **`qa-security-sca` hides a pip-audit failure.** `make/qa-security.mk` runs `uvx pip-audit ... \|\| true`. When PyPI was unreachable no `pip-audit.json` was written, and `tests.security.sca_gate` then crashed with `FileNotFoundError`. It fails closed, but the message does not say that pip-audit did not run | section 3.1 | qa-security / platform (`make/`) |
| 3 | **Mock lateness in `qa-gates` is unchanged.** 9 of 13 holdback cells were invalid (unpinned mock, shared host); every bound was met | section 3.4 | qa-tools ([mvp-acceptance §7.1](../mvp-acceptance.md) item 10) |
| 4 | **The acceptance report's basis is stale.** [../mvp-acceptance.md](../mvp-acceptance.md) and this brief call the Wave F work "uncommitted". It is commit `10aa2d4`, tag `wave-f` | section 2 | acceptance report owner |
| 5 | **Another role edited the tree during this gate.** The edits are docs plus the `tools/bootstrap_dev.py` docstring, uncommitted at the end. They are covered by the final `make check`, `observability-check` and `check_docs.sh` (section 5). Their content was not reviewed here | `/tmp/noema-rg/logs/tree-watch.log` | orchestrator |
| 6 | **The media image ships the base image's pip 25.0.1.** trivy reports 5 MEDIUM and 1 LOW on it (not gating). The api and worker images carry no pip | section 3, image-scan | infra / media-pipeline |

### 7.1 Status of [mvp-acceptance §7.1](../mvp-acceptance.md) after this gate

| §7.1 # | Item | After this gate |
|---|---|---|
| 1 | K1 FAIL | **Open, unchanged**: it needs live calls, and none are allowed here (ledger 85 of 87 untouched) |
| 2 | L1 as written (200 streams per pod) | **Open, not re-run as L1**: the Wave F knee stands, as the brief says. Indicative only, not a gated L1 window: the ramp's 200-per-pod step in the container reads p50 11.20 [11.13, 11.28] and p95 18.00 ms, with n 3,454 in 120 s. That agrees with E3's venv 11.70 / 20.13 |
| 3 | 24 h soak | **Open** (not in this gate) |
| 4 | Multi-pod load on shared datastores | **Open** (every load run used one pod and its own datastores) |
| 5 | Hardware and workload of the knee | **Open** (same dedicated-core placement as Wave F) |
| 6 | SigV4 session-token branch live | **Open** (no live calls) |
| 7 | Phase 0 gates | **Open** |
| 8 | Wave F tree not re-gated | **Closed.** Every suite ran on `:rgate` images built from the tree: `make check`, e2e x2, UI, contract, security, fault, gates, media, observability, image scan, docs. C2, C3 and C5 ran in the container, and L1 was confirmed at 100 per pod. The C5 runs were repeated after the keep-alive fix with 0 transport errors |
| 9 | `make check` at ownership | **Closed** (still green: 38 changed paths, all owned) |
| 10 | Mock lateness in `qa-gates` | **Open**: 9 of 13 cells invalid this run, every bound met |
| 11 | L4 rests on few live samples | **Open** (L4 was not in this brief and was not re-run) |

### 7.2 Status of [mvp-acceptance §7.2](../mvp-acceptance.md) (checked with `grep` at 00:55 and again at 03:47 UTC)

All four "still stale" places were edited during this gate by the other role (section 2), uncommitted:
- `docs/modules/infra.md` §1 now names `debian:trixie-slim` with python-build-standalone CPython 3.12.15 and
  372 MB (lines 10-19);
- `docs/modules/integration.md` §5.1 now lists the `telemetry.pool_gauges` task (line 91) and the fair-share
  wording (line 172);
- the `tools/bootstrap_dev.py` docstring now says `0003_operator_proof`;
- the 08 §6 onboarding row (`08-technical-reference.md:418`) now carries "As built in Phase 1 (IMPL-091, pending
  owner review)".

`check_docs.sh` and `make check` pass with these edits (section 5). Their accuracy is the docs owners' to review.

## 8. Teardown

Each stack was brought down with its own `*-down` target, or with `docker compose ... --profile '*' down -v
--remove-orphans` for the load stack, as soon as its suite ended. Checked at 03:44 UTC (`docker ps -a`,
`docker volume ls`, `docker network ls` by compose label):
- `noema-rg-e2e`, `-e2e2`, `-fault`, `-contract`, `-sec`, `-gates` and `-perf`: **0 containers, 0 volumes,
  0 networks** each;
- no `noema-rg-perf-api-load` container and no throwaway `noema-media-test-*` project left;
- no listener of this role on 21000-22999;
- no `tests.load` process alive.

**Kept:**
- **`noema-dev`** (4 containers healthy, untouched).
- The images `noema-{api,worker,media,media-test}:rgate`, as the gated artifacts. Remove them with `docker rmi
  noema-api:rgate noema-worker:rgate noema-media:rgate noema-media-test:rgate`.
- The stack state files `~/.noema/stacks/noema-rg-*.{json,env}` (mode 0600; dev keys of stacks that no longer
  exist).
- The raw data in `/tmp/noema-rg/`.

**Not this role's, left alone:**
- a `node server/main.ts` UI proxy on 127.0.0.1:28592, started 2026-10-11 04:03 UTC;
- the other projects' compose stacks.

The tree watcher was stopped.

## 9. How to reproduce

```
cd /codes/aghooai/noema
make e2e-images image-media-test IMAGE_TAG=rgate
export NOEMA_API_IMAGE=noema-api:rgate NOEMA_WORKER_IMAGE=noema-worker:rgate NOEMA_MEDIA_IMAGE=noema-media:rgate
NOEMA_AGENT_ID=rgate make check XDIST=4
uv run --frozen --no-sync python tools/check_ownership.py --paths $(git diff --name-only wave-e3 HEAD); make qa-ownership-all
make observability-check; NOEMA_OBS_RUNBOOKS=1 make observability-check
(cd /codes/aghooai/docs/noema && ./check_docs.sh)
make ui-check
make e2e E2E_PROJECT=noema-rg-e2e E2E_PORT_BASE=21100 E2E_BUILD=1 IMAGE_TAG=rgate
NOEMA_UI_PORT=21290 make ui-e2e
NOEMA_UI_PORT=21295 make ui-e2e UI_STACK_STATE=$HOME/.noema/stacks/noema-rg-e2e.json UI_STACK_MOCK=1
make e2e-down E2E_PROJECT=noema-rg-e2e E2E_PORT_BASE=21100
make e2e E2E_PROJECT=noema-rg-e2e2 E2E_PORT_BASE=21200 E2E_BUILD=0; make e2e-down E2E_PROJECT=noema-rg-e2e2 E2E_PORT_BASE=21200
make qa-contract QA_CONTRACT_PROJECT=noema-rg-contract QA_CONTRACT_PORT_BASE=21400 QA_CONTRACT_BUILD=0
make qa-contract-down QA_CONTRACT_PROJECT=noema-rg-contract QA_CONTRACT_PORT_BASE=21400
make test-media IMAGE_TAG=rgate
make image-scan IMAGE_TAG=rgate IMAGE_SCAN_OUT=reports/qa/release-gate/image-scan
SEC="QA_SEC_PROJECT=noema-rg-sec QA_SEC_PORT_BASE=21500 QA_SEC_TAG=rgate QA_SEC_BUILD=0 QA_SEC_UI_PORT=21590 \
     QA_SEC_OUT=$HOME/.noema/rg-security QA_SEC_REPORTS=reports/qa/release-gate/security"
make qa-security $SEC            # if only qa-security-sca fails on a network error: make qa-security-sca $SEC
make qa-security-down QA_SEC_PROJECT=noema-rg-sec QA_SEC_PORT_BASE=21500 QA_SEC_TAG=rgate
FAULT="FAULT_PROJECT=noema-rg-fault FAULT_PORT_BASE=21300 FAULT_IMAGE_TAG=rgate FAULT_BUILD=0"
make qa-fault $FAULT FAULT_RESULTS=reports/qa/release-gate/fault-results-final.json
make qa-fault-down FAULT_PROJECT=noema-rg-fault FAULT_PORT_BASE=21300 FAULT_IMAGE_TAG=rgate
make qa-gates QA_GATES_PROJECT=noema-rg-gates QA_GATES_PORT_BASE=21600 QA_GATES_TAG=rgate QA_GATES_BUILD=0 \
    QA_GATES_RESULTS=reports/qa/release-gate/gates-results.json QA_GATES_OUT=$HOME/.noema/rg-gates
make qa-gates-down QA_GATES_PROJECT=noema-rg-gates QA_GATES_PORT_BASE=21600 QA_GATES_TAG=rgate
# load stack (base services), seeded, load config v2
env NOEMA_PG_PORT=22032 NOEMA_MOTO_PORT=22050 NOEMA_VALKEY_PORT=22079 \
    docker compose -p noema-rg-perf -f deploy/compose/docker-compose.yml up -d --wait postgres valkey moto
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tools.bootstrap_dev --compose-project noema-rg-perf \
    --state ~/.noema/stacks/noema-rg-perf.json
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.load.stack activate --state ~/.noema/stacks/noema-rg-perf.json
# each run: /tmp/noema-rg/run.sh <name> --results reports/qa/release-gate/load/<name>.json <args>; run.sh adds
#   --state ~/.noema/stacks/noema-rg-perf.json --api-port 22090 --mock-port 22081 --api-cpus 46-47 --api-procs 2
#   --api-image noema-api:rgate --stack-cpus 44-45 --mock-cpus 16,40,17,41,18,42,19,43 --driver-cpus 4-15,28-39
rg-l1-100   --plan 100:740 --warmup-s 60 --settle-s 30
rg-ramp     --plan 50:120,100:120,150:120,200:120,250:120,300:120,400:120,600:120,800:120,1000:120 --warmup-s 60 --settle-s 30
#   (activate --limit t_beta:concurrent_streams_per_project=40)
rg-c5       --plan 100:300,100:300 --warmup-s 60 --settle-s 60 --side flood:t_beta:200:1      # and rg-c5-run2
#   (activate)
rg-c3       --plan 180:180,180:180,180:180 --warmup-s 60 --settle-s 120 --scenario tests/load/scenarios/slow.yaml \
            --sse-probe --side fast:20:1 --side slow:20:2:14500
env NOEMA_PG_PORT=22032 NOEMA_MOTO_PORT=22050 NOEMA_VALKEY_PORT=22079 \
    docker compose -p noema-rg-perf -f deploy/compose/docker-compose.yml --profile '*' down -v --remove-orphans
```

Analysis helpers (outside the repo, the E3 QA lead's, adapted to the container's `out/api/` files):
`/tmp/noema-rg/bin/{l1table,summ,dscompact,c5table,ratio,c3,trivy_count.sh,digest.sh}`.
