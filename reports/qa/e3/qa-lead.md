# QA lead (Wave E3): final re-gate, the L1 knee, and the full regression

Role: QA lead, final re-gate after the Wave E3 performance sprint, 2026-10-12 (11:30 UTC onwards). No live Bedrock
call by this role (the wire mock only; the live results are the live-smoke role's, [live-smoke](live-smoke.md)), no
AWS resource (moto only), no git write command. Edits: `tests/**` and `reports/**` only (section 2). Rulings R6-R9
applied as binding. Gate table: [wave-e-summary.md](../wave-e-summary.md), updated in place with a "Wave E3 delta"
section. Sizing: [reports/perf/sizing.md](../../perf/sizing.md). Results files: `reports/qa/e3/qa-lead/` (load
`load/<run>.json`, fault, gates, security); raw logs and load data in `/tmp/noema-qae3/` on the build host.

## 1. Setup

- **Code under test**: HEAD `1a28933` (tag wave-e2) plus the whole E3 working tree, as the four E3 roles left it
  (perf-gc-objects, perf-cpu-shed, misc-fixes, live). Last product edit 08:36 UTC (`src/noema/composition.py`);
  nothing under `src deploy tools` changed during this session. Digest of `src deploy tools` (sha256 over the sorted
  per-file sha256, no `__pycache__`, the E2 method; `/tmp/noema-qae3/bin/digest.sh`): **`5e4146f4ff4cf8d7`** at the
  start (11:29) and at the start and end of every load run (`start` / `end` lines of `logs/<run>.out`). The load
  results' own `provenance.code.worktree_digest` (src, tests, tools, deploy, lock files) is the same in every run.
- **Images** `noema-{api,worker,media,media-test}:qae3`, built at 11:30 from that tree (`make e2e-images
  image-media-test IMAGE_TAG=qae3`; api `sha256:0ca275e2cc9a`). Checked: `api/shed.py`, `adapters/_sigv4.py`,
  `adapters/_eventstream.py`, `adapters/resilience/retry_budget.py`, `composition.py`, `core/clock.py`,
  `streaming/sse_writer.py`, `turns/runner.py`, `decision/run.py`, `db/engine.py` in the image have the tree's
  sha256. Interpreter: venv CPython 3.12.12, image CPython 3.12.15.
- **Deployment as configured**: 2 api processes per 2-vCPU pod (`WEB_CONCURRENCY=2`, IMPL-087), per-process cap
  `NOEMA_MAX_TURNS_PER_PROCESS` 120, the composition's GC setup (`gc.freeze()` after startup, thresholds
  **(700, 10, 100)**, perf-gc-objects), the edge shed (perf-cpu-shed; off only with the test-mode response
  validator), the bounded Redis pool (256) and the R4 datastore metrics (misc-fixes).
- **Own stacks**, each torn down at the end (section 8): `noema-qae3-perf` (postgres 34032, moto 34050, valkey
  34079; api 34090, mocks 34081/34082), `noema-qae3-e2e` (port base 34200), `-fault` (34300), `-contract` (34400),
  `-sec` (34500, UI 34590/34595), `-gates` (34600).
- **Load placement** (the E2 QA lead's and both perf agents', so the numbers compare): one 2-vCPU pod = **2 api
  processes on CPUs 46-47** (physical cores 22-23; their sibling CPUs 22-23 idle), Postgres + Valkey on 44-45, 8 mock
  processes (Converse 16-19, Mantle 40-43; C1: 1 Converse process on 16-17), drivers on 4-15 and 28-39. The api runs
  from the venv unless marked "image" (`--api-image noema-api:qae3 --api-procs 2`: the image's interpreter and venv,
  the compose app settings, `docker run --cpuset-cpus 46-47`).
- **Quiet host, serialized**: runner `/tmp/noema-qae3/run.sh` starts a run only when no other `tests.load.run` is
  alive and the 1-minute loadavg is < 6 (< 8 after 20 min of waiting); a monitor records the loadavg and any
  foreign load process every 10 s (`runs/<run>.monitor`: 0 foreign processes in every sample of every venv run; the
  image runs count only their own container's api processes, section 3.1). None of this role's suites ran during a
  load run (queue `/tmp/noema-qae3/bin/queue2.sh`; idle regression stacks existed only during part of L4, section 7). Window loadavg means are in each
  table (`resources.loadavg_1m`); every gated window had a 1-minute loadavg max < 8.
- **Load config** v2 = the bootstrap config with lifted tenant limits (`tests.load.stack activate`; digest
  `sha256:b475dc16...`, the E2 QA lead's and both perf agents'); v3 for C5 (t_beta `concurrent_streams_per_project`
  40; `sha256:916f53ef...`, the perf-cpu-shed agent's C5 digest); v5 for C1 (t_acme `model_quotas.sol-6.1` rpm 600);
  v4 / v6 = v2 again.
- **Quantities** (spec-10 §5): TTFT overhead = client TTFT - mock TTFT per turn; percentiles with bootstrap 95% CIs
  (1,000 resamples), written `p [low, high]`; a window is valid when >= 99.9% of its turns have `holdback_ms` 0 with
  `spec_agrees` and every driver and mock process has lag p99 <= 2 ms. CPU per turn = pod CPU (both processes) /
  turns per second. Full-GC pauses from each process's `gc.callbacks` log (`/tmp/noema-qae3/bin/summ.py`).

## 2. Harness changes of this role

None. Every load run used `tests/load/**` as the sprint left it, every suite its make target as committed plus the E3
roles' test edits (misc-fixes section 8, perf-gc-objects, perf-cpu-shed, live). Helper scripts outside the repo
(`/tmp/noema-qae3/bin/`): `digest.sh`, `run.sh`, `queue2.sh`, `activate.sh` (config versions), `l1table.py`,
`summ.py` (GC, from perf-gc-objects), `ratio.py`, `c1.py`, `c3.py` (E2 QA lead's), `c5table.py` (perf-cpu-shed's),
`dscompact.py` (the R4 datastore metrics per step from the step-end scrapes).

## 3. Load gates (`run.sh` = section 1; results `reports/qa/e3/qa-lead/load/<run>.json`)

Every load run below measured tree digest `5e4146f4ff4cf8d7`; 0 contract violations in every run (the drivers
validate the first turn of every stream and every non-200); every window valid unless marked.

### 3.1 L1 proxy overhead and the knee (06 §4; owner decision 2026-10-12: size from the knee)

Runs: venv `e3-l1a` (`--plan 50:1450,75:980,100:740`), `e3-l1b` (`125:600,150:500,200:450`), replicate `e3-l1c`
(`125:600,150:500`); image `e3-l1-img` (`100:740,125:600,150:500`), `e3-l1-img2` (`50:1450,75:980`); all
`--warmup-s 60 --settle-s 30`, holds sized for n >= 10,000 first deltas per window. The knee = the highest density
whose window passes both bounds on the upper end of the 95% CI: **p50 <= 10 ms and p95 <= 25 ms**. Every window
valid: 100% of turns `holdback_ms` 0 with `spec_agrees`, worst driver lag p99 0.94 ms, worst mock 1.17 ms, 15
decision calls in every turn.

**Venv (CPython 3.12.12, python-build-standalone: PGO + LTO + BOLT, static libpython) = the history's interpreter:**

| streams/pod | run | n | turns/s | TTFT ovh p50 [95% CI] | p95 [95% CI] | p99 | e2e ovh p50 / p95 | ingress p50 / p95 | api lag p99 / max | pod CPU | CPU ms/turn | full GC p50 / max (pod) | RSS MiB | load mean (max) | both bounds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | l1a | 10,477 | 7.23 | 8.48 [8.46, 8.50] | 10.79 [10.67, 10.96] | 14.4 | 14.7 / 18.4 | 7.9 / 9.7 | 1.9 / 21 | 0.328 | 45.4 | 15.4 / 19.2 every 69-104 s | 458 | 2.4 (5.0) | PASS |
| 75 | l1a | 10,617 | 10.83 | 8.75 [8.72, 8.77] | 11.40 [11.28, 11.52] | 15.2 | 15.3 / 19.5 | 8.1 / 10.2 | 2.9 / 137 | 0.468 | 43.2 | 22.1 / 26.2 every 52-61 s | 519 | 3.1 (4.6) | PASS |
| 100 | l1a | 10,697 | 14.46 | 9.16 [9.14, 9.19] | 12.63 [12.49, 12.80] | 18.3 | 15.8 / 21.3 | 8.5 / 10.9 | 3.6 / 36 | 0.607 | 42.0 | 27.7 / 32.2 every 37-46 s | 577 | 3.3 (4.9) | PASS |
| **125** | l1b | 10,830 | 18.05 | **9.60 [9.57, 9.63]** | **13.89 [13.71, 14.10]** | 22.2 | 16.2 / 22.3 | 8.7 / 11.4 | 3.9 / 65 | 0.742 | 41.1 | 36.8 / 55.2 every 30-38 s | 640 | 4.0 (5.9) | **PASS** |
| **125** | l1c | 10,827 | 18.05 | **9.65 [9.62, 9.68]** | **13.86 [13.70, 14.03]** | 21.3 | 16.4 / 22.0 | 8.7 / 11.3 | 4.0 / 44 | 0.744 | 41.2 | 33.5 / 44.3 | 640 | 3.3 (4.8) | **PASS** |
| 150 | l1b | 10,811 | 21.62 | 10.03 [9.99, **10.07**] | 15.06 [14.87, 15.28] | 23.4 | 17.2 / 25.5 | 9.1 / 12.7 | 4.4 / 65 | 0.883 | 40.8 | 41.5 / 58.4 every 26-29 s | 699 | 3.3 (4.4) | FAIL (p50) |
| 150 | l1c | 10,811 | 21.62 | 10.16 [10.12, **10.20**] | 15.14 [14.97, 15.36] | 24.6 | 17.3 / 24.8 | 9.2 / 12.6 | 4.4 / 59 | 0.887 | 41.0 | 42.4 / 57.9 | 699 | 3.9 (4.8) | FAIL (p50) |
| 200 | l1b | 12,993 | 28.87 | 11.70 [11.64, 11.76] | **20.13 [19.77, 20.54]** | 42.4 | 20.3 / 36.6 | 10.1 / 16.6 | 5.2 / 92 | 1.160 | 40.2 | 64.0 / 82.1 every 20-23 s | 808 | 4.6 (5.6) | FAIL (p50) |

**Image (`noema-api:qae3`, CPython 3.12.15 of the official Python image: PGO + LTO, shared libpython, GCC) = what the
deployment runs:**

| streams/pod | run | n | turns/s | TTFT ovh p50 [95% CI] | p95 [95% CI] | p99 | e2e ovh p50 / p95 | ingress p50 / p95 | api lag p99 / max | pod CPU | CPU ms/turn | full GC p50 / max (pod) | RSS MiB | load mean (max) | both bounds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | img2 | 10,474 | 7.22 | 9.17 [9.15, 9.20] | 11.81 [11.64, 11.94] | 15.6 | 15.9 / 19.8 | 8.5 / 10.4 | 2.8 / 20 | 0.366 | 50.7 | 14.1 / 19.1 | 446 | 2.7 (4.0) | PASS |
| **75** | img2 | 10,616 | 10.83 | **9.65 [9.63, 9.67]** | **12.38 [12.26, 12.50]** | 16.2 | 16.7 / 21.4 | 9.0 / 11.0 | 3.7 / 27 | 0.521 | 48.1 | 20.1 / 22.6 | 506 | 2.7 (4.4) | **PASS** |
| 100 | img | 10,695 | 14.45 | 10.35 [10.32, **10.38**] | 14.50 [14.35, 14.67] | 18.1 | 17.5 / 22.9 | 9.5 / 11.8 | 4.3 / 41 | 0.681 | 47.1 | 28.6 / 34.6 | 568 | 3.5 (4.9) | FAIL (p50) |
| 125 | img | 10,854 | 18.09 | 10.81 [10.79, 10.84] | 15.60 [15.37, 15.80] | 23.3 | 18.3 / 25.8 | 9.9 / 12.6 | 4.7 / 59 | 0.842 | 46.5 | 34.9 / 46.2 | 629 | 3.1 (3.9) | FAIL (p50) |
| 150 | img | 10,837 | 21.67 | 11.62 [11.58, 11.67] | 18.12 [17.87, 18.42] | 29.1 | 19.6 / 30.5 | 10.5 / 15.0 | 5.1 / 69 | 1.002 | 46.2 | 43.6 / 54.5 | 684 | 4.5 (6.8) | FAIL (p50) |

(The image runs' monitors count 2 "foreign" load processes in every sample: the api container's own two
`tests.load.serve` processes, whose command line does not carry the run directory; no other load process ran.)

**The knee.**
- **Venv: 125 streams per pod** (p50 9.60 / 9.65, upper CIs 9.63 / 9.68; p95 13.9, upper CI 14.1), reproduced in two
  runs; 150 per pod misses the p50 bound in both runs by 0.07 and 0.20 ms (upper CIs 10.07 and 10.20).
- **Image: 75 streams per pod** (p50 9.65 [9.63, 9.67], p95 12.38 [12.26, 12.50]); 100 per pod misses p50 by 0.38 ms
  (upper CI). The image spends **+11-13% CPU per turn** (46-51 vs 40-45 ms) and adds 0.7-1.5 ms to the p50 at every
  density: its interpreter is the official image's CPython (shared libpython, GCC), the venv's is
  python-build-standalone (static libpython for the interpreter, clang PGO + ThinLTO + BOLT). The GC pauses are
  about the same on both (GC columns above). Production runs the image, so **the sizing uses the image knee, 75 streams
  per pod** ([sizing.md](../../perf/sizing.md)).
- p95 now passes everywhere measured, **including 200 per pod on the venv (20.13 [19.77, 20.54])**: the full GC fell
  from 118-130 ms every ~6 s per process (E2) to 64 ms p50 / 82 ms max every 20-23 s (perf-gc-objects), CPU per turn
  from 46 to 40-41 ms (both sprints). What holds p50 is a floor of ~8.5 ms (venv) / ~9.2 ms (image) at 50
  streams (ingress p50 7.9 / 8.5 ms of it) plus queueing that grows with density, for the event loops and the pod's
  two cores (four loops instead of two do not lower it, diagnostic below): the p50 rises by 0.3-0.5 ms per 25 streams
  per pod up to 150 on the venv (0.8 per 25 from 150 to 200) and 0.5-0.8 ms on the image.

**L1 = PASS at the sized density** (owner decision 2026-10-12: size from the knee, report the SLO at that density):
on the image at 75 streams per pod p50 9.65 [9.63, 9.67] / p95 12.38 [12.26, 12.50] ms. **The gate as written (200
streams per pod) still FAILS on p50** (11.70 [11.64, 11.76] venv; p95 passes); E2: 14.76 / 49.79, Wave E 15.16 /
71.54.

**Diagnostic: 4 api processes on the same 2 vCPU (image, `e3-l1-img4p`, `--api-procs 4`; not the configured
deployment)**, to decide the processes-per-pod column of the sizing:

| streams/pod | n | TTFT ovh p50 [95% CI] | p95 [95% CI] | pod CPU | CPU ms/turn | RSS MiB | vs 2 processes (p50 / CPU per turn / RSS) |
|---|---|---|---|---|---|---|---|
| 100 | 10,693 | 10.16 [10.13, 10.19] | 14.42 [14.24, 14.64] | 0.776 | 53.7 | 888 | -0.19 ms / +14% / +56% |
| 125 | 10,851 | 10.81 [10.78, 10.84] | 16.84 [16.56, 17.20] | 0.952 | 52.6 | 945 | 0.00 ms / +13% / +50% |
| 150 | 10,837 | 11.49 [11.45, 11.54] | 19.95 [19.65, 20.38] | 1.125 | 51.9 | 1,001 | -0.13 ms / +12% / +46% |

Four processes do not move the knee (every window still misses p50; upper CI 10.19 at 100 per pod) and cost 12-14% more CPU
per turn and ~50% more memory: the split loops now queue for the same two cores. Two processes per pod stay.

### 3.2 C2 ramp 0 -> 1,000 streams, event-loop lag, pools and datastores — `e3-ramp.json`

`run.sh e3-ramp --plan 50:120,100:120,150:120,200:120,250:120,300:120,400:120,600:120,800:120,1000:120 --warmup-s 60
--settle-s 30` (Wave E's form: 2-minute holds, **shortened and labeled as spec-10 C2 allows**; 06 §5 asks 10 min).
Pass: api event-loop lag p99 < 50 ms at every step up to 200 streams per pod; the rest measured. The R4 metrics
(misc-fixes, IMPL-088) now make pool saturation and datastore latency measurable: `dscompact.py` takes the
difference of each process's cumulative histograms between the step-end scrapes (1/16 of operations sampled) and
the pool gauges at the scrape.

| streams/pod | n | turns/s | TTFT ovh p50 / p95 / p99 | e2e ovh p50 / p95 | ingress p50 / p95 | api lag p99 / p99.9 / max | pod CPU | ms/turn | RSS MiB | fds max | 429s | valid | Wave E: lag p99, TTFT ovh p50 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 854 | 7.12 | 8.3 / 11.7 / 14.7 | 14.2 / 16.9 | 7.7 / 9.7 | **2.1** / 4.9 / 24 | 0.33 | 45.9 | 401 | 182 | 0 | yes | 3.9, 8.9 |
| 100 | 1,748 | 14.57 | 9.2 / 12.9 / 16.6 | 15.7 / 20.2 | 8.5 / 10.7 | **3.4** / 5.3 / 29 | 0.60 | 41.0 | 509 | 327 | 0 | yes | 5.4, 10.0 |
| 150 | 2,622 | 21.85 | 10.1 / 14.5 / 18.5 | 17.2 / 24.2 | 9.1 / 11.9 | **4.2** / 6.5 / 50 | 0.88 | 40.4 | 616 | 447 | 0 | yes | 6.8, 11.5 |
| 200 | 3,455 | 28.79 | 11.2 / 18.9 / 41.3 | 19.1 / 31.5 | 9.9 / 15.5 | **5.1** / 10.7 / 162 | 1.16 | 40.3 | 730 | 575 | 0 | yes | 8.0, 14.2 |
| 250 | 4,009 | 33.41 | 13.4 / 30.8 / 60.0 | 23.2 / 58.6 | 11.2 / 26.9 | 6.1 / 12.7 / 104 | 1.33 | 39.7 | 820 | 675 | 2,271 | yes | 10.4, 20.7 (invalid) |
| 300 | 4,153 | 34.61 | 14.2 / 35.7 / 71.0 | 25.0 / 70.0 | 11.8 / 31.3 | 6.7 / 12.2 / 113 | 1.39 | 40.1 | 866 | 723 | 7,277 | yes | 10.9, 23.9 (invalid) |
| 400 | 4,147 | 34.56 | 13.5 / 26.6 / 70.8 | 23.6 / 49.8 | 11.2 / 22.3 | 6.1 / 10.4 / 112 | 1.43 | 41.5 | 889 | 742 | 19,160 | yes | 11.2, 27.4 (invalid) |
| 600 | 4,168 | 34.73 | 14.4 / 36.6 / 74.5 | 25.9 / 75.0 | 11.9 / 32.4 | 6.6 / 12.0 / 121 | 1.49 | 42.8 | 897 | 783 | 43,061 | yes | 14.8, 47.9 (invalid) |
| 800 | 4,148 | 34.57 | 15.8 / 45.4 / 82.6 | 28.6 / 99.7 | 12.9 / 39.1 | 7.2 / 14.3 / 126 | 1.56 | 45.0 | 901 | 811 | 66,945 | yes | 23.8, 115.2 (invalid) |
| 1,000 | 4,162 | 34.68 | 16.1 / 40.3 / 96.6 | 28.8 / 72.8 | 13.0 / 32.6 | 6.7 / 12.3 / 132 | 1.63 | 46.9 | 905 | 826 | 90,897 | yes | 48.9, 276.0 (invalid) |

Datastores per step (both processes; "in use" is the gauge at the step-end scrape, per process; histogram buckets
start at 5 ms, so the mean is the informative figure, misc-fixes §9):

| streams/pod | Postgres in use (of 20) | PG acquire mean / max bucket | PG unit of work mean / p99 bucket | Redis in use (of 256) | Redis acquire mean | Redis command mean / p99 bucket |
|---|---|---|---|---|---|---|
| 50 | 0, 0 | 0.03 ms / 5 ms | 2.29 / 10 ms | 5, 5 | 0.02 ms | 0.29 / 5 ms |
| 100 | 0, 0 | 0.03 / 5 | 2.68 / 10 | 6, 5 | 0.02 | 0.32 / 5 |
| 150 | 0, 0 | 0.03 / 5 | 2.71 / 10 | 5, 5 | 0.02 | 0.38 / 5 |
| 200 | 0, 0 | 0.03 / 5 | 3.16 / 10 | 5, 5 | 0.02 | 0.53 / 5 |
| 250 | 0, 0 | 0.06 / 25 | 4.04 / 25 | 5, 6 | 0.02 | 0.63 / 10 |
| 300 | 0, 0 | 0.03 / 5 | 4.19 / 25 | 6, 6 | 0.02 | 0.71 / 10 |
| 400 | 1, 0 | 0.03 / 5 | 3.84 / 25 | 5, 6 | 0.02 | 0.68 / 10 |
| 600 | 0, 0 | 0.03 / 5 | 4.55 / 25 | 6, 6 | 0.02 | 0.74 / 10 |
| 800 | 0, 0 | 0.03 / 5 | 4.97 / 25 | 5, 5 | 0.02 | 0.85 / 10 |
| 1,000 | 1, 0 | 0.03 / 5 | 5.12 / 25 | 7, 6 | 0.02 | 0.85 / 10 |

- **C2 = PASS**, now in full: lag p99 **2.1 / 3.4 / 4.2 / 5.1 ms** at 50 / 100 / 150 / 200 per pod (Wave E 3.9 / 5.4 /
  6.8 / 8.0), every window valid, and the formerly unmeasurable part measured: no pool saturates at any step (Postgres
  at most 1 of 20 connections checked out at a scrape, acquire wait 0.03-0.06 ms mean; Redis 5-7 of 256, 0.02 ms), the
  datastore latency rises with load but stays small (Postgres unit of work 2.3 -> 5.1 ms mean, Redis command 0.29 ->
  0.85 ms mean from 50 to 1,000 streams).
- **Capacity and overload**: the pod tops out at **34.6-34.7 turns/s from 300 streams** (first sheds at 250: the
  per-process cap, 2 x 120 = 240 running turns); past it the edge shed answers 429 `queue_timeout` (`retry-after:
  1`), **0 x 5xx and 0 contract violations at every step up to 1,000 streams** (the drivers validate every non-200).
  New in E3: every window up to 1,000 streams is **valid** (Wave E: invalid above 200), and the served turns barely
  notice the overload: TTFT overhead p50 16.1 ms at 1,000 streams (Wave E 276 ms), lag p99 6.7 ms (Wave E 48.9), pod
  CPU 1.63 cores (Wave E 1.99) for 90,897 sheds in 120 s. That is the edge shed (perf-cpu-shed) at work.
- Memory per stream ~2.2 MiB (RSS 401 -> 730 MiB from 50 to 200 streams, incl. the 300 s resume window of sealed
  logs), fds 182 -> 575, 18 threads. One driver transport `ReadError` in the whole run (the load driver's keep-alive
  expiry = uvicorn's 5 s timeout, perf-cpu-shed finding b), outside every window; 0 tracebacks in the api logs.

### 3.3 C3 slow clients — `e3-c3.json`

`run.sh e3-c3 --plan 180:180,180:180,180:180 --warmup-s 60 --settle-s 120 --scenario tests/load/scenarios/slow.yaml
--sse-probe --side fast:20:1 --side slow:20:2:14500` (the Wave E / E2 form: 180 normal L1 streams in every window;
window 1 adds 20 heavy clients read at full speed = the baseline, window 2 the same 20 read at ~10% of the byte rate).
Stored status from Postgres (`c3.py`), the SSE writer's queue high-water and `client_too_slow` timing from the probe.

| Criterion | Measured (E3) | E2 | Verdict |
|---|---|---|---|
| normal TTFT p95 within 10% of the baseline | window 2 / window 1 = **0.998 [0.994, 1.002]** (321.8 vs 322.5 ms); vs window 0: 1.016 [1.014, 1.019]; 0 errors, 0 contract violations, every window valid (loadavg mean 4.1-4.3) | 0.976 | PASS |
| coalescing | 180 coalescing runs, 15,660 frames merged (108 + 72 runs over the 2 processes) | 15,720 | PASS |
| `client_too_slow` at 30 +/- 1 s | **60 of 60** slow streams, **30.000-30.029 s** after the stream stayed blocked (36 + 24 per process); 60 "client too slow" log lines | 60/60 at 30.000-30.175 s | PASS |
| ... and the turn ends (spec-04 §12: cancelled, stored `failed`) | **60 of 60 slow turns stored `failed` at 64.3-64.6 s**; the 80 fast-read heavy turns `completed` at 90.3-90.4 s; 60 uvicorn `ASGI callable returned without completing response` lines (the documented cost of the bounded closing send), 0 tracebacks | 60/60 `failed` | PASS |
| queue high-water <= 256 events / 1 MiB | **253 events, 1,048,270 B** in both processes: **306 B under 1 MiB** (largest frame 11,540 B) | 254 events, 1,052,270 B (one frame over) | **PASS** (was FAIL by one frame) |

**C3 = PASS** (was PARTIAL): QL-3 is fixed at load scale. The SSE writer now admits a frame only if `nbytes +
len(frame.wire) <= queue_bytes` (perf-gc-objects, `streaming/sse_writer.py`; unit test
`tests/unit/streaming/test_sse_queue_bound.py`), and the high-water mark stopped below the bound. Pod RSS in window 2:
2.76-2.82 GB (E2 2.8-2.9 GB; the heavy turns' sealed logs held for the 300 s resume window).

### 3.4 C5 fairness: one tenant flooding — `e3-c5.json`, `e3-c5-run2.json`

Config v3 (t_beta `concurrent_streams_per_project` 40, digest `sha256:916f53ef...` as perf-cpu-shed). `run.sh e3-c5
--plan 100:300,100:300 --warmup-s 60 --settle-s 60 --side flood:t_beta:200:1` (the E2 gated form: victim t_acme 100
streams per pod in both windows; window 1 adds t_beta at 200 streams = 5x its limit), run twice back to back. Pass:
victim client TTFT p95 <= 1.10 x solo (bootstrap 95% CI of the ratio, `ratio.py`). Columns as perf-cpu-shed §4
(`c5table.py`): the flooder's statuses and the admission `queue_timeout` / cap-shed counters are both processes'.

| Run | victim p95 solo -> flood (ms) | ratio [95% CI] | victim errors | victim ovh p95 | victim ingress p95 | victim decision p95 | pod CPU solo -> flood | full GC p50 per process (flood) | flooder 200 / 429 / transport err | admission timeouts / sheds | load mean | valid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **e3-c5** | 312.9 -> 319.6 | **1.021 [1.019, 1.026]** | 0 | 19.0 | 16.1 | 75 | 0.60 -> 0.93 | 95 / 98 ms | 2,110 / 43,391 / 1 | 5,912 / 37,449 | 2.7 / 3.9 | yes |
| **e3-c5-run2** | 313.1 -> 320.1 | **1.022 [1.019, 1.026]** | 0 | 19.1 | 15.7 | 76 | 0.60 -> 0.94 | 98 / 88 ms | 2,114 / 43,424 / 4 | 5,877 / 37,511 | 3.3 / 4.0 | yes |
| E2 `e2-c5` (E2 tree) | 315.2 -> 386.3 | 1.226 [1.214, 1.244] | 0 | - | 70.4 | 152 | - | - | 2,119 / 24,354 / - | - | - | yes |
| perf-cpu-shed `c5-comb-1` (both sprints' code, snapshot) | 314.1 -> 324.0 | 1.032 [1.026, 1.036] | 0 | 23.2 | 19.1 | 77 | 0.61 -> 0.94 | 59 / 55 | 2,109 / 43,013 / 4 | 5,947 / 37,034 | 4.0 / 5.9 | yes |

**C5 = PASS** (was FAIL): 1.021 and 1.022 on the final tree, upper CI 1.026 in both runs, far under 1.10; 0 victim
errors. Against E2 (1.20-1.23) the victim's ingress p95 under the flood fell from 61-70 to 16 ms and its decision p95
from 137-152 to 75-76 ms: the flood's ~120 rejected createTurns per second (43.4k in the 360 s side group) are
now cap sheds (37.5k per run; the edge shed refuses the unkeyed ones before the middleware stack, ~10 us each,
perf-cpu-shed) instead of being refused after the full ingress, and the max-min
share bounds the flooder's waiting turns (admission timeouts 5.9k per run; on the E2 tree 14.2-14.7k in
perf-cpu-shed's base runs). The two runs agree to 0.001; E2's 1.06-1.29 spread is gone. The
flooder's 1-4 transport `ReadError`s per run are the load driver's keep-alive race (perf-cpu-shed finding b), never
on the victim.

### 3.5 C1 fan-out against a per-tenant Bedrock quota — `e3-c1.json`

Config v5: t_acme `model_quotas.sol-6.1` = {rpm 600, tpm 1e11}. `run.sh e3-c1 --plan 200:300 --warmup-s 60
--mock-cpus 16-17,40-41 --mock-quotas '[{"model": "us.openai.gpt-6.1-sol", "rpm": 600, "akid": "*"}]'` (the E2 form:
2 processes = 2 STS sessions / AKIDs against ONE account-scoped mock window, one Converse mock process, the full
router-default fan-out of 15 decision calls per turn). Pass: `aghoo_reservation_overrun_total` = 0 and 0 mock
`ThrottlingException`s while admission answers 429. Counts from the mock records (`c1.py`).

| Quantity | E3 (`e3-c1`) | E2 (`e2-c1`) |
|---|---|---|
| turns sent / served | 27,617 / 2,220 (window valid, loadavg mean 3.9) | 28,998 / 2,218 |
| admission 429s | 25,397 `rpm_exceeded` (driver); server 25,579 `rpm_exceeded` + 204 `queue_timeout`, 544 cap sheds | 26,780; server 26,962 + 259 |
| Sol calls at the mock / mock throttles | 2,767 / **0** (all 44,272 mock requests `ok`) | 2,782 / 0 |
| busiest 60 s, all AKIDs (per AKID) | **546** (255 / 291) against rpm 600 | 562 (293 / 269) |
| Sol calls per minute from the first | 546, 484, 431, 413, 445, 448 | 562, 483, 426, 421, 443, 447 |
| overrun counter | absent on both processes (0) | 0 |
| admission fallback to local buckets | **none** (`aghoo_admission_degraded` 0 on both processes) | once (one Redis timeout in a GC pause, QL-5) |

**C1 = PASS** (unchanged): the window held with 0 mock throttles and 0 overruns. The E2 run's one admission fallback
to the local buckets (a > 50 ms Redis wait inside a full-GC pause) did not recur: in this run the full GCs were 25-27
ms p50 (max 31 ms) every 50-60 s per process, api lag p99 2.9 ms / max 35 ms.

### 3.6 C4 soak, shortened — `e3-soak.json`

`run.sh e3-soak --plan 150:3600 --warmup-s 600`: **1 h at 60% of the measured maximum** after a 10 min warm-up (the
300 s resume window fills). The maximum is the ramp's (section 3.2): the pod tops out at 34.6-34.7 turns/s, first
sheds at 250 streams (240 running turns = 2 x the cap 120); 0.6 x 250 = **150 streams per pod** (Wave E's density,
so the runs compare; = 21.7 turns/s, 0.63 x the ceiling). Labeled "shortened" as spec-10 C4 allows (06 §5 asks
24 h). Pass: RSS growth <= 5% after the warm-up; `aghoo_stuck_attempts_total` = 0; every attempt terminal.

| Quantity | E3 | Wave E (`e5-soak`) |
|---|---|---|
| turns in the 60 min window | 78,005 timed (21.67 turns/s), **0 errors**, 0 contract violations, window valid (worst driver lag p99 0.93 ms, mock 1.05 ms; loadavg mean 3.5, max 6.2) | 77,960, 0 errors |
| pod RSS (both processes, 5 s samples) | **698.3 MiB at the end of the warm-up -> 703.6 MiB at 60 min: +0.76%**; 703.4 MiB at every 10-min mark from 10 to 50 min (flat after the first 10 min); fds 490-511 | 731.3 -> 736.4 MiB (+0.7%) |
| full GC over the hour | per process 37-38 / 44-46 ms p50 in every 10-min slice (no drift), every 26-29 s; max 77 ms | - |
| `aghoo_stuck_attempts_total` | absent from both processes' `/metrics` at the end (never incremented: 0); outbox backlog 0 / 0 | 0 |
| terminal states (Postgres, every turn created 15:46:24-16:56:23 UTC, warm-up included) | **91,050 turns, all `completed`; 91,050 attempts, all `committed` with `ended_at` set; 0 non-terminal** | 91,202 / 91,202 |
| overhead at 150 streams (not gated here) | TTFT p50 10.21 [10.19, 10.22] / p95 15.87 [15.78, 15.97] / p99 27.3 ms; api lag p99 4.4 ms, max 95 ms; pod CPU 0.88 cores (40.8 ms/turn) | 11.96 / 23.63 / 94.2; lag p99 6.9 |

**C4 (shortened) = PASS.** The 24 h soak itself is NOT-RUN (the 1 h form; a 24 h run needs the host for a day).

### 3.7 L4 decision quorum on the probe latencies — `e3-l4.json` (not in the brief; re-run because the decision run changed)

The E3 decision run drops its coordinator at the final snapshot and A1 now starts before the decision entry
(perf-gc-objects), so L4 was re-run in the E2 form: config v6 (= v2), `run.sh e3-l4 --plan 50:330 --warmup-s 60
--scenario tests/load/scenarios/decision.yaml` (the mock's probe table from the live cassettes; 15-20 decision
calls per turn). Pass: p95 time to the commit-critical quorum <= 1,000 ms, quorum met >= 99%, n >= 2,000 turns
(`l4.py`: `aghoo_decision_quorum_reached_seconds{profile="router-default"}` against
`aghoo_decision_seconds_count{stage="quorum"}`, both processes).

| Quantity | E3 | E2 |
|---|---|---|
| turns, errors | 2,381, 0 | 2,379, 0 |
| quorum reached | **2,815 of 2,817 decisions = 99.9%** | 2,816 of 2,816 |
| time to quorum | 850 within 0.5 s, 2,749 (97.6%) within 0.75 s, all reached within 1.0 s: p95 in (0.5, 0.75] s | 97.4% within 0.75 s |
| members | Haiku 13,535 ok / 345 timeout (2.5%), DeepSeek 13,685 / 195 (1.4%), Luna 13,880 / 0 | Haiku 2.3%, DeepSeek 1.6% timeouts |
| S1 `decision_ms` | p50 839 / p95 1,000 / max 1,005 ms (2,307 turns commit at `holdback_deadline`: early commit off, R5) | p50 844 / p95 1,000 |

**L4 = PASS** (unchanged). The window is "invalid" by the scenario's design (holdback), which does not touch the
quorum counts; loadavg mean 3.8 (max 6.2).

## 4. Regression and suite gates on the final tree

Images `:qae3`, own projects and port bases, 2 workers per api pod everywhere (the compose default). The suites ran
after the last gated load window, in batches (`/tmp/noema-qae3/bin/regress-all.sh`): B1 = the e2e chain (e2e twice,
ui-check, ui-e2e on the fixture and on the e2e stack), qa-contract, test-media and check_docs in parallel; then
qa-security, image-scan, qa-fault, qa-gates and make check, each alone.

| Check | Command | Pass criterion | Measured | Result |
|---|---|---|---|---|
| make check | `NOEMA_AGENT_ID=qa-lead make -k check XDIST=4` at 11:31 (before any run) and at 18:47-18:49 (the last suite) | every CHECK_STEP green | **all green both times**: ruff (916 files formatted), mypy 327 src / 590 test files 0 issues, import-linter 27 kept / 0 broken, forbidden-scan clean, ownership 236 / 265 changed paths ok (the second run counts this role's new report files) / 0 BAD, contracts-check (70 vendored files identical, generated files up to date), unit **3,447**, conformance 757 (+8 skipped), integration **573** (+21 skipped), sdk-check TS 86 (+6) / Python 192 (+1) | **PASS** |
| make e2e, twice | `make e2e E2E_PROJECT=noema-qae3-e2e E2E_PORT_BASE=34200 E2E_BUILD=0` (`NOEMA_*_IMAGE=...:qae3`) | 52/52 both runs | **52 passed (155.6 s), 52 passed (153.9 s)**; plus 52/52 inside `qa-security` | **PASS** |
| make ui-check | `make ui-check` | green | tsc (app + node), vitest 75/75, vite build, ruff, mypy --strict (3 files) | **PASS** |
| make ui-e2e | `NOEMA_UI_PORT=34290 make ui-e2e` (fixture API); `NOEMA_UI_PORT=34295 make ui-e2e UI_STACK_STATE=~/.noema/stacks/noema-qae3-e2e.json UI_STACK_MOCK=1` (e2e stack) | all specs; every proxied response contract-valid | 12/12 (12.1 s) and 12/12 (38.1 s); 55 responses validated, 0 failures, both runs (+12/12 inside `qa-security`) | **PASS** |
| make qa-contract (Q1, Q2, Q3, X1) | `make qa-contract QA_CONTRACT_PROJECT=noema-qae3-contract QA_CONTRACT_PORT_BASE=34400 QA_CONTRACT_BUILD=0` | every test green; 0 server-side validator violations | docs-drift 8/8, sdk-compat 65/65; stack **131 passed** (facade Python 14, retries 3, TypeScript 12, input robustness 67/67 incl. the 18 NUL-id reproducers, Jev 8, schemathesis stateless 25 operations + tally, stateful 1); schemathesis stateless **6,160 calls, 0 failures** (only 5xx: `resumeTurnEvents` 501 x65, allowed), stateful **1,664 calls, 0 failures**; ContractValidator violations (api-1, api-2) **0**; tallies `reports/qa/e3/qa-lead/contract-schemathesis-*.txt` | **PASS** |
| R9 on the stack (not a make target) | `/tmp/noema-qae3/probe/r9.py` against the e2e stack, then the stored row in its Postgres | ruling R9 | createSession with U+0000 in a metadata value: **400 `schema_violation`, `param` `metadata.k`**; in a key: 400, `param` `metadata`; updateSession value: 400, `metadata.k`; createTurn with `"a\u0000b"`: 200, completed, `input_parts` stored as `"a�b"` | **PASS** |
| make qa-security (SE1-SE13) | `make qa-security QA_SEC_PROJECT=noema-qae3-sec QA_SEC_PORT_BASE=34500 QA_SEC_TAG=qae3 QA_SEC_BUILD=0 QA_SEC_UI_PORT=34590 QA_SEC_OUT=~/.noema/qae3-security QA_SEC_REPORTS=reports/qa/e3/qa-lead/security` | every part green | local **72/72**; stack: e2e 52/52, ui-e2e 12/12, `tests/security/stack` **31/31** (authz matrix 5/5: **356 cells, 356 equal to the oracle, 0 leaks**, the 18 `resumeTurnEvents` cells included; SE7 1/1 (44 requests, none unseen); injection 11/11 (52 requests, 51 canaries); RLS 2/2; cache isolation 3/3; edge and config 4/4; CSP 2/2; authn hygiene, trace scan (49 spans), secrets scan (46 values)); fuzz (SE9) **6/6** (12 min 22 s; atheris `pdf.sanitize` crash input -> jail `rejected:corrupt`); SCA: pip-audit 0 of 243 (torch, torchvision not auditable), npm 0 / 0, trivy api/worker 44 HIGH, media 4 CRITICAL + 94 HIGH, **51 ids waived, 0 unwaived, SE13 PASS**; "qa-security: all green" | **PASS** |
| make image-scan | `make image-scan IMAGE_TAG=qae3 IMAGE_SCAN_OUT=~/.noema/qae3-image-scan` | exit 0: no CRITICAL outside the waivers | exit 0 (no unwaived CRITICAL in api, worker, media); CycloneDX SBOMs 155 / 155 / 331 components | **PASS** |
| make test-media | `make test-media IMAGE_TAG=qae3` | green | **307 passed, 8 skipped** (no network in the media image) | **PASS** |
| design docs | `cd /codes/aghooai/docs/noema && ./check_docs.sh` | exit 0 | exit 0: mermaid (every diagram), tsc, schema, pydantic, config, audit, openapi, bundle, prose, links PASS | **PASS** |

### 4.1 Fault rows (`make qa-fault FAULT_PROJECT=noema-qae3-fault FAULT_PORT_BASE=34300 FAULT_IMAGE_TAG=qae3 FAULT_BUILD=0 FAULT_RESULTS=reports/qa/e3/qa-lead/fault-results.json`; 2 workers per api pod)

**22 passed (36 min 16 s).** S1a (zero duplicated client-visible text) is asserted in every row before its other
assertions: 0 problems.

| Row | Pass criterion (spec-10 §4) | Measured (E3) | E2 | Result |
|---|---|---|---|---|
| F1 throttling | fallback >= 99% pre-commit; no cross-tenant impact | fallback **52/52** (gap p95 5.2 ms); tenant B TTFT p95 635.1 under the fault vs 642.3 ms clean | 60/60, gap p95 14.9 | PASS |
| F2 AccessDenied / AssumeRole | typed 503, no retry, 100% classified | 503 `tenant_access_misconfigured`, not retryable, 0 mock requests (3 variants x 20); F2b 0 Sol requests (no fallback); F2c Haiku's 500 votes typed, 100/100 completed | same | PASS |
| F3 5xx burst | circuit opens in the 30 s window, half-open, recovers | open **4.29 s** after the burst, recovered 5.13 s after the window; F3b (p95 TTFT rule) open 7.83 s after the slowdown, closed 0.23 s after the restore | 7.93 / 9.05 s; F3b 6.86 / 0.21 s | PASS |
| F4 hang before first byte | TTFT timeout -> fallback; no turn > deadline + 1 s | fallback **100/100**, e2e max 11.3 s at deadline 30 s; F4b (no member left) 100/100 typed `all_routes_unavailable`, max 5.4 s at 10 s | 50/50 | PASS |
| F5 stall mid-stream | post-commit typed error, partial kept | pre-commit fallback **100/100**; post-commit 100/100 `upstream_stalled` retryable, partial rendered, stored text partial 20/20 | same | PASS |
| F6 exception event | post-commit typed, pre-commit fallback, 100% classified | pre-commit fallback **60/60** (100 turns completed); post-commit 5 exception types x 20 `all_routes_unavailable` retryable, 5 chunks rendered | 80/80 | PASS |
| F7 reasoning-only | nothing committed, fallback, no empty answer | **50/50 + 50/50** served, 0 committed from GLM, 0 empty answers | same | PASS |
| F8 malformed SSE / decision JSON | member error / fallback, no 500 | attempt fallback **30/30 + 30/30** (CRC, truncation; 100 x 200); members get their parse codes (`parse_error`, `missing_options`, `sum_out_of_range`, `upstream_error`), 100/100 completed, 0 x 500 | 40/40 + 40/40 | PASS |
| F9 all members down | 100% served, flagged | 200/200 served, every answer `unavailable`, `decision unavailable: all_members_failed` 188 / `no_quorum` 12 | same | PASS |
| F10 Redis down / hung | availability unaffected, local buckets, resume disabled | 125/125 + 125/125 completed during the 25 s outages, `admission_degraded` 4, L2 bypassed (misses `error`); same-pod cancel 202, cross-pod cancel 500 (as specified); cross-pod attach **410 `resume_window_expired` after 2.5 s / 3.3 s** | same | PASS |
| F11 Postgres down 60 s | fail fast, ephemeral turns continue, no data loss | `store: true` 500 `internal_error` at ingress (p95 5.3 ms); `store: false` turns completed through the outage (0 failures); 80/80 finalized turns stored exactly once with one generation usage line | same | PASS |
| **F12 retry storm (R6, R8)** | **requests to X / X's first sends <= 1.1 for every failing upstream X**, both rows | **rate 0.5: Sol 1.038, Opus 1.000, GLM 1.000** (generation-only Sol 1.100; requests per turn 0.216); **rate 0.25: Sol 1.070, Opus 1.000, GLM 1.000** (generation-only 1.086; requests per turn 1.208, each fallback a first send) | rate 0.5 gated on requests per turn (0.223), Sol 1.149 per upstream (IMPL-016 floor) | **PASS** (residual closed) |
| S1b pre-commit fallback >= 99% | every fallback row | **472 of 472** failed-first turns served (F1 52, F4 100, F5 100, F6 60, F7 100, F8 60) | 470 / 470 | PASS |
| C3 at fault scale | slow turns cancelled, others unaffected | 4/4 slow stored `failed` at 47.6-47.7 s; 8/8 normal completed, TTFT p95 739 vs 949 ms baseline | 4/4 at 47.4-47.9 s | PASS |

F12 under R8 (misc-fixes: 10% of first sends, no floor; a provably unsent connect retry exempt, one per call): the
E2 residual (Sol 1.149 at rate 0.5, from IMPL-016's floor of 10 re-sends per account-minute below 100 first sends)
is gone: at rate 0.5 Sol saw 135 requests (55 generation + 80 labeler/other calls) for 130 first sends, i.e. 5
re-sends against R8's allowance of 13 (10% of first sends).

### 4.2 QA-harness gates (`make qa-gates QA_GATES_PROJECT=noema-qae3-gates QA_GATES_PORT_BASE=34600 QA_GATES_TAG=qae3 QA_GATES_BUILD=0 QA_GATES_RESULTS=reports/qa/e3/qa-lead/gates-results.json QA_GATES_OUT=~/.noema/qae3-gates`)

"qa-gates: all green": selftest 8/8, holdback 13/13, backup tail 1/1, replay 2/2, Q9 4/4, SE7 1/1, M3 2/2,
ownership of every tracked path (1,707 tracked, 0 unowned). L2 / L3 / Q8 (n 200 per cell after a 50-turn ramp-in,
concurrency 50, client TTFT on the raw stream, every body contract-validated; a bound met in an invalid window
passes, a miss there would be "invalid, re-run"):

| Cell | client TTFT p50 / p95 / p99 ms | bound ms | premise | `holdback_deadline` | driver lag / mock lateness p99 ms | Result |
|---|---|---|---|---|---|---|
| L2 agree T_d=300 TTFA=300 | 315.8 / **322.2** / 330.5 | 350 | 1.0 | 0.0 | 1.10 / 81.49 (invalid) | PASS |
| L2 agree T_d=300 TTFA=720 | 727.9 / **731.5** / 747.2 | 770 | 1.0 | 0.0 | 1.10 / 1.71 | PASS |
| L2 agree T_d=300 TTFA=2400 | 2407.8 / **2411.1** / 2413.9 | 2450 | 1.0 | 0.0 | 1.15 / 1.79 | PASS |
| L2 agree T_d=900 TTFA=300 | 708.9 / **715.1** / 726.8 | 950 | 1.0 | 1.0 (hold cap) | 1.11 / 14.84 (invalid) | PASS |
| L2 agree T_d=900 TTFA=720 | 918.2 / **923.9** / 927.8 | 950 | 1.0 | 0.0 | 1.09 / 3.08 (invalid) | PASS |
| L2 agree T_d=900 TTFA=2400 | 2407.9 / **2412.2** / 2415.6 | 2450 | 1.0 | 0.0 | 1.16 / 1.64 | PASS |
| L3 disagree T_d=300 TTFA=300 | 619.5 / **633.6** / 649.6 | 700 | 1.0 | 0.0 | 1.10 / 1.88 | PASS |
| L3 disagree T_d=300 TTFA=720 | 1038.3 / **1041.4** / 1044.0 | 1120 | 1.0 | 0.0 | 1.11 / 1.26 | PASS |
| L3 disagree T_d=300 TTFA=2400 | 2718.1 / **2726.3** / 2733.3 | 2800 | 1.0 | 0.0 | 1.12 / 1.28 | PASS |
| L3 disagree T_d=900 TTFA=300 | 1220.8 / **1227.4** / 1232.1 | 1300 | 1.0 | 0.0 | 1.11 / 2.85 (invalid) | PASS |
| L3 disagree T_d=900 TTFA=720 | 1641.7 / **1649.5** / 1672.8 | 1720 | 1.0 | 0.0 | 1.10 / 3.18 (invalid) | PASS |
| L3 disagree T_d=900 TTFA=2400 | 3320.9 / **3328.4** / 3339.4 | 3400 | 1.0 | 0.0 | 1.13 / 4.92 (invalid) | PASS |
| Q8 late disagreement T_d=900 TTFA=300 | 708.5 / **713.9** / 721.1 | hold cap 700 | 1.0 | 1.0 | 1.12 / 2.28 (invalid) | PASS; stored `served_spec_differs` true **200/200** |

Seven of 13 cells were flagged invalid by the compose mock's lateness (p99 2.3-14.8 ms; the first cell's 81 ms is the
stack's warm-up, as in E2's 78.6 ms); every bound was met, with the tightest margins 923.9 vs 950 (E2 926.3) and
3,328.4 vs 3,400 ms (E2 3,338.4). The mock runs unpinned in the gates stack; E2 had 3 of 13 invalid cells.

| Gate | Measured (E3) | Result |
|---|---|---|
| L5 (`make qa-load-backup-tail`) | n 5,000 (+50): client p50 535.9 / p95 607.3 / **p99 929.5** / max 8,602 ms (bound 1,500); 5,050 `predicate:ok`; mock: 10,100 DeepSeek calls, the 90 slow primaries (k0 disconnected) answered by their backups; driver lag p99 1.85 ms, valid | PASS |
| R4 (`make qa-replay`) | replay set 40 x 5: 200/200 served, restarts 46 = scripted 46, share 0.23, waste 0.404 of generation cost (3,140 wasted output tokens); reported against the P0 10% / 5%, not gated. Idempotent replay 60/60 (20 SSE, 20 JSON, 20 decisions) `x-aghoo-idempotent-replay`, identical, 0 model calls, usage by line unchanged | PASS (meas) |
| Q9 (`make qa-hier-deadline`) | lightweight-model-decide and router-default, n 200 each: `deadline_exceeded` share 1.0 at 1,000 ms, 0.0 at 3,000 ms (all `choice:hierarchical`) | PASS |
| SE7 (`make qa-media-injection`) | 1/1: 0 policy violations, every carrier reached a model (also in `qa-security-stack`) | PASS |
| M3 (`make qa-ocr-cer`, media image) | CER 0.0 on the probe `number.png`, `invoice.pdf` p1 and the clean renders (aggregate 0.0) | PASS (meas) |
| ownership, every tracked path | 1,707 tracked, 0 unowned | PASS |

## 5. Gate status after Wave E3 (what changed, and what decided it)

| Gate | E2 | **E3** | Decided by (E3) | Change behind it (owner) |
|---|---|---|---|---|
| L1 | FAIL | **PASS** at the sized density (owner decision); as written at 200 per pod still FAIL (p50) | `e3-l1*`: image knee 75 per pod, p50 9.65 [9.63, 9.67] / p95 12.38 [12.26, 12.50]; 200 per pod (venv) p50 11.70 [11.64, 11.76], p95 20.13 [19.77, 20.54] | GC re-tune and per-turn object churn (perf-gc-objects); SigV4 / eventstream / pool-release CPU (perf-cpu-shed); sizing to the knee (owner) |
| C3 | PARTIAL | **PASS** | `e3-c3`: queue high-water 1,048,270 B <= 1 MiB, 253 events; 60/60 slow turns `failed`; others 0.998 | QL-3: frame admitted only if it fits (perf-gc-objects, `sse_writer`) |
| C5 | FAIL | **PASS** | `e3-c5`, `e3-c5-run2`: 1.021 [1.019, 1.026], 1.022 [1.019, 1.026] | edge shed before the middleware stack, max-min share, saturated tenants (perf-cpu-shed) |
| R5 | PARTIAL | **PASS** (machinery, live) / [P0] quality | live: CR #26 quorum 2/2 on both questions | live-smoke (harness cap 1,024; CR #26 in config since E2) |
| L6 | FAIL (live-n) | **PASS** (live-n) / [P0] | live: 6 interleaved pairs, p50 e2e ratio 0.899 | live-smoke (interleaved method); lean signer / decoder (perf-cpu-shed) |
| M2 | PARTIAL | **PASS** | `test_degradation_reporting.py`: 8 reasons, right event, right `attempt_id` | misc-fixes |
| F12 | PASS (residual Sol 1.149 at rate 0.5) | **PASS**, residual closed | `make qa-fault`: per upstream Sol 1.038 (0.5) / 1.070 (0.25), others 1.000; both rows gated per upstream | R8 in `SlidingRetryBudget` and the F12 test (misc-fixes) |
| C2 | PASS (pools / datastores not measurable) | **PASS** in full | `e3-ramp`: lag p99 2.1-5.1 ms to 200; pools <= 1 / 20 and 5-7 / 256 in use; Postgres 2.3-5.1 ms, Redis 0.29-0.85 ms mean | R4 metrics (misc-fixes) |
| K1 | FAIL (tokens, live-n) | FAIL (tokens, live-n; improved) | live: 14 of 25 within 2% out of sample (chars/4: 5 of 25) | table override wired (misc-fixes); refit is Phase 0 |
| SigV4 `:` model ids live (supporting) | NOT-RUN | **PASS** | live: Luna via its inference-profile ARN, `%3A` in the path, signature equal | live-smoke |
| Live budget (supporting) | PASS (57 of 60) | **PASS** (85 of 87) | `tests/live/budget.py` `LIMIT = 87` | owner decision 2026-10-12 |

Unchanged and re-measured in E3 (all PASS): Q1, Q2, Q3, Q6, Q8, Q9, R4 (meas), L2, L3, L4, L5, C1, C4 (1 h), F1-F11,
S1a, S1b, SE1-SE4, SE6-SE9, SE11-SE13, SE15, M1, M3, M5, X1. NOT-RUN by design ([P0] / [prod] / pre-GA): Q4, Q5,
Q7, R1, R2, R3, L7, S2, SE5, SE10, SE14, M4.

**Count over the 63 rows** (the E2 rule: F rows and S1's two criteria one by one, a row with a [P0] half by its
measured half): **50 PASS, 1 FAIL (K1), 0 PARTIAL, 12 NOT-RUN** (E2: 44 / 4 / 3 / 12; Wave E: 24 / 17 / 4 / 18).

## 6. Live items (live-smoke role, gated here from [live-smoke](live-smoke.md); no live call by this role)

The live-smoke role made **28 Bedrock calls (57 -> 85 of the owner-approved 87)** on the final tree (tree fingerprint
`c7ab5a60c98fb974` over `src tools`, no product edit after 08:36 UTC, the same product code as this re-gate), after a
mock rehearsal with 0 egress attempts. Pre-flight: `aws sts get-caller-identity` (account 976939723775), the bearer
token checked by length only. Gate readings:

| Item / gate | Live result (E3) | Before | Reading here |
|---|---|---|---|
| lean ConverseStream, new signer + decoder (perf-cpu-shed) | Opus 5.5 low first text 1,117 ms, GLM-5.3 low 397 ms; signer equal to botocore `SigV4Auth` on **28 / 28** requests, decoder equal to `EventStreamBuffer` on **4,304 / 4,304** messages (16 streams), 0 fallbacks, no 403 | offline only | **PASS** (live) |
| `:` model id through the lean SigV4 path (io-fixer finding 1) | Luna via its inference-profile ARN, `%3A` / `%2F` in the path, signature equal, answered in 375 ms | NOT-RUN (no live call in E2) | **PASS** |
| R5: CR #26 verification quorum | `turn.verified` completed; `answers_question` and `unsupported_claims` quorum **2/2**; Haiku at max_tokens 600: 85 / 270 output tokens, `end_turn`, text present | Wave E: `no_quorum` | **PASS** (machinery live) / quality [P0] |
| GLM race at low (Wave E L2 PARTIAL) | GLM won (first text 497 ms), Sol cancelled `lost_race` at 549 ms, no error, 7 calls | GLM rejected at `none` (Wave E) | **PASS** |
| L6: e2e <= 5% over direct, p50, pinned | **6 interleaved pairs**: p50 e2e ratio **0.899**, first delta 1.003; gateway overhead on the first delta 5-22 ms when the router answers before Sol's first text | 5 pairs, not interleaved: 1.093 | **PASS at live-n** / gate proper [P0] |
| L7: first answer per route | single samples: Opus low 1,117 ms, GLM low 397 ms | 1,078 / 358 ms | NOT-RUN [P0] (n = 1) |
| K1: estimator tokens <= 2% | shipped per-model table, out of sample (25 E3 calls): **14 of 25** within 2% (chars/4: 5 of 25); mean error Haiku 1.1%, Opus 3.2%, GLM 6.2%, Sol 6.9%, Luna 8.5% (max 42.9% on a 59-char prompt) | 46 offline calls, 33 within 2% (in sample) | **FAIL** (tokens, live-n) / cost [P0] |
| Live budget (spec-10 §6, cap 87) | 85 of 87, never exceeded; `tests/live/budget.py` `LIMIT = 87` with the owner-approval comment | 57 of 60 | **PASS** |

Notes from the live role that touch gates: the Luna router member hit its 1,000 ms deadline in 2 of 7 live router
calls (warm connections), which shows as first-delta holdback on a pinned turn (spec behavior) and lowers a
router-default quorum to 2 of 3; GLM at low reasoned this time (nondeterministic); the signer's session-token branch
is verified offline only (every live call used long-term keys).

## 7. Still open, and findings for other owners (measured; not fixed here: outside `tests/**` and `reports/**`)

| # | Item | Measured | Owner |
|---|---|---|---|
| 1 | **K1 FAIL** (live-n): 14 of 25 E3 calls within 2% with the shipped table, out of sample; Luna and Sol mix short adapter prompts and templates that one (cpt, overhead) line per model cannot fit | [e3/live-smoke §4](live-smoke.md) | Phase 0 refit (per call kind); generation's waste estimate (misc-fixes §9) |
| 2 | **The image's interpreter**: +11-13% CPU per turn, +0.7-1.5 ms p50, knee 75 vs 125 streams per pod (official-image CPython 3.12.15: shared libpython, GCC; venv: python-build-standalone 3.12.12, static libpython, clang PGO + ThinLTO + BOLT) | section 3.1 | infra / supply-chain (`deploy/docker/api.Dockerfile`) |
| 3 | **L1 as written (200 per pod)**: p50 11.70 ms; p95 passes. The p50 is the ingress floor plus queueing for the event loops and the pod's cores; 4 processes per pod do not help | section 3.1 | api, perf (or restate the gate at the sized density: spec owner) |
| 4 | **No streams-per-pod metric** for the 08 §9 scale signal (`TurnSlots.held` in `api/overload.py` is not exported) | sizing §7 | telemetry, api |
| 5 | **Stale docs** (not this role's paths): GC thresholds `(700, 10, 40)` in IMPL-087 + its GC paragraph, spec-11 §7 GC bullet, OWNER-CHECKPOINT, `docs/modules/integration.md` §5 (shipped: `(700, 10, 100)`); the cap's position (edge shed before authentication) in spec-11 §7, spec-04 §1 step 5 and IMPL-087; spec-04 §3 "A1 starts at the same point"; spec-10 §6's live `max_tokens` cap 256 (now 1,024 for decision / verification members); `integration.md` without `telemetry.pool_gauges` and the 2 new variables; the 08 §9 / 08:L488 design patch (now: [sizing.md](../../perf/sizing.md)) | grep of `docs/` | spec owner, integrator |
| 6 | The compose mock's lateness in `qa-gates`: 7 of 13 holdback cells invalid (E2: 3), every bound met; the stack's mock runs unpinned | section 4.2 | qa-tools (pin or warm the gates mock) |
| 7 | The load driver's keep-alive expiry equals uvicorn's 5 s: 1-4 transport `ReadError`s per C5 run on the flooder, 1 in the ramp, never on a measured window | sections 3.2, 3.4 | qa-tools (perf-cpu-shed finding b) |
| 8 | Not run: the 24 h soak (1 h form ran), multi-pod load against shared datastores, the target instance type | sizing §7 | Phase 0/1 load test |

Order of the regression stacks: the security stack stayed up (idle) during `qa-fault`, and the fault and security
stacks (idle) during `qa-gates` and the L4 run; all three were torn down right after. No load gate except L4 ran
while any regression stack existed (the L1, C2, C3, C5, C1 and C4 windows all precede the first one).

## 8. Teardown and what is left

- Stacks: `noema-qae3-perf`, `-e2e`, `-contract`, `-sec`, `-fault`, `-gates` brought down with their volumes and
  networks (`docker ps -a`, `docker volume ls`, `docker network ls`: 0 `qae3` objects left). The shared `noema-dev`
  services were used only by `make check`'s integration step.
- Kept: the images `noema-{api,worker,media,media-test}:qae3` (the evidence for the regression and the image runs),
  the raw run data in `/tmp/noema-qae3/` (large, not in the repo), the analyzed results in
  `reports/qa/e3/qa-lead/` (load, fault, gates, security, schemathesis tallies). Stack state files
  `~/.noema/stacks/noema-qae3-*` removed.
- Tree digest `5e4146f4ff4cf8d7` at the end, as at the start; `git status` shows the same 81 entries outside
  `reports/` as at the start (this role added `reports/perf/sizing.md`, `reports/qa/e3/qa-lead*` and edited
  `reports/qa/wave-e-summary.md`).
