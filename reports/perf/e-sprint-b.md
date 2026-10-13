# E perf sprint B: per-frame path, GC and churn, overload protection, 2 api processes per pod

Wave E, perf-streaming-runtime (sprint B), 2026-10-11. Scope (brief): `src/noema/{generation,streaming,api,telemetry}`,
`composition.py` outside the adapter/decision/identity construction, `deploy/**`, their tests; the load harness
`tests/load` got small additive options for the measurements (section 2). No live Bedrock call, no AWS
resource touched (moto and the wire mock only). Raw data: `/tmp/noema-b/runs/<run>` on the build host; the analyzed
`results.json` of every run cited here: `reports/perf/e-sprint-b/<run>.json` (each with `provenance`).

## 1. Verdict

| Item | Result |
|---|---|
| (1) per-frame path | Redis mirror: one EVALSHA per batch (`WRITE_LUA`), client CPU 17 -> 6 us per frame; SSE frames to the server's own `send` past four wrappers; spans: memoized redaction, pre-redacted static attributes, non-recording without an exporter, 99 -> 14.5 us per span; per-frame wake-ups on one `asyncio.Event` each, 6.7 -> 4.4 us per wake. botocore eventstream decoding is adapter code (sprint A's paths): measured and handed over (section 7, F2) |
| (2) GC and churn | `gc.freeze()` after startup kept (C2b). Thresholds `(700, 10, 40)` adopted after a 3-way A/B (full GC every 5.3 s instead of 1.45 s; TTFT overhead p95 124 -> 72 ms and p99 175 -> 118 ms at 100 streams; section 6). The largest task source per turn is core's `deadline_scope` (a watcher task per scope): a task-free variant measured at -36% tasks and -4.4% CPU per turn, handed over (section 7, F1) |
| (3) overload | per-process turn cap, 429 `queue_timeout` + `retry-after: 1` before any session, admission or model work (`noema.api.overload`, default 120, `NOEMA_MAX_TURNS_PER_PROCESS`): at 250 streams on one process 1,672 x 500 -> 0 x 500 with the default 120, goodput unchanged (15.07 vs 15.02 turns/s), TTFT overhead p50 4.2 -> 1.0 s (section 4) |
| (4) deployment | `WEB_CONCURRENCY=2` uvicorn workers per 2-vCPU pod in compose (each worker its own pod id, metrics port and fallback share); `make e2e` 52/52 twice on the first build and twice on the end state; one pod (2 processes on 2 vCPUs) carries 200 streams at 28.7 turns/s with lag p99 17 ms and no error where one process collapsed at ~150, and sheds with 429 (no 500) past its capacity (300 streams: 29.5 turns/s) (section 5) |
| CPU per turn, 1 process | 67.3 -> 60.7 ms at 50 streams (-9.8%), 64.1 -> 59.0 ms at 100 (-8.2%); TTFT overhead p50 at 100 streams 53.0 -> 36.8 ms; interleaved ABAB, every window valid (section 3.2). With the GC thresholds on top, 100 streams: TTFT overhead p50 / p95 / p99 22.2 / 71.5 / 118 ms, lag p99 15.9 ms (section 6) |
| L1 at 200 streams/pod | **not met**: end state, 2 processes per pod: TTFT overhead p50 29.9 / p95 108 ms at 200 streams (pod at 1.75 of 2 cores; diagnostic window), 13.2 / 25.8 ms at 100 (valid window). The remaining CPU is the decision fan-out (42% of samples, sprint A), the adapter stream and core's deadline scopes (section 7) |

The host was shared and loaded during the whole sprint (1-minute loadavg 4-18; other agents' stacks and fuzzers run
unpinned on every core, including ours), so many windows above 50 streams fail spec-10 §5's mock-lag rule (a mock
process with lag p99 > 2 ms; the mocks use < 0.2 of a core, they are preempted, not saturated); the final A/B of
section 3.2 ran on the quietest stretch and all its windows are valid. Invalid windows are labeled "diagnostic"
and are not gate measurements; the QA lead re-runs the gates (E5) on a quiet host with the
deployment configured here (`--api-procs 2`).

## 2. Method

- **Stack**: compose project `noema-perf-b` (postgres, valkey, moto; ports 298xx) seeded by `tools/bootstrap_dev.py`,
  load config v2 (`python -m tests.load.stack activate`, the same digest `sha256:848146b0...de4b6` as C2b).
- **Placement** (brief: sprint B on 40-47, drivers 20-31; sprint A on 32-39): api on 46-47 (one 2-vCPU pod: one
  process, or with `--api-procs 2` both processes pinned to 46-47), four mock processes on 40, 41, 42 and 43 (two
  Converse, two Mantle, SO_REUSEPORT), the stack's Postgres and Valkey on 44-45 (`--stack-cpus 44-45`), drivers on
  20-31. Note: CPUs 20-23 are the hyperthread siblings of 44-47 on this host (lscpu: CPU n and n+24 share a core).
- **Code under test, isolated from the concurrent sprint**: every run uses a snapshot in `/tmp/noema-b/<snap>`:
  `snap-base` = `git archive wave-c2b` (b85f318), and each later snapshot = the previous one plus only my changed
  files (`SNAPSHOT.txt` in each), run with `PYTHONPATH=<snap>/src` from the snapshot directory, so sprint A's edits in
  the shared working tree never enter a measurement. `provenance.code` is a sha256 over the snapshot's files.
  Snapshots: `snap-mirror` (+ mirror), `snap-spans` (+ redaction memo, static attributes), `snap-raw` (+ raw SSE
  send), `snap-sampler` (+ non-recording spans without an exporter), `snap-final` (+ turn cap, workers, harness),
  `snap-final2` (+ asyncio wake-ups).
- **Harness additions** (tests/load, additive): `run.py --api-procs N` (N api processes on one inherited listening
  socket, as `uvicorn --workers N` = `WEB_CONCURRENCY=N`; CPU/RSS/fds summed per pod, lag merged), `--api-env K=V`,
  `serve.py --fd`; the driver honors `retry-after` on a 429/503 and keeps its session (an SDK's behavior; before, it
  slept 0.5 s and opened a new session after every error).
- **Quantities** as C2b (spec-10 §5): TTFT overhead = client TTFT - mock TTFT, e2e overhead likewise, bootstrap 95%
  CIs, CPU from /proc (cores and ms of api CPU per turn), validity per window.
- **Per-change deltas**: end-to-end windows cannot resolve 2-5% CPU changes on this host (back-to-back windows of the
  same code differed by ~1.5%), so each change is shown three ways: a microbenchmark of the changed path on an idle
  core (best of 5-7), its share in a CPU profile at 50 streams (SIGPROF sampler, `--profile cpu`; base and final
  interleaved ABAB), and the end-to-end windows.

## 3. Per-change deltas (the per-frame path, item 1)

### 3.1 The changes, each measured

| # | Change (files) | Microbenchmark, idle core | CPU profile at 50 streams: share of api samples, base #3 -> final #2 (ABAB, section 3.2) |
|---|---|---|---|
| 1 | Redis mirror: a batch is one `EVALSHA` of `WRITE_LUA` (DEL of broken turns, every XADD with the same ids, fields and MAXLEN, the EXPIRE; returns only the rejected turns), split into calls of <= 1,024 frames of whole turns (`streaming/mirror.py`) | redis-py client CPU per frame, batch of 40 frames of 20 turns: pipeline 17.1 / 17.6 us -> script 5.8 / 5.9 us (`/tmp/noema-b/bench/mirror_bench.py`) | mirror path 7.93% -> 5.89% (457 -> 315 samples, -31%); what is left is the per-batch round trip (pack, send, read: ~60%), batches every 5 ms |
| 2 | Spans: string attribute values redacted through a memo (`Redactor.redact_value`, <= 256 chars, 4,096 entries, dropped whenever a secret is registered or dropped), static attributes redacted once (`telemetry/redact.py`, `otel.py`) | `generation.attempt` span with 8 string attributes: 96-99 -> 29-30 us (`span_bench.py`) | `otel.py` 5.21% -> 3.18%, `redact.py` 1.61% -> 0.37% |
| 3 | Spans: no exporter configured (every deployed process today, section 7 F3) -> `ALWAYS_OFF` sampler: non-recording spans, valid ids (log `trace_id` kept) (`telemetry/setup.py`) | 29 -> 14.5 us per span | SDK tracing 1.81% -> 0.78% (2 + 3 together) |
| 4 | SSE frames go to the server's own `send` (`RAW_SEND_KEY`, set by `NoemaApp` outside every middleware; off with the test-mode response validator); the response start, the end of body and every pre-stream error still pass the stack and keep their envelopes (`api/app.py`, `api/sse_frame.py`) | - | the four pass-through wrappers (ServerErrorMiddleware, the ingress tracker, ExceptionMiddleware, the route wrapper) 4.86% inclusive -> 0.11%; their own cost ~0.5-0.9% of the api's CPU. The rest of the SSE send is uvicorn's `transport.write`, one syscall per frame (4.1-4.4%) |
| 5 | Per-frame wake-ups on one `asyncio.Event` each, cleared before each wait, instead of a new `anyio.Event` per wait (sniffio backend lookup + two objects every frame): the run's `EventQueue`, every `_TurnLog` subscriber, both sides of the SSE writer queue (`generation/events.py`, `streaming/event_log.py`, `streaming/sse_writer.py`) | one put + consumer wake-up: 6.74 -> 4.36 us (`queue_bench.py`, uvloop) | anyio `Event.__new__`/`wait` on these three paths were 3.6% of samples in final #4; in `snap-final2` (section 3.2) |

Tests: the mirror's unit fake records the script's ops (whole-turn splitting, a failed later call breaking only the
unwritten turns), a Valkey test for a rejected write inside a shared call; the memo's equality with `redact` on every
SEC-RED-2 vector, invalidation by a newly registered secret, the size bounds; non-recording spans with valid ids;
`NoemaApp` sending SSE frames past a middleware only when raw; an event-log subscriber cancelled while waiting
leaving the others waiting.

### 3.2 End to end, one process (all per-frame changes: `snap-base` vs `snap-final2`, default GC thresholds)

Interleaved ABAB, 150 s windows at 50 then 100 streams after 45 s warm-up / 30 s settle, 09:00-09:19 (load 3.9-6.3);
**every window valid by spec-10 §5** (holdback 0 with spec_agrees >= 99.9%, every driver and mock lag p99 <= 2 ms).

| run | streams | n | turns/s | TTFT overhead p50 [CI] | p95 | p99 | e2e overhead p50 / p95 | api lag p99 / max | api CPU | ms/turn | load |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ab2-base-1 | 50 | 1,076 | 7.17 | 13.36 [13.22, 13.52] | 26.8 | 53.9 | 22.0 / 44.1 | 12.7 / 51 | 0.479 | 66.8 | 4.7 |
| ab2-final2-1 | 50 | 1,077 | 7.18 | 12.49 [12.36, 12.65] | 28.8 | 48.0 | 19.5 / 32.1 | 11.2 / 52 | 0.435 | 60.6 | 3.9 |
| ab2-base-2 | 50 | 1,076 | 7.17 | 14.15 [13.97, 14.37] | 29.9 | 50.5 | 21.8 / 41.6 | 12.6 / 53 | 0.486 | 67.8 | 6.1 |
| ab2-final2-2 | 50 | 1,078 | 7.19 | 12.29 [12.17, 12.43] | 24.9 | 42.1 | 19.6 / 31.9 | 11.2 / 54 | 0.437 | 60.8 | 5.7 |
| ab2-base-1 | 100 | 2,140 | 14.27 | 52.9 [50.6, 56.0] | 129.5 | 169.7 | 102.5 / 203.5 | 29.2 / 132 | 0.912 | 63.9 | 4.8 |
| ab2-final2-1 | 100 | 2,149 | 14.33 | 35.5 [33.5, 38.6] | 113.1 | 150.7 | 69.9 / 176.4 | 24.4 / 142 | 0.848 | 59.2 | 5.1 |
| ab2-base-2 | 100 | 2,143 | 14.29 | 53.1 [51.5, 55.2] | 121.3 | 162.2 | 103.3 / 192.6 | 31.1 / 147 | 0.918 | 64.2 | 5.4 |
| ab2-final2-2 | 100 | 2,151 | 14.34 | 38.0 [35.0, 40.1] | 105.7 | 144.1 | 74.7 / 164.8 | 24.3 / 137 | 0.842 | 58.7 | 6.3 |

- **CPU per turn: -9.8% at 50 streams (67.3 -> 60.7 ms), -8.2% at 100 (64.1 -> 59.0 ms)**, both pairs agreeing
  within 1 ms. Expected from the parts: mirror ~1.1-1.4 ms, spans ~2.5-3 ms (about 25-30 spans per turn), wrappers
  ~0.5 ms, wake-ups ~0.7 ms per turn.
- TTFT overhead p50 -1.4 ms at 50 streams (13.75 -> 12.39), -31% at 100 (53.0 -> 36.8); p95 at 100 -11% (125 -> 109);
  e2e overhead p95 at 50 streams -25% (42.9 -> 32.0). At 50 the TTFT p95 moved within its noise (26.8-29.9 vs
  24.9-28.8): it is dominated by the decision fan-out and the per-turn ingress, not by the per-frame path.
- The earlier per-change windows (`ab-snap-*-r1`, single windows, load 4-12, most invalid by the mock-lag rule)
  are in the results directory; they cannot separate 1-3% steps (e.g. base 68.8 vs mirror 67.3 vs spans 67.6 vs raw
  65.6 ms per turn at 50 streams, while the two base windows above differ by 1.0 ms).
- With the adopted GC thresholds on top (`gc-t2-40`, same code plus `(700, 10, 40)`, 100 streams, valid): TTFT
  overhead p50 / p95 / p99 22.2 / 71.5 / 118.4 ms, lag p99 15.9 ms (section 6).

## 4. Overload protection (item 3)

**Design** (`src/noema/api/overload.py`, `api/routes/turns.py`, `api/app.py`). `TurnSlots(cap)` lives on
`app.state`; createTurn calls `acquire(len(TurnService.running))` first in its body (after authentication and the
request gate, whose 401/400s keep their precedence; both are in-memory on the hot path) and `release()` once the
turn is in the turn service's registry or failed before it started (try/finally around steps 6-12), so turns
between the check and their start count too, and the sync check-and-increment cannot overshoot. Above the cap:
**429 `queue_timeout`** (`rate_limit_error`, retryable) with `retry-after: 1`, before any Postgres, Redis or model
work; `aghoo_turns_shed_total` counts them.

**Why 429 `queue_timeout`**: spec-11 §1.1 gives per-pod capacity exhaustion (stream slots, the fair queue) exactly
this code and `retry-after: 1`; it is in createTurn's `x-noema-error-codes` and the openapi `RateLimited` response
(`retry-after` header); SDKs retry it after `retry-after`. The alternative 503 `all_routes_unavailable`
(`upstream_error`) would tell the client the model routes failed, which they did not. The cap is
`NOEMA_MAX_TURNS_PER_PROCESS` (default 120, 0 = off; per process, so a 2-worker pod admits 240).

**Measurement**: one process (`snap-final`), 250 streams (well past the knee), 150 s windows; the driver retries a
shed turn after `retry-after` on the same session. All diagnostic (host load; mock lag rule failed).

| run | cap | turns/s (goodput) | TTFT overhead p50 / p95 ms | api lag p99 / max ms | api CPU | 500s | 429s | 409s |
|---|---|---|---|---|---|---|---|---|
| ovl-nocap | none | 15.02 | 4,219 / 5,903 | 258 / 381 | 1.00 | **1,672** | 0 | 31 |
| ovl-cap150 | 150 | 14.66 | 2,525 / 3,276 | 250 / 293 | 1.00 | 1 | 11,991 | 0 |
| ovl-cap110 | 110 | 13.77 | 1,003 / 1,397 | 125 / 185 | 1.00 | 0 | 19,479 | 0 |
| final3-ovl-cap120 (end state, default cap) | 120 | 15.07 | 1,017 / 1,353 | 84 / 302 | 1.00 | 0 | 18,027 | 0 |

- The cap ends the collapse: no pool timeouts (500 `internal_error` 1,672 -> 0-1), no `session_busy`; admitted
  turns are 2-4x faster; with the default 120 (end state) the goodput equals the uncapped one (15.07 vs 15.02
  turns/s) at a quarter of its TTFT and a third of its loop lag. Every 429 (76,906 over the five shedding runs,
  warm-ups included) and every first turn of a stream passed the driver's `ContractValidator` (0 violations).
- Goodput does not rise above the uncapped one: at this workload (15 decision calls and a 400-token stream per 7 s
  turn, ~65 ms of CPU per turn) one process is CPU-bound from ~110 concurrent turns, and the sheds cost CPU too. A
  profile at cap 150 (`ovl-cap150-prof`, 80 sheds/s): the ingress steps of the turn requests (auth with its key
  cache 2.7%, error envelope 1.0%, access log 1.6% of samples, all requests) put a shed at ~0.6-0.8 ms of CPU, so a
  flood that retries every second still takes 5-10% of the process. A cheaper shed would move the check ahead of
  authentication into the ingress middleware (at the price of 429 before 401 during overload); not done.
- Default 120: about the knee of one process at this workload (C2b ramp: 125 streams -> lag p99 72 ms, 0.94 of a
  core) and above the per-process target (100 streams with 2 workers per pod) with room for an uneven split. It is a
  coarse guard sized for the heaviest workload measured: lighter turns (slower streams, reasoning routes) use less
  CPU per concurrent turn, so production should set it from its own turn mix (a lag-aware shed would adapt by
  itself; not built, the brief asks for a concurrency cap).

## 5. Two api processes per 2-vCPU pod (item 4)

**Why**: one api process is one event-loop thread. At the L1 workload it is CPU-bound from ~110-150 streams and
past ~150 it collapses (C2b section 4); a 2-vCPU pod (08 §9) with one process leaves one vCPU idle. Two processes
per pod double the CPU and halve each process's GC heap; the per-pod target becomes 2 x 100 streams.

**How** (`deploy/compose/docker-compose.yml`, `composition.py`, docs/modules/integration.md §5.1): `uvicorn
--workers` defaults to `WEB_CONCURRENCY`, so the api containers set `WEB_CONCURRENCY=2` (`NOEMA_STACK_API_WORKERS`)
and `cpus: 2` (`NOEMA_STACK_API_CPUS`); the image CMD is unchanged and jobs/media-worker never get the variable.
uvicorn's supervisor binds port 8000 once, both workers accept on it (the less busy one tends to win the accept),
restarts a dead worker, forwards SIGTERM (each worker runs the spec-11 §7 drain chain as before) and rolls workers
on SIGHUP. Chosen over two containers per pod: those would need two ports behind one Service (no shared listen
socket without SO_REUSEPORT, which uvicorn does not set) and two scrape configs anyway. With N > 1 workers the
composition makes each worker its own pod: `pod_id` = `<pod>-<pid>` (two processes with one id would share the
outbox consumer name, so one could ack the other's pending entries, overwrite each other's retry-budget field and
claim each other's turn owners), `expected_pods` x N (the Redis-down local shares), metrics on the first free port
of 9464.. (compose publishes 9464 and 9465). Each worker keeps its own DB pool (20): 40 Postgres connections per pod.

**Regression**: `make e2e E2E_PROJECT=noema-e2e-b E2E_PORT_BASE=28600 E2E_BUILD=0` on images built from
`snap-final` (`noema-api:sprintb`, `noema-worker:sprintb`; 2 workers per api container, verified with `docker top`
and the two "metrics on port" log lines): **52 passed (158 s), then 52 passed (157 s)**; on the end state (images
`:sprintb2` from `snap-final3`: + wake-ups, GC thresholds, cap 120) **52 passed (156 s), then 52 passed (155 s)**;
both include the SIGTERM
drain of a 2-worker container (test_shutdown), the cross-pod breaker, GCRA and auth-failure buckets
(test_multipod), cancel across pods (G07) and the idempotent attach (G12). Stack torn down afterwards.

**Measurement** (`pod2-100-200`: `--api-procs 2`, both processes on 46-47, `snap-final`; diagnostic: host load
10-12, mock lag p99 4.3 / 8.3 ms):

| pod | streams | n | turns/s | TTFT overhead p50 / p95 / p99 ms | e2e overhead p50 / p95 | api lag p99 / max | pod CPU (cores) | ms/turn | pod RSS MiB | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 processes | 100 | 2,596 | 14.42 | 16.3 / 45.5 / 76.9 | 25.8 / 76.1 | 15.0 / 85 | 0.98 | 68.1 | 399 -> 502 | 0 |
| 2 processes | 200 | 5,125 | **28.47** | 62.5 / 162.4 / 217.0 | 114.6 / 244.2 | **36.1** / 165 | 1.86 | 65.3 | 571 -> 718 | 0 |
| 1 process (C2b fix-F1) | 200 | 14,412 | 16.01 | 3,670 / 5,382 / 6,064 | 4,994 / 7,025 | 284.6 / 444 | 1.00 | - | 440 -> 473 | 1,500 x 500 |
| 2 processes, end state (`final3-pod2`, + waiters + GC thresholds; load 5.6) **valid** | 100 | 2,598 | 14.43 | 13.2 [13.1, 13.4] / 25.8 [24.8, 26.7] / 57.9 | 21.0 / 38.6 | 11.3 / 99 | 0.90 | 62.4 | 416 -> 522 | 0 |
| 2 processes, end state (load 7.2; holdback and mock-lag rules failed) | 200 | 5,162 | 28.68 | 29.9 [29.3, 30.6] / 108.3 [104.8, 112.3] / 180.1 | 55.8 / 160.9 | 17.1 / 217 | 1.75 | 61.1 | 597 -> 743 | 0 |
| 2 processes, end state, past capacity (cap 120 each; diagnostic) | 300 | 4,419 | 29.46 | 1,101 / 1,473 / 1,627 | 1,307 / 1,795 | 84.1 / 329 | 2.00 | 67.9 | 507 -> 667 | 7,982 x 429, no 500 |

The split was even (4,394 / 4,856 turns per worker over the run; end state 4,676 / 4,623). End state at 100 streams per pod (valid window): TTFT overhead p50 13.2 ms, p95 25.8 ms (upper CIs 13.4 and 26.7: L1's bounds 10 and 25 are missed by 3.4 and 1.7 ms); at 200 per pod p50 29.9 / p95 108 ms, each process's full GC every 5.3 s at 118-126 ms (p50). At 200 streams the pod runs at 1.86 of its 2
cores: 200 streams per pod is the knee of this workload even with both cores busy, which is why L1 (p50 <= 10 ms,
p95 <= 25 ms) is out of reach without less CPU per turn (decision fan-out, adapter stream; section 7). The lag
target (C2: p99 < 50 ms up to 200 streams) held in this window, which is diagnostic (mock lag rule), so it is not
claimed as a gate pass. The image's interpreter is ~22% slower (C2b R5); `--api-image` does not combine with
`--api-procs` yet (section 8).

## 6. GC and object churn (item 2)

**Freeze**: `composition.start` already ends with `gc.collect(); gc.freeze()` outside test (C2b fix 1); kept.

**Thresholds** (`--gc-threshold`, one process, 100 streams, 180 s, `snap-final2` back to back on the quietest stretch
of the day, load 5.3-5.9; GC pauses from the api's `gc.callbacks` log):

| run | thresholds | full GC every | full GC p50 / max ms | GC share of wall time (gen0 + gen1 + gen2) | TTFT overhead p50 / p95 / p99 ms | e2e p50 / p95 | api lag p99 / max | RSS MiB | ms/turn |
|---|---|---|---|---|---|---|---|---|---|
| gc-default-2 | (700, 10, 10) | 1.45 s | 67 / 145 | 2.6 + 2.0 + 5.0 = 9.7% | 38.7 / 124.4 / 175.1 | 77.9 / 198.0 | 26.3 / 163 | 335 | 59.0 |
| gc-t2-40 | (700, 10, 40) | 5.29 s | 91 / 114 | 2.7 + 2.1 + 1.7 = 6.5% | **22.2 / 71.5 / 118.4** | 42.7 / 116.7 | 15.9 / **134** | 343 | 58.4 |
| gc-t2-100-2 | (700, 10, 100) | 12.9 s | 134 / 173 | 2.6 + 1.9 + 1.1 = 5.6% | 19.4 / 68.7 / 132.2 | 37.3 / 115.6 | 14.9 / 194 | 360 | 56.7 |

Earlier pair on a loaded host (`gc-default` / `gc-t2-100`, `snap-final`, load 8-10): the same picture (p50 38.4 ->
25.4, p95 112 -> 85, p99 153 -> 174, lag max 151 -> 220). Fewer full collections let more cyclic garbage wait for the
next one, so each pause grows (67 -> 91 -> 134 ms p50); with 40 the turns' p95 and p99 both improve and the worst
stall is below the default's; with 100 the p99 and the lag max get worse again (C2b's (2000, 10, 100) experiment saw
the same). **Adopted: (700, 10, 40)**, set by `_freeze_startup_heap` after the freeze unless the thresholds were
already changed (the harness's `--gc-threshold` keeps working); `tests/unit/composition/test_heap.py`. Larger
young thresholds do not help: turn objects live seconds, so they reach the old generation whatever the young
thresholds are (C2b: (20000, 10, 10) made each young collection 12.6 ms).

**Tasks per turn** (diagnostic task-factory counter on the api, 50 streams, ~810 turns, `diag-tasks-a/b`): ~74 anyio
tasks per turn. My paths create few of them: the SSE response's disconnect watcher and the writer's reader, writer
and timer (4 per stream, one long-lived each), the run's walk and deadline watcher and one per attempt. The largest
source is core's `deadline_scope`, which starts a task group and a watcher task for every scope, also on the real
clock: a variant that uses a native anyio deadline for `SystemClock` (FakeClock unchanged) took the count from
60,135 to 38,657 tasks over the same run (-36%, ~26 per turn), CPU 66.3 -> 63.4 ms per turn and TTFT overhead p50
14.6 -> 12.8 ms. core is frozen and not mine: handed over (section 7, F1). The per-frame wake-ups (section 3.1, #5)
removed one `anyio.Event` (two objects) per frame and subscriber.

## 7. Findings for other owners (measured; not in sprint B's paths)

- **F1, core (`noema.core.clock.deadline_scope`)**: every scope starts a task group plus a watcher task that sleeps on
  the injected clock, also for `SystemClock`; it is used per decision member call (adapters' `complete`), per
  decision cache call, per idempotency, outbox and turn-runner step, ~26 tasks and 4.0% of the api's CPU samples per
  turn at 50 streams. Reproducer: `diag-tasks-a` vs `diag-tasks-b` (section 6; patch in
  `/tmp/noema-b/snap-diag-clock/src/noema/core/clock.py`, 4 lines: `if type(clock) is SystemClock: with
  anyio.CancelScope(deadline=anyio.current_time() + remaining) as native: yield native; return`): -36% tasks,
  -4.4% CPU per turn, TTFT overhead p50 -1.8 ms. Same expiry semantics (`cancelled_caught`, the body's exceptions
  unwrapped); FakeClock keeps the watcher.
- **F2, adapters (ConverseStream decoding, sprint A's paths)**: aiobotocore's `AioEventStream` is 8.5-9.3% of the
  api's samples at 50 streams (~100 events per turn): the raw-chunk generator plus botocore's `EventStreamBuffer` and
  shape-driven `EventStreamJSONParser`. Decoding alone: botocore 15.6 us per event vs 4.8 us for a lean decoder
  (struct prelude, zlib.crc32 checks, header walk, `json.loads`) on 100 mock `contentBlockDelta` frames
  (`/tmp/noema-b/bench/es_bench.py`, identical event types). The stream watchdog also enters a fresh `CancelScope`
  per provider event (`resilience/timeouts.py` `begin_wait`). A lean ConverseStream reader (the decision path's lean
  client of sprint A, extended to the stream) would take most of it.
- **F3, integrator/telemetry (no trace exporter anywhere)**: `build()` never passes `otlp_endpoint`, so in every
  environment spans were recorded by the SDK and dropped (no span processor). Mitigated here (non-recording spans,
  section 3.1 #3); exporting traces needs the wiring (e.g. `OTEL_EXPORTER_OTLP_ENDPOINT` -> `setup_telemetry`), and
  then ~15-30 us of CPU per span returns.
- **F4, decision (sprint A)**: the decision fan-out is 41.7-42.3% of the api's samples (15 member calls per turn);
  with 2 processes per pod it is what keeps 200 streams per pod at the knee.
- **F5, load harness (qa-tools)**: `run.py --api-image` cannot run several api processes (`--api-procs` is venv
  only), and the mock processes get one core each in this placement (`--mock-cpus 40,41,42,43`); under host load
  their lag p99 exceeded 2 ms in most windows above 50 streams although they used < 0.2 of a core.
- **Process note**: at 07:54 a `pkill -f tests.load.run` of mine (meant for my own runner) could have matched another
  agent's runner on this host; sprint A's last result file had been written at 07:53 and no other `tests.load`
  process was running afterwards.

## 8. Open items

- Compatibility (seen by the contract track, reports/qa/contract.md §1): the working-tree compose file with an image
  built before this sprint crash-loops api-1/api-2 (both workers bind 9464, Errno 98, uvicorn stops the parent).
  Images and compose ship together (`make e2e-up` rebuilds both); older images run with
  `NOEMA_STACK_API_WORKERS=1` (compose comment, docs/modules/integration.md §5.1).
- The load gates were not run as gates here (QA lead, E5): L1, the ramp C2, C3 slow clients, C4 soak, C5 fairness,
  C1 quota, on a quiet host with `--api-procs 2` (the deployment now configured), and from the image (R5; needs
  `--api-image` with several processes, F5).
- L1 at 200 streams per pod needs less CPU per turn than sprint B's paths hold: F1 (core `deadline_scope`), F2
  (ConverseStream decoding) and sprint A's decision transport.
- `NOEMA_MAX_TURNS_PER_PROCESS` (120) is sized for the L1 workload; set it from the production turn mix. A cheaper
  shed (before authentication) or a lag-aware one are possible follow-ups.
- spec-05 §5 does not list `WEB_CONCURRENCY` and `NOEMA_MAX_TURNS_PER_PROCESS` yet; 08 §9's sizing table (design
  rev 6, outside this repo) still says one process per pod.
- Redis mirror: `batch_s` stays 5 ms; at 10 ms the per-batch round trip (now ~60% of the mirror's cost) would halve,
  for remote readers seeing frames up to 10 ms later (not changed).

## 9. Reproduce

```
# the stack (once): base services of project noema-perf-b on 298xx, bootstrap, load config
env NOEMA_PG_PORT=29832 NOEMA_MOTO_PORT=29850 NOEMA_VALKEY_PORT=29879 NOEMA_API1_PORT=0 NOEMA_API2_PORT=0 \
  NOEMA_API1_METRICS_PORT=0 NOEMA_API2_METRICS_PORT=0 NOEMA_MOCKBEDROCK_PORT=0 NOEMA_FAULT_TOXIPROXY_PORT=0 \
  NOEMA_FAULT_PG_PORT=0 NOEMA_FAULT_VALKEY_PORT=0 NOEMA_FAULT_MOTO_PORT=0 \
  docker compose -p noema-perf-b -f deploy/compose/docker-compose.yml up -d --wait postgres valkey moto
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tools.bootstrap_dev --compose-project noema-perf-b \
  --state ~/.noema/stacks/noema-perf-b.json
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.load.stack activate --state ~/.noema/stacks/noema-perf-b.json
# a window (from a snapshot directory, PYTHONPATH=<snap>/src; /tmp/noema-b/bin/run.sh wraps this)
python -m tests.load.run --state ~/.noema/stacks/noema-perf-b.json --out /tmp/noema-b/runs/<name> \
  --api-port 29890 --mock-port 29881 --api-cpus 46-47 --stack-cpus 44-45 --mock-cpus 40,41,42,43 \
  --driver-cpus 20-31 --plan 50:150,100:150 --warmup-s 45 --settle-s 30 \
  [--api-procs 2] [--api-env NOEMA_MAX_TURNS_PER_PROCESS=0] [--gc-threshold 700,10,40] [--profile cpu]
```

Runs: `ab2-{base,final2}-{1,2}` (section 3.2), `prof-*` (3.1 profiles, ABAB at 50 streams), `ab-snap-*-r1`
(per-change windows), `ovl-*` (section 4), `pod2-100-200` and `final3-*` (section 5), `gc-*` (section 6),
`diag-tasks-{a,b}` (task counts; `snap-diag*` add a task-factory counter, not in the repo). Microbenchmarks:
`/tmp/noema-b/bench/{mirror,span,queue,es}_bench.py`. e2e: `make e2e E2E_PROJECT=noema-e2e-b E2E_PORT_BASE=28600
E2E_BUILD=0` with `NOEMA_API_IMAGE=noema-api:sprintb NOEMA_WORKER_IMAGE=noema-worker:sprintb
NOEMA_MEDIA_IMAGE=noema-media:dev` (images built from the snapshot with `docker build -f deploy/docker/...`).
