# QA lead (Wave E2): re-gate of every 06 §11 gate on the final tree

Role: QA lead, re-gate after the Wave E2 fixers, 2026-10-11/12 (23:38-03:55 UTC). No live Bedrock call (the wire mock
only), no AWS resource (moto only), no git write command, the shared `noema-dev` services used only by `make check`'s
integration step (as every role does). Edits: `tests/**`, `reports/**`, `make/qa-*.mk` only (section 2). Rulings R6
and R7 applied as binding. Gate table: [wave-e-summary.md](../wave-e-summary.md) (updated in place, with a "Wave E2
delta" section). Results files: `reports/qa/e2/qa-lead/` (fault, gates, load, security scans, schemathesis tallies);
raw logs and load data in `/tmp/noema-qae2/` on the build host.

## 1. Setup

- **Code under test**: the working tree on top of `4bc26a7` with every Wave E2 fixer edit. Last product edit
  22:39 UTC (`src/noema/adapters/resilience/breakers.py`); digest of `src deploy tools` (sha256 over the sorted file
  hashes, no `__pycache__`) **`0be5e6d7b39287cb` at the start (23:38) and at the end of the session**: every run
  below measured the same product code. Images `noema-{api,worker,media,media-test}:qae2` built at 23:42 from that
  tree (`make e2e-images image-media-test IMAGE_TAG=qae2`); checked: `api/sse_frame.py`, `generation/walk.py`,
  `adapters/resilience/breakers.py`, `api/middleware/{gate,idempotency}.py`, `ops/admission/gcra.py`,
  `config/models.py` in the image have the tree's sha256.
- **Deployment as configured**: every stack ran the compose default of **2 uvicorn workers per api pod** (no
  `NOEMA_STACK_API_WORKERS=1` / `QA_SEC_API_WORKERS=1` any more: defect 24's per-worker metric scraping is in the
  tree, and SE12's capture shows `metrics-<replica>-<worker>.txt` for all 4 processes). Load runs: 2 api processes
  per pod on the venv (as in Wave E, for comparability).
- **Own stacks** (each torn down at the end, section 6): `noema-qae2-contract` (port base 30400), `-e2e` (30200),
  `-fault` (30300), `-sec` (30500, UI 30590/30595), `-gates` (30600), `-perf` (base services 30132/30150/30179; api
  30190, mocks 30181/30182).
- **Load placement** (unchanged from Wave E, physical-core isolation): api pod CPUs 46-47, Postgres + Valkey 44-45,
  8 mock processes (Converse 16-19, Mantle 40-43), drivers 4-15 and 28-39; runner `/tmp/noema-qae2/run.sh` (waits
  for the 1-minute loadavg < 4). Load config v2 = the bootstrap config with lifted tenant limits; its digest changed
  with the E2 example config (`sha256:b475dc16...`, GLM effort list, Haiku `controls.reasoning`).

## 2. Harness changes of this role (tests/**, make/qa-*.mk)

| # | Change | Why | Before | After |
|---|---|---|---|---|
| H1 | `make/qa-security.mk`: `QA_SEC_WAIVERS ?= deploy/docker/.trivyignore.yaml`, passed to `tests.security.sca_gate` | the infra fixer moved the SE13 waivers to the trivy ignore file; the gate still read the absent `reports/security/waivers.md` (supply-chain-infra §5 item 1) | `python -m tests.security.sca_gate reports/qa/e2/security`: **waived ids 0, SE13 FAIL, exit 1** | with the file: waived ids 51, 0 unwaived, **SE13 PASS, exit 0**; in `make qa-security-sca` (section 3) PASS |
| H2 | `tests/fault/test_f12_retry_storm.py`: records `first_sends_by_model` and `amplification_per_upstream` (requests to X / turns with an attempt started on X) for both rows; the `rate: 0.5` row (spec-10's gated row) still asserts requests / turns <= 1.1; the `rate: 0.25` row asserts R6's measure, amplification against each failing upstream <= 1.1 | R6 (binding): a fallback to another model is a first send of that model, not a retry, so requests per turn count every fallback (~1.2 by construction at 0.25: resilience-fixer §2); R6 states the storm gate "against the failing upstream" | resilience fixer's run of the old assertion: 0.25 row **1.221 > 1.1, FAIL as asserted** | `make qa-fault`: 0.5 row 0.223 requests/turn PASS; 0.25 row per upstream Opus 1.000, Sol 1.078, GLM 1.000 PASS (requests/turn 1.204 recorded) |
| H3 | `tests/qa/contract/test_schemathesis_stateful.py`: `after_call` waits (bounded getFile poll, 60 s) until an accepted upload leaves `quarantined`/`processing`; `before_call` gives every keyed call a fresh `Idempotency-Key` (generated key + 12 hex, only for keys <= 115 chars so valid stays valid and negative stays negative) | since finding 18 the generated uploads are accepted (202) and processed asynchronously, and generated keys repeat across scenarios (and across the stateless test): a step's answer then depends on timing or on what ran before, and hypothesis' stateful data tree raises `FlakyStrategyDefinition` | `pytest test_schemathesis_stack.py test_schemathesis_stateful.py -k 'files or stateful'` on the contract stack: **2 of 6 runs FAILED** (`FlakyStrategyDefinition: Inconsistent data generation`); with the upload wait alone still 1 of 6 | both changes: **8 of 8 runs passed**; the full `make qa-contract` passed with it (and once before it, the flake being intermittent) |
| H4 | `tests/load/scenarios/decision.yaml`: comment only (the probe table it uses is the mock's, now the live-cassette rows) | the comment named the old probe numbers | - | - |

The fixers' `make qa-contract` failures in `test_schemathesis_stack.py[POST /v1/files]` (`SerializationNotPossible:
image/gif`) did not reproduce on the final contract: the contracts fixer replaced the first finding-18 form by
`{type: string, format: binary}`, which schemathesis serializes. No serializer was added (50 examples of POST
/v1/files passed, and the full suite twice).

## 3. Regression and suite gates on the final tree

| Check | Command | Pass criterion | Measured | Result |
|---|---|---|---|---|
| make check | `NOEMA_AGENT_ID=qa-lead make -k check XDIST=4`, at 23:47 and again at 03:45 after the last harness edit (H3) | every CHECK_STEP green | **all green both times**: ruff (895 files), mypy 323 src / 573 test files, import-linter 27 kept / 0 broken, forbidden-scan clean, **ownership 237 / 260 changed paths ok, 0 BAD** (was the only red step in Wave E), contracts-check, unit 3,294, conformance 757 (+8 skipped), integration 561 (+21 skipped), sdk-check TS 86 (+6) / Python 192 (+1) | **PASS** |
| ownership, every tracked path | `make qa-ownership-all` (inside `make qa-gates`) | 0 unowned | tracked paths 1,559, unowned 0 | **PASS** |
| make e2e, twice | `make e2e E2E_PROJECT=noema-qae2-e2e E2E_PORT_BASE=30200 E2E_BUILD=0` (images `:qae2`, 2 workers per api pod) | 52/52 both runs | **52 passed (154.8 s), 52 passed (153.8 s)**; plus 52/52 inside `qa-security` | **PASS** |
| make ui-check | `make ui-check` | green | tsc, vitest 75/75, vite build, ruff, mypy (3 files) | **PASS** |
| make ui-e2e | `NOEMA_UI_PORT=30290 make ui-e2e` (fixture API); `NOEMA_UI_PORT=30295 make ui-e2e UI_STACK_STATE=~/.noema/stacks/noema-qae2-e2e.json UI_STACK_MOCK=1` (e2e stack) | all specs; every proxied response contract-valid | 12/12 and 12/12; 55 responses validated, 0 failures, both runs (+12/12 inside `qa-security`) | **PASS** |
| make qa-contract (Q1, Q2, Q3, X1) | `make qa-contract QA_CONTRACT_PROJECT=noema-qae2-contract QA_CONTRACT_PORT_BASE=30400 QA_CONTRACT_BUILD=0`, twice (before and after H3) | every test green; 0 server-side validator violations | docs-drift 8/8, sdk-compat 65/65; stack **131 passed** both runs (facade Python 14, retries 3, TypeScript 12, **input robustness 67/67 incl. the 18 NUL-id reproducers**, Jev 8, schemathesis stateless 25 operations + tally, stateful 1); schemathesis stateless **6,160 calls, 0 failures** (only 5xx: `resumeTurnEvents` 501 x65, allowed), stateful **1,701 calls, 0 failures**; server-side ContractValidator violations **0** | **PASS** |
| make qa-security (SE1-SE13) | `make qa-security QA_SEC_PROJECT=noema-qae2-sec QA_SEC_PORT_BASE=30500 QA_SEC_TAG=qae2 QA_SEC_BUILD=0 QA_SEC_UI_PORT=30590 QA_SEC_OUT=~/.noema/qae2-security QA_SEC_REPORTS=reports/qa/e2/qa-lead/security` (2 workers per api pod) | every part green | local **72/72**; stack: e2e 52/52, ui-e2e 12/12, `tests/security/stack` **31/31** (authz matrix 5/5: **356 cells, 356 equal to the oracle, 0 leaks**, the 18 `resumeTurnEvents` cells included; SE7 media injection 1/1; injection 11/11; RLS 2/2; cache isolation 3/3; edge and config 4/4; CSP 2/2; authn hygiene, trace scan, secrets scan); fuzz (SE9) **6/6** (12 min 22 s; one atheris `pdf.sanitize` crash input replayed through the jail -> `rejected:corrupt`, typed); SCA: pip-audit 0 of 243, npm 0 / 0, trivy api/worker 44 HIGH, media 4 CRITICAL + 94 HIGH, **all 51 ids waived, 0 unwaived, SE13 PASS**; SBOMs in `reports/qa/e2/qa-lead/security/sbom/`; "qa-security: all green" | **PASS** |
| make image-scan (infra, defect 29) | `make image-scan IMAGE_TAG=qae2 IMAGE_SCAN_OUT=~/.noema/qae2-image-scan` | exit 0: no CRITICAL outside the waivers | exit 0; unsuppressed: api/worker 0 HIGH/CRITICAL (63 MEDIUM, 62 LOW), media 0 HIGH/CRITICAL; suppressed (waived): api/worker 44 HIGH, media 4 CRITICAL + 94 HIGH; CycloneDX SBOMs 155 / 155 / 331 components | **PASS** |
| make qa-fault (F1-F12, S1, C3 fault scale) | `make qa-fault FAULT_PROJECT=noema-qae2-fault FAULT_PORT_BASE=30300 FAULT_IMAGE_TAG=qae2 FAULT_BUILD=0 FAULT_RESULTS=reports/qa/e2/qa-lead/fault-results.json` (2 workers per api pod) | 22/22 | **22 passed (36 min 8 s)**; numbers in section 4.2 | **PASS** |
| make test-media | `make test-media IMAGE_TAG=qae2` | green | 307 passed, 8 skipped (no network in the media image) | **PASS** |
| make qa-gates (L2/L3/Q8, L5, R4, Q9, SE7, M3, ownership) | `make qa-gates QA_GATES_PROJECT=noema-qae2-gates QA_GATES_PORT_BASE=30600 QA_GATES_TAG=qae2 QA_GATES_BUILD=0 QA_GATES_RESULTS=reports/qa/e2/qa-lead/gates-results.json QA_GATES_OUT=~/.noema/qae2-gates` | every part green | selftest 8/8, holdback 13/13, backup tail 1/1, replay 2/2, Q9 4/4, SE7 1/1, M3 2/2, ownership 0 unowned; "qa-gates: all green" (section 4.3) | **PASS** |
| design docs | `cd /codes/aghooai/docs/noema && ./check_docs.sh` | exit 0 | exit 0: mermaid, tsc, schema, pydantic, config, audit, openapi, bundle, prose, links PASS | **PASS** |

## 4. Gates in detail

### 4.1 Q1 (contract) and the security gates

- **Q1 = PASS** (was FAIL): the 18 deterministic NUL-id reproducers answer 404 / 400 (`test_nul_in_ids_and_strings_is_a_client_error`
  and the rest of `test_input_robustness.py`: 67/67; Wave E: 16 x 500), schemathesis stateless and stateful 0
  failures over 7,861 calls, 0 server-side validator violations, every e2e / UI / security / fault / gate response
  validated (0 violations). Tallies: `reports/qa/e2/qa-lead/contract-schemathesis-{stateless,stateful}.txt`. The
  generated uploads now reach the media pipeline (uploadFile 202 x48 in the stateful run) but most generated
  uploads still answer 415 (no accepted file extension: the contracts fixer's open B3 coverage gap).
- **SE1 = PASS** (was FAIL): 356 / 356 cells equal the oracle, 0 leaks (`~/.noema/qae2-security/authz-matrix.jsonl`;
  Wave E: 8 `resumeTurnEvents` mismatches). SEC-AZ-03 passes; SEC-AZ-02/04 stay unit-level.
- **SE13 = PASS** (was FAIL): no unaddressed critical CVE: pip-audit 0 / 243, npm 0, every HIGH/CRITICAL OS finding
  waived with a statement and a 2027-01-09 review date (51 ids), `make image-scan` exit 0. Residual: the media
  image keeps 4 waived CRITICAL (libxml2 and others, reachable only in the jailed tesseract child per the waiver
  statements; bookworm kept for libmagic, supply-chain-infra §5).
- **SE7 = PASS** (policy half; was NOT-RUN): one corpus in PNG (OCR), PDF text layer, scanned PDF and Markdown, 44
  captured model requests, 0 policy violations, every canary reached a model (`media-injection-findings.jsonl`:
  `unseen: []`), in `qa-security-stack` and in `qa-gates`; `injection_suspected` recall stays [P0].
- SE2 (RLS 2/2), SE3 L2 half (3/3), SE4 moto half (in local 72/72), SE6 (11/11, 52 captured requests, 51 canaries),
  SE8, SE9 (6/6), SE11 (12/12 three times, CSP 2/2), SE12 (secrets / trace / authn hygiene; the capture now holds
  all 4 worker metric files), SE15 (4/4): PASS, as in Wave E. gitleaks: the capture's 3,748 `aws-access-token`
  hits are the mock's records of moto STS AKIDs and the 19 `generic-api-key` hits in `db.sql` are idempotency owner
  ids (the triaged false-positive classes); the repo scan (git history, 47 hits) finds moto STS AKIDs in committed
  fault results, the fake keys and tokens of the redaction / secret-pattern / auth test fixtures, and strings in
  committed trivy JSON (`reports/qa/e2/qa-lead/security/gitleaks-repo.json`): no real credential.

### 4.2 Load gates (`run.sh` = section 1; results `reports/qa/e2/qa-lead/load/<run>.json`)

**L1 proxy overhead** — `run.sh e2-l1 --plan 50:300,100:300,200:450 --warmup-s 60 --settle-s 30`. Pass: upper 95%
CI of the TTFT overhead p50 <= 10 ms and p95 <= 25 ms at 200 streams per pod, n >= 10,000, valid window. Every
window valid (100% `holdback_ms` 0 with `spec_agrees`, driver and mock lag p99 <= 2 ms).

| streams/pod | n | turns/s | TTFT ovh p50 [CI] | p95 [CI] | p99 | e2e ovh p50 / p95 | api lag p99 / max | api CPU | load 1m mean | Wave E p50 / p95 |
|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 2,155 | 7.18 | 9.09 [9.04, 9.13] | 11.24 [10.95, 11.51] | 19.9 | 16.0 / 19.9 | 3.8 / 50 | 0.35 | 2.6 | 9.12 / 12.17 |
| 100 | 4,349 | 14.50 | 10.36 [10.31, 10.40] | 15.85 [15.44, 16.34] | 33.0 | 17.7 / 26.9 | 5.5 / 76 | 0.68 | 3.2 | 10.48 / 17.72 |
| **200** | **12,991** | 28.87 | **14.76 [14.66, 14.88]** | **49.79 [48.64, 51.50]** | 137.3 | 25.8 / 93.7 | 8.4 / 183 | 1.33 | 4.7 | 15.16 / 71.54 |

**L1 = FAIL** at 200 per pod (p50 14.8 > 10, p95 49.8 > 25); passes at 50 per pod; at 100 per pod p95 passes and p50
misses by 0.4 ms. p95 at 200 improved from 71.5 to 49.8 ms. What holds it is still the full GC (QL-5, perf owners):
at 200 per pod one gen-2 collection every 6.1 / 5.8 s per process at **118 / 130 ms p50 (max 149 / 167 ms)**, GC
5.9-6.7% of wall time (`/tmp/noema-qae2/bin/gc.py` on `api-{0,1}.gc`); api lag p99.9 133 ms.

**C3 slow clients** — `run.sh e2-c3 --plan 180:180,180:180,180:180 --warmup-s 60 --settle-s 120 --scenario
tests/load/scenarios/slow.yaml --sse-probe --side fast:20:1 --side slow:20:2:14500` (as Wave E: 180 normal streams,
window 1 + 20 heavy clients read at full speed = the baseline, window 2 the same 20 read at ~10% of the byte rate).
Stored status from Postgres (`/tmp/noema-qae2/bin/c3.py`).

| Criterion | Measured | Wave E | Verdict |
|---|---|---|---|
| normal TTFT p95 within 10% of the baseline | window 2 / window 1 = **0.976 [0.961, 0.996]** (349.5 vs 358.0 ms); vs window 0: 1.051 [1.037, 1.067]; 0 errors, every window valid | 0.989 | PASS |
| coalescing | 180 coalescing runs, 15,720 frames merged (both processes) | same | PASS |
| `client_too_slow` at 30 +/- 1 s | **60 of 60** slow streams, 30.000-30.175 s after the stream stayed blocked | 60/60 | PASS |
| ... and the turn ends (spec-04 §12: cancelled, stored `failed`) | **60 of 60 slow turns stored `failed` at 64.3-64.6 s** (the 80 fast-read heavy turns `completed` at 90.3-90.4 s); api logs: 60 "client too slow" lines, 60 uvicorn `ASGI callable returned without completing response` ERROR lines (the documented cost of the bounded closing send, edge-fixer §2), 0 tracebacks | 60/60 `completed` at 90.3-90.5 s (QL-1) | **PASS** (was FAIL) |
| queue high-water <= 256 events / 1 MiB | 254 events; **1,052,270 B** in both processes (1 MiB + 3,694 B: one ~4 KB frame past the mark) | same | events PASS; bytes FAIL by one frame (QL-3, open: streaming `sse_writer._reader`) |

**C3 = PARTIAL** (was FAIL): QL-1 is fixed at load scale (and at fault scale: 4/4 slow turns `failed` at 47.4-47.9 s,
8/8 normal completed, TTFT p95 817 vs 979 ms baseline); only QL-3's one-frame overshoot of the 1 MiB byte bound
remains. Pod RSS in window 2: 2.8-2.9 GB (Wave E 3.1 GB: the slow turns now end at ~64 s).

### 4.3 Fault rows (`make qa-fault`, 2 workers per api pod; `reports/qa/e2/qa-lead/fault-results.json`)

S1a (zero duplicated client-visible text) is asserted in every row before its other assertions: 0 problems.

| Row | Pass criterion (spec-10 §4) | Measured | Wave E final | Result |
|---|---|---|---|---|
| F1 throttling | >= 99% of failed-first turns served, no cross-tenant impact | fallback **60/60** started and served (gap p50 2.4 / p95 14.9 ms); tenant B TTFT p95 648.1 vs 652.3 ms clean | 15/39 | **PASS** (was FAIL) |
| F2 access | typed 503, no retry, 100% classified | F2a 3 variants x 20: 503 `tenant_access_misconfigured`, 0 mock requests; F2b 0 Sol requests (no fallback); F2c members degraded, 100/100 completed | PASS | PASS |
| F3 5xx burst | opens in the 30 s window, half-open, recovers | open **7.93 s** (77 failed requests before), recovered 9.05 s after the window; 164/164 after recovery | 3.93 / 5.09 s | PASS (slower to open with 4 processes: each counts its own failures) |
| F3b p95-TTFT rule | opens on latency alone, closes after the restore | open 6.86 s after the slowdown (not during the baseline), closed 0.21 s after the restore; 42 slow turns refused without a model call | 4.4 / 14.8 s | PASS |
| F4 hang | TTFT timeout -> fallback >= 99%; no turn > deadline + 1 s | F4a **50/50**, e2e max 11.6 s (deadline 30 s); F4b 100/100 typed `all_routes_unavailable`, e2e max 5.4 s (deadline 10 s) | 40/53 | **PASS** (was FAIL) |
| F5 stall | post-commit typed + partial kept; pre-commit fallback | pre **100/100**; post 100/100 `upstream_stalled` retryable, partial rendered and stored (20/20 checked) | 40/52 pre | **PASS** (was FAIL) |
| F6 stream exception | post-commit typed, pre-commit fallback, 100% classified | pre **80/80**; post 5 types x 20 `all_routes_unavailable` retryable | 20/47 pre | **PASS** (was FAIL) |
| F7 reasoning-only / empty text | nothing committed from GLM, fallback, no empty answer | **50/50 + 50/50** served, 0 committed from GLM, 0 empty answers | 40/100 | **PASS** (was FAIL) |
| F8 malformed | member error / fallback, no 500 | attempt rows **40/40 + 40/40** (crc, trunc), 0 x 500; members get their codes, 16+16 turns completed | 30/40 | **PASS** (was FAIL) |
| F9 members down | 100% served, flagged | 200/200 served, every answer `unavailable`, reason `decision unavailable: ...` | PASS | PASS |
| F10 Redis down / hung | availability, local buckets, resume disabled; cross-pod attach waits then 410 | 125/125 + 125/125 completed during the outages, `admission_degraded` 4, L2 misses counted; same-pod cancel 202, cross-pod cancel 500 (as specified); **cross-pod attach 410 `resume_window_expired` after 2.5 s / 3.3 s** | attach 500 at once | **PASS** (was FAIL) |
| F11 Postgres down 60 s | fail fast, ephemeral turns continue, no data loss | `store: true` 500 `internal_error` retryable at ingress for 60 s; `store: false` turns completed through the whole outage (0 failures); 80/80 finalized turns stored once, one usage line each; 10/10 in-flight turns completed | PASS | PASS |
| F12 retry storm | amplification <= 1.1x (R6: against the failing upstream) | rate 0.5 (gated): **0.223** requests per turn; per upstream Opus 1.000, GLM 1.000, Sol 1.149 (floor regime, below). Rate 0.25 (R6 measure, H2): per upstream **Opus 1.000, Sol 1.078, GLM 1.000** (requests per turn 1.204, each fallback a first send) | 0.283 / 1.047 | PASS |
| S1b pre-commit fallback >= 99% | every fallback row | 100% in F1, F4, F5, F6, F7, F8 (470 of 470 failed-first turns served) | 37.5-80% | **PASS** (was FAIL) |
| C3 at fault scale | slow turns cancelled, others unaffected | 4/4 slow stored `failed` at 47.4-47.9 s; 8/8 normal completed, TTFT p95 817 vs 979 ms baseline | 4/4 `completed` | **PASS** (was FAIL) |

F12 at rate 0.5, per upstream: Sol 1.149 (first sends below 100 per account and minute, so IMPL-016's floor of 10
re-sends per (account, model) and minute applies, not the 10% ratio; the resilience fixer measured 1.19 / 1.114).
Not gated by spec-10's row (requests per turn), recorded as a residual for the spec owner (section 5).

**C1 fan-out against a per-tenant Bedrock quota** — config v4: t_acme `model_quotas.sol-6.1` = {rpm 600, tpm 1e11};
`run.sh e2-c1 --plan 200:300 --warmup-s 60 --mock-cpus 16-17,40-41 --mock-quotas '[{"model":
"us.openai.gpt-6.1-sol", "rpm": 600, "akid": "*"}]'`: the deployment's 2 processes (2 STS sessions, 2 AKIDs) against
ONE account-scoped mock window (QL-4's `akid: "*"`), one Converse mock process, the full router-default fan-out (15
decision calls per turn). Pass: `aghoo_reservation_overrun_total` = 0 and 0 mock `ThrottlingException`s while
admission answers 429. Counts from the mock records (`/tmp/noema-qae2/bin/c1.py`).

| Quantity | E2 (`e2-c1`) | Wave E (`e5-c1`, GCRA, per-AKID mock) |
|---|---|---|
| turns sent / served | 28,998 / 2,218 (window valid) | 40,996 / 3,088 |
| admission 429s | 26,780 `rpm_exceeded` (driver); server 26,962 `rpm_exceeded` + 259 `queue_timeout` | 37,908 |
| Sol calls at the mock / mock throttles | 2,782 / **0** (all 44,512 mock requests `ok`) | 4,005 / 0 (only because of 2 AKIDs; 874 with one) |
| busiest 60 s, all AKIDs (per AKID) | **562** (293 / 269) against rpm 600 | **915** (464 / 451) |
| Sol calls per minute from the first | 562, 483, 426, 421, 443, 447 | 915, 686, 605, 600, 600, 599 |
| overrun counter | absent on both processes (0) | 0 |

**C1 = PASS** (was FAIL). One admission Redis timeout on one process sent it to the local buckets (1/6 of each limit,
60 s hysteresis) once in the run, as in Wave E (QL-5: GC pauses exceed the 50 ms admission Redis bound); the window
still held. The sustained rate (~420-450 Sol calls/min against 600) is the accepted throughput cost of worst-case
reservations under the window (admission-estimator §1, spec-11 §1.2).

**C5 fairness: one tenant flooding** — config v3/v5: t_beta `concurrent_streams_per_project` 40; `run.sh e2-c5 --plan
100:300,100:300 --warmup-s 60 --settle-s 60 --side flood:t_beta:200:1` (victim t_acme 100 streams per pod in both
windows; window 1 adds t_beta at 200 streams = 5x its limit). Pass: victim client TTFT p95 <= 1.10 x solo (bootstrap
95% CI of the ratio, `/tmp/noema-qae2/bin/ratio.py`). It failed, so it was re-run, then A/B'd against the Wave E
code (the `:qalead` image's package, run in the image and on the venv through `PYTHONPATH`; checked live: an unknown
turn's `/events` answered the Wave E 501, not E2's 404). Results: `reports/qa/e2/qa-lead/load/e2-c5*.json`:

| Run | code / api | victim p95 solo -> flood (ms) | ratio [95% CI] | victim ingress p95 / decision p95 under the flood (ms) | flooder 200 / 429 | victim errors | windows |
|---|---|---|---|---|---|---|---|
| `e2-c5` (gated form, 300 s windows) | E2 / venv | 315.2 -> 386.3 | **1.226 [1.214, 1.244]** | 70.4 / 152 | 2,119 / 24,354 | 0 | valid |
| `e2-c5-run2` (same) | E2 / venv | 314.5 -> 377.2 | **1.199 [1.181, 1.213]** | 61.4 / 137 | 2,118 / 23,445 | 0 | valid |
| `e2-c5-ab-qae2` (180 s windows) | E2 / image | 318.5 -> 338.5 | 1.063 [1.050, 1.076] | 26.1 / 89 | 1,400 / 15,514 | 0 | valid |
| `e2-c5-ab-qalead` (180 s) | Wave E / image | 317.8 -> 410.2 | 1.291 [1.271, 1.309] | 95.6 / 166 | 1,400 / 16,863 | 0 | valid |
| `e2-c5-ab-venv-qalead` (180 s) | Wave E / venv | 316.2 -> 365.1 | 1.155 [1.128, 1.181] | 45.5 / 131 | 1,400 / 17,482 | 0 | valid |
| Wave E `e5-c5` (for reference) | Wave E / venv | 316.9 -> 341.1 | 1.076 [1.068, 1.092] | 26.5 / 93 | 2,115 / 26,051 | 0 | valid |

**C5 = FAIL** (was PASS): the gated form fails twice on the E2 tree (1.20-1.23). It is **not an E2 regression**: the
Wave E code fails the same way on today's host (1.155 on the venv, 1.291 in the image) and the E2 image passed once
(1.063). The ratio spreads 1.06-1.29 across runs of either tree, so Wave E's single 1.076 sat inside a spread that
crosses the bound. In every failing run the victim's TTFT moves through ingress (p95 26 -> 45-96 ms) and the
decision stage (p95 89 -> 131-166 ms) while GC, shed counts per kind and datastore CPU (Valkey ~6%, Postgres ~13%
of a core, `docker stats` every 5 s in `e2-c5-run2`) do not change: the flooder's ~65-80 rejected createTurns per
second (fair-queue `queue_timeout` after its 2 s wait, and per-process-cap sheds) cost the shared processes' event
loops enough to reach the victim. Owner: api overload / ops-admission (cost of a shed request; e.g. shed before
authentication-heavy work, or bound the waiting flooders per process) and perf; this role cannot change product
code. Raw runs: `/tmp/noema-qae2/runs/e2-c5*`.

**L4 decision quorum** (not in the E2 brief; re-run because the contracts fixer moved the mock's probe table to the
live cassettes: DeepSeek 300-560 ms + 2% 4-12 s tail, Luna 350-610, Haiku 640-950; Wave E: Haiku 890-1,800) —
config v6 (= v2), `run.sh e2-l4 --plan 50:330 --warmup-s 60 --scenario tests/load/scenarios/decision.yaml`. Pass:
p95 time to the commit-critical quorum <= 1,000 ms, quorum met >= 99%, n >= 2,000 turns. Quorum from
`aghoo_decision_quorum_reached_seconds{profile="router-default"}` against `aghoo_decision_seconds_count{stage="quorum"}`,
both processes (`/tmp/noema-qae2/bin/l4.py`; on Wave E's raw run it reproduces 2,728 / 2,814 = 96.9%).

| Quantity | E2 | Wave E |
|---|---|---|
| turns, errors | 2,379, 0 (20 decision calls per turn) | 2,379, 0 |
| quorum reached | **2,816 of 2,816 decisions** (the reached counter reads 2,818: it can exceed the decision count by a few, as in the contracts fixer's run, 2,357 / 2,356) | 2,728 / 2,814 = 96.9% |
| time to quorum | 766 within 0.5 s, 2,744 (97.4%) within 0.75 s, all within 1.0 s: p95 in (0.5, 0.75] s | p95 in (0.75, 1.0] s |
| members | Haiku 13,553 ok / 317 timeout (2.3%), DeepSeek 13,644 / 226, Luna 13,870 / 0 | Haiku 88% timeout |
| S1 `decision_ms` | p50 844 / p95 1,000 / max 1,049 ms (2,305 turns commit at `holdback_deadline`: early commit off, R5) | p50 1,001 / p95 1,003 |

**L4 = PASS** (was FAIL) on the mock's current probe table; the window is "invalid" by the scenario's design
(holdback), which does not touch the quorum counts. The probe rows rest on few live samples per model, and 06 §4
still asks for the deadline to be re-set from Phase 0 data (QL-6 stays a Phase 0 item, no longer a gate failure).

### 4.4 QA-harness gates (`make qa-gates`, stack `noema-qae2-gates`; `reports/qa/e2/qa-lead/gates-results.json`)

L2 / L3 / Q8 (`make qa-load-holdback`: n 200 per cell after a 50-turn ramp-in, concurrency 50, L1-shaped streams,
client TTFT on the raw stream, every body contract-validated; a bound met in an invalid window passes, a miss there
would be "invalid, re-run"):

| Cell | n | client TTFT p50 / p95 / p99 ms | bound ms | premise share | `holdback_deadline` | driver lag / mock lateness p99 ms | Result |
|---|---|---|---|---|---|---|---|
| L2 agree T_d=300 TTFA=300 | 200 | 318.1 / **328.8** / 336.4 | 350 | 1.0 | 0.0 | 1.1 / 78.63 (invalid) | PASS |
| L2 agree T_d=300 TTFA=720 | 200 | 729.0 / **731.4** / 746.2 | 770 | 1.0 | 0.0 | 1.09 / 1.29 | PASS |
| L2 agree T_d=300 TTFA=2400 | 200 | 2408.3 / **2411.4** / 2416.5 | 2450 | 1.0 | 0.0 | 1.12 / 1.55 | PASS |
| L2 agree T_d=900 TTFA=300 | 200 | 710.0 / **719.4** / 747.8 | 950 | 1.0 | 1.0 (hold cap) | 1.09 / 6.33 (invalid) | PASS |
| L2 agree T_d=900 TTFA=720 | 200 | 919.9 / **926.3** / 946.1 | 950 | 1.0 | 0.0 | 1.09 / 1.26 | PASS |
| L2 agree T_d=900 TTFA=2400 | 200 | 2408.9 / **2413.0** / 2417.2 | 2450 | 1.0 | 0.0 | 1.13 / 1.51 | PASS |
| L3 disagree T_d=300 TTFA=300 | 200 | 622.8 / **641.5** / 669.9 | 700 | 1.0 | 0.0 | 1.09 / 1.23 | PASS |
| L3 disagree T_d=300 TTFA=720 | 200 | 1042.9 / **1055.4** / 1075.6 | 1120 | 1.0 | 0.0 | 1.09 / 1.35 | PASS |
| L3 disagree T_d=300 TTFA=2400 | 200 | 2721.6 / **2732.7** / 2761.8 | 2800 | 1.0 | 0.0 | 1.1 / 1.32 | PASS |
| L3 disagree T_d=900 TTFA=300 | 200 | 1223.9 / **1234.3** / 1261.3 | 1300 | 0.996 | 0.0 | 1.09 / 1.62 | PASS |
| L3 disagree T_d=900 TTFA=720 | 200 | 1643.7 / **1652.8** / 1677.4 | 1720 | 1.0 | 0.0 | 1.1 / 1.36 | PASS |
| L3 disagree T_d=900 TTFA=2400 | 200 | 3324.0 / **3338.4** / 3371.2 | 3400 | 1.0 | 0.0 | 1.14 / 4.5 (invalid) | PASS |
| Q8 late disagreement T_d=900 TTFA=300 | 200 | 709.3 / **714.8** / 740.1 | hold cap 700 | 1.0 | 1.0 | 1.08 / 1.49 | PASS; stored `served_spec_differs` true 200/200 |

| Gate | Measured | Result |
|---|---|---|
| L5 (`make qa-load-backup-tail`) | n 5,000 (+50): client p50 536.6 / p95 613.5 / **p99 941.5** / max 4,925.9 ms (bound 1,500); 5,050 `predicate:ok`; mock: 10,100 DeepSeek calls, primaries 4,951 ok + 99 cancelled in the tail, backups the converse; driver lag p99 1.9 ms (valid) | PASS |
| R4 (`make qa-replay`) | replay set 40 x 5: 200/200 served, restarts 46 = scripted 46, share 0.23, waste 0.404 of generation cost (3,138 wasted output tokens = the sum of `timings.wasted_output_tokens`); reported against the P0 10% / 5%, not gated. Idempotent replay: 60/60 (20 SSE, 20 JSON, 20 decisions) `x-aghoo-idempotent-replay: true`, identical, 0 model calls, usage by line unchanged | PASS (meas) |
| Q9 (`make qa-hier-deadline`) | lightweight-model-decide and router-default, n 200 each: `deadline_exceeded` share 1.0 at 1,000 ms, 0.0 at 3,000 ms (all `choice:hierarchical`) | PASS |
| SE7 (`make qa-media-injection`) | 0 policy violations, every carrier reached a model (also in `qa-security-stack`) | PASS |
| M3 (`make qa-ocr-cer`, media image) | CER 0.0 on the probe `number.png`, `invoice.pdf` p1 and 3 clean renders (aggregate 0.0); degraded: 14 px 0.0, noise 0.0, JPEG q30 0.0, 3-degree rotation 0.552 | PASS (meas) |
| ownership, every tracked path | 1,559 tracked, 0 unowned | PASS |

## 5. Gates still FAIL or PARTIAL, and residuals (owners)

| Gate / item | Status | What is left | Owner |
|---|---|---|---|
| L1 | FAIL | 200/pod p50 14.8 / p95 49.8 ms vs 10 / 25: full GC 118-130 ms every ~6 s per process (QL-5); the same stalls trip admission's 50 ms Redis bound once per run (C1) | perf owners (core, api) |
| C5 | FAIL | victim p95 ratio 1.20-1.23 in the gated form; spread 1.06-1.29 over both trees: not an E2 regression; shed flood traffic (~70 rejected createTurns/s) slows the victim's ingress and decision stage | api overload / ops-admission, perf; spec owner if the C5 flood shape (closed-loop retry after `retry-after`) is to be restated |
| L6 | FAIL (live-n) | live only, not re-run (no live call in E2) | perf; Phase 0 |
| K1 | FAIL (tokens, live-n) / [P0] cost | per-call 2% not reachable with two parameters per model (max 5-10%); not re-measured live | Phase 0 refit; platform / integrator for the table override wiring; live-smoke for `input_chars` in cassettes |
| C3 | PARTIAL | queue high-water 1 MiB + one frame (QL-3) | streaming (`sse_writer._reader`) or spec-10 C3 wording |
| R5 | PARTIAL | CR #26 not re-verified live; quality [P0] | live-smoke; Phase 0 |
| M2 | PARTIAL | no single per-reason event + `attempt_id` test | media-negotiation / generation |
| F12 residual | PASS (gated row) | per failing upstream at rate 0.5, Sol 1.149: IMPL-016's floor (10 re-sends per account-minute) exceeds 1.1x below 100 first sends; R6 states the gate per upstream | spec owner (IMPL-016 floor vs R6 wording) |
| C2, C4 | PASS (Wave E) | not re-run in E2 (not in the brief); pool / datastore latency still not measurable (R4 metrics) | telemetry registry + composition (R4) |
| Harness | - | (a) the gate stack's mock stalled once after start (L2 300/300 lateness p99 78.6 ms; bound met): qa-tools finding 3 (mock record ring); (b) the stateful schemathesis flake is fixed by H3 | qa-tools |
| Known cost | - | one uvicorn `ASGI callable returned without completing response` ERROR line per too-slow client (60 in C3) | api (edge) |
| Orchestrator | - | the spec agent's discrepancy: R7 says U+0000 in stored text becomes U+FFFD, while session `metadata` holding U+0000 answers 400 `schema_violation` (edge fixer); Q1 is green either way (both are declared outcomes) | orchestrator ruling |

## 6. Teardown, final checks, what is left

- Final `make check` after the last harness edit (H3), 03:45: all green (section 3). Tree digest at 03:51: `0be5e6d7b39287cb`, unchanged.
- Every stack of this role is down with its volumes: `noema-qae2-{contract,e2e,fault,sec,gates,perf}` (`docker ps -a` and
  `docker volume ls` show nothing named `qae2`); the `make test-media` projects removed themselves; no load process
  left; the shared `noema-dev` services untouched.
- Left on purpose: the images `noema-{api,worker,media,media-test}:qae2`, the state files
  `~/.noema/stacks/noema-qae2-*.{json,env}` (0600; stale once their stack is gone), raw logs and runs in
  `/tmp/noema-qae2/` (incl. the extracted Wave E package used for the C5 A/B), security artifacts in
  `~/.noema/qae2-security/`, image-scan output in `~/.noema/qae2-image-scan/`.
- Files of this role: `make/qa-security.mk` (H1), `tests/fault/test_f12_retry_storm.py` (H2),
  `tests/qa/contract/test_schemathesis_stateful.py` (H3), `tests/load/scenarios/decision.yaml` (H4, comment), this
  report, `reports/qa/e2/qa-lead/**`, and `reports/qa/wave-e-summary.md` (updated in place).
