# QA lead (Wave E, E5): final load gates and the full regression

Role: QA lead, final gates, 2026-10-11. Gate ids: 06 §11 (the design repo's `06-validation-plan.md`) as mapped in
spec-10 §3. No live Bedrock call (the wire mock only), no AWS resource touched (moto only), the shared `noema-dev`
services untouched. Edits: `tests/load/**` (harness additions, section 6) and `reports/**` only. Analyzed results of
every load run: `reports/qa/load/<run>.json` (each with `provenance`); raw data in `/tmp/noema-qalead/runs/<run>`
on the build host (large, not in the repo).

## 1. Setup

- **Code under test**: the working tree on top of `b85f318` (tag wave-c2b) with every Wave E change, as left by the
  fixers (last product edit 12:33 UTC; `find src tools deploy -newermt 12:40` is empty for the whole session, so
  every run below measured the same product code). `provenance.code.worktree_digest` changes between runs only
  through this role's own `tests/load` files (the C3/L4 scenarios, the probe).
- **Deployment as configured now** (docs/modules/integration.md §5.1, E sprint B): one 2-vCPU pod = **2 api processes**
  (`run.py --api-procs 2`: two `tests.load.serve` processes, i.e. the image CMD's uvicorn run programmatically, on one
  inherited listening socket, as the 2 workers of `WEB_CONCURRENCY=2`), each with
  the default per-process cap `NOEMA_MAX_TURNS_PER_PROCESS` = 120 (tenant-fair, edge fix 11), the composition's GC
  setup (`gc.freeze()` after startup, thresholds `(700, 10, 40)`), no trace exporter. The api runs on the dev venv's
  CPython 3.12.12 (the image's CPython 3.12.15 is ~22% slower per turn, C2b R5; `--api-image` cannot run several
  processes yet, sprint B F5): **the numbers below are optimistic for the image by about that factor**.
- **Stack**: compose project `noema-qalead-perf` of my own (postgres, valkey, moto; ports 301xx), seeded by
  `tools/bootstrap_dev.py`, load config v2 = the bootstrap config with lifted tenant limits (`tests.load.stack
  activate`; digest `sha256:848146b0...de4b6`, the same as C2b and sprint B). C5 and C1 ran on their own activations
  (v3: t_beta `concurrent_streams_per_project` 40; v4/v6: t_acme `model_quotas.sol-6.1` rpm 600), then v5/v7 = v2
  again.
- **Placement** (pinned, physical-core isolation; lscpu: CPU n and n+24 are one core): api pod on CPUs 46-47 (cores
  22-23; their siblings 22-23 left idle), Postgres + Valkey on 44-45 (siblings 20-21 idle), **8 mock processes**
  (4 Converse on 16-19, 4 Mantle on their siblings 40-43, SO_REUSEPORT), drivers on 4-15 and 28-39 (50 streams per
  process). The first two ramps used 4 mock processes (1 and then 2 CPUs each): their windows at >= 200 streams
  were invalid by spec-10 §5 (mock lag p99 4.1 and 4.6 ms > 2 ms), so they are kept as diagnostics only
  (`diag-ramp-mock1cpu.json`, `diag-ramp-mock4.json`); with 8 mocks the worst mock process stayed <= 1.6 ms up to
  200 streams.
- **Quiet host**: 48 vCPU shared with other projects (a chroma-mcp, a moto, an aster Postgres: ~1 core in total,
  unpinned). Every run started only once the 1-minute loadavg was < 4 (the runner script waits up to 15 min);
  `provenance.loadavg_at_start` and `resources.loadavg_1m` per window are in each results file (window means
  2.7-5.5 in the gated windows; my own drivers, mocks and api are part of it).
- **Quantities** (spec-10 §5, CLOCK_MONOTONIC shared by every process): TTFT overhead = client TTFT - mock TTFT, per
  turn; percentiles with bootstrap 95% CIs (1,000 resamples), written `p [low, high]`; validity per window: >= 99.9%
  of turns with `holdback_ms` = 0 and `spec_agrees`, every driver and every mock process lag p99 <= 2 ms. The api's
  event-loop lag is the harness's 10 ms sampler in both processes (merged); `aghoo_event_loop_lag_seconds` is a
  gauge (one value per scrape), recorded in each step's metrics file. Every driver validates the first turn of each
  stream and every non-200 with the `ContractValidator`; the C3 clients validate every complete frame.
- **Runner**: `/tmp/noema-qalead/run.sh <run> <args>` = `python -m tests.load.run --state
  ~/.noema/stacks/noema-qalead-perf.json --out /tmp/noema-qalead/runs/<run> --api-port 30190 --mock-port 30181
  --api-cpus 46-47 --api-procs 2 --stack-cpus 44-45 --mock-cpus 16,40,17,41,18,42,19,43 --driver-cpus 4-15,28-39
  --results reports/qa/load/<run>.json`.

## 2. Verdicts

Load gates (section 3; every number from this role's runs on the final tree, deployment as configured):

| Gate (06 §11 / spec-10 §3) | Command (`run.sh` = section 1) | Pass criterion | Measured | Result |
|---|---|---|---|---|
| L1 proxy overhead (06 §4) | `run.sh e5-l1 --plan 50:300,100:300,200:450` | upper 95% CI of TTFT overhead p50 <= 10 ms and p95 <= 25 ms at 200 streams/pod, n >= 10,000, valid window | 200/pod: p50 **15.16 [14.99, 15.31]**, p95 **71.54 [68.21, 73.99]**, n 12,974, valid; 100/pod 10.48 / 17.72; 50/pod 9.12 / 12.17 (passes there) | **FAIL** |
| C2 ramp 0 -> 1,000, loop lag (06 §5) | `run.sh e5-ramp --plan 50:120,...,1000:120` (2 min holds, shortened) | api lag p99 < 50 ms at every step <= 200 streams/pod; the rest measured | 3.9 / 5.4 / 6.8 / 8.0 ms at 50 / 100 / 150 / 200 (all valid); past it 10-49 ms (invalid windows, measured); capacity 34.3 turns/s, typed 429 shedding, **0 x 500 up to 1,000 streams** | **PASS** (lag); pool/datastore latency NOT measurable (no metric, R4) |
| C3 slow clients (06 §5) | `run.sh e5-c3 ... --side fast:20:1 --side slow:20:2:14500 --sse-probe` | coalesced frames; `client_too_slow` at 30 +/- 1 s; normal TTFT p95 within 10% of a baseline; queue <= 256 events / 1 MiB | normal p95 ratio 0.989 [0.971, 1.009]; coalescing 15,720 frames merged; detection 60/60 at 30.00-30.22 s; **but 60/60 slow turns stored `completed` (stream never ended, QL-1)**; queue 254 events, **1,052,270 B** (QL-3) | **FAIL** |
| C5 fairness (06 §5) | `run.sh e5-c5 --plan 100:300,100:300 --side flood:t_beta:200:1` (t_beta limit 40) | victim p95 TTFT <= 1.10 x solo | **1.076 [1.068, 1.092]**, 0 victim errors; flooder 2,115 served, 26,051 x 429 | **PASS** |
| C1 fan-out vs quota (06 §5) | `run.sh e5-c1 --plan 200:300 --mock-quotas ...` (+ `e5-c1-1proc`) | `aghoo_reservation_overrun_total` = 0; 0 mock ThrottlingExceptions while admission answers 429 | overrun 0 (all runs); 37,908 admission 429s; mock throttles 0 with 2 processes (2 AKIDs, QL-4) but **915 Sol calls in the first minute vs rpm 600**; with 1 process **874 mock throttles** (QL-2) | **FAIL** |
| C4 soak (06 §5), shortened 1 h | `run.sh e5-soak --plan 150:3600 --warmup-s 600` (60% of the 250-stream knee) | RSS growth <= 5% after 10 min warm-up; stuck attempts 0 | RSS **+0.7%** (731.3 -> 736.4 MiB); `aghoo_stuck_attempts_total` 0; 91,202 turns and attempts all terminal; 77,960 turns, 0 errors | **PASS** (24 h: NOT-RUN) |
| L4 decision quorum (06 §4; not in the brief) | `run.sh e5-l4 --plan 50:330 --scenario tests/load/scenarios/decision.yaml` | quorum p95 <= 1,000 ms; quorum met >= 99%; n >= 2,000 | p95 <= 1.0 s (histogram); **quorum met 96.9%** (2,728 / 2,814); n 2,379 | **FAIL** (QL-6) |
| spec-10 §5 run validity | every results file `steps[].validity` | >= 99.9% `holdback_ms` 0 with `spec_agrees`; every driver and mock process lag p99 <= 2 ms | valid: every L1, C3, C5, soak window and the ramp up to 200/pod; invalid (reported as measurements): ramp > 200, C1 1-proc, L4 (holdback by design of the scenario), the two 4-mock ramps (diagnostics) | PASS for every gated window |
| Q1 (load-layer sample) | the drivers' and C3 clients' `ContractValidator` | 0 violations | 0 in every run (first turn of every stream, every non-200: 402,519 x 429 in the e5 runs, every C3 frame) | PASS (sample) |

Regression on the final tree (section 4):

| Check | Command | Pass criterion | Measured | Result |
|---|---|---|---|---|
| make check | `NOEMA_AGENT_ID=qa-lead make -k check` | every CHECK_STEP green | all green but `ownership`: **19 unowned paths** (`make/qa-{contract,fault,security}.mk`, `tests/qa/contract/**`: OWNERS.yaml rules missing, platform/orchestrator); lint, typecheck (322), typecheck-tests (548), importlint 27/0, forbidden-scan, contracts-check, unit 3,186, conformance 757 (+8 skipped), integration 547 (+21 skipped), sdk-check TS 86 / Python 192 | **FAIL** (ownership only) |
| make e2e, twice | `make e2e E2E_PROJECT=noema-qalead-e2e E2E_PORT_BASE=30200 E2E_BUILD=0` (images `:qalead` = the tree) | 52/52 both runs | **52 passed (153.7 s), 52 passed (152.8 s)**; 2 uvicorn workers per api container | **PASS** |
| make ui-check | `make ui-check` | green | tsc, vitest 75/75, vite build, ruff, mypy (3 files) | **PASS** |
| make ui-e2e | `NOEMA_UI_PORT=30290 make ui-e2e` (fixture API) and on the e2e stack (`UI_STACK_STATE=... UI_STACK_MOCK=1`) | all specs pass; every proxied response contract-valid | 12/12 and 12/12; 55 responses validated, 0 failures, both runs | **PASS** |
| make qa-contract | `make qa-contract QA_CONTRACT_PROJECT=noema-qalead-contract QA_CONTRACT_PORT_BASE=30400 QA_CONTRACT_BUILD=0` | every test green; 0 server-side validator violations | 115 passed, **16 failed** (`test_nul_in_ids_and_strings_is_a_client_error`: a NUL in a path or body id -> **500**, CR item 6 / finding 17); schemathesis stateless (~6,000 calls) and stateful 0 failures; the 3 NUL-text cases now pass; 0 server-side violations | **FAIL** |
| make qa-security | `make qa-security QA_SEC_PROJECT=noema-qalead-sec ... QA_SEC_TAG=qalead QA_SEC_BUILD=0 QA_SEC_API_WORKERS=1 QA_SEC_REPORTS=reports/qa/final/security` | every part green | local 70/70; stack: e2e 52/52, ui-e2e 12/12, `tests/security/stack` 29 passed, **1 failed** (SE1 matrix: 0 leaks, 8 mismatches `resumeTurnEvents` 501 before visibility); fuzz 6/6; SCA **SE13 FAIL** (unwaived, unfixed Debian CVEs: api/worker 2 critical + 55 high, media 4 + 94) | **FAIL** |
| make qa-fault | `NOEMA_STACK_API_WORKERS=1 make qa-fault FAULT_PROJECT=noema-qalead-fault FAULT_PORT_BASE=30300 FAULT_IMAGE_TAG=qalead FAULT_RESULTS=reports/qa/final/fault-results.json` | 22/22 | 13 passed, **9 failed** (F1, F4, F5-pre, F6-pre, F7, F8-attempt: fallback share; F10 down/hung: cross-pod attach 500; C3); F11 now passes | **FAIL** |
| make test-media | `make test-media IMAGE_TAG=qalead` | green | 305 passed, 8 skipped (no network in the image) | **PASS** |
| design docs | `cd /codes/aghooai/docs/noema && ./check_docs.sh` | exit 0 | exit 0: mermaid, tsc, schema, pydantic, config, audit, openapi, bundle, prose, links PASS | **PASS** |

## 3. Load gates in detail

### 3.1 L1 proxy overhead (06 §4; spec-10 §3 L1, §5) — `e5-l1.json`

`run.sh e5-l1 --plan 50:300,100:300,200:450 --warmup-s 60 --settle-s 30`. Pass: upper bootstrap 95% CI of the TTFT
overhead p50 <= 10 ms and of p95 <= 25 ms **at 200 streams per pod**, n >= 10,000 first deltas, window valid.
Every window valid (100% `holdback_ms` = 0 with `spec_agrees`; worst driver lag p99 0.94 ms, worst mock 1.19 ms;
15 decision calls per turn in every turn).

| streams/pod | n | turns/s | TTFT overhead p50 | p95 | p99 | e2e overhead p50 / p95 | ingress p50 / p95 | api lag p99 / max | pod CPU (cores) | CPU ms/turn | RSS MiB first -> last | load 1m mean |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 2,155 | 7.18 | 9.12 [9.07, 9.16] | 12.17 [11.71, 12.60] | 19.6 [17.0, 25.0] | 15.8 / 20.9 | 8.5 / 10.9 | 4.0 / 48 | 0.36 | 50.4 | 387 -> 473 | 3.2 |
| 100 | 4,348 | 14.49 | 10.48 [10.43, 10.53] | 17.72 [17.25, 18.22] | 39.1 [31.1, 51.0] | 17.9 / 29.6 | 9.6 / 13.9 | 5.7 / 115 | 0.68 | 47.0 | 498 -> 594 | 3.9 |
| **200** | **12,974** | 28.83 | **15.16 [14.99, 15.31]** | **71.54 [68.21, 73.99]** | 142.7 [137.9, 148.6] | 26.8 / 142.6 | 12.6 / 54.6 | 8.7 / 188 | 1.33 | 46.0 | 647 -> 842 | 4.7 |

- **L1 = FAIL** at 200 streams per pod: p50 15.2 ms (bound 10), p95 71.5 ms (bound 25). It passes at 50 streams per
  pod (p50 9.12, p95 12.2) and misses p50 by 0.5 ms at 100 (p95 17.7 passes).
- Against sprint B's end state (2 processes, p50 29.9 / p95 108 at 200 per pod, invalid window) the final tree is
  about 2x better at 200 per pod (sprint A's lean decision and stream transport: 46-50 ms of pod CPU per turn instead
  of 61-62).
- What is left at 200 per pod is the full GC of each process: from `api-{0,1}.gc` in that window, one gen-2
  collection every 5.4 / 6.8 s at **139 / 118 ms (p50; max 180 ms)**, 7.2% / 5.3% of each process's wall time.
  Every turn whose first delta falls into a pause pays it: the api lag p99.9 is 135 ms, the TTFT overhead p95/p99
  71 / 143 ms, the ingress p95 55 ms. At 50 and 100 per pod the full GCs are 27-68 ms every 8-38 s.
- CPU per 1k tokens/s (C2's measured quantity): 1.326 cores for 28.83 x 400 = 11.5k tokens/s -> **0.115 cores per
  1k tokens/s**.

### 3.2 C2 ramp 0 -> 1,000 streams, event-loop lag (06 §5; spec-10 §3 C2) — `e5-ramp.json`

`run.sh e5-ramp --plan 50:120,100:120,150:120,200:120,250:120,300:120,400:120,600:120,800:120,1000:120
--warmup-s 60 --settle-s 30` (2-minute holds per step, **shortened and labeled as spec-10 C2 allows**; 06 §5 asks
10 min). Pass: api event-loop lag p99 < 50 ms at every step up to 200 streams per pod; the rest measured.

| streams/pod | n | turns/s | TTFT ovh p50 / p95 / p99 | e2e ovh p50 / p95 | api lag p99 / max | pod CPU | ms/turn | RSS MiB | fds max | 429s | valid |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 855 | 7.12 | 8.9 / 14.3 / 33.2 | 15.5 / 23.0 | **3.9** / 40 | 0.36 | 50.6 | 424 | 197 | 0 | yes |
| 100 | 1,747 | 14.56 | 10.0 / 15.9 / 25.7 | 17.3 / 25.9 | **5.4** / 67 | 0.67 | 45.8 | 534 | 331 | 0 | yes |
| 150 | 2,618 | 21.82 | 11.5 / 22.7 / 80.5 | 19.7 / 39.2 | **6.8** / 118 | 1.00 | 45.7 | 647 | 463 | 0 | yes |
| 200 | 3,456 | 28.80 | 14.2 / 41.7 / 125.7 | 25.1 / 76.5 | **8.0** / 168 | 1.32 | 45.9 | 767 | 597 | 0 | yes |
| 250 | 4,092 | 34.10 | 20.7 / 105.9 / 198.5 | 40.7 / 182.4 | 10.4 / 213 | 1.56 | 45.7 | 866 | 742 | 1,463 | no (mock lag 2.5 ms; holdback 99.88%) |
| 300 | 4,118 | 34.32 | 23.9 / 112.5 / 211.0 | 49.8 / 192.4 | 10.9 / 373 | 1.61 | 46.9 | 908 | 771 | 7,213 | no |
| 400 | 4,120 | 34.33 | 27.4 / 114.5 / 223.7 | 58.6 / 191.7 | 11.2 / 233 | 1.69 | 49.3 | 921 | 796 | 19,078 | no |
| 600 | 4,102 | 34.18 | 47.9 / 165.8 / 265.7 | 111.6 / 297.7 | 14.8 / 244 | 1.84 | 53.7 | 923 | 858 | 42,700 | no |
| 800 | 4,024 | 33.53 | 115.2 / 264.3 / 384.1 | 284.9 / 523.2 | 23.8 / 245 | 1.94 | 57.9 | 928 | 968 | 65,829 | no |
| 1,000 | 3,853 | 32.11 | 276.0 / 617.6 / 756.1 | 599.1 / 1,041.0 | 48.9 / 285 | 1.99 | 62.1 | 938 | 1,229 | 87,029 | no |

- **C2 (lag p99 < 50 ms up to 200 streams per pod) = PASS**: 3.9 / 5.4 / 6.8 / 8.0 ms at 50 / 100 / 150 / 200, every
  one of those windows valid. Above 200 the windows are invalid (a mock process's lag p99 2.5-3.7 ms; S1 after the
  first text in 0.1-31% of the turns), so they are measurements, not L1 points ("meas above", spec-10 C2).
- **Capacity and overload**: the pod tops out at **34.3 turns/s from 250 streams** (240 concurrent turns = 2 x the
  per-process cap 120); past it the overload guard sheds with 429 `queue_timeout` (`retry-after: 1`, typed,
  contract-valid): **0 x 500 and 0 non-429 errors at every step up to 1,000 streams**, goodput 32-34 turns/s, the
  loop lag p99 still < 50 ms at 1,000 (48.9). C2b's single process collapsed past ~150 streams (goodput 19 -> 9
  turns/s, 9,337 x 500 per window at 1,000).
- Measured quantities: memory per stream ~2.3 MiB (RSS 424 -> 767 MiB from 50 to 200 streams; it includes the 300 s
  resume window of sealed logs, which grows with turns/s), CPU per 1k tokens/s 0.115 cores (section 3.1), fds 197
  -> 597 (50 -> 200), 18 threads. Pool saturation and Redis/Postgres latency are **not measurable**: no pool or
  datastore latency metric is exported (C2b R4, still open); the only signal is the absence of pool/Redis timeouts
  (one admission Redis timeout in C1, section 3.5).

### 3.3 C3 slow clients (06 §5; spec-10 §3 C3) — `e5-c3.json`

`run.sh e5-c3 --plan 180:180,180:180,180:180 --warmup-s 60 --settle-s 120 --scenario tests/load/scenarios/slow.yaml
--sse-probe --side fast:20:1 --side slow:20:2:14500`. 180 normal L1 streams in every window; window 1 adds 20 heavy
clients reading at full speed, window 2 the same 20 clients reading slowly (`tests/load/slow.py`: a fresh session per
turn, a 90 s stream of 2,700 x 4,000-character chunks at 30 chunks/s, ~126 KB/s of SSE, as the fault suite's C3; the
slow ones read through a 16 KiB receive buffer, **measured 12,983 B/s p50 = 10.3% of the stream's byte rate**, and
close the connection after 110 s). The 120 s settle lets the previous window's 90 s turns end before the next
window. Server side, `serve.py --sse-probe-out` (`tests/load/sseprobe.py`) records the SSE writer's queue
high-water mark, its coalescing and, per `client_too_slow`, the time from the stream first staying blocked (the
writer's own `min(full_since, send_since)`) to the detection. The stored status of every heavy turn was read from
Postgres afterwards.

Pass (spec-10 C3): coalesced frames seen; `client_too_slow` at 30 +/- 1 s; the normal clients' TTFT p95 within 10% of
a baseline run; queue high-water <= 256 events / 1 MiB. Baseline (decided before the run): window 1 (the same 20
heavy streams, read at full speed), so that only the slow reading differs; window 0 (no heavy stream) is reported too.

| Window | normal n | client TTFT p50 / p95 [CI] | TTFT ovh p95 | api lag p99 | pod CPU | RSS MiB (last) | valid |
|---|---|---|---|---|---|---|---|
| 0: 180 normal | 4,675 | 313.8 / 336.3 [333.4, 340.3] | 34.6 | 7.6 | 1.19 | 677 | yes |
| 1: + 20 heavy, read at full speed | 4,680 | 315.7 / 357.7 [353.1, 362.7] | 54.4 | 8.7 | 1.33 | 2,207 | yes |
| 2: + 20 heavy, read at 10% | 4,676 | 315.1 / 353.9 [349.7, 359.9] | 50.7 | 8.2 | 1.32 | 3,133 | yes |

| Criterion | Measured | Verdict |
|---|---|---|
| normal TTFT p95 within 10% of the baseline | window 2 / window 1 = **0.989 [0.971, 1.009]** (bootstrap of the ratio); vs window 0: 1.052 [1.035, 1.071]; 0 errors among the normal turns in every window | PASS |
| coalescing | server: 180 coalescing runs, 15,720 frames merged (both processes); the slow clients received none of the merged frames within their 110 s (each read ~1.4 MB, the merged ~1 MiB frame sits behind ~4 MiB of kernel buffers) | PASS (server side) |
| `client_too_slow` at 30 +/- 1 s | **60 of 60 slow turns detected, 30.000-30.221 s** after the stream stayed blocked (probe; 60 "sse client too slow; stream closed" log lines) | PASS (detection) |
| ... and the stream ended (spec-04 §12: the turn is cancelled, stored `failed`) | **60 of 60 slow turns stored `completed` at 90.3-90.5 s** (the whole stream generated; attempts `committed`, finish `stop`), the same as the 80 fast-read heavy turns | **FAIL** (bug QL-1 = fault B2, still open) |
| queue high-water <= 256 events / 1 MiB | 254 events, **1,052,270 bytes** (1 MiB + 3,694 B: the queue is checked before a push, so it ends one ~4.2 KB frame past the mark) | events PASS; bytes FAIL by one frame (minor, QL-3) |

**C3 = FAIL**: everything holds but the end of the slow stream. `SseResponse.__call__` (`src/noema/api/sse_frame.py`)
still sends the final empty body (`more_body: False`) **before** `on_close`; that send waits for the transport to
drain, i.e. for the slow client to read its multi-MB backlog, so the cancel (`runner.request_cancel("failed")`)
never runs while the client stays connected, and the turn keeps generating for the full 90 s. The edge list's #24
("SseResponse on_close before the final send") is not in the tree. Memory: pod RSS 2.2 GB in window 1 and 3.1 GB in
window 2 (the heavy turns' sealed logs held for the 300 s resume window, plus in window 2 the slow connections'
unsent backlog); not a C3 criterion, reported because nothing bounds it per connection while the stream runs on.

### 3.4 C5 fairness: one tenant flooding (06 §5; spec-10 §3 C5) — `e5-c5.json`

Config v3: t_beta `concurrent_streams_per_project` 40 (every other limit lifted). `run.sh e5-c5 --plan 100:300,100:300
--warmup-s 60 --settle-s 60 --side flood:t_beta:200:1`: the victim t_acme runs 100 streams per pod in both windows;
window 1 adds t_beta at 200 streams = **5x its concurrency limit** (driver group `flood`, its own developer key; a
shed turn retries after `retry-after` on its session). Pass: victim p95 TTFT <= 1.10 x its solo run.

| Window | victim n | victim client TTFT p50 / p95 [CI] / p99 | victim TTFT ovh p50 / p95 | api lag p99 | pod CPU | victim errors | flooder |
|---|---|---|---|---|---|---|---|
| 0: t_acme alone | 4,310 | 310.4 / 316.9 [316.4, 317.3] / 336.0 | 10.3 / 16.6 | 5.7 | 0.69 | 0 | - |
| 1: + t_beta flood | 4,307 | 313.8 / 341.1 [338.4, 346.2] / 438.5 | 13.6 / 39.9 | 7.3 | 1.15 | 0 | 2,115 served (~40 concurrent), 26,051 x 429 `queue_timeout` |

- **C5 = PASS**: victim p95 ratio **1.076 [1.068, 1.092]** (bootstrap 95% CI of the ratio; the upper bound is below
  1.10), victim turns/s unchanged (14.37 -> 14.36), 0 victim errors, both windows valid. The flooder's sheds: per
  process 7,535 / 5,888 admission `queue_timeout` (the fair queue's 2 s wait on its full slot set) and 6 / 12,555
  per-process-cap sheds (`aghoo_turns_shed_total`; the tenant-fair cap, edge fix 11).
- Not a pass of the overhead: the victim's TTFT overhead p95 rises 16.6 -> 39.9 ms (the flooder's 40 admitted
  streams plus ~80 sheds/s cost the pod 0.46 cores); the gate is stated on the TTFT.

### 3.5 C1 fan-out against a per-tenant Bedrock quota (06 §5; spec-10 §3 C1) — `e5-c1.json`, `e5-c1-1proc.json`

Config v6: t_acme `model_quotas.sol-6.1` = {rpm 600, tpm 1e11}; the mock gets the same Bedrock quota
(`--mock-quotas '[{"model": "us.openai.gpt-6.1-sol", "rpm": 600}]'`: a sliding 60 s window per (AKID, model), spec-10
§2.4 / qa-tools). One Converse mock process (`--mock-cpus 16-17,40-41`) so that every Sol call meets the same
window. `run.sh e5-c1 --plan 200:300 --warmup-s 60 ...` (the deployment: 2 processes) and, as a check of the
mock's per-AKID scope, `e5-c1-1proc` (`--api-procs 1`, 150 streams). Every turn carries the full router-default
fan-out (15 decision calls). Pass: `aghoo_reservation_overrun_total` = 0 over the run, and 0 mock
`ThrottlingException`s while admission answers 429 ("throttles before Bedrock does").

| Run | turns sent / served | admission 429s (driver) | Sol calls at the mock | mock throttles | Sol calls in the busiest 60 s (all AKIDs / per AKID) | overrun counter |
|---|---|---|---|---|---|---|
| e5-c1 (2 processes, 2 AKIDs) | 40,996 / 3,088 | 37,908 `rpm_exceeded` | 4,005 | **0** | **915** / 464 and 451 | 0 (absent on both processes) |
| e5-c1-run1 (same, AKIDs not recorded) | 40,989 / 3,088 | 37,883 `rpm_exceeded`, 18 `queue_timeout` | 3,989 | 0 | 900 / - | 0 |
| e5-c1-1proc (1 process, 1 AKID; diagnostic) | 22,925 / 2,374 | 16,160 `rpm_exceeded`, 1,239 `queue_timeout` | 3,855 | **874** | 995 / 995 | 0 |

- Sol calls per minute after the first one (e5-c1): **915, 686, 605, 600, 600, 599**. Admission's GCRA bucket
  (`mdl:t_acme:sol-6.1:rpm`, spec-11 §1.2: T = 60,000 / limit ms, **tau = 60,000 ms**) admits a full burst of
  `limit` calls on top of the steady rate, so any 60 s window that starts with a full bucket (the start of traffic, or
  after a lull) can carry up to 2 x limit - 1 calls; measured 1.5x.
- With 2 processes the mock refused nothing only because each process signs with its own STS session (2 AKIDs,
  each at most 464 calls per 60 s): the mock's quota windows are per AKID, while a Bedrock quota is per account and
  region. With one process (one AKID) the same admission let **874 calls be throttled by the mock** while it also
  answered 17,399 x 429 itself; 3,142 turns ended `error{all_routes_unavailable}` (sol-fast has no fallback member),
  the Sol quota circuit opened 4 times, and at 150 streams on one process the loop lag p99 reached 32.6 ms with
  Postgres pool acquire timeouts (3 x 500 `internal_error` at createTurn after ~2 s, 2 turn finalizes queued in the
  outbox), 1 x 409 `session_busy` and one admission fallback to local buckets (window invalid).
- **C1 = FAIL**: the overrun half holds (reservation >= issued: 0 overruns in every run, `consume` never exceeded a
  reservation), the throttle-before-Bedrock half does not (QL-2). The deployment run's 0 throttles is a mock
  fidelity gap (QL-4), not a pass.

### 3.6 C4 soak, shortened (06 §5; spec-10 §3 C4) — `e5-soak.json`

`run.sh e5-soak --plan 150:3600 --warmup-s 600`: **1 h at 60% of the measured maximum** (the ramp's knee: the pod
serves at most 240 concurrent turns and tops out from 250 streams; 0.6 x 250 = 150 streams per pod), after a 10 min
warm-up (it covers the 300 s resume window filling). Labeled "shortened" as spec-10 C4 allows (06 §5 asks 24 h).
Pass: RSS growth <= 5% after the warm-up; `aghoo_stuck_attempts_total` = 0; every attempt reaches a terminal state.

| Quantity | Measured |
|---|---|
| turns in the 60 min window | 77,960 sent, 77,960 timed (21.66 turns/s), **0 errors**, window valid (worst driver lag p99 0.93 ms, mock 1.24 ms) |
| pod RSS (both processes, 5 s samples) | **731.3 MiB at the end of the warm-up -> 736.4 MiB at 60 min: +0.7%**; at 10 / 20 / 30 / 40 / 50 min 734.5 / 734.8 / 736.1 / 736.3 / 736.3 MiB (flat after ~30 min); fds 493 -> 507-510 |
| `aghoo_stuck_attempts_total` | absent from both processes' `/metrics` at the end (never incremented: 0) |
| terminal states (Postgres, every turn created during the run, warm-up included) | 91,202 turns, all `completed`; 91,202 attempts, all `committed` with `ended_at` set; 0 non-terminal (the count includes the first ~210 turns of the next run, all terminal too) |
| overhead at 150 streams (not gated here) | TTFT p50 11.96 [11.94, 11.98] / p95 23.63 [23.26, 24.00] / p99 94.2 ms; api lag p99 6.9 ms, p99.9 87.6 ms; pod CPU 1.01 cores |

**C4 (shortened) = PASS.** The 24 h soak itself is NOT-RUN (spec-10 allows the 1 h form; a 24 h run would need the
host for a day).

### 3.7 L4 decision quorum on the probe latencies (06 §4; spec-10 §3 L4) — `e5-l4.json` (not in the brief; run because nobody had)

`run.sh e5-l4 --plan 50:330 --warmup-s 60 --scenario tests/load/scenarios/decision.yaml`: L1's turns, but the decision
members on spec-10 §2.2's probe latency distributions (DeepSeek 470-590 ms with a 2% 4-12 s tail, Luna 640-880 ms,
Haiku 890-1,800 ms) instead of 50 ms; router-default deadline 1,000 ms as shipped. Pass: p95 time to the
commit-critical quorum <= 1,000 ms and quorum met in >= 99% of turns, n >= 2,000 turns. Quorum times from
`aghoo_decision_quorum_reached_seconds{profile="router-default"}` (observed when every commit-critical question has
its quorum) against `aghoo_decision_seconds_count{stage="quorum"}` (every decision), both processes, whole run.

| Quantity | Measured |
|---|---|
| turns | 2,379 in the window (330 s), 0 errors; 20 decision calls per turn (15 + 5 DeepSeek backups) |
| quorum reached | **2,728 of 2,814 decisions = 96.9%** (86 decisions reached S1 at the deadline without the quorum) |
| time to quorum (decisions that reached it) | 517 within 0.75 s, all 2,728 within 1.0 s (histogram buckets: the p95 lies in (0.75, 1.0] s; counting the 86 misses as > 1 s, the p95 over all decisions is still <= 1.0 s) |
| members | Haiku 1,731 ok / 12,139 `timeout` (its p5 890 ms: it misses the 1 s deadline ~88% of the time), DeepSeek 219 timeouts of 13,870 calls, Luna 25 of 13,870 |
| S1 (`decision_ms` = S1 - t_d0) | p50 1,001 / p95 1,003 / max 1,061 ms: with early commit off (R5) S1 waits until every commit-critical slot is final, i.e. the deadline whenever Haiku is late, so every turn committed at `holdback_deadline` (TTFT overhead ~410 ms) |

**L4 = FAIL** (quorum met 96.9% < 99%; the p95 half holds). 06 §4 itself says the 1,000 ms deadline is to be re-set
from Phase 0 data (P11 saw Haiku at 1.6-2.7 s); on the mock's probe latencies the quorum is DeepSeek + Luna, and their
joint tail (DeepSeek's 2% outliers beyond its backup, Luna's p95 880 ms) misses 3%.

## 4. Full regression in detail (final tree, 17:25-19:05 UTC)

Images `noema-{api,worker,media}:qalead` built with `make e2e-images IMAGE_TAG=qalead` from the working tree (a full
cache hit of a build of the same content; checked: `identity/authn.py`, `api/deps.py`, `composition.py` and
`api/sse_frame.py` in the image have the tree's sha256). Every stack had its own project and port base and was taken
down afterwards (`noema-qalead-e2e` 30200, `-fault` 30300, `-contract` 30400, `-sec` 30500). The fault and security
stacks ran **one uvicorn worker per api container** (`NOEMA_STACK_API_WORKERS=1` / `QA_SEC_API_WORKERS=1`): their
metric helpers still scrape only port 9464 (defect 24 and the QA-security item are open), so with 2 workers their
counter-delta assertions would read half the counters; e2e, UI and contract ran the deployment's 2 workers.

- **make check**: red only at `ownership` (the 19 unowned E1 QA paths; every path of this role is owned by `qa`). The
  items the brief listed as red before (mypy `api/deps.py:249`, the test typing errors, identity
  `test_cli_refusals`, `test_uvicorn_server` chunked cap) are green now.
- **make qa-contract** vs the contract track (`reports/qa/contract.md`): schemathesis stateless and stateful now 0
  failures (the track's B1 hits came from generated NULs); the deterministic NUL reproducers still answer 500 for 16
  of the 18 id cases (the 2 turn-id cases 404, as before), the 3 NUL-text cases pass now (engines fix 20, U+FFFD).
  Q2, Q3, X1 (docs drift, SDK compat) green again. Tallies: `reports/qa/final/contract-schemathesis-*.{txt,json}`
  (the suite also rewrote `reports/tmp/qa-contract/schemathesis-stateless.txt`).
- **make qa-security** vs the security track (`reports/qa/security.md`): SE1 leaks 2 -> **0** (engines fix 21), the 8
  `resumeTurnEvents` mismatches remain (edge #22); SEC-ISO-02's security event now emitted (engines fix 23: `test_forged_cross_tenant_cancel_emits_a_security_event`
  passes); SE13 unchanged (no waivers file, no fixed versions). gitleaks on the capture: 3,578 `aws-access-token` hits
  in `mock-requests.jsonl` (the mock's own AKID records of moto STS keys) and 19 `generic-api-key` hits in `db.sql`
  (`api-1:req_...` idempotency owner ids), the classes the security track triaged as false positives. Scanner outputs:
  `reports/qa/final/security/`; artifacts in `~/.noema/qa-lead-security/`.
- **make qa-fault** vs the fault track (`reports/qa/fault.md`, numbers `reports/qa/final/fault-results.json`):
  - S1 zero duplicated client-visible text: held in every row (asserted before each failing assertion), 0 HTTP 500 and
    0 `internal_error` events outside F11's specified ingress 500s;
  - S1 fallback success >= 99% fails as before (IMPL-016 retry budget vs the fallback gate, CR item 5): F1 15/39
    (tenant B unaffected: TTFT p95 676.6 vs 680.8 ms clean), F4 40/53, F5 40/52, F6 20/47, F7 40/100 (0 empty answers
    delivered), F8 30/40 (0 HTTP 500); every fallback that started was served;
  - F10 down and hung: >= 99.9% of turns completed (125/125), admission degraded, L2 bypassed, cancels as specified,
    but the cross-pod attach answers **500 after 6 ms / 507 ms** instead of waiting then 410 (IMPL-021; edge #25);
  - **F11 now passes** (was FAIL): `store: false` turns kept working for the whole 60 s outage (200/200 in the
    outage buckets; the key cache's stale entry, edge fix 31), 80/80 finalized turns stored exactly once;
  - F2, F3 (open 3.93 s, recovered 5.09 s after the window), F3b (open 4.4 s after the slowdown, closed 14.8 s after
    the restore), F5/F6 post-commit, F8 members, F9 (200/200 served), F12 (amplification 0.283 at rate 0.5, 1.047
    at 0.25): PASS; C3 at fault scale: 4/4 slow turns stored `completed` (QL-1).

## 5. Bugs and findings (this role; not fixed here: outside `tests/load/**` and `reports/**`)

| Id | Severity, owner | Finding | Repro | Fix idea |
|---|---|---|---|---|
| QL-1 (= fault B2, still open) | major; api (`src/noema/api/sse_frame.py`) | `client_too_slow` is detected at 30.0 s but never applied while the slow client stays connected: `SseResponse.__call__` awaits the final empty body send (`more_body: False`), which waits for the transport to drain the client's multi-MB backlog, before `on_close` (the turn's cancel). 60/60 slow turns stored `completed` after generating the whole 90 s stream | `run.sh e5-c3 ...` (section 3.3) or `make qa-fault FAULT_TESTS=tests/fault/test_c3_slow_clients.py` | on `client_too_slow` run `on_close` first, then skip or bound the closing send and close the transport (edge list #24) |
| QL-2 | major; ops-admission, spec-11 §1.2 (owner ruling) | Model-quota admission does not throttle before Bedrock: the `mdl:<tenant>:<alias>:rpm` GCRA bucket has tau = 60 s, so it admits a burst of `rpm` calls on top of the steady rate; 915 Sol calls reached the mock in the first minute against `model_quotas.sol-6.1.rpm` 600 (then 686, 605, 600 ...); with one STS session the mock's quota refused 874 calls and 3,142 turns ended `all_routes_unavailable` | `tests.load.stack activate --quota t_acme:sol-6.1:600:100000000000`, then `run.sh e5-c1 --plan 200:300 --mock-cpus 16-17,40-41 --mock-quotas '[{"model": "us.openai.gpt-6.1-sol", "rpm": 600}]'` (add `--api-procs 1` for the mock throttles) | a small burst for quota buckets (tau = T x b with b of the order of the bulkhead `ceil(rpm/6)`), or a sliding-window counter for `mdl:` buckets |
| QL-3 | minor; streaming (`sse_writer._reader`) or spec-10 C3 wording | The SSE queue ends one frame past its byte mark: high-water 1,052,270 B > 1 MiB (the queue is checked before a push); events stay <= 256 (254) | section 3.3 probe (`api-*.sseprobe.json`) | check `nbytes + len(frame) > queue_bytes` before pushing, or state the bound as "1 MiB + one frame" |
| QL-4 | minor; qa-tools (`tools/mockbedrock` quotas) | The mock's quota windows are per AKID, a Bedrock quota is per account and region; with 2 api processes per pod (2 STS sessions, 2 AKIDs) a per-account overshoot is invisible (C1's 2-process run: 0 mock throttles while 915 calls arrived in 60 s) | `e5-c1` (`mock-*.jsonl` `akid`) | an account-scoped quota (e.g. `akid: "*"` = one window for every AKID, or the account from the STS session) |
| QL-5 | observation; perf owners (core, api) | L1 at 200 streams per pod is held by the full GCs: 118-139 ms (p50, max 180) every 5-7 s per process (section 3.1). The same stalls exceed admission's 50 ms Redis bound: 1 fallback to local buckets (limit / 6 per process, 60 s hysteresis) in the whole L1 run, 36 in the ramp's steps from 400 streams (past the cap); with real limits a pod would then admit 1/6 of a limit | `api-*.gc`, api logs of `e5-l1`, `e5-ramp` | fewer objects per turn or an incremental GC (3.13+/3.14); a GC-aware admission bound |
| QL-6 | spec owner (06 §4, Phase 0) | On the probe latencies the router-default 1,000 ms deadline gives the commit-critical quorum in 96.9% of decisions (L4 needs 99%); Haiku misses it ~88% of the time | `run.sh e5-l4 --plan 50:330 --warmup-s 60 --scenario tests/load/scenarios/decision.yaml` | re-set the deadline from Phase 0 data, as 06 §4 says |

Still open from earlier tracks and visible here: no Postgres/Redis pool or latency metrics (C2b R4: C2's pool
saturation and datastore latency cannot be measured); the image interpreter ~22% slower than the venv (C2b R5) and
`run.py --api-image` limited to one process (sprint B F5), so the venv numbers above flatter the image.

## 6. Harness changes (`tests/load/**`, this role)

All additive; `pytest tests/load/test_harness.py` 14 passed (4 new tests), `ruff check` / `ruff format --check` /
`mypy --strict tests/load` clean (tests/load is part of `make typecheck-tests`).

| File | Change |
|---|---|
| `run.py` | `--side flood:<tenant>:<n>:<step>` / `slow:<n>:<step>[:<bps>]` / `fast:<n>:<step>` (client groups that run during one step; the main statistics keep only group `main`, `steps[].side` summarizes the others: status and error counts, violations, coalesced frames, read rate, turn ids); `--mock-quotas`; `--sse-probe` |
| `driver.py` | `--group` (recorded in every turn line) |
| `slow.py` (new) | C3 heavy-stream clients on raw sockets (reuses `tests/fault/slowclient.py`), every complete frame and every non-200 contract-validated |
| `sseprobe.py` (new), `serve.py` | `--sse-probe-out`: wraps the SSE writer's queue push, coalescing and finish to record high-water marks and the too-slow timing |
| `mockserve.py` | `--quotas` (the mock's per-(AKID, model) quotas); records carry `akid` |
| `stack.py` | `activate --limit <t>:<key>=<int>` and `--quota <t>:<alias>:<rpm>:<tpm>` on top of the lifted load limits |
| `scenarios/slow.yaml`, `scenarios/decision.yaml` (new) | C3 and L4 scenarios |
| `README.md`, `test_harness.py` | the new files and options; tests for the side syntax, the heavy turn body, the side summaries and the config overrides |

## 7. Teardown and what is left behind

Every stack this role started is down with its volumes (`noema-qalead-perf`, `-e2e`, `-fault`, `-contract`, `-sec`;
`docker ps -a` shows none, and no `qalead` volume); the shared `noema-dev` services were not touched; no load
process is left. Left on the host on purpose: the images `noema-{api,worker,media,media-test}:qalead`, the state files
`~/.noema/stacks/noema-qalead-*.{json,env}` (0600; stale once their stack is gone, a bootstrap re-seeds), the raw runs
in `/tmp/noema-qalead/runs/`, the security artifacts in `~/.noema/qa-lead-security/`.

Not done by this role: L2/L3 (holdback cells) and L5 (DeepSeek backup tail) have no load harness
(`tests/load/test_holdback.py`, `test_backup_tail.py` do not exist) and were not in any brief; R4 (`test_replay.py`)
likewise; the 24 h soak; an image-based load run with 2 processes (sprint B F5). The wave summary lists them as NOT-RUN.
