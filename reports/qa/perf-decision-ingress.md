# QA track perf-decision-ingress (Wave E, perf sprint A, 2026-10-11)

Scope: CPU per turn and the serial overhead floor of one api process in this role's areas (adapters' transport,
ingress units of work, decision member runner). Details, method and every number: `reports/perf/e-sprint-a.md`.
No live Bedrock call by this role (live calls belong to the live-smoke agent); no AWS resource touched; the shared
noema-dev services untouched; this role's stacks (`noema-perf-a`, `noema-e2e-perfa`) torn down at the end.

Measured code: snapshots of wave-c2b (b85f318) plus this sprint's files only (other agents edit the tree
concurrently); `make check` ran on the live working tree.

## Gates

| Gate (06 §11 / spec-10) | Command | Pass criterion | Measured | Result |
|---|---|---|---|---|
| L1 proxy overhead (06 §4, spec-10 §3/§5) | `tests/load/run.py` via `/tmp/perfa/tools/run.sh <snapshot> <run> --plan 50:120,100:120` (ABBA campaign c1-c8, d1-d3) and the ramp `--plan 125:120,150:120,175:120,200:120` | upper bootstrap 95% CI of TTFT overhead p50 <= 10 ms and of p95 <= 25 ms at 200 streams per pod, n >= 10,000 first deltas | final code (a4, d1/d3): 50 streams p50 10.4 [10.3, 10.5] / 10.1 [10.0, 10.2] ms, p95 19.5 [17.8, 25.2] / 17.2 [16.5, 18.5]; 100 streams p50 18.9 / 18.3, p95 68.7 / 67.8; base (wave-c2b, c1/c8): 50 streams p50 14.2 / 13.9, p95 29.3 / 30.5. 200 streams (a3, ramp): p50 1,827 ms, saturated (capacity 21 turns/s, about 150 streams) | **FAIL** (not met at 200 streams; even at 50 streams the p50 CI upper bound is 10.2-10.5 ms; n < 10,000 per window: 120 s windows, labeled shortened) |
| C2 ramp, event-loop lag p99 < 50 ms up to 200 streams (06 §5, spec-10 §3) | the ramp above (`f-a3-ramp`, `f-base-ramp`), api lag from the harness's 10 ms sampler | lag p99 < 50 ms at every step up to 200 streams | a3: 32.7 ms at 125, 128.4 at 150, 240.6 at 175, 317.2 at 200; base: 174.6 at 125 (saturated) | **FAIL** (holds up to 125 streams, was < 125) |
| C2 measured quantities (CPU per 1k tokens/s, memory per stream) | same | measured | CPU per turn (400 tokens): base 63.8-67.6 ms -> a3 49.4-52.9 ms (ABBA means); RSS 499 MiB at 200 streams (a3) | measured (no threshold) |
| spec-10 §5 run validity | `results.json` `steps[].validity` | >= 99.9% of turns `holdback_ms = 0` and `spec_agrees`; every driver and mock process lag p99 <= 2 ms | every ABBA and d-window (50 and 100 streams) valid; ramp: a3 valid at 125, invalid at >= 150 (the knee); base invalid at >= 125 | PASS for the reported L1 windows |
| Q1 contract-validated responses (load layer sample) | the drivers' `ContractValidator` on the first turn of every stream and every non-200 | 0 violations | 0 violations in every run (100 validated turns per 50/100-stream run) | PASS (sample; Q1 proper is the unit-security layers) |
| spec-02 §4.2 wire mapping (frozen goldens) | `make test MOD=adapters`, `make test-conformance` | every golden on the wire through both adapters and both kits | `test_adapters_put_the_goldens_on_the_wire`, `test_golden_wire_requests` (emulator and `@mockbedrock` kits) green; request bytes = aiobotocore's (`test_lean_converse_wire.py`, 40 cases) | PASS |
| spec-02 §4.3 error classification (frozen) | same | every §4.3 provider signal classified as before | identical for every §4.3 row: `test_lean_converse_parse.py` (28 of 36 responses identical), `test_unary_mapping.py` (15 transport failures), `test_lean_converse_stream.py` (76 of 84 stream cases identical), `tests/contract_internal/test_adapter.py::test_error_classification`. Deliberately changed: a 2xx Converse body that is no Converse response (8 cases) is the adapters' Mantle rule, retryable `malformed:response` (was an empty `end_turn` answer, or a fatal AttributeError, which §4.3's 'anything else -> fatal' would also allow); a ConverseStream payload botocore cannot parse (2 bodies x 4 chunkings) is `malformed:chunk` (F8) | PASS (the malformed-body rule: adapters/IMPL owner to confirm, see Remaining) |
| spec-02 §14 conformance suites | `make test-conformance` | green | 757 passed, 8 skipped | PASS |
| make check (wave gate) | `taskset -c 0-3 make check`, then the steps after `ownership` (`make contracts-check test-unit test-conformance test-integration sdk-check`) | every step green | lint, typecheck (320 files), typecheck-tests (541), importlint (27 contracts kept), forbidden-scan, contracts-check, test-unit 3,107 passed, test-conformance 757 passed / 8 skipped, test-integration 532 passed / 21 skipped, sdk-check (TS 86, Python 192) green; `ownership` FAILS on 19 files of other QA agents (`make/qa-{contract,fault,security}.mk`, `tests/qa/contract/*`), every path of this sprint "ok" | **FAIL** (ownership only, not this track's files; every other step PASS) |
| e2e (spec-10 §1 layer) on this sprint's code | in `/tmp/perfa/final` (wave-c2b + this sprint's files): `make image-api image-worker IMAGE_TAG=perfa`, then `NOEMA_API_IMAGE=noema-api:perfa NOEMA_WORKER_IMAGE=noema-worker:perfa NOEMA_MEDIA_IMAGE=noema-media:dev make e2e E2E_PROJECT=noema-e2e-perfa E2E_PORT_BASE=29400 E2E_BUILD=0` (twice; `RUN` = the dev venv with the snapshot first on `PYTHONPATH`) | 52/52 both runs, every response contract-validated | 52 passed (151.9 s), 52 passed (151.8 s) | PASS |

## Per-change results (ABBA means, 50 / 100 streams; perf report section 4)

| Change | CPU ms per turn | TTFT overhead p50 / p95 at 50 streams | at 100 streams |
|---|---|---|---|
| base (wave-c2b) | 67.6 / 63.8 | 14.1 / 29.9 | 51.7 / 128.3 |
| a1 lean unary path (members' Converse and Mantle) | 56.9 / 53.2 (-10.7 / -10.7) | 13.3 / 24.1 | 40.1 / 127.1 |
| a2 ingress steps 6-10 in one unit of work | 56.9 / 52.9 (-0.0 / -0.3) | 12.0 / 21.1 | 31.0 / 99.9 |
| a3 lean ConverseStream + member fast path | 52.9 / 49.4 (-4.0 / -3.5) | 11.3 / 20.8 | 21.4 / 82.9 |
| a4 (final: + dispatch order kept; d1-d3, quieter host) | 49.9-51.1 / 48.1-48.7 | 10.1-10.4 / 17.2-19.5 | 18.3-18.9 / 67.8-68.7 |

Serial floor (2 streams, createTurn tracer): PG `execute` round trips per turn 18 -> 10, units of work before the
generation request 3 -> 1, generation request leaves the api 9.19 -> 7.96 ms after the client sent it, TTFT overhead floor 11.5 -> 9.55 ms (back-to-back traces g1/g2). Capacity of one process 15.5 -> 21.0
turns/s.

## Bugs

Found and fixed in this role's areas (adapters; tests added): perf report section 9 (a garbage 2xx Converse body
committed as an empty answer; wrong-shaped ConverseStream payloads fatal instead of `malformed:chunk`; a body-read
failure retried as a connect error against IMPL-016; redirects followed with signed requests). Outside this role's
areas: none. Follow-ups for other owners (not defects): perf report section 8.

## Remaining

- L1 and C2 (lag up to 200 streams) are not met by one process (see the gates); the deployment answer stays two api
  processes per 2-vCPU pod (c2b section 7.1; 08 §9), now with about 35% more headroom per process.
- Owner confirmation: a 2xx Converse body that is no Converse response is now retryable `malformed:response` (as
  Mantle's), not §4.3's catch-all fatal; a one-line IMPL note would settle it (adapters / lead).
- Follow-ups for other owners: perf report section 8 (A1 start before the decision entry, asyncpg reset on release,
  a faster SigV4 and eventstream decoder, `DISPATCH_YIELDS` A/B, the trace tool's labels).
- The lean ConverseStream (a3) has not met live Bedrock yet (the lean `complete()` path did, in the live-smoke run of
  07:34 UTC); the next live smoke covers it (L2, L7, L8 streams).
