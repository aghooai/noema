# QA track: perf-streaming-runtime (Wave E, perf sprint B; 2026-10-11)

Details, method and every number: `reports/perf/e-sprint-b.md`; analyzed runs: `reports/perf/e-sprint-b/*.json`.
Code under test: `wave-c2b` plus only sprint B's files (snapshots, so sprint A's concurrent edits never entered a
measurement). No live Bedrock call, no AWS resource touched. Placement: api 46-47, mocks 40-43, stack 44-45, drivers
20-31; host shared (loadavg 4-18), so windows that fail spec-10 §5 validity are labeled diagnostic.

Runner (abbreviated `RUN`): `python -m tests.load.run --state ~/.noema/stacks/noema-perf-b.json --api-port 29890
--mock-port 29881 --api-cpus 46-47 --stack-cpus 44-45 --mock-cpus 40,41,42,43 --driver-cpus 20-31` from the
snapshot directory with `PYTHONPATH=<snap>/src` (`/tmp/noema-b/bin/run.sh`).

| Gate (06 §11 / spec-10 §3) | Command | Pass criterion | Measured | Result |
|---|---|---|---|---|
| L1 proxy overhead, 200 streams/pod | `RUN --api-procs 2 --plan 100:180,200:180 --warmup-s 45` (`final3-pod2`, end state) | upper 95% CI of p50 <= 10 ms and of p95 <= 25 ms, n >= 10,000, valid window | 200/pod: p50 29.9 [29.3, 30.6], p95 108.3 [104.8, 112.3], n 5,162, window invalid (holdback and mock-lag rules); 100/pod (valid): p50 13.2 [13.1, 13.4], p95 25.8 [24.8, 26.7] | **FAIL** (also not a formal gate run: n < 10,000, shortened window; E5 re-runs) |
| C2 ramp, event-loop lag p99 < 50 ms up to 200 streams | same run (no full ramp) | lag p99 < 50 ms at every step <= 200 streams/pod | 100/pod 11.3 ms (valid), 200/pod 17.1 ms (window invalid by mock lag); one process at 200 streams collapsed in C2b (285 ms) | **PARTIAL**: two steps measured, no 0-1,000 ramp, the 200 step invalid; E5 runs the ramp |
| C1 fan-out quota, C3 slow clients, C4 soak, C5 fairness | - | spec-10 §3 rows | not run in this track | **NOT-RUN**: E5 QA lead's final gates (brief), on the deployment configured here (`--api-procs 2`) |
| Q1 100% schema-valid responses (scope: this track's runs) | `make e2e` x4 (below); load drivers validate every stream's first turn and every non-200 | 0 invalid responses | e2e: 4 x 52 tests, every response through `CheckedClient`; load: 0 violations in every run, incl. 76,906 x 429 `queue_timeout` shed responses (whole runs, warm-up included); unit test of the 429 envelope with the response validator on | **PASS** for this track's scope (schemathesis and the other layers: contract track) |
| Overload (06 §5 ramp past the knee; C2b risk 4): shed before the pools time out | `RUN --plan 250:150 --warmup-s 30 [--api-env NOEMA_MAX_TURNS_PER_PROCESS=0]` | no 500 from pool exhaustion past the knee; typed 429 + `retry-after` | no cap: 1,672 x 500 in 150 s; default cap 120: 0 x 500, 18,027 x 429 (`retry-after: 1`), goodput 15.07 vs 15.02 turns/s, TTFT p50 1.0 vs 4.2 s; pod of 2 at 300 streams: 0 x 500, 7,982 x 429 | **PASS** (diagnostic windows: host load) |
| Deployment, 2 api processes per 2-vCPU pod (08 §9 sizing) | compose `WEB_CONCURRENCY=2`; `make e2e E2E_PROJECT=noema-e2e-b E2E_PORT_BASE=28600 E2E_BUILD=0` with images `noema-{api,worker}:sprintb` then `:sprintb2` | e2e green twice per build; per-pod capacity measured | e2e 52/52, 52/52 (first build), 52/52, 52/52 (end state); pod of 2 processes: 28.7 turns/s at 200 streams, no error (one process: collapse past ~150) | **PASS** |
| Regression: module tests | `make test MOD=streaming`, `MOD=api`, telemetry, composition, conformance (`tests/contract_internal`), integration streaming/api/composition/infra/turns | green | unit 732 passed; conformance 757 passed, 8 skipped; integration 104 passed, 10 skipped | **PASS** |
| Regression: `make check` (shared tree, 08:35) | `make -k check` (taskset 0-15) | all steps green | lint, typecheck, importlint (27 contracts kept), forbidden-scan, contracts-check, conformance 757, integration 532, sdk-check green; **failed**: typecheck-tests (4 errors) and test-unit (1 of 3,002: `tests/unit/adapters/test_prewarm.py`) in sprint A's in-progress adapter files, ownership (19 unowned paths of the contract track) | **PARTIAL**: no failure in sprint B's files; the rest belongs to concurrent tracks |

Changes and their per-change deltas, the GC threshold decision and the hand-overs (core `deadline_scope`, adapter
stream decoding, the missing trace exporter): `reports/perf/e-sprint-b.md` sections 3-7.
