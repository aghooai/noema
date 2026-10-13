# E3 perf-gc-objects: GC pauses and object churn per live turn (role report)

Wave E3 sprint, role **perf-gc-objects**, 2026-10-12. Detailed method, per-change evidence and raw tables:
[reports/perf/e3-perf-gc-objects.md](../../perf/e3-perf-gc-objects.md); analyzed results (with provenance) in
`reports/perf/e3-perf-gc-objects/`. No live Bedrock call, no AWS resource, no git write; own compose projects
`noema-e3gc-perf` (perf, ports 317xx) and `noema-e3gc-e2e` (e2e, port base 31800), both torn down at the end.

## 1. Verdict

L1 = TTFT overhead p50 <= 10 ms and p95 <= 25 ms at 200 streams per pod (2 processes, 2 vCPU). Base = `base-1`
(the wave-e2 tree); end state = `final-1` (base + this role's changes, the thresholds from `composition`). Both: the
QA lead's E2 placement, 1-minute loadavg mean 5.0 / 6.2 at 200/pod (peaks 7.0 / 8.6), every window valid by
spec-10 §5, n 12,961 / 12,991 at 200/pod.

| Item | base -> end state |
|---|---|
| QL-5 full-GC pause at 200/pod (pooled over the pod, p50 / p99 / max) | 126.9 / 177.1 / 208.3 ms -> **61.3 / 78.1 / 78.7 ms**; every 5.8-6.2 s -> every 20.5-22.5 s per process; GC share of wall time 6.0-7.1% -> 3.1-3.9% |
| full-GC pause at 100/pod | 63.7 / 86.3 ms every 8.8-15.8 s -> 30.3 / 34.3 ms every 33-60 s |
| tracked heap (live, census) at 200/pod | 342.5k -> 249.7k objects over the pod (-27%); per live turn ~1,750 -> ~1,270 |
| cyclic garbage | per finished turn ~430 -> ~27 objects (the decision run: 672 -> 0 per decision) |
| CPU per turn (pod) | 46.3 -> **42.0 ms** at 200/pod (-9%), 47.4 -> 43.9 ms at 100/pod (-7%) |
| **L1 at 100 streams/pod** | p50 10.54 [10.48, 10.62] -> **9.57 [9.52, 9.63]**, p95 18.03 -> **14.57 [14.24, 15.01]**: passes |
| **L1 at 200 streams/pod** | p50 16.82 [16.63, 16.99] -> **12.74 [12.66, 12.81]** (still > 10), p95 61.08 [58.13, 63.42] -> **23.73 [23.19, 24.23]** (<= 25), p99 144.1 -> 48.7 ms: **p95 passes, p50 does not** (section 3) |
| tasks / Events created per turn (diagnostic counters) | anyio tasks **68.7 -> 35.7**, anyio Events 154.7 -> 58.5, cancel scopes ~299 -> ~237 (the 28.9 deadline watchers, 3 of the 4 turn watchers and the run's deadline watcher are gone) |
| A1 before the decision entry | done (unit-verified: the stream request opens at the entry's start); at 4 streams/pod the floor moved within noise (perf report 4.5) |
| QL-3 SSE queue <= 1 MiB | done: a frame is admitted only if `nbytes + len(frame) <= queue_bytes` (a lone frame always fits) |
| GC re-tune | thresholds (700, 10, 40) -> **(700, 10, 100)**; a post-warm-up `gc.freeze()` not adopted (section 2 item 10) |

## 2. What was done (files; evidence in the perf report section 3)

1. **Task-free `deadline_scope`** on `SystemClock` (`core/clock.py`, sprint B F1 / defect 27): 67.6 -> 7.8 us and
   52 -> 9 tracked objects per scope, 28.9 watcher tasks per turn gone; same outcomes as the watcher variant in 9
   scenarios (`tests/unit/core/test_deadline_scope_loop.py`).
2. **One turn watcher** (cancel/abandon, shutdown, deadline, lease renewal) instead of four tasks (`turns/runner.py`;
   `tests/unit/turns/test_stop_watch.py`).
3. **The run's T19 deadline on the loop's timer** instead of a watcher task (`generation/run.py`, `walk.py`;
   `tests/unit/generation/test_deadline_timer.py`).
4. **The decision run drops its coordinator at the final snapshot** (`decision/run.py` `_Settled`): its cyclic garbage
   per decision 672 -> 0 (`tests/unit/decision/test_heap.py`).
5. **Event log**: live frames as untracked tuples (54 -> 5 tracked objects per live 50-frame log), a sealed turn one
   tracked object (4 -> 1) (`streaming/event_log.py`; `tests/unit/streaming/test_log_heap.py`).
6. **Finished turns as untracked tuples** (`turns/service.py`).
7. **Mirror wake-ups reused** instead of 2 new anyio Events per 5 ms batch (`streaming/mirror.py`).
8. **A1 before the decision run's entry** (`turns/runner.py`; `tests/unit/turns/test_early_a1.py`): the event order,
   W, t_d0 and gate are unchanged; a failing entry stops A1 first (spec-04 §4 / P-SSE-1 hold). The floor gain is
   within noise at 4 streams per pod (the perf report 4.5): the entry is no longer what the request waits for.
9. **QL-3**: the SSE queue never exceeds `queue_bytes` (`streaming/sse_writer.py`;
   `tests/unit/streaming/test_sse_queue_bound.py`).
10. **GC re-tune**: `GC_THRESHOLDS` (700, 10, 40) -> **(700, 10, 100)** (`composition.py`; `tests/unit/composition/
    test_heap.py`): at 200/pod full collections every 21 s instead of 8.6 s at about the same pause, TTFT p95 25.8 ->
    23.7 ms, p99 68.7 -> 48.8 ms, RSS +0.5%. A second `gc.freeze()` after warm-up: not adopted (at most ~4k objects,
    ~3% of a full collection, perf report section 5).

Not changed (behavior-identical bar or out of scope): `__slots__` on the per-turn service objects (TurnRunner,
Coordinator, DecisionRunImpl, the generation run: one `__dict__` each, ~6 of ~1,300 objects per turn, for the risk
of breaking attribute assignment by fakes and tests; every per-event dataclass already has slots); the SSE
writer's timer task (merging it would add a deadline scope per frame sent); the decision member unit and hedge tasks
(one task per concurrent HTTP call is the floor; hedges live ~50 ms); `DISPATCH_YIELDS` (dispatch order, the decision
owner's call, perf report section 7).

## 3. L1 and what is left (QL-5)

- **Full GC (QL-5)**: no longer what holds L1's p95 at 200 streams per pod. Pauses halved (p50 127 -> 57-62 ms,
  p99 177 -> 78-92 ms) and, with (700, 10, 100), come every ~21 s instead of ~6 s per process; the TTFT overhead
  p95 at 200/pod went 61.1 -> 23.7 ms (upper CI 24.3: **within L1's 25 ms**), p99 144 -> 49 ms.
- **p50 at 200/pod: 12.7-12.9 ms (bound 10)** is CPU queueing, not GC: the pod runs at ~1.22 of its 2 cores, the
  idle floor (4 streams per pod) is ~8.5 ms of which ~7.9 ms is ingress (auth, idempotency, session lease, admission,
  turn start, preflight; the decision entry is not a measurable part of it, section 2 item 8), and at 100/pod p50
  is 9.6 ms. Lowering it needs less CPU per turn (the parallel adapter work: the dict SigV4 signer and the
  eventstream decoder, estimated at ~3.6 ms of the 42 ms per turn in its brief) or fewer streams per pod: the QA
  lead's knee search (50-200 per pod) decides.
- Residual GC cost: the live heap (~1,300 tracked objects per live turn: coroutines and tasks of ~12 long-lived anyio
  tasks per turn, the turn's JSON snapshots, prompt and history blocks), ~27 objects of cyclic garbage per request in
  the api / uvicorn request objects, and 104 cancel scopes per turn in the adapters' stream watchdog (perf report
  section 7: other owners).

## 4. Checks

| Check | Command | Result |
|---|---|---|
| make check (final tree, every agent's edits) | `NOEMA_AGENT_ID=perf-gc-objects taskset -c 0-3,24-27 make -k check XDIST=4` at 08:08 and again at 09:13 (after the last edit) | **all green both times**: ruff, mypy 327 src / 590 test files, import-linter 27 kept / 0 broken, forbidden-scan clean, ownership ok, contracts-check, unit 3,444, conformance 757 (+8 skipped), integration 573 (+21 skipped), sdk TS 86 (+6) / Python 192 (+1) |
| make e2e, twice, own stack | `NOEMA_{API,WORKER,MEDIA}_IMAGE=noema-*:e3gc make e2e E2E_PROJECT=noema-e3gc-e2e E2E_PORT_BASE=31800 E2E_BUILD=0` (images built from `snap-final`: base + this role's files; 2 workers per api container) | **52 passed (155.0 s), 52 passed (154.3 s)**; stack torn down (`make e2e-down`) |
| owned unit suites | `pytest tests/unit/{core,turns,generation,streaming,decision,composition} tests/contract_internal` | green; the new tests fail on `snap-base` where they pin a change (decision heap, early A1, QL-3, log heap) |
| integration (streaming, turns, decision) | `pytest -m "not live and not e2e and not perf" tests/integration/{streaming,turns,decision}` | 28 passed |
| load windows | `/tmp/noema-e3gc/bin/run.sh` (QA lead E2 placement, 2 api processes per pod, loadavg gate, no overlap with the other perf agent's runs: 0 foreign load processes in every sample) | 22 windows (9 runs at 100 + 200 per pod, 4 floor windows) and 2 diagnostic runs, all valid by spec-10 §5 except `c5-1` at 200/pod (marked, re-measured as `c5-2`) |

## 5. Left behind, handed over

- Torn down: compose projects `noema-e3gc-perf` and `noema-e3gc-e2e` (containers, networks, volumes). Kept: the
  images `noema-{api,worker,media}:e3gc`, the stack state files `~/.noema/stacks/noema-e3gc-{perf,e2e}.*`, raw runs
  and snapshots in `/tmp/noema-e3gc/`.
- Doc follow-ups (not this role's paths): `GC_THRESHOLDS` is now (700, 10, 100) where IMPL-DECISIONS (IMPL-087 and
  its GC paragraph), spec-11 (the GC bullet), OWNER-CHECKPOINT and `docs/modules/integration.md` §5 still say
  (700, 10, 40); spec-04 §3 "A1 starts at the same point" as t_d0 should read "A1 starts with the decision run's
  entry, t_d0 when it returns".
- Other owners (perf report section 7): the adapters' per-event stream-watchdog cancel scope (104 per turn) and the 2
  stream tasks; the api / uvicorn request objects' ~27 objects of cyclic garbage per request; `DISPATCH_YIELDS` now
  only delays S1 (A1 leaves during the decision entry).
- L1 at 200 streams per pod still misses p50 (12.7 ms): CPU queueing (section 3); the QA lead's knee search on the
  combined tree decides the pod density.
