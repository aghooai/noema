# C2b perf smoke: one api process against the wire mock (Wave C2b/D, perf engineer, 2026-10-11)

Scope: spec-10 §5 load harness (driver + metrics) and a first measurement of one api process ("pod") against
`tools/mockbedrock` with scripted latencies; targets from 06 §11 / spec-10 §3 L1 and C2. No live Bedrock call, no
AWS resource touched (moto and the mock only). Raw data: `/tmp/noema-perf/runs/<run>` on the build host (large, not
in the repo); analyzed summaries: `reports/perf/c2b/*.json`.

**Revised after the perf review (C2b/D perf fixer, 2026-10-11; section 10).** The first version's runs were invalid
by spec-10 §5 (19.5% of the 50-stream turns had `holdback_ms` > 0): the orchestrator measured the commit at the
moment its consumer task dequeued `generation.committed` instead of the engine's commit, fixed now (residual R3,
section 8). Every number in sections 1, 3 (but its labeled baseline) and 4 is from the re-runs
`reports/perf/c2b/fix-*.json` with that fix,
the spec-10 §5 placement (Postgres and Valkey pinned, 4 mock processes) and `provenance` (code and config digests)
in each file. The `baseline-*`, `final-F1-*`, `ramp-*` and `intermediate-*` files and the section 5 progression are
the first version's diagnostic runs, kept for the before/after comparison; they are not spec-10 §5 measurements.

## 1. Verdict

| Target (06 §11) | Result | Met? |
|---|---|---|
| Overhead p50 <= 10 ms at 200 streams/pod | one process saturates at about 19 turns/s (about 150 streams of 7 s turns); at 200 streams p50 = 3,670 ms (and the process can fall into an overload state, section 3) | **no** |
| Overhead p95 <= 25 ms at 200 streams/pod | p95 = 5,382 ms at 200 (saturated); 29.3 ms at 50 streams (42.0 ms from the image) | **no** |
| Event-loop lag p99 < 50 ms (C2: up to 200 streams) | 12.6 ms at 50, 33.4 ms at 100, 285 ms at 200 (image: 17.0 at 50, 41.1 at 100) | 50 and 100 yes, **200 no** |
| Memory over 15 min (C4 shortened style) | RSS 440 -> 472 MiB in the first 5 min (the 300 s resume window filling), then flat (+0.05% in the last 8 min) | no leak seen |

All numbers are from the re-runs of the venv api unless marked "image" (valid by spec-10 §5 at 50 and 100 streams;
the 200-stream windows are saturated, invalid as L1 points, and reported for capacity). The fixes of section 5 took the
50-stream overhead from p50 42 / p95 243 / p99 277 ms to 13.8 / 29.3 / 49.6 ms and the CPU per turn from 84 to
64-68 ms. The 200-streams-per-pod target is far out of reach for one Python process with the router-default fan-out:
the loop thread is CPU-bound (decision fan-out through aiobotocore/httpx, per-event streaming work) and its full GCs
grow with the number of live turns (section 6). The production image is about 22% slower still (its interpreter
build, R5). Section 7 lists the options.

## 2. Method

- **Stack**: compose project `noema-perf` of its own (postgres, valkey, moto only; ports 287xx), seeded by
  `tools/bootstrap_dev.py`, plus config v2 = the bootstrap config with lifted tenant rate limits
  (`python -m tests.load.stack activate`; the 6,000 rpm per-key default admits about 214 turns a minute, spec-05
  §1.2 says the load test sets its own). The shared `noema-e2e` stack and its mock were not used: other agents load
  scenarios there and the limits would have throttled them. Its Postgres and Valkey containers are pinned to cores
  40-45 (`docker update --cpuset-cpus`, spec-10 §5's "Postgres/Valkey on 2-7" with the same size; moto, which no
  turn calls, is not pinned).
- **The api**: `python -m tests.load.serve` = the image CMD (`uvicorn --factory noema.api.app:create_app --loop
  uvloop --http httptools --no-access-log --timeout-graceful-shutdown 35`) run programmatically, `NOEMA_ENV=dev`,
  `E2E_VALIDATE` off, the stack's host env, pinned with `taskset -c 46-47` (2 vCPU, 08 §9), on the dev venv's
  managed CPython 3.12.12. One confirmation run (section 3) used the `noema-api` image instead (CPython 3.12.15, no
  dev dependencies): `docker run --cpuset-cpus 46-47` with the compose app settings (init, uid 10001, read-only
  root, `cap_drop: ALL`, no-new-privileges, /tmp tmpfs), host network, `tests/load` mounted read-only so the same
  `serve.py` hooks (lag sampler, GC log) run on the image's interpreter.
- **The mock**: four in-process `MockBedrock` servers (`tests/load/mockserve.py`, spec-10 §5's 4 processes) on cores
  32-33, 34-35, 36-37 and 38-39: two serve Converse (Luna and Haiku members, the Sol stream) and two Mantle (DeepSeek
  members), each pair sharing its port with SO_REUSEPORT. Scenario `tests/load/scenarios/overhead.yaml`
  (spec-10 §5 L1): every member answer at 50 ms; Sol first event 200 ms, first text 300 ms, 400 tokens in 4-token
  chunks at 60 tokens/s (stream end at 6,900 ms), every chunk stamped `«ts:<monotonic_ns>»`. Records are drained
  in-process (no `/_mock/requests` paging on the mock's loop).
- **Drivers**: `tests/load/driver.py`, 50 streams per process on cores 20-31. Each stream loops turns on its own
  session (a new one every 10 turns), every pre-commit key pinned (`sol-fast`, effort low, 600 tokens,
  `conditioning: off`), a nonce per turn (no L2 hit: all 15 router-default calls run). The first turn of every
  stream and every non-200 response were validated with the `ContractValidator` (0 violations in every run). The
  drivers are closed-loop, as spec-10 §5 defines the scenario (N concurrent streams): past the knee a stream's next
  turn waits for its previous one, so the latencies there are queueing at that concurrency and the turns/s column
  is the capacity; they are not the response to an offered arrival rate (no coordinated-omission correction).
- **Quantity** (spec-10 §5, CLOCK_MONOTONIC shared by all processes): per turn, `TTFT overhead = (t_first_delta -
  t_send) - (t_mock_first_text_sent - t_mock_req_recv)`; `e2e overhead = (t_turn_completed - t_send) -
  (t_mock_stream_done - t_mock_req_recv)`; decomposition `ingress = t_mock_req_recv - t_send`, `egress = t_first_delta
  - stamp0`. Percentiles with bootstrap 95% CIs (1,000 resamples), written `p [low, high]`.
- **Plans**: F1 = 60 s warm-up, 50 streams 300 s, 30 s settle, 100 streams 300 s, 30 s settle, 200 streams 900 s
  (the 15-minute memory run). Ramps: 2-minute windows (spec-10 C2 "shortened, labeled") after a 30 s settle each.
  Image confirmation: 50 and 100 streams, 300 s each. DISPATCH_YIELDS A/B: 50 and 100 streams, 120 s each, with
  `--dispatch-yields 0` then `6`, back to back (section 5, fix 5).
- **Host**: 48 vCPU (AMD EPYC 7R13, 24 cores x 2 threads), shared with other agents' compose stacks and test runs,
  whose processes are not kept off our cores (taskset only confines ours). The 1-minute loadavg is sampled every 5 s
  and reported per window; every run was started below 8.
- **Provenance** (spec-10 §5 "config hash, commit"): every `results.json` carries `provenance`: HEAD (`4252561`,
  tag wave-c2a) plus a sha256 over the uncommitted state of src, tests, tools, deploy, pyproject.toml and uv.lock
  (tracked diff and untracked files; the working tree of this wave is uncommitted), the active config version and
  digest (v2, `sha256:848146b0...de4b6`), the scenario's sha256, the api interpreter (or the image id), the placement
  as applied and the load at the start.
- **Validity** (spec-10 §5, computed per window by `stats.validity`): >= 99.9% of the turns with `holdback_ms = 0`
  and `spec_agrees`, and every driver and every mock process with lag p99 <= 2 ms (column "valid"). Every driver and
  mock process passes in every window. The holdback rule passes in the 300 s F1 windows at 50 and 100 streams, the
  image's 50, the A/B windows and the ramp's 50; the ramp's 2-minute 75- and 100-stream windows miss it by one turn
  each (2 of 1,297 and 2 of 1,714: real holds of 4-79 ms where S1 came 318-391 ms after t_d0, behind a loop stall;
  they are C2 lag points there, not L1 points); saturated windows (>= 125 streams; from the image, 100) fail it,
  because S1 then comes after the first text (`holdback_deadline` commits): they are the knee and overload.

## 3. Final numbers (run F1 re-run, all fixes; `reports/perf/c2b/fix-F1-50-100-200.json`)

Overhead in ms, `p [95% CI]`; ingress = client send -> generation request at the mock; lag = api event loop;
valid = spec-10 §5 (section 2).

| streams | n | turns/s | TTFT overhead p50 | p95 | p99 | e2e overhead p50 | p95 | p99 | ingress p50 / p95 | api lag p99 / max | api CPU (cores) | RSS MiB first -> last | load 1m mean / max | errors | valid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 2155 | 7.18 | 13.8 [13.6, 13.9] | 29.3 [27.8, 30.9] | 49.6 [44.5, 56.3] | 21.4 [21.2, 21.6] | 38.2 [36.0, 42.2] | 75.6 [64.0, 87.3] | 12.1 / 19.6 | 12.6 / 63 | 0.49 | 205 -> 287 | 6.3 / 8.1 | 0 | yes |
| 100 | 4279 | 14.26 | 54.7 [53.1, 56.5] | 134.3 [128.9, 137.5] | 182.4 [172.6, 189.5] | 111.2 [108.8, 113.0] | 232.0 [228.6, 237.8] | 1494.6 [1372.3, 1561.9] | 41.8 / 112.7 | 33.4 / 177 | 0.92 | 313 -> 403 | 5.1 / 6.4 | 0 | yes |
| 200 | 14412 | 16.01 | 3670.5 [3645.3, 3691.6] | 5382.1 [5342.5, 5420.6] | 6063.6 [6013.8, 6117.6] | 4994.2 [4969.8, 5020.9] | 7025.4 [6978.5, 7067.2] | 7874.4 [7812.0, 7987.4] | 2982.7 / 4686.4 | 284.6 / 444 | 1.00 | 440 -> 473 | 7.6 / 16.2 | 1500 x 500, 7 x 409 | no |

- 50 streams (valid: 100% `holdback_ms = 0` with `spec_agrees`, 15 decision calls per turn; worst driver lag p99
  0.93 ms, worst mock process 0.97 ms): egress of the first delta p50 0.9 / p95 13.3 ms; the client TTFT p50 is
  313.9 ms for a 300 ms upstream. Against the nominal script (charging the mock's own lateness to Noema) p50 13.9 /
  p95 29.4 ms. `decision_ms` p50 75 / p95 104 ms. 0 contract violations. Same as the first version's F1 within the
  CIs (13.7 / 30.3 / 55.3 ms).
- 100 streams (valid: 99.953%, two turns with `holdback_ms` 4 and 19 where a loop stall put S1 after the first
  text; worst mock process lag p99 1.25 ms): the loop is at 0.92 of a core; lag p99 33 ms is inside the 50 ms
  target, the overhead is not. `decision_ms` p50 113 / p95 223 ms.
- 200 streams: saturated (1.00 of a core), turns queue in the api (client turn p50 11.9 s for a 6.9 s stream). The
  first 3 minutes ran as the first version's F1 did (19.4 turns/s, 28% `holdback_deadline`); then the process fell
  into the overload state of section 4 and stayed there to the end (about 15 turns/s, 70% `holdback_deadline`,
  ~130 x 500 a minute: DB pool acquire timeouts, admission and the turn-owner refresh on Redis timeouts, the
  DeepSeek breaker cycling every ~18 s). The first version's 15-minute window at the same load stayed in the first
  state (14 x 500): past the knee one process is metastable (section 4). Invalid as an L1 point (S1 after the first
  text), reported for capacity and overload only.
- Memory (200 streams, 15 min): RSS 439.7 MiB at the window start, 472.4 at +5 min, 472.6 at +8 min, 472.9 at the
  end (VmHWM 472.9 MiB; +0.05% over the last 8 minutes); the growth is the resume window (300 s of sealed logs and
  finished turns) filling, then flat: no leak. fds max 565, threads 9.

### Image confirmation (`noema-api:perf`, CPython 3.12.15; `reports/perf/c2b/fix-image-50-100.json`)

The same harness with the api from the image (section 2; image `sha256:65341e15...5416`, built from this working
tree), 300 s windows:

| streams | n | turns/s | TTFT overhead p50 | p95 | p99 | e2e overhead p50 | p95 | api lag p99 / max | api CPU (cores) | CPU ms / turn | load 1m mean / max | errors | valid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 2156 | 7.19 | 17.6 [17.4, 17.9] | 42.0 [39.4, 44.4] | 62.6 [56.8, 67.3] | 28.7 [28.2, 29.0] | 60.8 [55.0, 64.9] | 17.0 / 76 | 0.60 | 83 (venv: 68) | 5.5 / 7.6 | 0 | yes |
| 100 | 4178 | 13.93 | 72.8 [68.5, 77.0] | 811.8 [769.9, 843.1] | 1037.5 [1004.2, 1070.7] | 246.8 [235.2, 257.8] | 633.1 [621.3, 636.7] | 41.1 / 205 | 0.92 | saturated | 6.5 / 9.0 | 0 | no (4% `holdback_deadline`) |

- The image needs about 22% more CPU per turn than the dev venv (83 vs 68 ms at 50 streams), so its knee comes
  before 100 streams (100 streams: p95 812 ms, S1 after the first text in 13% of the turns). The cause is the
  interpreter build, not the container: the image's CPython (the Docker Official `python:3.12-slim` build) runs a
  pure-Python micro-benchmark (dicts, f-strings, json, hmac, re, a full GC; no syscalls) 26% slower than the dev
  venv's managed CPython 3.12.12 (python-build-standalone) on the same core (0.178 vs 0.141 s, best of 5, repeated),
  and its full GCs at 50 streams take 30.3 vs 26.5 ms (p50). The venv numbers above therefore flatter production by
  about that factor; residual R5 (section 8).
- 50 streams from the image: TTFT overhead p50 17.6 / p95 42.0 ms, against the venv's 13.8 / 29.3 ms; valid.

### Before the fixes (the first version's baseline run, diagnostic; `reports/perf/c2b/baseline-50-100-200.json`; 120/120/180 s windows)

The first version's placement (2 mocks, stack unpinned, api 44-45) and holdback measurement; for the before/after
comparison only (not a spec-10 §5 measurement).

| streams | n | turns/s | TTFT overhead p50 | p95 | p99 | e2e overhead p50 | p95 | api lag p99 / max | api CPU | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 858 | 7.15 | 42.0 [40.8, 43.5] | 242.8 [232.0, 251.9] | 277.4 [267.3, 288.0] | 57.3 [54.1, 62.2] | 300.8 [271.6, 331.8] | 15.7 / 251 | 0.60 | 0 |
| 100 | 1472 | 12.27 | 1070.0 [1043.4, 1087.9] | 1788.9 [1697.4, 1919.0] | 2088.8 [2058.3, 2312.0] | 1376.4 [1352.1, 1405.1] | 2190.5 [2122.1, 2516.2] | 434.8 / 552 | 0.98 | 0 |
| 200 | 2685 | 14.92 | 3925.8 [3875.7, 3965.3] | 5440.2 [5383.2, 5499.9] | 6298.8 [6066.4, 6423.2] | 5617.8 [5564.6, 5664.8] | 7657.6 [7556.3, 7750.1] | 673.7 / 775 | 1.00 | 310 x 500, 5 x 409 |

The capacity of one process went from about 15 to about 19 turns/s; 100 streams went from saturated to just below.

## 4. Ramp toward 1,000 streams: the knee (re-run, `reports/perf/c2b/fix-ramp-50-1000.json`)

2-minute windows (labeled shortened, spec-10 C2), 30 s settle each, one fresh api process, all fixes. Errors are
non-200 turn responses (500 `internal_error`, each after >= 1 s: pool timeouts, the api logged Postgres "temporarily
unavailable" and Redis timeouts; 409 `session_busy`); n counts timed turns; valid = spec-10 §5 (section 2).

| streams | n | turns/s | TTFT overhead p50 | p95 | p99 | e2e overhead p50 | p95 | api lag p99 / max | api CPU | RSS MiB | load mean / max | errors | valid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 855 | 7.12 | 14.5 [14.3, 14.8] | 30.9 [28.3, 35.9] | 54.7 [48.3, 58.5] | 21.9 [21.5, 22.2] | 36.7 [33.9, 40.1] | 12.9 / 69 | 0.49 | 238 | 6.0 / 7.6 | 0 | yes |
| 75 | 1297 | 10.81 | 23.1 [22.4, 24.1] | 87.7 [81.0, 94.4] | 124.9 [120.2, 140.6] | 39.7 [38.1, 42.5] | 147.3 [136.6, 156.8] | 21.4 / 140 | 0.71 | 314 | 6.4 / 7.4 | 0 | no (2 holds) |
| 100 | 1714 | 14.28 | 45.9 [43.2, 48.9] | 122.1 [118.1, 129.8] | 168.0 [160.7, 175.5] | 96.0 [92.7, 99.1] | 231.8 [213.0, 263.2] | 31.3 / 132 | 0.92 | 368 | 11.5 / 14.8 | 0 | no (2 holds) |
| 125 | 2080 | 17.33 | 90.3 [84.4, 98.0] | 842.2 [784.0, 896.2] | 1027.5 [1006.2, 1093.0] | 273.8 [263.4, 287.1] | 641.4 [625.0, 656.5] | **71.6** / 180 | 0.94 | 419 | 6.4 / 8.0 | 0 | no |
| 150 | 2297 | **19.14** | 507.4 [473.9, 517.4] | 1035.0 [1008.9, 1055.6] | 1244.2 [1216.0, 1297.1] | 934.6 [923.4, 948.7] | 1390.7 [1362.7, 1430.0] | **150.3** / 250 | 1.00 | 451 | 6.4 / 8.9 | 0 | no |
| 200 | 1894 | 15.78 | 3799.3 [3762.7, 3839.1] | 5375.7 [5302.0, 5478.1] | 6120.7 [5960.0, 6298.5] | 5174.5 [5112.7, 5226.2] | 6955.9 [6861.1, 7126.3] | **279.4** / 331 | 1.00 | 478 | 6.9 / 8.7 | 201 x 500 | no |
| 300 | 1667 | 13.89 | 4316.6 | 6072.8 | 6631.4 | 5670.3 | 7932.4 | 234.5 / 302 | 1.00 | 480 | 4.5 / 5.2 | 2451 x 500, 45 x 409 | no |
| 400 | 1573 | 13.11 | 4886.1 | 6903.7 | 7229.1 | 6077.5 | 8772.4 | 228.0 / 346 | 1.00 | 480 | 6.1 / 9.2 | 4044 x 500, 39 x 409 | no |
| 600 | 1392 | 11.60 | 4786.1 | 7041.1 | 7362.1 | 5649.1 | 8968.9 | 231.0 / 327 | 1.00 | 482 | 6.3 / 8.3 | 6919 x 500, 35 x 409 | no |
| 800 | 1183 | 9.86 | 4671.9 | 7036.7 | 7407.8 | 5532.4 | 8400.1 | 224.3 / 395 | 1.00 | 481 | 5.4 / 7.2 | 9205 x 500, 51 x 409 | no |
| 1000 | 1124 | 9.37 | 4877.1 | 7264.3 | 7594.0 | 5694.7 | 8917.5 | 263.0 / 414 | 1.00 | 488 | 4.4 / 5.7 | 9337 x 500, 27 x 409 | no |

Every driver process lag p99 <= 0.95 ms and every mock process <= 1.53 ms in every window (the first version's ramp
had three windows over 2 ms from its 2 mocks). 0 contract violations. "2 holds": 2 turns with S1 after the first
text (section 2).

- **Knee**: the event-loop lag target (p99 < 50 ms) holds up to 100 streams and breaks between 100 and 125; the
  throughput tops out at ~19.1 turns/s at 150 streams (the loop at 1.00 of a core), as in the first version's ramp
  (19.3). The overhead targets (p50 <= 10, p95 <= 25 ms) are met at no load level: even at 2 streams the p50 is
  11.3 ms (tracer on; section 6). From the image the knee is lower (section 3).
- **Overload has no shedding**: past the knee the goodput falls from 19.1 to 9.4 turns/s at 1,000, while 500s grow
  to 9,337 per window: Postgres and Redis pool exhaustion (DB acquire timeouts, Redis timeouts in the admission
  store, the turn-owner refresh and the outbox), breakers opening on loop-lag timeouts, `holdback_deadline` commits.
  The admission concurrency limit is per project (10,000 in the load config, 1,000 by default), not per pod, so
  nothing bounds the work one process accepts.
- **Metastable past the knee**: the re-runs fall into that degraded state already at 200 streams (ramp: 15.8
  turns/s, 201 x 500; F1: after 3 minutes), where the first version's ramp and F1 ran 19.2-19.4 turns/s with 0-14
  errors. Not the Postgres/Valkey pinning: two 300 s windows at 200 streams with the stack unpinned and pinned gave
  15.33 and 15.33 turns/s, 539 and 430 x 500 (`fix-200-stack-{unpinned,pinned}.json`). The first version recorded no
  code digest, so whether code that landed in between moved this cannot be told; from now on `provenance` can.

## 5. Hotspots found and fixed (each measured, small, tested)

Progression at 50 streams (the first version's diagnostic runs: single 90-120 s windows, its placement and
holdback measurement; the fix3 run had the CPU profiler on, about +1-2% CPU):

| run (`/tmp/noema-perf/runs/`) | change | TTFT overhead p50 / p95 / p99 | CPU ms/turn | api lag p99 / max | full GC p50 |
|---|---|---|---|---|---|
| baseline-1 | none | 42.0 / 242.8 / 277.4 | 83.9 | 15.7 / 251 | 192 ms |
| fix1-50 | 1 + 3 | 29.2 / 81.9 / 103.3 | 75.1 | 13.0 / 99 | 47 ms |
| fix2-50 | + 2 | 29.9 / 57.3 / 69.7 | 74.5 | 13.2 / 58 | 27 ms |
| fix3-50 | + 4 | 29.7 / 57.3 / 71.9 | 67.8 | 13.7 / 89 | 27 ms |
| fix4-50 | + 5 | 13.7 / 29.1 / 50.7 | 64.6 | 12.4 / 54 | 31 ms |
| final-F1 (300 s) | + packed offsets as bytes, smaller mock ring | 13.7 / 30.3 / 55.3 | 67.1 | 12.8 / 75 | 32 ms |

1. **Full GC pauses** (`api.gc` log: 110-125 ms every ~10 s at 20 streams, 192 ms every ~2.7 s at 50; each one a
   loop stall that every in-flight turn pays: this was the p95/p99 of the baseline). The tracked heap held every
   startup object (modules, botocore service models, schema validators). Fix: `composition.start` ends with
   `gc.collect(); gc.freeze()` outside test processes (`_freeze_startup_heap`, `src/noema/composition.py`; test
   `tests/unit/composition/test_heap.py`). Full GC at 50 streams 192 -> 47 ms.
2. **Sealed turn logs as objects.** A GC heap census (`serve.py --census-out`) at 50 streams: 116,516 `SseFrame`
   objects, 58% of the tracked heap, kept by sealed logs for the 300 s resume window. Fix: at `seal` the frames are
   packed into one bytes blob plus offsets (`_SealedFrames`, `src/noema/streaming/event_log.py`; one tracked object
   per sealed turn; reads rebuild equal frames; test `tests/unit/streaming/test_sealed_frames.py`). Tracked heap
   201k -> 91k objects, full GC 47 -> 27-31 ms.
3. **`clean_text` per character** (`unicodedata.category` per char; called by `esc` on the history and on every
   decision member's input: ~10% of the turn CPU). Fix: one regex pass over the Cc ranges (identical by definition;
   `src/noema/core/text.py`; `tests/unit/core/test_text_clean.py` checks every code point). 30x faster.
4. **Redis event mirror: one pipeline per frame** (XADD + EXPIRE each; ~18% of the loop CPU at 50 streams in the
   CPU-time profile). Fix: the writer gathers `batch_s` = 5 ms after a wake (one pipeline for every running turn's
   frames) and sets the orphan TTL once per turn (`src/noema/streaming/mirror.py`; tests
   `tests/unit/streaming/test_mirror_batching.py`). Mirror share 18% -> 8%; remote readers see frames up to 5 ms
   later.
5. **The generation request waited for the decision fan-out.** The createTurn tracer (`serve.py --trace-out`) at
   2 streams: decision entry done at 8.8 ms, then the 15 member calls dispatched one after another (about 1.1 ms of
   CPU each: render, botocore build, SigV4), and the Sol stream request only at 26.8 ms, because the attempt needs
   about 4 task hops (profile run, attempt, stream open) and every member task was queued ahead of it. Fix: a member
   call unit yields `DISPATCH_YIELDS` = 6 loop passes before its first step (`src/noema/decision/coordinator.py`;
   ordering only, no clock time; test `tests/unit/decision/test_dispatch_order.py` fails without it). The stream
   request now leaves 2.3 ms after the decision entry: TTFT overhead at 2 streams 24 -> 11 ms, at 50 streams
   p50 29.7 -> 13.7 ms.

   **Its effect on the decision (perf review, A/B runs `reports/perf/c2b/fix-yields{0,6}-50-100.json`**, back to
   back, 120 s windows, `serve.py --dispatch-yields`; all four windows valid). `decision_ms` = S1 - t_d0 and
   `decision_lead_ms` = first text - S1, from each turn's `timings`:

   | streams | DISPATCH_YIELDS | n | decision_ms p50 | p95 [CI] | p99 | lead p50 / p5 / p1 / min | TTFT overhead p50 / p95 |
   |---|---|---|---|---|---|---|---|
   | 50 | 0 | 857 | 74 | 102 [98, 105] | 122 | 245 / 231 / 219 / 192 | 28.3 / 56.8 |
   | 50 | 6 | 855 | 75 | 106 [98, 110] | 123 | 230 / 204 / 182 / 152 | 13.6 / 30.2 |
   | 100 | 0 | 1716 | 99 [98, 100] | 186 [180, 189] | 229 | 231 / 167 / 130 / 73 | 54.4 / 133.4 |
   | 100 | 6 | 1712 | 110 [108, 112] | 213 [206, 217] | 245 | 204 / 113 / 74 / 1 | 49.1 / 119.6 |

   At 50 streams the yields cost the decision ~1 ms (p50; p95 within the CIs) and save 15 ms of TTFT overhead. At
   100 streams each loop pass is longer, so the 6 passes cost S1 +11 ms (p50) / +27 ms (p95) while TTFT gains 5 /
   14 ms; with the first text earlier and S1 later the lead shrinks (p1 130 -> 74 ms, one turn at 1 ms). In this
   scenario S1 still preceded the first text in every turn (holdback 0), but the margin is what the yields spend:
   on routes whose first text comes sooner, or nearer the knee, they move S1 past the first text (a hold) and add
   their delay to every restart (L3: T_d + TTFT(r*)). Kept (an L1 win at the loads where L1 can hold); a load-aware
   yield (e.g. yield only while the attempt has not reached the network) is a decision-owner follow-up.
6. **`timings.holdback_ms` measured the event hop, not the commit** (perf review; orchestrator, residual R3). See
   section 10, finding 1: `finish.timings` now ends the hold at the engine's commit (the runner's commit request or
   the committed attempt's first text, whichever is later), not when the runner dequeued `generation.committed`.
   No runtime effect; it made the runs valid by spec-10 §5 (19.5% -> 0% non-zero at 50 streams).

Tried and not kept: raising the GC thresholds. At 70 streams, `(2000, 10, 100)` gave 3 full collections of
212-276 ms in 150 s instead of 85 of 35 ms (p95 44 -> 35 ms but p99 70 -> 106 ms, lag max 89 -> 306 ms);
`(20000, 10, 10)` made each gen0 collection 12.6 ms. The defaults stay.

## 6. Remaining hotspots (measured, not fixed: not small, or in third-party code)

From the first version's diagnostic runs (the re-runs did not profile; their CPU per turn at 50 streams, 68 ms,
matches). CPU-time profile of the loop thread at 50 streams after the fixes (`--profile cpu`: SIGPROF every 1 ms of
CPU, 4,204 samples; inclusive shares; run `prof-final-50`, 64-67 ms of CPU per turn):

| Share | Where | Per turn |
|---|---|---|
| 30.9% | decision member calls (`MemberCallRunner._attempt`): Converse via aiobotocore 17.3% (10 calls: serialize, validate, hooks, SigV4, aiohttp, parse), Mantle via httpx 9.4% (5 calls), the members' own parsing and bookkeeping ~4% | ~20 ms, ~1.1-1.3 ms per call |
| 12.0% | the generation attempt; botocore eventstream decoding of the 100 events 8.0% | ~8 ms, ~60 us per event |
| 8.6% | the Redis event mirror (after the batching fix): one XADD per frame | ~5.5 ms |
| 7.5% | `TurnRunner._main` (prompt, commit rule, finish) | ~5 ms |
| 6.5% | the SSE writer: one ASGI `send` per frame through 4 middleware wrappers (ingress tracker, starlette error/exception middlewares) | ~4 ms |
| 4.4% | ingress middleware (auth, gate, context) | ~3 ms |
| 4.0% | asyncio task bookkeeping (`WeakSet._remove`: ~80 tasks per turn) | ~2.5 ms |
| 2.8% | OTel spans (about 40 per turn; every span re-redacts the static attributes) | ~2 ms |
| 2-3% | GC (charged to `GcLog.callback`, the harness hook that runs at each collection's end) | |

Serial latency (the overhead floor, createTurn tracer at 2 streams): ~5 ms of ingress before the decision entry
(authn; 3 units of work = 15 Postgres round trips of 0.1-0.3 ms: BEGIN, `set_config`, the query, COMMIT and the
pool's reset on release each; admission EVALSHA; turn start), 2.0-2.6 ms of decision entry (an L2 GET that waits
behind other ready tasks), then ~1.8 ms to build and sign the Sol request: the generation request leaves the api
~9.4 ms after the client sent it, plus ~1 ms of egress. Every round trip also waits behind whatever the loop is
running, so this floor grows with load.

GC: the tracked heap grows with the live turns (~1,450 objects each: tasks, coroutines, cancel scopes, events,
decision state); a full collection scans all of it, and full collections come faster as the allocation rate grows:

| streams (F1) | tracked heap | full GC every | full GC p50 / p99 | GC share of the loop's wall time |
|---|---|---|---|---|
| 50 | 88k | 2.5 s | 31.6 / 42.2 ms | 3.2% |
| 100 | 178k | 1.4 s | 71.7 / 121.2 ms | 10.5% |
| 200 | 290k | 1.4 s | 175.1 / 256.6 ms | 21.5% |

The re-run F1 (no census) agrees: full GC every 2.5 / 1.4 / 1.2 s, p50 26.5 / 62.7 / 160.7 ms, GC share 2.9 / 9.9 /
21.7% at 50 / 100 / 200 streams.

## 7. Remaining risk and options

1. **One process cannot serve 200 streams of this workload** (router-default fan-out of 15 calls + a 400-token
   stream per 7 s turn). Capacity ~19 turns/s (~150 streams); the lag target holds up to ~100 streams; the
   overhead targets hold at no load (floor p50 ~11 ms; at 50 streams p50 13.6-14.5 / p95 29-31 ms; from the image
   17.6 / 42.0 ms). The 08 §9 sizing (4 pods x 2 vCPU, ~200 streams each) needs, per 2-vCPU pod, either 2 api
   processes (uvicorn `--workers 2` or two containers; each also halves its own GC heap) or about 3x less CPU per
   turn.
   Recommendation: plan 2 processes per 2-vCPU pod and re-measure at 100 streams per process (from the image:
   R5 puts its knee below 100); scale out on the lag signal (08 §9's own trigger; R2 is closed).
2. **CPU per turn** (64-68 ms in the venv, ~83 ms from the image): the biggest lever is the decision fan-out transport
   (~20 ms per turn): a lean SigV4 + aiohttp client for Converse instead of aiobotocore for decision calls, or
   botocore `parameter_validation=False` (~2.5% of the turn CPU; not done: it would let a malformed request through to
   Bedrock instead of failing in the SDK, the probes' audio case). Then the per-frame path (eventstream decoding,
   mirror XADD, SSE send through 4 wrappers: ~17 ms per 100 frames together).
3. **GC** grows with concurrency (section 6). Fewer objects per turn, or Python 3.13+ (3.14's incremental GC), are
   the structural fixes; threshold tuning only trades frequency for pause length.
4. **Overload collapse**: no per-pod bound on accepted turns, so past the knee goodput halves and the pools and
   breakers fail (section 4); at 200 streams the process is metastable (the re-run's F1 window fell from 19.4 to
   ~15 turns/s after 3 minutes and stayed there, ~130 x 500 a minute; section 3). A per-process concurrency cap
   (uvicorn `--limit-concurrency`, or a per-pod admission slot set) would shed load with 429 / 503 + Retry-After
   before the pools time out.
5. **Serial ingress path**: ~15 Postgres round trips before the generation request (three units of work with
   BEGIN/`set_config`/COMMIT each). Folding them (one unit of work, `set_config` in the first statement) would take
   ~2-3 ms off the overhead floor at low load and more under load (db and orchestrator owners).
6. **Measurement caveats**: one host shared with other agents, whose processes are not kept off our cores
   (loadavg 3-16 during the re-runs, every run started below 8); moto is not pinned (no turn calls it); the api's
   cores 46-47 are one hardware thread each of two physical cores (an EC2 2-vCPU pod is the two threads of one
   core; the api is one loop thread, so this favors our numbers a little); the venv interpreter is ~20-26% faster
   than the image's (R5); closed-loop drivers (section 2); 2-minute ramp windows; sessions of 10 turns.

## 8. Residuals for other owners (found here, not fixed: outside the perf scope)

- **R1, integrator/platform (SIGTERM drain never starts)** (closed in C2b by the integrator: `_chain_sigterm` now
  starts `begin_drain` inside a task; the re-runs' api logs have no `NoEventLoopError`). Was: `_chain_sigterm`
  scheduled `loop.call_soon_threadsafe(services.runtime.begin_drain)`; `begin_drain` calls the task group's
  `start_soon` from a plain loop callback, and anyio 4.15 raises `NoEventLoopError` there (no current task), so the
  drain hooks do not start on SIGTERM ("Exception in callback TaskGroupRuntime.begin_drain ... NoEventLoopError" and
  "coroutine 'TaskGroupRuntime._start_hook.<locals>.run_hook' was never awaited" in every api log of these runs;
  reproduced in isolation with `tg.start_soon` inside `loop.call_soon_threadsafe`). Fix idea: run `begin_drain` in a
  task (`loop.call_soon_threadsafe(lambda: loop.create_task(<async wrapper>))`) or have the runtime keep a token for
  `anyio.from_thread`.
- **R2, telemetry/integrator** (closed in C2b by the integrator: `composition` samples it every 0.5 s in every
  process; the harness keeps its own 10 ms sampler for the validity checks). Was: `aghoo_event_loop_lag_seconds`
  was registered but never set by any code, so neither spec-10 §5's lag source nor 08 §9's scaling trigger existed;
  the harness measures the lag itself (`tests/load/lag.py`).
- **R3, orchestrator** (closed by the perf fixer, section 10 finding 1; this first diagnosis was wrong):
  `timings.holdback_ms` was nonzero on 20-25% of the turns at 50 streams with S1 long decided. Not a rounding of a
  sub-ms gap: the engine commits an armed run at its first text, but the hold was ended at `commit_wall`, the moment
  the runner's consumer dequeued `generation.committed` (0.5-2 ms later on a busy loop, 10-13 ms behind a GC). The
  hold now ends at the engine's commit; spec-10 §5's rule stays as written and the re-runs pass it.
- **R4, ops**: no Postgres/Redis pool or latency metrics are exported (spec-10 C2 asks for pool saturation and
  Redis/Postgres latency); under overload both pools ran out (`MaxConnectionsError`, acquire timeouts).
- **R5, infra (the image's interpreter is ~20-26% slower than the dev venv's)**: `deploy/docker/api.Dockerfile`
  uses the Docker Official `python:3.12-slim-bookworm` CPython 3.12.15; the dev venv uses uv's managed CPython
  3.12.12 (python-build-standalone). Same core: a pure-Python micro-benchmark 0.178 vs 0.141 s; the api 83 vs 68 ms
  of CPU per turn at 50 streams; the knee below 100 instead of ~120 streams (section 3). Production capacity per
  process is the image's. Option to measure: the same uv-managed CPython in the image (`uv python install` in the
  build stage, copied with the venv) or a static-libpython build; re-run `--api-image` (one command, section 9).

## 9. Reproduce

See `tests/load/README.md`. The re-runs (perf review; `--out /tmp/noema-perf/runs/<name>`, `--results
reports/perf/c2b/<name>.json`, default placement): `fix-F1-50-100-200` (`--plan 50:300,100:300,200:900`),
`fix-ramp-50-1000` (the ramp plan below), `fix-image-50-100` (`--plan 50:300,100:300 --api-image noema-api:perf`
after `make image-api IMAGE_TAG=perf`), `fix-yields0-50-100` / `fix-yields6-50-100` (`--plan 50:120,100:120
--dispatch-yields 0|6`), `fix-200-stack-{unpinned,pinned}` (`--plan 200:300 --stack-cpus 0-47|40-45`). The
first version's runs (2 mocks on 40-43, api 44-45, drivers 28-39, stack unpinned):
`final-F1` (`--plan 50:300,100:300,200:900 --census`),
`final-F3-ramp` (= ramp B,
`--plan 50:120,75:120,100:120,125:120,150:120,200:120,300:120,400:120,600:120,800:120,1000:120`),
`baseline-1` (`--plan 50:120,100:120,200:180`, before the fixes), `fix*-50` (`--plan 50:90|120 --warmup-s 30`),
`prof-final-50` (`--profile cpu`), `trace-2*` (`--plan 2:40 --trace`), `gc-*` (`--plan 70:120 --gc-threshold`).
An earlier ramp (`ramp-A-relabeled.json`) under-started streams through a runner bug (fixed: actual counts 50, 75,
75, 100, 100, 150, 250, 350, 550, 750, 950; relabeled in that file); its numbers agree with ramp B.

## 10. Perf review (C2b/D) and what changed

1. **The runs were invalid by spec-10 §5 (holdback_ms = 0 on 80.5%, not 99.9%)**: valid finding; R3's "rounding"
   explanation was wrong. A non-zero value was real delay, but of the event, not of the commit: the generation
   engine commits an armed run synchronously when its first text arrives (`generation/run.py` `_on_text`, T7) and
   queues `generation.committed`; `finish.timings` ended the hold at `runner.commit_wall`, set when the runner's
   consumer task dequeued that event (0.5-2 ms later on a loaded loop, 10-13 ms behind a full GC). spec-04 §10
   defines holdback as the committed attempt's first text -> its commit, "0 when committed at its first text (A2 or
   armed)". The hop is already in the TTFT overhead (the first delta follows it in the same queue). Fix
   (orchestrator, minimal): `runner.commit_asked` is stamped where A1 is asked to commit (`_apply_s1`, `_hold_timer`)
   and the hold is max(0, asked - first text) for A1, 0 for runs that auto-commit at their first text; regression
   test `tests/unit/turns/test_timing.py::test_holdback_ends_at_the_engine_commit_not_when_the_runner_sees_it` (the
   committed event delivered 3 ms late: armed 0, buffered 210 ms; it failed with 3 before the fix). No owner ruling
   was needed: the rule stays as written. The re-runs' F1 windows pass it (100% at 50, 99.953% at 100 streams), as do
   the A/B and image-50 windows; the ramp's short 75/100-stream windows miss by one turn each. Every remaining
   non-zero value is a real hold (S1 after the first text, 4-79 ms), which is what the rule is there to catch.
2. **No code version or config digest**: fixed; `results.json` `provenance` (section 2).
3. **The api was the host venv, not the image**: fixed; `run.py --api-image` and the confirmation run of section
   3. It changed the conclusion: the image needs ~22% more CPU per turn (its interpreter build), so the venv numbers
   are optimistic for production by about that much (R5).
4. **Postgres and Valkey unpinned, two mock processes**: fixed; Postgres and Valkey pinned to 40-45 (at 50 streams
   they use 6% and 2% of one core), four mock processes behind the two endpoints (SO_REUSEPORT,
   `MockBedrock.start(reuse_port=True)`), every mock process judged on its own lag (worst p99 <= 1.73 ms in every
   window). The pinning does not change the results (section 4: 200 streams unpinned vs pinned, same throughput).
5. **Closed-loop drivers**: valid as a caveat, no harness change. spec-10 §5 defines the scenario by concurrency
   (N streams looping turns) and the L1/C2 gates are stated per stream count, so the driver stays closed-loop; the
   report (section 2), `tests/load/README.md` and `run.py` now say that the latencies past the knee are queueing at
   that concurrency and the turns/s column is the capacity. An open-loop (arrival-rate) driver would be a new
   Wave E tool for overload tests, not a correction of these numbers.
6. **DISPATCH_YIELDS' effect on S1 not reported**: measured, section 5 fix 5 (A/B): +1 ms `decision_ms` p50 at 50
   streams, +11 / +27 ms (p50 / p95) at 100, and the decision lead's low tail shrinks (p1 130 -> 74 ms).
