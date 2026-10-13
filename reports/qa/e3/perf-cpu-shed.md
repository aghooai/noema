# E3 perf-cpu-shed: CPU per turn, nearly free shedding, pool release (role report)

Wave E3, role perf-cpu-shed, 2026-10-12. Edits only in this role's paths (`src/noema/adapters/**` outside
`resilience/**`, `src/noema/api/**` overload/shed path, `src/noema/db/engine.py`, their tests, reports). No live
Bedrock call, no AWS resource, no git write. Method, raw tables and per-change evidence:
[reports/perf/e3-perf-cpu-shed.md](../../perf/e3-perf-cpu-shed.md); results files:
`reports/perf/e3-perf-cpu-shed/<run>.json`.

## 1. Verdict

| Item (brief) | Before | After | Evidence |
|---|---|---|---|
| (1) dict-based SigV4 signer | botocore `SigV4Auth` on an `AWSRequest`: 73.1 us per call; 1.49 ms per turn (profile) | `_sigv4.sign_post`: **16.9 us per call; 0.58 ms per turn** | byte-compare with SigV4Auth: every golden x 6 regions x token/no-token, 2,000 fuzzed requests (regions, ARN / `:` / arbitrary ids, tokens, endpoints); full request vs aiobotocore 60 cases; wire path vs signature over sockets |
| (2) eventstream decoder | botocore `EventStreamBuffer`: 6.48 us per message; reader 3.27 ms per turn | `EventStreamDecoder`: **1.63 us per message; reader 2.12 ms per turn** | message and exception compare with botocore on 21 suite bodies, 16 wire-mock streams, 1,800 fuzzed streams, each in 5-6 chunkings incl. random splits; reader vs aiobotocore 84 cases |
| (3) shedding + fair share; **C5** | refused at the route after the full ingress (~0.2-0.7 ms); share `cap // tenants`; flood turns queue 2 s for their own limit. **C5 1.309, 1.253, 1.165: FAIL** | edge shed before the middleware stack (**~10-17 us**), max-min share, saturated tenants wait at most 12 turns per process (not counted against others). **C5 1.078 [1.069, 1.087], 1.079 [1.069, 1.096]: PASS**, 0 victim errors | `tests/unit/api/test_edge_shed.py`, `test_overload.py`; C5 runs `c5-*` (section 4 of the perf report); with the GC sprint's code also: 1.032 |
| (4) pool reset on release | asyncpg reset query on every release | none on the app pool; **240 -> 207 us client CPU per unit of work**; floor -0.2..-0.4 ms | `tests/integration/db/test_pool_reset.py`: no `app.tenant_id` on the same backend after commit / rollback / cancel / savepoint / system call; reset query absent (query logger); source scan |
| CPU per turn (L1, 2-process pod, ABAB) | 46.95 ms (100/pod), 45.75 ms (200/pod) | **44.70 ms (-4.8%), 43.65 ms (-4.6%)** | `cpu-{base,e3b}-{1,2}`, every window valid |
| L1 TTFT overhead | 100/pod p50 10.4, p95 16.4; 200/pod p50 14.7, p95 ~50 | 100/pod p50 10.0 (upper CI 10.02-10.07), p95 14.6; 200/pod p50 14.0, p95 48 | not an L1 pass on this role's code alone (the 200/pod p95 is GC-bound: the parallel GC sprint) |
| floor (TTFT overhead p50, 2-4 streams) | 9.25 / 8.97 / 8.93 ms | 8.88 / 8.79 / 8.74 ms | `floor*-{base,e3b}-*` |
| make check / make e2e | green | **green** on the working tree; e2e **52/52 x3** on own pinned stack | section 3 |

## 2. Files

| File | Change |
|---|---|
| `src/noema/adapters/_sigv4.py` (new) | dict-based SigV4 (`sign_post`), byte for byte `SigV4Auth.add_auth` for the lean Converse request |
| `src/noema/adapters/_converse_wire.py` | `signed_headers` signs with `sign_post`; `parse_event(headers, payload, model)` |
| `src/noema/adapters/_eventstream.py` (new) | `EventStreamDecoder`: botocore `EventStreamBuffer`'s messages and exceptions, faster |
| `src/noema/adapters/converse.py` | `_ConverseRaw` reads with `EventStreamDecoder` |
| `src/noema/api/shed.py` (new) | the edge shed: the per-process cap before the middleware stack |
| `src/noema/api/overload.py` | max-min fair share; saturated tenants (waiting-turn bound, not counted against others); `held`, `sheds_early` |
| `src/noema/api/app.py` | `NoemaApp` calls the edge shed (off with the response validator) |
| `src/noema/api/routes/capacity.py`, `routes/turns.py` | createTurn slots marked as turns; an admission `queue_timeout` saturates the tenant |
| `src/noema/db/engine.py` | `PoolSettings.reset_on_release`; the app pool skips asyncpg's reset query |
| tests | `tests/unit/adapters/{test_sigv4,test_eventstream,test_eventstream_mock}.py`, `eventstream_compare.py` (new); `lean_reference.py`, `tests/integration/adapters/test_unary_transport.py` (freeze the new signer's clock too); `tests/unit/api/test_edge_shed.py` (new), `test_overload.py` (+2); `tests/integration/db/test_pool_reset.py` (new) |

## 3. Checks

| Command | Result |
|---|---|
| `make check` (working tree with all three E3 roles' edits; 09:13-09:19 UTC; `taskset -c 0-3,24-27`) | `make check: all green` (unit 3,444 passed; conformance 757 passed, 8 skipped; integration 573 passed, 21 skipped; lint, typecheck 327 + 590 files, importlint 27 kept, forbidden-scan, ownership, contracts-check, sdk-check) |
| `make check` at the end (10:34-10:45 UTC, the final tree; this role's 19 files identical to `snap-final`) | first run: 1 flaky failure outside this role's paths (`tests/integration/media_pipeline/test_pg_s3_redis.py::test_upload_process_watch_download_and_purge`, `partial` instead of `ready`; passed 3 of 3 alone); second run: **`make check: all green`** |
| `pytest tests/unit tests/contract_internal` on `snap-e3b` (wave-e2 + this role's files only) | 4,151 passed, 8 skipped |
| `pytest tests/integration` on `snap-e3b` | 568 passed, 22 skipped |
| `make e2e E2E_PROJECT=noema-e3cpu-e2e E2E_PORT_BASE=32100 E2E_BUILD=0` (+ compose cpuset override, images `noema-api:e3cpu`, `:e3cpu-final` built from the snapshots) | 52 passed (154 s) on `snap-e3a`; 52 passed (152.9 s) and 52 passed (153.6 s) on `snap-final` |
| microbenchmarks (`reports/perf/e3-perf-cpu-shed/bench/`, `taskset -c 2`) | signer 73.1 -> 16.9 us; decode 6.48 -> 1.63 us per message; reader 8.67 -> 3.51 us per event; shed 187.2 -> 16.5 us; unit of work 240.0 -> 206.7 us |

## 4. For the QA lead's re-gate

- **C5** varies with where the flood lands (uvicorn's accept split between the 2 processes) and with the full-GC
  pauses: the base spread was 1.165-1.309 in 3 runs here (QA E2: 1.06-1.29). After: 1.078 and 1.079 on this role's
  code, 1.032 with the GC sprint's code as well; gate it on the final tree with >= 2 runs.
- **Precedence change** (documented in `api/shed.py`; spec text proposed in the perf report section 7): over the
  cap, a request with a cached key id is refused before authentication, so a wrong secret for that key id is 429
  rather than 401 and the gate's 400/413/415 are not reached; an uncached key id always gets the full ingress.
  Edge refusals have no access line (one `edge shed` summary line per second at most); `aghoo_turns_shed_total`
  counts them all.
- The flooder sees 2-4 transport `ReadError`s per C5 run (the HTTP keep-alive close race, both sides 5 s; never the
  victim; an early 429 keeps keep-alive intact: `bench/early_response.py`).
- `tests/unit/adapters/test_complete.py::test_connect_retry_needs_the_retry_budget` failed in the working tree for a
  while during the sprint: the misc-fixes role's R8 change, whose test update is in the tree now (`make check` green).

## 5. Teardown and what is left behind

Stacks `noema-e3cpu-perf` and `noema-e3cpu-e2e` removed with their volumes (`docker compose ... down -v`), the
buildx builder `noema-e3cpu` removed, the images `noema-{api,worker}:e3cpu`, `:e3cpu-final` removed. Left: raw run
data in `/tmp/noema-e3cpu/` (runs, snapshots), the state files `~/.noema/stacks/noema-e3cpu-*.json`.
