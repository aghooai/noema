# API pod sizing from the measured knee (Wave E3; container knee Wave F)

Owner decision of 2026-10-12: one more performance sprint (E3), then **size the pods from the measured knee** (design
08 §9), reporting the SLO at that density. Two measurement rounds:

- **Wave F (infra-perf, 2026-10-12, this revision): the knee in the container** on the rebuilt shipped image
  `noema-api:wfinfra` (python-build-standalone CPython 3.12.15, section 8), method as in E3. Results files
  `reports/perf/wf-infra/load/wf-l1-{a,b,c}.json`; tree digest `9317251824d587ef` (src, deploy, tools) at the start
  and end of every run. **These numbers decide the sizing (sections 5 and 6).** Measured image
  `sha256:3c6ddf87aca6`; the final build of the same tree (`sha256:893760a9a29f`, also `noema-api:dev`) differs only
  in the wheel metadata (README.md, edited by another role during the runs): the noema sources are identical.
- **Wave E3 (QA lead)**: the official-python image `noema-api:qae3` and the dev venv, tree digest
  `5e4146f4ff4cf8d7`; method and raw tables in [reports/qa/e3/qa-lead.md](../qa/e3/qa-lead.md) section 3, results
  `reports/qa/e3/qa-lead/load/<run>.json`. Kept as history (section 2.2).

**Recommendation (section 5).** `api` pods of 2 vCPU / 4 GiB with 2 api processes each, **at most 125 concurrent
streams per pod** (the knee of the shipped image in the container, two runs: TTFT overhead p50 9.72 [9.69, 9.75] and
9.73 [9.71, 9.77] ms, p95 14.06 [13.88, 14.23] and 13.98 [13.83, 14.12] ms), planned at **100 per pod** (80% of the
knee; measured there: p50 9.30, p95 13.20 ms): **8 pods for the design's ~800 concurrent streams** (E3: 14).
Scale out above 100 streams per pod (`sum by (pod) (aghoo_turn_slots_held)`) or 0.62 vCPU per pod.

## 1. What was measured

- **Workload** = spec-10's L1 overhead scenario (`tests/load/scenarios/overhead.yaml`), closed loop: every turn runs
  the `router-default` fan-out (15 decision calls: DeepSeek on Mantle, Luna and Haiku on Converse, answers after
  50 ms) and one Sol ConverseStream of 400 tokens in 4-token chunks at 60 tokens/s (first text at 300 ms, ~6.95 s
  per stream). One stream = one turn in flight at a time, so "streams per pod" = concurrent turns per pod. The wire
  mock is plain HTTP on the host; the drivers validate every response against the contract.
- **Pod** = 2 vCPU (two physical cores 22-23 as CPUs 46-47, their hyper-thread siblings idle), **2 api processes**
  (`--api-image ... --api-procs 2`: `docker run --cpuset-cpus 46-47` with the compose app settings, uid 10001,
  read-only root, no capabilities; IMPL-087), per-process cap 120, GC thresholds (700, 10, 100) after `gc.freeze()`.
  Postgres and Valkey of the own stack `noema-wfinfra-perf` pinned to CPUs 44-45; 8 mock processes (Converse 16-19,
  Mantle 40-43) and the drivers (4-15, 28-39) on their own cores. Load config v2 (lifted tenant limits,
  `sha256:b475dc16...`, the E3 digest).
- **Quiet host**: a run starts only with no other `tests.load.run` alive and the 1-minute loadavg < 6; loadavg and
  foreign load processes are recorded every 10 s (`reports/perf/wf-infra/load/<run>.monitor`, copied from
  `/tmp/noema-wf-infra/runs/`; the only "foreign" processes in every sample are the container's own two api
  processes). Window loadavg max < 6 in `wf-l1-b` and
  `wf-l1-c`; `wf-l1-a` peaked at 8.8 in its 75 window (repeated in `wf-l1-c`, max 4.2) and 7.7 in its 100 window
  (< 8, the E3 rule).
- **SLO** (06 §4 L1): proxy (TTFT) overhead = client TTFT - mock TTFT; **p50 <= 10 ms and p95 <= 25 ms**, read on the
  upper end of the bootstrap 95% CI, n >= 10,000 first deltas per window, window valid by spec-10 §5 (every window
  here: 100% of turns `holdback_ms` 0 with `spec_agrees`, 15 decision calls per turn, worst driver lag p99 0.94 ms,
  worst mock 1.03 ms). **Knee** = the highest density whose window meets both.

## 2. The knee

### 2.1 Wave F: the shipped image in the container (`noema-api:wfinfra`, CPython 3.12.15, python-build-standalone)

| streams/pod | run | n | turns/s | TTFT ovh p50 [95% CI] | p95 [95% CI] | p99 | e2e ovh p50 / p95 | ingress p50 / p95 | api lag p99 / max | pod CPU | CPU ms/turn | RSS MiB | load mean (max) | both bounds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 75 | c | 10,618 | 10.83 | 8.74 [8.72, 8.76] | 11.40 [11.27, 11.54] | 15.1 | 15.2 / 18.8 | 8.1 / 10.1 | 2.9 / 32 | 0.467 | 43.1 | 503 | 2.8 (4.2) | PASS |
| 75 | a | 10,618 | 10.83 | 8.88 [8.86, 8.90] | 12.70 [12.51, 12.89] | 21.3 | 15.3 / 21.1 | 8.2 / 10.8 | 3.3 / 28 | 0.475 | 43.9 | 504 | 3.5 (8.8) | PASS (load spike) |
| 100 | a | 10,693 | 14.45 | 9.30 [9.27, 9.32] | 13.20 [13.02, 13.43] | 20.9 | 15.9 / 22.7 | 8.5 / 11.3 | 3.8 / 42 | 0.617 | 42.7 | 562 | 4.2 (7.7) | PASS |
| **125** | b | 10,826 | 18.04 | **9.72 [9.69, 9.75]** | **14.06 [13.88, 14.23]** | 21.9 | 16.4 / 22.5 | 8.9 / 11.6 | 4.1 / 48 | 0.750 | 41.6 | 624 | 3.5 (5.6) | **PASS** |
| **125** | c | 10,828 | 18.05 | **9.73 [9.71, 9.77]** | **13.98 [13.83, 14.12]** | 20.6 | 16.6 / 22.9 | 8.9 / 11.6 | 4.0 / 54 | 0.753 | 41.7 | 621 | 3.4 (4.8) | **PASS** |
| 150 | b | 10,809 | 21.62 | 10.21 [10.18, **10.26**] | 15.41 [15.22, 15.62] | 23.8 | 17.5 / 25.0 | 9.3 / 12.6 | 4.4 / 63 | 0.894 | 41.4 | 682 | 3.8 (5.8) | FAIL (p50) |

Runs (`reports/perf/wf-infra/load/run.sh` = section 1, with its `digest.sh`; both ran from `/tmp/noema-wf-infra/`;
`--warmup-s 60 --settle-s 30`): `wf-l1-a` `--plan 75:980,100:740`,
`wf-l1-b` `--plan 125:600,150:500`, `wf-l1-c` `--plan 75:980,125:600` (the replicate). RSS = pod, last 5 s sample.

- **Knee in the container: 125 streams per pod**, reproduced in two runs (upper CIs 9.75 and 9.77 ms); 150 misses
  p50 by 0.26 ms (upper CI). On the 25-stream grid the true knee lies in [125, 150).
- **The image now matches the venv** (section 2.2): CPU per turn 43.1 / 42.7 / 41.6 / 41.4 ms at 75 / 100 / 125 /
  150 vs the venv's 43.2 / 42.0 / 41.1 / 40.8 (-0.2 to +1.7%), p50 within 0.0-0.2 ms of it. Against the E3 image
  (official CPython) the rebuild saves **9-11% CPU per turn** and **0.9-1.4 ms of p50** at every density.
- p95 passes everywhere (15.4 ms at 150); **p50 is the binding bound**: ~8.7 ms at 75 (ingress p50 8.1 ms of it),
  +0.4-0.6 ms per 25 streams per pod.
- Full GC per process: 20-22 ms p50 every 52-61 s at 75, 31-38 ms (max 51 ms) every 30-38 s at 125 (E3 image at 75:
  20 ms). The knee is a latency knee at low CPU: 0.75 of the pod's 2 vCPU at 125 streams.
- `aghoo_turn_slots_held` (section 5) at each step-end scrape, the two processes summed: 75 (27 + 48, 40 + 35),
  100 (37 + 63), 125 (59 + 66, 67 + 58), 150 (67 + 83): the gauge equals the streams per pod in every step
  (`reports/perf/wf-infra/load/wf-l1-{a,b,c}/metrics-step<n>.txt` and `metrics-step<n>-1.txt`, the two processes'
  scrapes; the results JSON files carry no gauge data).

### 2.2 Wave E3 (history): the official-python image and the dev venv

QA lead's runs, [e3/qa-lead §3.1](../qa/e3/qa-lead.md); image = `noema-api:qae3` (CPython 3.12.15 of the official
Python image: PGO + LTO, shared libpython, GCC), venv = the dev venv (CPython 3.12.12 from python-build-standalone:
clang PGO + ThinLTO + BOLT, static libpython), the venv api outside a container.

| streams/pod | image p50 [CI] | image p95 [CI] | image CPU ms/turn | venv p50 [CI] | venv p95 [CI] | venv CPU ms/turn |
|---|---|---|---|---|---|---|
| 50 | 9.17 [9.15, 9.20] | 11.81 [11.64, 11.94] | 50.7 | 8.48 [8.46, 8.50] | 10.79 [10.67, 10.96] | 45.4 |
| 75 | **9.65 [9.63, 9.67]** | **12.38 [12.26, 12.50]** | 48.1 | 8.75 [8.72, 8.77] | 11.40 [11.28, 11.52] | 43.2 |
| 100 | 10.35 [10.32, 10.38] | 14.50 [14.35, 14.67] | 47.1 | 9.16 [9.14, 9.19] | 12.63 [12.49, 12.80] | 42.0 |
| 125 | 10.81 [10.79, 10.84] | 15.60 [15.37, 15.80] | 46.5 | **9.60 [9.57, 9.63]**; replicate **9.65 [9.62, 9.68]** | **13.89 [13.71, 14.10]**; 13.86 [13.70, 14.03] | 41.1 / 41.2 |
| 150 | 11.62 [11.58, 11.67] | 18.12 [17.87, 18.42] | 46.2 | 10.03 [9.99, 10.07]; replicate 10.16 [10.12, 10.20] | 15.06 [14.87, 15.28]; 15.14 | 40.8 / 41.0 |
| 200 | - | - | - | 11.70 [11.64, 11.76] | 20.13 [19.77, 20.54] | 40.2 |

E3 knees: image 75 streams per pod (the E3 sizing: 14 pods for ~800 streams), venv 125. The E3 image's interpreter
cost +11-13% CPU per turn and +0.7-1.5 ms p50; the Wave F rebuild removes that gap (section 2.1).

## 3. CPU per turn, memory, capacity, datastores (per 2-vCPU pod)

| Quantity | Wave F image (deployment) | E3 image / venv | Source |
|---|---|---|---|
| CPU per turn at the knee (pod, both processes) | **41.6-41.7 ms** at 125 streams (41-44 ms over 75-150) | 48.1 ms at 75 (image) / 41.1 ms at 125 (venv) | `wf-l1-b/c`; `e3-l1-img2`, `e3-l1b` |
| pod CPU at the knee | 0.75 cores (18.0 turns/s) | 0.52 (image, 75) / 0.74 (venv, 125) | same |
| pod CPU at the planned 100 streams | 0.62 cores (14.5 turns/s), ~6.2 millicores per stream | 0.61 (venv) | `wf-l1-a`; `e3-l1a` |
| RSS (pod, 2 processes) at the knee | 621-624 MiB | 506 MiB (image, 75) / 640 MiB (venv, 125) | 5 s samples |
| RSS, heavy streams (C3: 180 streams + 20 x 90 s / 126 KB/s turns) | - | 2.3-2.8 GB (venv; sealed logs held for the 300 s resume window) | `e3-c3` |
| throughput ceiling (per-process cap 120 = 240 running turns) | - | **34.6-34.7 turns/s** from 300 streams (venv); 429 `queue_timeout` past it, 0 x 5xx to 1,000 streams per pod | `e3-ramp` |
| Postgres connections | 40 per pod (2 x `DB_POOL_MAX` 20) | at most 1 checked out at any step-end scrape, acquire wait 0.03-0.06 ms mean up to 1,000 streams | `e3-ramp` (R4 metrics) |
| Redis connections | at most 2 x 256 (bounded pool) | 5-7 in use per process, acquire 0.02 ms mean | same |
| datastore latency | - | Postgres unit of work 2.3 -> 5.1 ms mean, Redis command 0.29 -> 0.85 ms mean (50 -> 1,000 streams) | same |

(E3 results files: `reports/qa/e3/qa-lead/load/<run>.json`; Wave F: `reports/perf/wf-infra/load/<run>.json`.)

## 4. Processes per pod (measured, E3)

Four api processes on the same 2 vCPU (E3 image, `e3-l1-img4p`) do not move the knee: p50 10.16 / 10.81 / 11.49 ms
at 100 / 125 / 150 streams per pod (2 processes: 10.35 / 10.81 / 11.62), for **+12-14% CPU per turn** and **~+50%
RSS**. The two-process pod (IMPL-087) stays; not re-measured on the Wave F image.

## 5. Recommended 08 §9 sizing (final table; replaces the `api` row, the other rows as in 08 §9)

| Component | Starting size | Scale signal |
|---|---|---|
| `api` pods | N x (2 vCPU, 4 GiB), **2 api processes each** (`WEB_CONCURRENCY=2`, IMPL-087), at most **125 concurrent streams per pod** (the measured knee of the shipped image in the container), planned at **100 per pod** (80% of the knee): N = ceil(peak concurrent streams / 100). For the design's ~800 concurrent streams: **8 pods** (7 at the knee itself; E3: 14; revision 6: 4 x ~200). `NOEMA_MAX_TURNS_PER_PROCESS` stays 120: an overload guard (240 per pod, 1.9x the knee), not the SLO | **Scale out**: `sum by (pod) (aghoo_turn_slots_held)` > 100, or pod CPU > 0.62 vCPU sustained (100 streams x ~6.2 millicores at this workload). **Alarm** (under-provisioned): any increase of `aghoo_turns_shed_total` (a process at its cap), event-loop lag p99 > 50 ms (C2 bound) |
| `jobs` workers | 2 x (1 vCPU, 2 GiB) (not measured) | Queue age > 5 min |
| `media` sandbox workers | 4 x (2 vCPU, 4 GiB) (not measured) | Queue depth; OCR CPU |
| Postgres | 2 vCPU / 8 GiB, Multi-AZ | Connections, p95 query latency > 20 ms. 40 connections per api pod (2 x `DB_POOL_MAX` 20): 320 at 8 pods, plus the jobs and media workers |
| Redis / Valkey | 1 GiB, replica | Memory, ops/s. At most 2 x 256 connections per api pod (`NOEMA_REDIS_MAX_CONNECTIONS`, IMPL-088); 5-7 per process in use (E3) |

**The streams-per-pod signal** (Wave F): `aghoo_turn_slots_held` (gauge, no labels) = `TurnSlots.held()` of one api
process: its running turns, its admitted createTurns not yet running and its createDecision / facade calls in flight
(`noema.api.overload`), set every 1 s by `export_held` (spawned in the api lifespan, `noema.api.app`). Each uvicorn
worker serves its own metrics port (9464, 9465), so **sum it per pod**. In every L1 step the per-pod sum at the
step-end scrape equaled the streams per pod (section 2.1); the split between the two processes is uneven (e.g. 27 +
48 at 75), so scale on the sum, never on one process. Decision-heavy traffic counts its decisions in flight too; the
125 knee is the turn workload's.

## 6. The SLO at the sized density (Wave F image, in the container)

| SLO / gate | At the knee, 125 per pod (`wf-l1-b` / `wf-l1-c`) | At the planned 100 per pod (`wf-l1-a`) | Measured elsewhere (E3, venv) |
|---|---|---|---|
| L1 TTFT overhead p50 <= 10 ms | **9.72 [9.69, 9.75] / 9.73 [9.71, 9.77]** | **9.30 [9.27, 9.32]** | - |
| L1 TTFT overhead p95 <= 25 ms | **14.06 [13.88, 14.23] / 13.98 [13.83, 14.12]** (p99 21.9 / 20.6) | **13.20 [13.02, 13.43]** (p99 20.9) | 20.13 at 200 per pod |
| e2e overhead p50 / p95 | 16.4 / 22.5; 16.6 / 22.9 ms | 15.9 / 22.7 ms | - |
| C2 event-loop lag p99 < 50 ms | 4.1 / 4.0 ms | 3.8 ms | 5.1 ms at 200, 6.7 ms at 1,000 per pod |
| C5 victim p95 ratio <= 1.10 under a 5x flood | - | - | **1.021 / 1.022** at 100 victim streams per pod |
| C3 others' p95 ratio <= 1.10 with slow clients; queue <= 1 MiB | - | - | **0.998**, 1,048,270 B at 200 per pod |
| C4 RSS growth <= 5% in 1 h | - | - | **+0.76%** at 150 per pod |
| pod CPU / RSS | 0.75 vCPU / 621-624 MiB | 0.62 vCPU / 562 MiB | - |

C3, C4 and C5 ran on the venv in E3 ([e3/qa-lead §3](../qa/e3/qa-lead.md)), whose CPU per turn and p50 the Wave F
image now matches (section 2.1); they were not re-run in the container.

## 7. Remaining performance risk (by weight)

1. **The p50 budget is most of the bound at low load.** The floor at 75 streams per pod is 8.74 ms of the 10 ms
   (ingress p50 8.1 ms); each 25 streams per pod adds 0.4-0.6 ms. Any work added to the ingress path (auth,
   admission, idempotency, Postgres, Redis) moves the knee down by roughly 25 streams per 0.5 ms. Owners: api, perf.
2. **Hardware of the measurement.** The pod had two dedicated physical cores (their hyper-thread siblings idle) on a
   quiet host. A cloud 2-vCPU slice is usually one core with two hyper-threads, with neighbours: expect a lower knee.
   Re-measure on the target instance type before fixing N (Phase 0/1 load test, 08 §9's own caveat).
3. **Workload dependence.** The knee is the L1 scenario's: `router-default` 15 decision calls, one 400-token stream at
   60 tokens/s, no escalation, verification, media or race. Heavier fan-out, faster token rates and live TLS to
   Bedrock (the wire mock is plain HTTP) add CPU per turn and lower the knee. Size from production telemetry once
   real traffic exists.
4. **One pod, dedicated datastores.** Production shares Postgres and Valkey across N pods. Per turn ~14.5 Redis
   commands or pipelines and ~2.5 Postgres units of work ([e3/misc-fixes §4](../qa/e3/misc-fixes.md)): ~1.7k Redis
   commands/s and ~290 Postgres units/s at 800 streams (~115 turns/s), small for the 08 §9 sizes, but both are on the
   ingress path (Postgres unit 2.3 -> 5.1 ms mean as one pod ramps 50 -> 1,000 streams, E3).
5. **The cap sits above the knee.** 120 per process = 240 per pod, 1.9x the knee (E3: 3.2x): past the knee the
   overhead grows smoothly (venv p50 11.7 ms at 200, 16.1 ms at 1,000 streams per pod, E3) before anything is shed,
   so the autoscaler must act on the scale signal, not on sheds.
6. **The interpreter is now a python-build-standalone release** (section 8): its bundled OpenSSL, SQLite, zlib, expat,
   libffi, xz, bzip2, ncurses and libedit are statically linked and invisible to trivy's Debian scan; a CVE in them needs a
   `PYTHON_VERSION` / `UV_IMAGE` bump in `deploy/docker/api.Dockerfile`, not an `apt-get upgrade`. Owner: infra.
7. **GC is not a risk at the knee** (full GC 31-38 ms p50, max 51 ms, every 30-38 s per process at 125). Out of scope
   here: the 24 h soak (C4 ran 1 h, E3), and the multi-pod Redis-down divisor (`NOEMA_EXPECTED_PODS x
   WEB_CONCURRENCY`) at N = 8.

Closed in Wave F: the E3 risk "the image's interpreter costs 40% of the per-pod capacity" (the rebuilt image reaches
the venv's knee, section 2.1) and "no exported streams-per-pod signal" (`aghoo_turn_slots_held`, section 5).

## 8. The image (Wave F rebuild of `deploy/docker/api.Dockerfile`; noema-worker derives from it)

| Item | Wave F `noema-api:wfinfra` | E3 `noema-api:qae3` |
|---|---|---|
| Base | `debian:trixie-slim@sha256:a29215f6...` (Debian 13.7), `apt-get upgrade`, + `ca-certificates netbase tzdata` | `python:3.12-slim-trixie@sha256:a6e34c59...` (Debian 13.7), `apt-get upgrade` |
| Interpreter | CPython 3.12.15 from python-build-standalone (clang 22: PGO + ThinLTO + BOLT, static libpython), `uv python install` (uv 0.13.0, the download checksum-verified by uv) into `/opt/python`; Tcl/Tk, the GUI modules, pip, the headers and the shared libpython removed | CPython 3.12.15 of the official image (GCC 14.2, shared libpython) |
| Statically bundled libraries | OpenSSL 3.5.9, SQLite 3.53.1, zlib 1.3.2, expat 2.9.0, ncurses 6.5, libedit | Debian packages (seen by trivy) |
| venv `/opt/noema/venv` | the same 66 distributions as `qae3` (`importlib.metadata`, diffed); no dev group, no media group, no tests | - |
| User, read-only root | uid 10001:10002; on `--read-only` every module loads from bytecode (`python -v`: no stale `.pyc`) | same |
| Size | 372 MB | 356 MB |
| trivy (`make image-scan`, 0.75.0) | **0 CRITICAL**; 43 HIGH, all waived (the same 8 CVE ids as E3; `libncursesw6` left the image); 0 Python package findings | 0 CRITICAL; 44 HIGH (8 ids); 6 MEDIUM/LOW in the base image's pip |
| Results, SBOM (CycloneDX) | `reports/perf/wf-infra/image-scan/trivy-noema-{api,worker,media}.json`, `.../sbom/noema-{api,worker,media}.cdx.json` | `reports/qa/e3/qa-lead/security/` |

TLS trust is unchanged: the adapters load certifi's bundle (botocore's `get_cert_path`, httpx's
`create_ssl_context`), 121 CAs in the image; `ca-certificates` covers the interpreter's default paths.

## 9. Reproduce (the commands of this revision)

```
make images IMAGE_TAG=wfinfra
make image-scan IMAGE_TAG=wfinfra IMAGE_SCAN_OUT=reports/perf/wf-infra/image-scan
# the own stack (base services), seeded, load config v2
env NOEMA_PG_PORT=35032 NOEMA_MOTO_PORT=35050 NOEMA_VALKEY_PORT=35079 \
    docker compose -p noema-wfinfra-perf -f deploy/compose/docker-compose.yml up -d --wait postgres valkey moto
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tools.bootstrap_dev --compose-project noema-wfinfra-perf \
    --state ~/.noema/stacks/noema-wfinfra-perf.json
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.load.stack activate --state ~/.noema/stacks/noema-wfinfra-perf.json
# one run; the runner script reports/perf/wf-infra/load/run.sh (run from /tmp/noema-wf-infra/) wraps it with the
# quiet-host wait and the loadavg monitor; a, b, c differ only in --plan
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.load.run --state ~/.noema/stacks/noema-wfinfra-perf.json \
    --out /tmp/noema-wf-infra/runs/wf-l1-b --api-port 35090 --mock-port 35081 --api-cpus 46-47 --api-procs 2 \
    --stack-cpus 44-45 --mock-cpus 16,40,17,41,18,42,19,43 --driver-cpus 4-15,28-39 \
    --api-image noema-api:wfinfra --plan 125:600,150:500 --warmup-s 60 --settle-s 30 \
    --results /tmp/noema-wf-infra/results/wf-l1-b.json   # copied to reports/perf/wf-infra/load/
```

## 10. Load-tooling changes of Wave F (measured)

- **Driver keep-alive** (`tests/load/driver.py`): idle pooled connections now expire after 4 s, below uvicorn's
  `timeout_keep_alive` of 5 s (E3 qa-lead finding 7: equal expiries let a request reuse a connection the server is
  closing; 1-4 transport `ReadError`s per C5 run). No transport error in any Wave F L1 step; the C5 and ramp runs
  where E3 saw them were not repeated. Self-test: `pytest tests/load/test_harness.py`.
- **Pinning the compose mock of the gates stack** (E3 qa-lead finding 6; `python -m tools.mockbedrock --cpus`, or
  `NOEMA_MOCK_CPUS`, set from `QA_GATES_MOCK_CPUS` through the compose variable `NOEMA_STACK_MOCK_CPUS`). `make
  qa-load-holdback` on the own stack `noema-wfinfra-gates` (images `:wfinfra`), three runs back to back, every bound
  met in each (13 passed):

| Mock placement | Cells invalid by mock lateness (p99 > 2 ms) | Lateness p99 per cell: median (range) | Results |
|---|---|---|---|
| pinned to CPUs 44-45 | 10 of 13 | 2.28 ms (1.23-132.8; the first cell is the stack's warm-up) | `reports/perf/wf-infra/gates/holdback-pinned-1.json` |
| unpinned | 3 of 13 | 1.55 ms (1.23-28.7) | `.../holdback-unpinned.json` |
| pinned to CPUs 44-45 again | 6 of 13 | 1.83 ms (1.23-61.3) | `.../holdback-pinned-2.json` |

  The mock used at most 0.13 of a core (5 s samples of its process: `reports/perf/wf-infra/gates/mockcpu-unpinned.txt`
  and `mockcpu-pinned2.txt`, sampler `mockcpu.sh`; the first pinned run was not sampled), so it is not CPU-bound; a likely cause (not
  measured) is that pinned to two CPUs other processes may still use, it waits for them instead of waking on an idle
  core. **`make qa-gates` therefore
  leaves the mock unpinned by default** (`QA_GATES_MOCK_CPUS` empty); pin only to CPUs isolated from the rest of the
  host. (E2 and E3, unpinned: 3 and 7 of 13 invalid.)
