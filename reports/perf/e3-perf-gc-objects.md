# E3 perf sprint: GC pauses and object churn per live turn (perf-gc-objects)

Wave E3, role perf-gc-objects, 2026-10-12 (04:45-09:30 UTC). Scope (brief): `core/clock.py` (the task-free
`deadline_scope`, sprint B F1), `turns/**` (not `coordination.py`), `generation/**`, `streaming/**`, `decision/**`
(object churn only), `composition._freeze_startup_heap`, their tests. No live Bedrock call, no AWS resource (moto and
the wire mock only), no git write. Raw data: `/tmp/noema-e3gc/runs/<run>` on the build host; the analyzed
`results.json` of every run cited here: `reports/perf/e3-perf-gc-objects/<run>.json` (each with `provenance` and
`config`).

## 1. Verdict

See the table in [reports/qa/e3/perf-gc-objects.md](../qa/e3/perf-gc-objects.md) section 1 (the same numbers);
this report holds the method, the per-change evidence and the raw tables.

## 2. Method

- **Stack**: compose project `noema-e3gc-perf` (postgres 31732, moto 31750, valkey 31779; api 31790, mocks
  31781/31782), seeded by `tools/bootstrap_dev.py`, load config v2 (`sha256:b475dc16...`, the E2 QA lead's digest).
- **Placement** = the QA lead's E2 L1 placement, so the numbers compare with the gate table: one 2-vCPU pod of **2 api
  processes on CPUs 46-47** (physical cores 22-23; their sibling CPUs 22-23 stay idle), Postgres + Valkey on 44-45,
  8 mock processes (Converse 16-19, Mantle 40-43), drivers on 4-15 and 28-39 (`/tmp/noema-e3gc/bin/run.sh`).
- **Quiet host, serialized**: the runner starts a window only when **no other `tests.load.run` is alive** and the
  1-minute loadavg is < 4 (from 07:40 < 6, from 08:45 < 8: the perf-cpu-shed agent, on the same placement, ran its
  windows back to back). Our runs never overlapped: every run's monitor file `runs/<run>.monitor` shows 0 foreign
  load processes in every 10 s sample. The 1-minute loadavg means of the windows were 3.0-7.4 (other agents' builds and
  test suites, unpinned; the 5 s samples peaked at 8.1-8.8 for a moment in `g100-1` and `final-1`, 10.9 in `c5-1`),
  and every window is valid by spec-10 §5 (holdback 0 with `spec_agrees` >= 99.9%, driver and mock lag p99 <= 2 ms)
  except `c5-1` at 200/pod (marked).
- **Code under test, isolated from the parallel sprint**: snapshots in `/tmp/noema-e3gc/snap-*`: `snap-base` =
  `git archive HEAD` (1a28933 = wave-e2, the tree make check is green on), each later snapshot = the previous one
  plus only my files (`SNAPSHOT.txt` in each), run with `PYTHONPATH=<snap>/src` from the snapshot directory (the
  parallel agents' adapter/api/composition edits never enter a measurement). `provenance.code.tree_digest` is a
  sha256 over the snapshot's code paths.
- **Plan** per run: `--plan 100:300,200:450 --warmup-s 60 --settle-s 30 --census` (L1 at 100 and 200 streams per pod,
  the E2 gate windows' lengths; the census runs after each window, outside it).
- **Quantities**: TTFT overhead (client TTFT - mock TTFT) p50/p95/p99 with bootstrap 95% CIs and CPU per turn
  (`results.json`, spec-10 §5); full-GC pauses from each process's `gc.callbacks` log (`api-<k>.gc`), p50 / p99 /
  max and the interval, per process and pooled over the pod (`/tmp/noema-e3gc/bin/summ.py`); the tracked heap from
  the census (`gc.get_objects()` at the window's end, frozen objects excluded, then the cyclic garbage a collection
  finds now): **live tracked = tracked - cyclic garbage**, and **per live turn = (live at 200/pod - live at 100/pod) /
  100** (closed-loop streams: a stream always has a turn in flight; the difference also carries the resume window's
  sealed turns, which double with the turn rate: ~44 sealed turns per extra live turn, see 4.6)
  (`census_live.py`).
- **Per-change evidence**: a microbenchmark or an in-process heap measurement of the changed path on an idle core
  (`/tmp/noema-e3gc/bench/*.py`, run against `snap-base` and the tree with `PYTHONPATH`), the per-turn task / cancel
  scope / Event creation counters of a diagnostic run (`/tmp/noema-e3gc/diag/sitecustomize.py`, api only, by first
  noema call site; never a reported window), and the cumulative load windows (section 4).

## 3. The changes, each with its evidence

Base = `snap-base` (wave-e2). "Tracked" = objects the cycle collector scans. Per-turn creations, diagnostic runs
`diag-base` / `diag-c5` (100 streams per pod, 2,194 / 2,197 turns; counts per turn driven, by the first noema call
site; anyio's `CancelScope.__init__` runs twice per object, so the scope counts are halved here):
**anyio tasks 68.7 -> 35.7**, cancel scopes ~299 -> ~237 (104 of them the adapters' stream watchdog, section 7),
**anyio Events 154.7 -> 58.5**, asyncio Events 313.7 -> 122.1 (`reports/perf/e3-perf-gc-objects/diag-*.counts.txt`).

| # | Change (files) | Microbenchmark / in-process measurement (idle core 26, uvloop) | Tests (new or changed) |
|---|---|---|---|
| 1 | **Task-free `deadline_scope`** on `SystemClock`: one `anyio.CancelScope` whose deadline the loop's timer enforces (`at` mapped by the time remaining, as a sleep on the loop counts it); every other clock keeps the watcher task (`core/clock.py`; sprint B F1) | per scope (enter, one checkpoint, exit) **67.6 -> 7.8 us**; tracked objects held per open scope **52 -> 9** (`deadline_bench.py`); the 28.9 watcher tasks per turn of every adapter call, decision wait, idempotency, outbox and turn step gone (anyio tasks per turn 68.7 -> 39.8 by this change, 35.7 with #2 and #3) | `tests/unit/core/test_deadline_scope_loop.py`: no task, fires at `at` and not before, no timer left, and 9 scenarios (expired, fires, body error, error after expiry, outer cancel, outer deadline first, explicit cancel, shielded body, plain) with **outcomes identical to the watcher variant**; the FakeClock suites unchanged |
| 2 | **One turn watcher** instead of four: cancel/abandon, shutdown and the deadline in one task, each trigger handled once in the order it fires (a stop path in progress ends first, as a concurrent watcher found it); the lease renewal's Postgres call runs in a short task per renewal, still due `RENEW_EVERY_S` after the previous one started, so a stalled renewal never delays a stop (`turns/runner.py`) | long-lived tasks per turn **4 -> 1** (+1 short task per 10 s renewal) | `tests/unit/turns/test_stop_watch.py`: one `_stop_watch`, renewals at t_in + 10 / 20 s, a cancel 1 s into a 6 s stalled renewal handled at once; every existing cancel / deadline / shutdown / lease test unchanged and green |
| 3 | **The run's T19 deadline is the loop's timer** on `SystemClock` (`call_later` -> the sync `_expire`), no watcher task; other clocks keep it; the watcher's cancel scope is only created for them (`generation/run.py`, `walk.py`) | tasks per generation run **3 -> 2** (walk + attempt) | `tests/unit/generation/test_deadline_timer.py`: a held answer ends by T19 at the deadline with no watcher task and no frame; a run that ends first cancels the timer |
| 4 | **The decision run drops its coordinator at the final snapshot** (`_Settled`: the usage lines, usage by alias, member statuses and the L2 eligibility, taken when every unit is closed; nothing reads the coordinator after it), which also breaks the run <-> coordinator callback cycle (`decision/run.py`) | cyclic garbage per router-default decision on the real clock **672 -> 362 (with #1) -> 0** (`decision_cycles3.py`); ~300 tracked objects (slots, call records, units, streams, member runner) freed ~1 s into a ~7 s turn instead of at a full GC | `tests/unit/decision/test_heap.py` (fails on base): 0 cyclic garbage over 3 decisions, coordinator None after the final, usage settled; every decision unit / conformance / integration test unchanged and green (usage lines, member metrics, L2 put, outbox) |
| 5 | **Event log**: a live log keeps its frames as plain `(id, type, payload, wire)` tuples (untracked by the GC; reads rebuild an equal `SseFrame` around the same bytes); a sealed log is one tracked object (its packed frames one untracked tuple, its wake-up Event + waiter deque dropped at the seal) (`streaming/event_log.py`) | tracked objects per live turn log of 50 frames **54 -> 5**; per sealed turn (resume window, 300 s) **4 -> 1** (`eventlog_heap.py`) | `tests/unit/streaming/test_log_heap.py` (fails on base); `test_event_log.py::test_frames_are_serialized_once_and_shared` now asserts equal frames sharing the **same bytes** (`wire is`, `payload is`) instead of the same `SseFrame` object (the only assertion changed in this sprint, see 6) |
| 6 | **Finished turns kept as plain tuples** (`FinishedTurn.row()`, untracked) for the resume-window cancel answers (`turns/service.py`) | 1 -> 0 tracked objects per finished turn | the cancel-after-finish tests unchanged and green |
| 7 | **Mirror writer wake-ups**: one `asyncio.Event` cleared per round instead of a new `anyio.Event` per batch and per flush (`streaming/mirror.py`) | `anyio.Event()` 1.14 us vs `clear()` 0.06 us; 2 anyio + 4 asyncio Events and ~6 tracked allocations less per 5 ms batch (59 anyio Events per turn in `diag-base`) | mirror unit + Valkey integration tests unchanged and green |
| 8 | **A1 before the decision run's entry** (holdback/auto): the generation driver starts in a task before `decisions.run().__aenter__`, the decision driver runs in `_main`'s task after it (same task count); W, t_d0 and `escalate_until` unchanged; gate unchanged. An entry that fails before A1's first step leaves A1 unstarted (wire output as before); one that fails after it stops A1 first (T19 without a frame for `deadline_exceeded`, else `client_abort`). `on_dispatch` (IMPL-077) is reported at the entry as before and at each `generation.started` (`turns/runner.py`) | the stream request opens **at the entry's start** instead of after it (unit: 200 ms slow entry -> opened 200 ms earlier); floor A/B in 4.5 (no measurable gain) | `tests/unit/turns/test_early_a1.py`: event order `turn.created, preflight.completed, route.selected, generation.started, decision.completed`, P-SSE-1, gate after S1, failing entries (both codes), dispatch reporting; all 17 golden SSE fixtures (34 golden tests) and the entry tests unchanged and green |
| 9 | **QL-3**: the SSE writer admits a frame only if `nbytes + len(frame.wire) <= queue_bytes` (and < `queue_events`), coalescing first; a frame alone always fits (`streaming/sse_writer.py`) | high-water <= the bound at 1,000 / 4,096 / 10,000 B (base: one frame past it) | `tests/unit/streaming/test_sse_queue_bound.py` (fails on base); the C3 / backpressure tests unchanged and green |

## 4. Load measurements

### 4.1 Cumulative snapshots, L1 at 100 and 200 streams per pod

Each snapshot adds the changes of section 3 to the previous one: `c1` = #1; `c2` = + #2, #3 (watcher tasks), #5's
sealed-log Event drop, #6, #7; `c3` = + #4; `c4` = + #8, #9, the lazy watcher scope of #3; `c5` = + #5 (live frames as
tuples, sealed frames as one untracked tuple) and the lease renewal folded into the turn watcher (#2). Times in ms;
TTFT overhead with bootstrap 95% CIs; full GC = gen-2 pauses pooled over the pod's 2 processes (p50 / p99 / max),
interval per process; live tracked = census minus cyclic garbage, summed over the pod, at the window's end.

| run | streams/pod | n | TTFT ovh p50 [CI] | p95 [CI] | p99 | e2e ovh p95 | CPU ms/turn | full GC p50 / p99 / max | full GC every (s) | live tracked | load 1m | valid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base-1 | 100 | 4,310 | 10.54 [10.48, 10.62] | 18.03 [17.52, 18.94] | 49.5 | 30.6 | 47.4 | 63.7 / 86.3 / 90.2 | 15.8 / 8.8 | 167.8k | 3.78 | yes |
| base-1 | 200 | 12,961 | 16.82 [16.63, 16.99] | 61.08 [58.13, 63.42] | 144.1 | 109.3 | 46.3 | 126.9 / 177.1 / 208.3 | 6.2 / 5.8 | 342.5k | 5.02 | yes |
| c1-1 | 100 | 4,309 | 9.58 [9.53, 9.62] | 14.79 [14.37, 15.13] | 43.9 | 24.0 | 45.0 | 68.6 / 119.2 / 135.1 | 15.8 / 15.0 | 168.2k | 3.8 | yes |
| c1-1 | 200 | 12,967 | 14.16 [14.07, 14.30] | 46.17 [44.53, 48.85] | 154.6 | 88.4 | 43.0 | 145.3 / 181.5 / 183.8 | 8.2 / 8.2 | 342.2k | 4.23 | yes |
| c2-1 | 100 | 4,310 | 9.54 [9.48, 9.59] | 15.80 [15.16, 16.57] | 49.3 | 30.6 | 44.1 | 64.4 / 124.2 / 135.6 | 21.4 / 13.6 | 147.0k | 4.06 | yes |
| c2-1 | 200 | 12,967 | 13.98 [13.86, 14.09] | 46.29 [44.19, 48.11] | 137.6 | 88.3 | 42.1 | 133.2 / 169.7 / 176.1 | 8.8 / 8.0 | 295.8k | 6.88 | yes |
| c3-1 | 100 | 4,310 | 9.95 [9.88, 10.01] | 16.93 [16.29, 17.94] | 33.2 | 32.9 | 45.0 | 26.4 / 39.1 / 40.9 | 21.4 / 13.6 | 132.7k | 7.23 | yes |
| c3-1 | 200 | 12,975 | 13.18 [13.09, 13.27] | 29.54 [28.49, 30.66] | 73.2 | 55.2 | 42.2 | 63.8 / 92.1 / 106.6 | 9.0 / 8.0 | 263.2k | 7.21 | yes |
| c4-1 | 100 | 4,310 | 9.75 [9.70, 9.81] | 14.99 [14.65, 15.31] | 29.0 | 26.6 | 44.5 | 26.7 / 33.3 / 33.7 | 15.0 / 20.0 | 134.9k | 4.85 | yes |
| c4-1 | 200 | 12,986 | 13.13 [13.04, 13.21] | 28.29 [27.18, 29.15] | 77.6 | 52.5 | 42.6 | 64.4 / 94.2 / 95.7 | 8.5 / 8.7 | 273.6k | 6.21 | yes |
| c5-1 | 100 | 4,310 | 9.87 [9.82, 9.93] | 15.79 [15.37, 16.28] | 27.0 | 27.2 | 45.4 | 22.5 / 26.2 / 26.4 | 18.8 / 15.8 | 120.9k | 5.93 | yes |
| c5-1 | 200 | 12,973 | 13.84 [13.75, 13.95] | 34.58 [33.12, 35.74] | 79.9 | 65.1 | 43.5 | 58.9 / 92.2 / 115.4 | 8.8 / 8.5 | 251.0k | 7.37 | no (mock lag 2.35 ms) |

### 4.2 What the numbers say (4.1)

- **The full GC pause halved** once the decision run stopped leaving its state as cyclic garbage (`c3`): at 200/pod
  the pooled gen-2 pause p50 / p99 went 126.9 / 177.1 (base) -> 145.3 / 181.5 (`c1`: fewer allocations, so fewer
  but larger collections: every 8.2 s instead of 6 s, each with more garbage) -> 133.2 / 169.7 (`c2`) -> **63.8 /
  92.1 ms (`c3`)** -> 64.4 / 94.2 (`c4`) -> 58.9 / 92.2 (`c5`); at 100/pod 63.7 / 86.3 -> 22.5 / 26.2 ms. A full
  collection scans live objects *and* the cyclic garbage that waits for it: before `c3` every finished turn left
  ~430 objects of garbage (the decision run's ~300 plus the request's), 30-80k objects at the census moment; after
  it ~27 per turn (uvicorn's request cycle and the api's request context, section 7).
- **The live heap per live turn** fell from ~1,750 (base) to ~1,300 objects (`c3`-`c5`; the census difference is
  noisy by about +-100), the live heap at 200/pod from 342k to 251k objects over the pod (-27%).
- **TTFT overhead at 200/pod**: p95 61.1 -> 28.3-29.5 ms and p99 144 -> 73-78 ms in the valid windows; p50 16.8 ->
  13.1-13.2 ms. At 100/pod both L1 bounds now hold (p50 9.75-9.95, upper CI <= 10.01; p95 15.0-16.9). The `c5`
  200/pod window is invalid (one mock process's lag p99 2.35 ms; loadavg up to 10.9 during it from other agents'
  builds): its p95 34.6 is a host artefact, re-measured in 4.3 (`c5-2`).
- **CPU per turn** fell 46.3 -> 42.1-43.5 ms at 200/pod and 47.4 -> 44.1-45.4 at 100/pod (the deadline scopes alone:
  -3.3 ms at 200/pod, `c1`).

### 4.3 The gen-2 threshold A/B on `c5` (same code, back to back)

`c5-2` = `snap-c5` with the thresholds of the tree (700, 10, 40); `g100-1` = the same with `--gc-threshold 700,10,100`
(set before the app starts, so `_freeze_startup_heap` keeps it). Columns as in 4.1.

| run | streams/pod | n | TTFT ovh p50 [CI] | p95 [CI] | p99 | e2e ovh p95 | CPU ms/turn | full GC p50 / p99 / max | full GC every (s) | live tracked | load 1m | valid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c5-2 | 100 | 4,310 | 9.57 [9.52, 9.62] | 14.86 [14.52, 15.21] | 27.7 | 25.2 | 44.3 | 22.8 / 42.8 / 49.3 | 14.3 / 21.4 | 121.6k | 4.25 | yes |
| c5-2 | 200 | 12,987 | 12.93 [12.86, 13.01] | 25.83 [24.93, 27.10] | 68.7 | 49.6 | 42.4 | 56.7 / 80.8 / 81.3 | 8.5 / 8.8 | 248.8k | 5.47 | yes |
| g100-1 | 100 | 4,310 | 9.57 [9.52, 9.61] | 14.49 [14.07, 14.73] | 22.4 | 24.7 | 44.1 | 25.3 / 30.6 / 30.6 | 42.9 / 42.9 | 122.8k | 5.41 | yes |
| g100-1 | 200 | 12,989 | 12.72 [12.65, 12.80] | 23.67 [23.26, 24.26] | 48.8 | 47.0 | 42.2 | 62.0 / 82.5 / 86.3 | 20.5 / 22.5 | 252.2k | 5.67 | yes |

### 4.4 End state (`final-1` = `snap-final`: base + every change of section 3 + `GC_THRESHOLDS` (700, 10, 100))

The thresholds come from `composition._freeze_startup_heap` here (no `--gc-threshold`), so this is what an api
process runs; the e2e images `noema-{api,worker,media}:e3gc` were built from the same snapshot.

| run | streams/pod | n | TTFT ovh p50 [CI] | p95 [CI] | p99 | e2e ovh p95 | CPU ms/turn | full GC p50 / p99 / max | full GC every (s) | live tracked | load 1m | valid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| final-1 | 100 | 4,311 | 9.57 [9.52, 9.63] | 14.57 [14.24, 15.01] | 21.0 | 23.8 | 43.9 | 30.3 / 34.3 / 34.3 | 60.0 / 33.3 | 122.7k | 4.43 | yes |
| final-1 | 200 | 12,991 | 12.74 [12.66, 12.81] | 23.73 [23.19, 24.23] | 48.7 | 44.4 | 42.0 | 61.3 / 78.1 / 78.7 | 22.5 / 20.5 | 249.7k | 6.15 | yes |

Against `base-1` at 200 streams per pod: TTFT overhead p50 16.82 -> **12.74** ms, p95 61.08 -> **23.73** [23.19,
24.23] ms, p99 144.1 -> 48.7 ms, e2e overhead p95 109.3 -> 44.4 ms, CPU 46.3 -> 42.0 ms per turn, full GC p50 / p99
126.9 / 177.1 -> 61.3 / 78.1 ms every 20.5-22.5 s instead of 5.8-6.2 s, live tracked 342.5k -> 249.7k objects. At 100
per pod: p50 10.54 -> 9.57 (upper CI 9.63), p95 18.03 -> 14.57 ms.

### 4.5 The TTFT overhead floor: A1 before the decision entry (`c3` vs `c4`, ABBA, 4 streams per pod)

`--plan 4:120 --warmup-s 30`, 70 turns per window (2 streams per process: no queueing). Ingress = client send to the
generation request at the mock (the time the request waits for).

| run | A1 starts | n | TTFT ovh p50 [CI] | p95 | ingress p50 / mean | load 1m |
|---|---|---|---|---|---|---|
| floor-c3-1 | after the entry | 70 | 8.48 [8.34, 8.69] | 14.72 | 7.92 / 9.04 | 4.35 (a make check ran on other cores) |
| floor-c4-1 | before it | 70 | 8.49 [8.34, 8.78] | 11.74 | 7.84 / 8.57 | 6.94 |
| floor-c4-2 | before it | 70 | 8.55 [8.39, 8.74] | 10.97 | 8.02 / 8.40 | 4.73 |
| floor-c3-2 | after the entry | 70 | 8.63 [8.41, 8.73] | 10.01 | 8.02 / 8.19 | 3.03 |

**No measurable floor gain** (p50 8.56 vs 8.52 ms, ingress p50 7.97 vs 7.93 ms; the CIs overlap): sprint A's 1.5 ms
entry (its L2 `GET` waiting behind other ready tasks) is no longer on the request's path at this load. A1's request
still needs its own task hops (the run entry, the walk, the attempt, the adapter's stream tasks) and leaves during
the entry's Redis round trip; what the floor holds is ingress (auth, idempotency, the session lease, admission, the
turn start, preflight: ~7.9 ms). The change is kept (it removes the entry from the path when it is slow, e.g. views
that resolve media) and its behavior is pinned by `test_early_a1.py`; at 200 streams per pod its effect is inside the
`c3` -> `c4` row (p50 13.18 -> 13.13 ms).

### 4.6 Where the live heap per turn went (census, `base-1` vs `c5-2`, 200/pod minus 100/pod, per extra live turn)

| type | base | c5 | why |
|---|---|---|---|
| all tracked | 1,746 | 1,272 | (the difference includes ~44 resume-window turns per extra live turn: the turn rate doubles) |
| `SseFrame` | 43.7 | 0 | #5 live frames as tuples |
| `_SealedFrames` / `FinishedTurn` | 43.3 / 43.0 | 0 / 0 | #5 packed tuple, #6 row tuples (the 44-53 `_TurnLog` per live turn stay: one per sealed turn) |
| `Event` (anyio + asyncio) | 103.6 | 47.9 | #5 sealed wake-ups, #2 turn watchers, #1 watcher task handles, #7 |
| `coroutine` | 113.5 | 89.4 | #1, #2, #3 (fewer tasks and await chains) |
| `Task` / `TaskState` | 15.5 / 15.7 | 11.9 / 11.9 | #1, #2, #3 |
| `CancelScope` / `TaskGroup` | 30.2 / 8.7 | 26.0 / 8.1 | #1 (each scope also holds 2 sets) |

What remains per live turn is mostly the ~12 tasks a turn keeps for its whole life (with anyio 4.15 each brings a
`TaskHandle`, its own cancel scope and Event, the wrapper coroutine, `TaskState`, a context and weak references): the
turn task and its watcher, the generation task, walk and attempt, the adapter stream's 2, the SSE response watcher
and the writer's reader, writer and timer; plus the turn's JSON snapshots, prompt and history blocks.

## 5. GC re-tune (`composition._freeze_startup_heap`)

- **Thresholds: adopted (700, 10, 100)** (`GC_THRESHOLDS`, was (700, 10, 40)). A full collection scans the live heap
  plus the cyclic garbage waiting for it. Sprint B kept 40 because with 100 each pause grew (the garbage of every
  turn waited longer: 91 -> 134 ms p50). With E3's churn cuts a turn leaves ~27 objects of garbage instead of ~430,
  so waiting longer no longer makes a pause longer: at 200 streams per pod, 40 -> 100 made the full collections
  every 20.5-22.5 s instead of 8.5-8.8 s at about the same pause (p50 56.7 -> 62.0, p99 80.8 -> 82.5 ms), so fewer
  turns meet one: **TTFT overhead p95 25.83 [24.93, 27.10] -> 23.67 [23.26, 24.26] ms** (CIs disjoint), p99 68.7 ->
  48.8 ms, p50 12.93 -> 12.72 ms, the GC share of wall time 4.3% -> 3.5%, RSS 808.5 -> 812.9 MiB (+0.5%) and the
  same CPU per turn. At 100/pod: p95 14.86 -> 14.49, one full collection per 43 s instead of per 14-21 s.
  `tests/unit/composition/test_heap.py` asserts the new triple. 250 was not measured (no window left in this
  sprint); the young thresholds stay (larger ones make each young collection longer, C2b and sprint B).
- **`gc.freeze()` after warm-up: not adopted.** The startup freeze already holds the long-lived heap (255.8k frozen
  objects): after 2.5 min of serving at 4 streams per pod (`floor-c3-1` census), a process tracks only 7.5k / 8.1k
  objects, of which ~2.6k belong to its 2 live turns, ~0.2k to the sealed turns of the resume window and 0.8k are
  cyclic garbage: a second freeze could take at most ~4k objects (~3%) off a full collection at 200 streams per
  pod, and it would pin forever whatever turns and garbage are live at that moment.

## 6. Behavior: what stayed identical, and the three deliberate differences

- **Kept**: every unit, conformance, integration and golden test passes unchanged (the full unit suite, 3,444 tests,
  the 17 golden SSE fixtures, the decision engine / usage / L2 / outbox suites), the spec-04 §4 event order
  (P-SSE-1 asserted in the new tests), W / t_d0 / `escalate_until`, the commit rule, the deadline semantics
  (`cancelled_caught`, unwrapped body exceptions; the loop variant has the watcher variant's outcomes in 9 scenarios),
  the usage lines, member metrics and L2 put of a decision (taken at the final snapshot instead of at `finalize()`,
  when they can no longer change), the 1 MiB / 256 event bounds (now never exceeded) and the wire bytes of every frame.
- **A1 before the decision entry** (requested): its stream request no longer waits for the decision run's entry
  (in the load runs the entry was not on the path any more: no measurable gain, 4.5). A decision entry that fails
  *after* A1's first step now shows A1's `generation.started` and its stop (T19 without a frame for
  `deadline_exceeded`, else `generation.cancelled{client_abort}`) before the turn's error, where the entry used to
  fail before any attempt existed (still P-SSE-1-valid; an entry that fails at once is unchanged: one
  `route.selected`, then `error`). With a slow entry (more than the hold cap minus S1's lead, e.g. 200 ms of views)
  A1's first text comes earlier relative to S1, so a hold-cap commit (`holdback_deadline`) becomes more likely; in
  every load window holdback 0 with `spec_agrees` stayed >= 99.98% of the turns, as in base. spec-04 §3 ("A1 starts
  at the same point" as t_d0) should say "A1 starts with the decision run's entry; t_d0 when it returns" (spec owner).
- **Event log frames**: a subscriber now receives an `SseFrame` rebuilt around the same `wire` / `payload` bytes
  instead of the very object the writer stored; `test_frames_are_serialized_once_and_shared` asserts shared bytes
  instead of object identity (the only existing assertion changed).
- **The run's T19 timer** on `SystemClock` is the loop's `call_later(..., _expire)` instead of a task: a bug inside
  `_expire` would be logged by the loop instead of failing the run's task group.

## 7. Findings for other owners (measured, outside this role's paths)

- **adapters (`adapters/resilience/timeouts.py` `begin_wait`)**: the stream watchdog enters a fresh `CancelScope` per
  provider event, ~104 per turn (each a scope + 2 sets; 35% of all cancel scopes a turn creates, `diag-base`). One
  scope per stream whose `deadline` is moved per event would remove them (sprint B F2 noted it).
- **adapters (`adapters/_stream.py`)**: 2 anyio tasks per generation stream (lines 134, 139) for the life of the
  stream; with anyio 4.15 every task is ~25 tracked objects (a `TaskHandle` with its own `CancelScope` and `Event`,
  the wrapper coroutine, `TaskState`, the context and weak references).
- **api / uvicorn (`api/middleware/context.py`, `idempotency.py`)**: the remaining cyclic garbage, ~27 objects per
  request (uvicorn's `RequestResponseCycle`, `RequestContext`, `TraceContext`, `IdempotencyHook`, 3 `AsyncExitStack`
  and their deques): freed only by a full collection.
- **decision (`coordinator.py` `DISPATCH_YIELDS = 6`)**: the 90 extra task steps per turn existed so that A1's request
  left before the 15 member calls; A1 now leaves during the decision entry, before any member unit exists, so the
  yields only delay S1. An A/B (`--dispatch-yields 0`) is the decision owner's call (dispatch order, not churn).
- **spec-04 §3** wording (section 6).

## 8. Reproduce

```
# stack (once): base services of project noema-e3gc-perf on 317xx, bootstrap, load config v2
env NOEMA_PG_PORT=31732 NOEMA_MOTO_PORT=31750 NOEMA_VALKEY_PORT=31779 NOEMA_API1_PORT=0 NOEMA_API2_PORT=0 \
  NOEMA_API1_METRICS_PORT=0 NOEMA_API2_METRICS_PORT=0 NOEMA_API1_METRICS2_PORT=0 NOEMA_API2_METRICS2_PORT=0 \
  NOEMA_MOCKBEDROCK_PORT=0 NOEMA_FAULT_TOXIPROXY_PORT=0 NOEMA_FAULT_PG_PORT=0 NOEMA_FAULT_VALKEY_PORT=0 \
  NOEMA_FAULT_MOTO_PORT=0 docker compose -p noema-e3gc-perf -f deploy/compose/docker-compose.yml up -d --wait \
  postgres valkey moto
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tools.bootstrap_dev --compose-project noema-e3gc-perf \
  --state ~/.noema/stacks/noema-e3gc-perf.json
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.load.stack activate --state ~/.noema/stacks/noema-e3gc-perf.json
# snapshots: git archive HEAD | tar -x -C /tmp/noema-e3gc/snap-base; /tmp/noema-e3gc/bin/mksnap.sh <new> <from> <files>
# a window (waits for loadavg < 4 / < 6 and no other tests.load.run; QA lead E2 placement, 2 api processes):
/tmp/noema-e3gc/bin/run.sh /tmp/noema-e3gc/snap-<x> <run> --plan 100:300,200:450 --warmup-s 60 --settle-s 30 \
  --census [--gc-threshold 700,10,100]
python3 /tmp/noema-e3gc/bin/summ.py /tmp/noema-e3gc/runs/<run>         # L1, CPU, gen-2 pauses per process
python3 /tmp/noema-e3gc/bin/census_live.py /tmp/noema-e3gc/runs/<run>  # live tracked per live turn, by type
# per-turn task / scope / Event creations (diagnostic): EXTRA_PYTHONPATH=/tmp/noema-e3gc/diag run.sh ... \
#   --api-env NOEMA_E3_DIAG=/tmp/noema-e3gc/runs/<run>.counts; diag_summ.py <run dir> <counts prefix>
# microbenchmarks: PYTHONPATH=<snap>/src python -P /tmp/noema-e3gc/bench/{deadline_bench,eventlog_heap,
#   decision_cycles3}.py [<snap root>]
```

Runs: `base-1`, `c1-1` .. `c5-1` (4.1), `c5-2`, `g100-1` (4.3, section 5), `final-1` (4.4), `floor-c3-1`,
`floor-c4-1`, `floor-c4-2`, `floor-c3-2` (4.5), `diag-base`, `diag-c5` (creation counters). Summaries:
`reports/perf/e3-perf-gc-objects/<run>.json` (results), `<run>.gc-census.txt` (pauses and census),
`diag-*.counts.txt`.
