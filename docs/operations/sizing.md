# Sizing

The measured basis is [reports/perf/sizing.md](../../reports/perf/sizing.md) (method, raw tables, risks and the
reproduce commands). This page is the operator's summary; when the two differ, the report wins. The owner decision
of 2026-10-12 sizes the pods from the measured L1 knee, not from the design's ~200 streams per pod. The numbers
below are the Wave F measurement **in the container** on the shipped image (`noema-api:wfinfra`, python-build-standalone
CPython 3.12.15; report sections 2.1, 5 and 6); the Wave E3 numbers are kept in the report as history (section 2.2).

## 1. Recommendation (report §5)

| Component | Size | Scale signal |
|---|---|---|
| `api` pods | N x (2 vCPU, 4 GiB), **2 api processes each** (`WEB_CONCURRENCY=2`); at most **125 concurrent streams per pod** (the knee of the shipped image, two runs), planned at **100** (80% of the knee): N = ceil(peak concurrent streams / 100); **8 pods** for the design's ~800 streams (Wave E3 image: 14) | out above 100 streams per pod (`sum by (pod) (aghoo_turn_slots_held)`) or 0.62 vCPU per pod sustained; under-provisioned on any shed (`aghoo_turns_shed_total`) or event-loop lag p99 > 50 ms |
| Postgres | 2 vCPU / 8 GiB, Multi-AZ (design 08 §9) | connections; p95 query latency > 20 ms |
| Redis / Valkey | 1 GiB with a replica (08 §9) | memory, ops/s |
| `jobs` | 2 x (1 vCPU, 2 GiB) (08 §9; not measured) | queue age > 5 min |
| `media` sandbox | 4 x (2 vCPU, 4 GiB) (08 §9; not measured) | queue depth; OCR CPU |

The alerts that carry these signals are in [runbooks.md](runbooks.md): [noema-pod-streams-high](runbooks.md#noema-pod-streams-high),
[noema-pod-cpu-high](runbooks.md#noema-pod-cpu-high), [noema-turns-shed](runbooks.md#noema-turns-shed),
[noema-event-loop-lag-high](runbooks.md#noema-event-loop-lag-high), [noema-postgres-latency-high](runbooks.md#noema-postgres-latency-high),
[noema-redis-latency-high](runbooks.md#noema-redis-latency-high). Their thresholds must equal the plan of this
page: when the plan changes, change the rules in `deploy/observability/prometheus/rules/` with it.

"Streams per pod" is concurrent turns per pod: `aghoo_turn_slots_held` is a gauge per api process (its running and
admitted turns plus its createDecision and facade calls in flight, set every second); each worker serves its own
metrics port (9464, 9465), so sum it per pod and never scale on one process (the split is uneven, e.g. 27 + 48 at
75 streams). In every L1 step the per-pod sum equaled the streams per pod (report §2.1). Keep
`NOEMA_MAX_TURNS_PER_PROCESS` at 120: 240 per pod is an overload guard 1.9x above the knee, not the SLO, so the
autoscaler must act on the scale signals, not on sheds (report §7 item 5).

## 2. What the numbers are (report §2.1, §3, §6)

| Per 2-vCPU pod, Wave F image | At the planned 100 streams | At the knee, 125 streams (two runs) |
|---|---|---|
| TTFT overhead p50 (SLO 10 ms, upper 95% CI) | 9.30 [9.27, 9.32] ms | 9.72 [9.69, 9.75] / 9.73 [9.71, 9.77] ms |
| TTFT overhead p95 (SLO 25 ms) | 13.20 [13.02, 13.43] ms | 14.06 [13.88, 14.23] / 13.98 [13.83, 14.12] ms |
| event-loop lag p99 (C2 bound 50 ms) | 3.8 ms | 4.1 / 4.0 ms |
| pod CPU; CPU per turn | 0.62 vCPU (~6.2 millicores per stream) | 0.75 vCPU; 41.6-41.7 ms |
| pod RSS (2 processes) | 562 MiB | 621-624 MiB |

At 150 streams per pod p50 misses (10.21 [10.18, 10.26] ms), so the true knee lies in [125, 150). The knee is a
latency knee at low CPU: p50 is the binding bound (a floor of ~8.7 ms at 75 streams, ingress 8.1 ms of it, then
+0.4-0.6 ms per 25 streams); p95 passes everywhere measured. The rebuilt image matches the dev venv's CPU per turn
and saves 9-11% CPU per turn and 0.9-1.4 ms of p50 against the Wave E3 image. The gate L1 as written (200 streams
per pod) was measured on the venv in E3: p50 11.70 ms, a FAIL ([reports/qa/wave-e-summary.md](../../reports/qa/wave-e-summary.md)).

Measured in Wave E3 only (on the venv, whose CPU per turn the Wave F image now matches; not re-run in the
container): capacity 34.6-34.7 turns/s per pod at the cap, then 429 `queue_timeout` with 0 x 5xx up to 1,000
streams; C5 fairness 1.021 / 1.022; C3 slow clients 0.998; C4 1 h RSS +0.76% (report §3, §6).

## 3. Datastore connections (report §3, §5)

| Store | Per api pod | At 8 pods | Measured use (E3) |
|---|---|---|---|
| Postgres | 40 (2 processes x pool max 20) | 320, plus jobs and media workers | at most 1 of 20 per process checked out, acquire 0.03-0.06 ms, up to 1,000 streams per pod |
| Redis | at most 2 x 256 (`NOEMA_REDIS_MAX_CONNECTIONS`) | up to 4,096 | 5-7 in use per process, acquire 0.02 ms |

Set Postgres `max_connections` and Valkey `maxclients` for the totals. Per turn the api does ~14.5 Redis commands
or pipelines and ~2.5 Postgres units of work: ~1.7k Redis commands/s and ~290 Postgres units/s at 800 streams
(report §7 item 4).

## 4. Caveats before fixing N (report §7)

1. The measurement pod had two dedicated physical cores on a quiet host; a cloud 2-vCPU slice is usually one core
   with two hyper-threads and neighbours: expect a lower knee. Re-measure on the target instance type.
2. The knee is the L1 workload's (`router-default`, 15 decision calls, one 400-token stream at 60 tokens/s, no
   escalation, verification, media or race, plain HTTP to the mock). Heavier turns and TLS to Bedrock lower it.
3. One pod on dedicated datastores was measured; multi-pod load on shared Postgres and Valkey was not run.
4. The 24 h soak was not run (C4 ran 1 h in E3); four processes per pod were measured on the E3 image only (no
   knee gain, +12-14% CPU per turn, ~+50% RSS: the pod keeps two).
5. The interpreter's bundled libraries (OpenSSL, SQLite, zlib and others) are invisible to trivy's Debian scan; a
   CVE in them needs a `PYTHON_VERSION` or `UV_IMAGE` bump ([SECURITY.md](../../SECURITY.md) section 3).

To re-measure: the load harness (`python -m tests.load.run ... --api-image noema-api:<tag> --api-procs 2`; the exact
commands are report §9 and [tests/load/README.md](../../tests/load/README.md); not run while writing this page).
