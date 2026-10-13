# Observability: Prometheus rules and Grafana dashboards

Noema's alerts, recording rules and dashboards live as code in `deploy/observability/`. Every metric they query is
declared in `src/noema/telemetry/registry.py`, which a unit test enforces. Each alert links to its runbook in
[runbooks.md](runbooks.md) (anchor = alert name). This page covers deployment, every alert, every panel, and what
the current metrics cannot show yet ([section 6](#6-gaps-and-limits)).

## 1. Files

| Path | Contents |
|---|---|
| `deploy/observability/prometheus/rules/noema-recording.rules.yml` | 19 recording rules (`noema:<metric>:<operation>`): SLO ratios, the overhead proxy, per-pod capacity, decisions, cost, datastores |
| `deploy/observability/prometheus/rules/noema-alerts-*.rules.yml` | 25 alerts in 6 groups (SLO, capacity, decisions, upstream, outbox, datastores) |
| `deploy/observability/prometheus/tests/*.test.yml` | `promtool test rules` unit tests: 53 alert cases (every alert fires in at least one; 27 assert no alert) and 3 recording-rule cases |
| `deploy/observability/prometheus/prometheus.example.yml` | Example Kubernetes scrape configuration that produces the labels the rules need |
| `deploy/observability/prometheus/prometheus.compose.yml` | Scrape configuration for the compose stack (`make e2e-up`), section 2.6 |
| `deploy/observability/grafana/build_dashboards.py` | Source of the 7 dashboards (panels, queries, descriptions) |
| `deploy/observability/grafana/dashboards/*.json` | The generated dashboards (committed; `make observability-dashboards` regenerates them) |
| `deploy/observability/grafana/provisioning/noema-dashboards.yml` | Grafana file provider (folder "Noema") |
| `tests/unit/observability/` | Registry, alert-hygiene, dashboard and documentation checks (run by `make check` and `make observability-check`) |

## 2. Deploy

### 2.1 Scrape contract

The rules depend on these labels, which the scrape configuration must add (as `prometheus.example.yml` does):

| Requirement | Why |
|---|---|
| One target per process | Every process serves its own `/metrics` on the internal port `NOEMA_METRICS_PORT`. An api pod with `WEB_CONCURRENCY=2` has 2 processes, and the second serves `NOEMA_METRICS_PORT + 1` (spec-05 §5). Scrape both ports: per-process gauges such as `aghoo_turn_slots_held` are summed per pod. |
| `pod` label = the pod name | Per-pod rules (`sum by (pod)`): streams, CPU, event-loop lag, sheds, pools |
| `job` label = `noema-api`, `noema-jobs` or `noema-media-worker` | The event-loop lag rule and the pod-CPU join select `job="noema-api"` |
| cAdvisor `container_cpu_usage_seconds_total` with `pod` and `container` labels | Pod CPU (`noema:pod_cpu_cores:rate5m`) |
| One Noema deployment per Prometheus | Cluster-wide ratios (`sum(...)`) are not split by environment |

`/metrics` is unauthenticated, so never expose it on the API port (spec-09). Prometheus Operator users can use a
PodMonitor that sets `jobLabel` and keeps the `metrics*` ports. The operator's default `pod` and `namespace`
labels fit the contract. Wrap the rule groups in a `PrometheusRule` object.

Phase 1 ships no Kubernetes manifests, so `prometheus.example.yml`'s selectors are requirements on yours: pod
labels `app.kubernetes.io/part-of=noema` and `app.kubernetes.io/component` = `api`, `jobs` or `media-worker`, and
container ports named `metrics` (`NOEMA_METRICS_PORT`) and `metrics-2` (the second api worker's port). Neither
the example nor these labels were run against a cluster.

### 2.2 Prometheus

1. Copy `deploy/observability/prometheus/rules/*.rules.yml` to the Prometheus server, e.g. `/etc/prometheus/rules/`.
2. Reference them with `rule_files: [/etc/prometheus/rules/noema-*.rules.yml]` and keep `evaluation_interval` at
   1m (the windows assume it).
3. Reload Prometheus (`SIGHUP`, or `POST /-/reload` with `--web.enable-lifecycle`).

The rules are checked with `promtool` from `prom/prometheus:v3.5.0` (`PROMETHEUS_IMAGE` in
`make/observability.mk`). They use no Prometheus 3-only syntax, but only v3.5.0 was run.

### 2.3 Alertmanager

Every alert carries `severity` (`critical` = page, `warning` = ticket) and the annotations `summary`,
`description` and `runbook_url`. `runbook_url` is repo-relative (`docs/operations/runbooks.md#<alert>`). Prefix it
with your repository or docs URL in the notification template. Route on `severity`.

### 2.4 Grafana

The dashboards use a `datasource` variable (type Prometheus), so they work with any datasource name.

- **Provisioning**: mount `deploy/observability/grafana/dashboards/` at `/var/lib/grafana/dashboards/noema` and
  `grafana/provisioning/noema-dashboards.yml` under `/etc/grafana/provisioning/dashboards/`. The dashboards are read-only
  in the UI (`allowUiUpdates: false`): change `build_dashboards.py`, then run `make observability-dashboards`.
- **Import**: in the UI, Dashboards > New > Import, then upload each JSON file.

Verified on 2026-10-12: Grafana `grafana/grafana:12.1.1` with this provisioning file loaded all 7 dashboards
(`/api/search?tag=noema` returned 7, in folder "Noema") and logged no provisioning errors. This was a one-off
check, not a make target.

### 2.5 Validate and change

| Command | What it does |
|---|---|
| `make observability-check` | `promtool check rules --lint=all` on every rule file; `promtool test rules` on every test file; `promtool check config --syntax-only` on the example config; every dashboard query parsed by `promtool check rules` (`build_dashboards.py --promql-rules`); then `pytest tests/unit/observability`, including the runbook-anchor check (`NOEMA_OBS_RUNBOOKS=1`) |
| `make observability-dashboards` | Regenerates `grafana/dashboards/*.json` from `build_dashboards.py` |
| `make check` | Runs `tests/unit/observability` with the other unit tests, without the promtool steps and the runbook-anchor check: run `make observability-check` too before a wave is tagged ([CONTRIBUTING.md](../../CONTRIBUTING.md) section 6) |

The unit tests assert the following:
- Every `aghoo_*` name in a rule or panel is in the registry with its kind and labels. Counters and histogram series
  are only used inside `rate()`/`increase()` over a range (the one exception is the first-sample clause
  `c unless increase(c[...] offset ...)`, section 6), and gauge ranges only inside `*_over_time`.
- Grouping, legend and `$labels` names exist on the queried metrics.
- Every `noema:*` name is a recording rule.
- The committed JSON equals the builder's output, panels do not overlap, and every panel has a description, a unit
  and the `${datasource}` variable.
- Alert names are kebab-case and `runbook_url` is `docs/operations/runbooks.md#<alert>`.
- Every alert, dashboard and panel title appears on this page.

To change a threshold, edit the rule file and the matching `thr=` in `build_dashboards.py` (the red line on the
panel), regenerate, and run `make observability-check`. If `reports/perf/sizing.md` is re-measured, update the
sizing thresholds (100 streams, 0.62 vCPU: Wave F, sizing §5) the same way, with the promtool cases that test them.

### 2.6 Compose stack

`prometheus.compose.yml` scrapes a stack started with `make e2e-up` from inside its network: `api-1` and `api-2`
on ports 9464 and 9465 (`job="noema-api"`, `pod` = the service name), `jobs:9464` and `media-worker:9464` (the
compose file publishes no port for these two). Compose has no cAdvisor, so the pod-CPU rule and alert have no
data there. Run on 2026-10-12 against the stack `noema-wffix` (images `:wfinfra`):

```bash
docker run -d --name <project>-prometheus --network <project>_default -p 127.0.0.1:<port>:9090 \
  -v $PWD/deploy/observability/prometheus/prometheus.compose.yml:/etc/prometheus/prometheus.yml:ro \
  -v $PWD/deploy/observability/prometheus/rules:/etc/prometheus/rules:ro prom/prometheus:v3.5.0
docker rm -f <project>-prometheus     # before make e2e-down, which removes the network
```

Result after 81 turns ([reports/qa/wave-f/prometheus-e2e.json](../../reports/qa/wave-f/prometheus-e2e.json)): all
6 targets up; all 44 rules (25 alerts, 19 recording rules) health `ok`; `noema:pod_turn_slots_held:sum` returned
`api-1` and `api-2`, the availability ratios 0, the overhead proxy 3.7 ms, the event-loop lag p99 per pod and the
cost per turn; `noema:pod_cpu_cores:rate5m` was empty (no cAdvisor); no alert fired. The config passed
`promtool check config --syntax-only` (run by hand; `make observability-check` checks only the Kubernetes example).

## 3. Alerts

Each alert name is also its runbook anchor: `docs/operations/runbooks.md#<alert>`. Every threshold cites its
source. "E3" numbers are the Wave E3 measurements in [reports/perf/sizing.md](../../reports/perf/sizing.md) and
[reports/qa/wave-e-summary.md](../../reports/qa/wave-e-summary.md).

### 3.1 SLO (`noema-slo.alerts`)

| Alert | Severity | Fires when | Threshold source | Meaning |
|---|---|---|---|---|
| `noema-availability-slo-fast-burn` | critical | Shed ratio > 14.4 x 0.001 over 1 h and over 5 min, for 2 min | design 08 §7 SLO 99.9% monthly; multiwindow burn rate (14.4x = 2% of the budget per hour) | Clients get 429 `queue_timeout` fast enough to spend the month's budget in about 2 days |
| `noema-availability-slo-slow-burn` | warning | Shed ratio > 6 x 0.001 over 6 h and over 30 min, for 15 min | same (6x = 5% of the budget in 6 h) | Steady shedding that spends the budget in about 5 days |
| `noema-proxy-overhead-high` | warning | Overhead proxy (5 min mean) > 25 ms, for 10 min | design 08 §7 alert "Proxy overhead p95 > 25 ms (10 min)" | Gateway time per turn may be above the SLO bound. At the Wave F knee the L1 TTFT overhead p95 was 14.06 / 13.98 ms (sizing §6). The proxy over-reads (section 6): rule out slow decisions first |
| `noema-overhead-slo-burn` | warning | Overhead proxy > 25 ms over 1 h and over 5 min, for 5 min | design 08 §7 SLO p95 <= 25 ms | The overhead SLO may have been missed for a sustained period. A ticket, not a page, until a gateway-overhead histogram exists (section 6) |

The availability SLI is the share of requests shed at the per-process cap. The overhead proxy is mean TTFT minus
mean model first text on holdback turns; it includes the holdback wait and spec-restart time, so it reads high.
Both are proxies; see [section 6](#6-gaps-and-limits).

### 3.2 Capacity (`noema-capacity.alerts`)

| Alert | Severity | Fires when | Threshold source | Meaning |
|---|---|---|---|---|
| `noema-pod-streams-high` | warning | `sum by (pod) (aghoo_turn_slots_held)` > 100, for 5 min | sizing §5: planned 100 streams per pod, 80% of the 125-stream knee of the Wave F image | Scale out. Past the knee the TTFT overhead p50 exceeds 10 ms |
| `noema-pod-cpu-high` | warning | api pod CPU > 0.62 cores, for 10 min | sizing §5: 100 streams x ~6.2 millicores per stream (0.617 vCPU measured at 100, sizing §2.1) | Scale out. The CPU per stream depends on the workload |
| `noema-event-loop-lag-high` | critical | Worst api process of a pod: lag p99 over 5 min > 50 ms, for 5 min | C2 bound (wave-e summary C2); 4.0-4.1 ms at the knee (sizing §6) | Every stream on that process is delayed: the pod is under-provisioned |
| `noema-turns-shed` | warning | Any increase of `aghoo_turns_shed_total` on a pod in 5 min, a process's first shed included (section 6) | sizing §5 "Alarm: any increase" | A process is at `NOEMA_MAX_TURNS_PER_PROCESS` (120): under-provisioned, or one tenant is flooding |

### 3.3 Decisions, calibration, labeling (`noema-decisions.alerts`)

| Alert | Severity | Fires when | Threshold source | Meaning |
|---|---|---|---|---|
| `noema-decision-quorum-slow` | warning | Quorum-reached p95 per decision profile > 1 s, for 10 min | design 08 §7 "commit-critical quorum p95 > the deadline"; default deadline 1,000 ms (06 §4 L4) | Decisions are late for the commit point. Profiles run with a longer deadline (the hierarchical example uses 3,000 ms, design 08 §3.4) need their own threshold |
| `noema-spec-restart-rate-high` | warning | Spec restarts / turns over 1 h > 10%, for 30 min | design 08 §6 and §7 | Stickiness is ineffective or conditioning is too chatty. The E3 replay's 0.23 is synthetic (wave-e summary R4) |
| `noema-member-parse-errors-high` | warning | A member's parse errors / its results over 30 min > 1%, for 15 min | design 08 §6 and §7 | Model or template drift for that member (model alias) |
| `noema-deepseek-p99-high` | warning | DeepSeek member p99 over 10 min > 1.5 s, for 10 min | design 08 §6; L5 measured 929.5 ms with backups (wave-e summary) | Tail latency of the DeepSeek member. **Inert today**: see section 6 |
| `noema-calibrator-ece-drift` | warning | The weekly drift job downgraded an active calibrator in the last day (`aghoo_calibrator_downgrades_total` by `reason`, the first downgrade included) | ADR-003 `drift_ece_max` 0.08 (06 §2.3): reason `drift_ece`; `drift_ci` = the ECE lower bound above `ece_max` | Calibration drift: the calibrator now serves `uncalibrated`. Not the `aghoo_calibration_ece` gauge, which a rejected fit's holdout ECE also sets |
| `noema-label-consensus-drop` | warning | A class's consensus < its average over the previous week (7 days, offset 1 day) minus 0.10 | design 08 §7 "consensus-rate drop > 10 pts" | Ambiguous class definition, labeler drift or poisoning (design 08 §6) |

### 3.4 Upstream, generation, cost (`noema-upstream.alerts`)

| Alert | Severity | Fires when | Threshold source | Meaning |
|---|---|---|---|---|
| `noema-circuit-open` | warning | `aghoo_circuit_state` = 2 (open) for a model and region, for 1 min | design 08 §7 "circuit open" | Traffic shifts to fallback members automatically. The breaker closes after the half-open probes pass |
| `noema-bedrock-throttling` | warning | Any ThrottlingException rate for a tenant and model, for 10 min | design 08 §7 "throttles > 2% per tenant" (no per-tenant call count: section 6); C1 measured 0 upstream throttles | Admission should throttle before Bedrock does, so the configured quota is above the account's real quota |
| `noema-stuck-attempts` | warning | Any increase of `aghoo_stuck_attempts_total` on a pod in 15 min, the first included | spec-11 §6; the C4 soak gate expects 0 (wave-e summary C4) | Attempts outlived their turn and were marked `cancelled(deadline)` with estimated usage |
| `noema-cost-per-turn-high` | warning | Recorded cost per turn over 1 h > 0.05 USD, for 1 h | design 08 §7 "cost per turn > budget". **0.05 USD is a placeholder**: no budget is configured anywhere | Spend per turn is above budget. Set the value per deployment |

### 3.5 Outbox (`noema-outbox.alerts`)

| Alert | Severity | Fires when | Threshold source | Meaning |
|---|---|---|---|---|
| `noema-outbox-records-dropped` | critical | Any increase of `aghoo_outbox_dropped_total` on a pod in 5 min, the first included | spec-11 §3 ("an ERROR log that pages") | Usage lines, audit events or turn finalizes were lost |
| `noema-outbox-memory-backlog` | critical | `aghoo_outbox_backlog{sink="memory"}` never reached 0 for 5 min | spec-11 §3 "both sinks down ... alert" | Postgres and Redis are both unreachable. The records are lost if the process stops |
| `noema-outbox-redis-backlog` | warning | `aghoo_outbox_backlog{sink="redis"}` never reached 0 for 15 min | spec-11 §3 drainer | Postgres is down or rejects records. Records dead-letter after 10 deliveries |

### 3.6 Datastores (`noema-datastores.alerts`)

| Alert | Severity | Fires when | Threshold source | Meaning |
|---|---|---|---|---|
| `noema-postgres-pool-saturated` | warning | A process's pool in use / size (5 min average) >= 0.9, for 5 min | operational default; E3 used at most 1 of 20 (sizing §3) | Requests queue for connections (acquire wait on the Datastores dashboard) |
| `noema-postgres-latency-high` | warning | Unit-of-work p95 over 5 min > 20 ms, for 10 min | design 08 §9 Postgres scale signal (p95 > 20 ms); E3 mean 2.3-5.1 ms (sizing §3) | Postgres is slow on the ingress path. The metric runs from acquire to release, so it reads high against pure query time |
| `noema-redis-pool-saturated` | warning | Redis pool in use / max (5 min average) >= 0.9, for 5 min | operational default; E3 used 5-7 of 256 (sizing §3) | The bounded pool (IMPL-088) is nearly exhausted |
| `noema-redis-latency-high` | warning | Redis command p99 over 5 min > 50 ms, for 10 min | spec-11 §1.2: at 50 ms admission treats Redis as down; E3 mean 0.29-0.85 ms (sizing §3) | Redis is close to the point where admission falls back to the local buckets |

## 4. Recording rules

| Rule | Definition |
|---|---|
| `noema:availability_errors:ratio_rate{5m,30m,1h,6h}` | `aghoo_turns_shed_total` rate / (`aghoo_e2e_seconds_count` rate + shed rate). Missing series count as 0 |
| `noema:proxy_overhead_seconds:mean{5m,1h}` | mean `aghoo_ttft_seconds` minus mean `aghoo_first_answer_seconds`, `strategy="holdback"` |
| `noema:pod_turn_slots_held:sum` | `sum by (pod) (aghoo_turn_slots_held)`: concurrent streams per pod |
| `noema:pod_cpu_cores:rate5m` | cAdvisor CPU of every container in the pod, for pods with a `job="noema-api"` target |
| `noema:pod_event_loop_lag_seconds:p99_5m` | worst api process per pod of `quantile_over_time(0.99, aghoo_event_loop_lag_seconds[5m])`. The gauge is sampled every 0.5 s and read at each scrape |
| `noema:decision_quorum_seconds:p95_10m` | p95 of `aghoo_decision_quorum_reached_seconds` per profile |
| `noema:spec_restart_ratio:rate1h` | `aghoo_spec_restarts_total` / turns |
| `noema:member_parse_error_ratio:rate30m` | `aghoo_decision_parse_errors_total` / `aghoo_decision_member_status_total` per member (0 for members without parse errors) |
| `noema:cost_usd_per_turn:rate1h` | `aghoo_cost_usd_total{line=~"decision\|generation\|verification\|waste"}` / turns |
| `noema:db_pool_utilization:avg5m`, `noema:redis_pool_utilization:avg5m` | 5 min average of in use / size, per process |
| `noema:db_command_seconds:p95_5m`, `noema:db_pool_acquire_seconds:p95_5m` | Postgres unit of work and acquire wait p95 per pool |
| `noema:redis_command_seconds:p99_5m`, `noema:redis_pool_acquire_seconds:p95_5m` | Redis command p99 and acquire wait p95 |

The datastore histograms are sampled 1 in 16 (IMPL-088). Their quantiles hold, but their rates are 1/16 of the real
counts.

## 5. Dashboards

All seven dashboards carry the tag `noema` and link to each other. Every panel's description (hover over `i` in
Grafana) repeats the short meaning given here. A red line on a panel is the matching alert's threshold.

### 5.1 Noema / Overview and SLO (`noema-overview`)

| Panel | Shows | How to read it |
|---|---|---|
| Availability error ratio | Shed share over 5 min, 1 h and 6 h | The SLO budget is 0.1% (red line). Above it the month's budget is being spent faster than it accrues |
| Availability budget burn rate | Error ratio / 0.001 over 1 h and 6 h | 1 = spending exactly the budget. 14.4 over 1 h pages; 6 over 6 h opens a ticket |
| Gateway overhead (mean proxy) | Overhead proxy over 5 min and 1 h | SLO p95 <= 25 ms (red). It rises during failovers and holdbacks as well as with gateway load |
| TTFT p50 / p95 | Ingress to first delta, every turn | Model time included. Read it with the overhead panel to separate model time from gateway time |
| Turns per second | Turns that ran, by `strategy` (holdback, gate) | Traffic |
| Sheds per second | 429 `queue_timeout` at the turn cap, per pod | Should be 0. Any shed means a process is at its cap |
| E2E p50 / p95 | Ingress to `turn.completed` | Mostly model streaming time |
| Processes reporting | Processes exporting metrics, per `job` | A drop means a process died or stopped being scraped |
| Max streams per pod | The busiest pod's turn slots | Plan 100 per pod (red at 100) |
| Max pod CPU (cores) | The busiest api pod | Scale out above 0.62 vCPU (red) |
| Max event-loop lag p99 | The worst api pod | C2 bound 50 ms (red) |
| Sheds (1 h) | Requests shed in the last hour | Red at 1: any shed |

### 5.2 Noema / Decisions (`noema-decisions`)

| Panel | Shows | How to read it |
|---|---|---|
| Decision time p95 by stage | `stage=quorum` (quorum on every critical question) and `stage=all` (every member) | The quorum p95 is what the commit waits for |
| Quorum reached p95 by profile | `noema:decision_quorum_seconds:p95_10m` | Red at the 1,000 ms default deadline |
| DeepSeek member p99 | p99 of the DeepSeek member | Empty until per-member decision times are exported (section 6). Red at 1.5 s |
| Member results by status | `ok`, `refused`, `timeout`, `error`, `late`, `skipped` per member alias | Rising timeouts or errors on one member point at that model |
| Parse-error ratio by member | Parse errors / results over 30 min | Red at 1% |
| Parse errors by code | Parse errors by member and code | Which failure mode is drifting |
| Escalations | By `from_profile`, `trigger`, `outcome` | How often the cascade escalates and why |
| Hierarchical answers | Hierarchical outcomes (`scored`, `refusal`, `unavailable`) and reasons | `unavailable` / `deadline_exceeded` means the deadline is too short for two rounds |
| L2 decision cache hit ratio | L2 hits / (hits + misses) | Misses include `bypass` and `error` |
| Cache misses by reason | `miss`, `bypass`, `error` per layer | `error` = Redis failures on the cache path |
| Spec restarts per turn | `noema:spec_restart_ratio:rate1h` | Red at 10% |
| Spec agreement ratio | Share of turns whose decided spec equals spec0 | A per-process running share since process start (not windowed) |
| Conditioning blocks | Blocks rendered by `answer_mode` | How often conditioning reaches the prompt |
| Calibrator ECE | Latest ECE per calibrator key | Red at 0.08. Set by the weekly drift job and by fits |
| Labeler consensus by class | Latest labeling run's consensus per class | A drop of more than 10 points from the previous week alerts |
| Label class share | Share of each label class in the latest run | Label-distribution shift |
| Labeled items per hour | By question and outcome (`kept`, `disagreed`, `refused`, ...) | Labeling throughput and disagreement |
| Calibrator fits and downgrades per day | Fits by status, downgrades by reason | A downgrade means a calibrator went back to `uncalibrated` |

### 5.3 Noema / Generation and streaming (`noema-generation`)

| Panel | Shows | How to read it |
|---|---|---|
| TTFT p95 by route and model | Ingress to first delta, by served route (`profile`) and committed model | Which route or model is slow |
| Model first text p95 by model | Attempt start to the first answer text, every attempt | Upstream (Bedrock) time to first text |
| E2E p95 by route | Ingress to `turn.completed` by served route | Long answers and slow token rates |
| Overhead proxy inputs | Mean TTFT and mean model first text on holdback turns | The gap between the two lines is the Overview's gateway overhead |
| Circuit state | 0 closed, 1 half-open, 2 open, per model and region | Red at 2 (open) |
| Bedrock throttles by model | ThrottlingException per second | Should stay near 0, because admission throttles first |
| Bedrock cached input ratio | Cached share of input tokens since process start (worst process) | Prompt-cache effectiveness per model |
| Degradations | Media and parameter degradations by reason and model | Inputs converted or cut for a model or budget |
| Wasted output tokens | Output tokens of attempts that did not commit, by reason | The cost of restarts, races and failures |
| Stuck attempts | Attempts left non-terminal at turn exit, per pod | Should be 0 (red at 1) |
| Idempotent duplicate attaches | Duplicates of a keyed request by result (`in_flight`, `completed`, `lost`, `deadline`) | `lost` and `deadline` mean the duplicate could not attach |
| Idempotency store errors | Response bodies that could not be stored, per hour | Each one can lead to a re-execution (spec-11 §5) |

### 5.4 Noema / Tenancy and cost (`noema-tenancy-cost`)

| Panel | Shows | How to read it |
|---|---|---|
| Cost per hour by tenant | Recorded USD per hour (every line except `cache_savings`) | Spend per tenant, from the price catalog |
| Cost per hour by line | `decision`, `generation`, `labeling`, `verification`, `waste`, `cache_savings` | Where the money goes. `cache_savings` is cost avoided, not spent |
| Cost per turn | `noema:cost_usd_per_turn:rate1h` | Red at the budget in the rule (0.05 USD, a placeholder) |
| Cache savings by tenant | Avoided cost per hour | Value of the caches |
| Bedrock throttles by tenant | ThrottlingException per tenant and model | A tenant's account quota is below its configured quota |
| Tenant access failures | AssumeRole and account failures by tenant and cause | `tenant_access_misconfigured` (runbook `tenant-access-misconfigured`) |
| Security violations | `account_mismatch`, `pubsub_tenant_mismatch` and other kinds | Any value needs investigation (red at 1) |

### 5.5 Noema / Media (`noema-media`)

| Panel | Shows | How to read it |
|---|---|---|
| Media jobs by kind and status | Sandbox jobs per second (`ok`, `rejected`, `failed`, `timeout`, `violation`) | Failure and rejection share per job kind |
| Media job p95 by kind | Sandbox job wall time | OCR and PDF work dominate |
| Sandbox slot wait p95 by kind | Wait for a sandbox slot | Rising wait = add media workers (design 08 §9: queue depth) |
| Inline image ingest p95 | Inline image ingest on the turn path | Budget p95 < 400 ms (IMPL-074, [media_pipeline](../modules/media_pipeline.md)) (red) |
| Sandbox violations | Jail violations by kind | Any value needs investigation (red at 1) |

### 5.6 Noema / Admission and overload (`noema-admission`)

| Panel | Shows | How to read it |
|---|---|---|
| Streams per pod | Turn slots held per pod (both processes) | Plan 100 (red); the knee is 125 (sizing §5) |
| Pod CPU (cores) | api pod CPU from cAdvisor | Scale out above 0.62 (red) |
| Event-loop lag p99 per pod | Worst api process per pod | Red at 50 ms |
| Sheds per pod (5 min) | 429 `queue_timeout` at the turn cap | Red at 1: any shed |
| Admission rejections by code | `rpm_exceeded`, `tpm_exceeded`, `queue_timeout` | Tenant quota and concurrency limits doing their job. Not an availability failure |
| Admission queue depth | Requests in the per-pod weighted fair queue (worst process) | Persistent depth = a tenant at its stream limit |
| Redis-down fallback active | 1 while admission runs on the local buckets | Redis is down or slow (spec-11 §1.2) |
| Refund failures and pending releases | Failed Redis refunds or releases, and the releases kept for retry | Slots or tokens may be held until their lease expires |
| Reservation overruns | Model calls above the reserved count, by kind | Should be 0 (C1 measured 0) |

### 5.7 Noema / Datastores (`noema-datastores`)

| Panel | Shows | How to read it |
|---|---|---|
| Postgres pool in use / size | Connections checked out per process, and the pool maximum (`DB_POOL_MAX`) | 40 connections per api pod: 2 x 20 (sizing §5) |
| Postgres pool utilisation | 5 min average of in use / size, per process | Red at 90% |
| Postgres acquire wait p95 | Wait for a connection | Rises before saturation shows in the gauges |
| Postgres unit of work p50 / p95 | Acquire to release | Red at the 20 ms p95 scale signal |
| Redis pool utilisation | 5 min average of in use / max connections, per process | Red at 90% |
| Redis acquire wait p95 | Wait for a Redis connection | Pool exhaustion |
| Redis command p99 | Every command or pipeline | Red at 50 ms, where admission treats Redis as down |
| Redis command p95 by command | Per command (`MULTI`, `PIPELINE` for batches) | Which command is slow |
| Outbox backlog by sink | `memory`: records only in the process; `redis`: records waiting for the drainer | Both should drain to 0 |
| Outbox records dropped | Write-behind records lost, per pod | Should be 0 (red at 1) |

## 6. Gaps and limits

What the current registry cannot show, and how each rule copes. Closing a gap needs a new instrument in
`src/noema/telemetry/registry.py`, which is frozen: request a change from fakes-telemetry (the zero-initialised
counters and the gateway-overhead histogram are filed in `.orchestration/requests/observability.md`).

| Gap | Effect here | Needed |
|---|---|---|
| No gateway-overhead instrument. The TTFT buckets near typical TTFTs are 0.25 s wide (`SECONDS_BUCKETS`: 0.25, 0.5, 0.75, 1.0), so no p95 of a 25 ms difference can be derived | The overhead alerts use a mean proxy: mean TTFT per turn (ingress to first delta) minus mean model first text per attempt. It misses a p95 breach whose mean stays below 25 ms. It also **over-reads**: TTFT includes the designed holdback wait, max(S1, first text) - first text, up to the router's 1,000 ms deadline (live E3: 282 and 407 ms in 2 of 6 pinned turns, [reports/qa/e3/live-smoke.md](../../reports/qa/e3/live-smoke.md) §3), the time of a spec restart's second attempt and pre-commit failovers. With real decision latency the mean can sit above 25 ms on a healthy gateway, so `noema-overhead-slo-burn` is a warning, not a page | A histogram of the gateway's own time per turn (spec-10's L1 quantity, client TTFT minus model TTFT, which the L1 windows measure with `holdback_ms` 0, reports/perf/sizing.md §1); change request to fakes-telemetry |
| Counters are exported only after their first `add` (OpenTelemetry), and `increase()` cannot count the step from an absent series to its first value: a process's first shed, stuck attempt, outbox drop or calibrator downgrade would be missed | The four "any increase" alerts add a first-sample clause, `sum by (...) (c unless increase(c[1h] offset <window>)) > 0`, which fires while a series is younger than the alert's window. After a scrape gap of more than 1 h, a non-zero counter fires once more. The promtool cases start these series absent. The dashboard panels on these counters (sheds, stuck attempts, outbox drops, downgrades) still use plain `increase()` and miss a first sample | Export every label-less counter at 0 at process start (change request to fakes-telemetry); the clause then stays inert |
| No request counter by status | The availability SLI counts only 429 sheds at the turn cap; 5xx answers and failed turns are invisible | A request counter labelled by operation and status class |
| No per-tenant Bedrock call count | `noema-bedrock-throttling` fires on any sustained throttling instead of "> 2% per tenant" (design 08 §7) | A per-tenant, per-model call counter |
| No job metrics | Not implemented: design 08 §7 "deletion job > 24 h" and the 08 §9 `jobs` scale signal "queue age > 5 min" | A gauge of the oldest queued or running job age, by kind |
| Outbox dead-letters and dead jobs are ERROR logs only (`ops/outbox/drainer.py`; spec-11 §4) | The outbox alerts watch drops and backlogs; dead-letters need a log-based alert | A dead-letter counter |
| `aghoo_decision_seconds` is observed with `member=""` (`decision/run.py`) | `noema-deepseek-p99-high` and the DeepSeek panel stay empty | Per-member observations |
| No per-turn cost budget is configured | `noema-cost-per-turn-high` uses a placeholder 0.05 USD | A value per deployment |
| `aghoo_verification_flags_total` is declared but not emitted (docs/modules/orchestrator.md) | No panel | An emitter |
| ECE, consensus and class-share gauges live in the jobs process that ran the job | After a restart they are absent until the next run (the drift job is weekly), so their alerts are silent meanwhile | - |

Pod CPU sums every container of the pod, sidecars included. Signals that have a dashboard panel but no alert
(outside the requested set): security and sandbox violations, tenant access failures, the admission Redis-down
fallback, idempotency store errors and reservation overruns. They are candidates for alerts.

`aghoo_turn_slots_held` (gauge, no labels, per process) was added to the registry in Wave F. The registry test
checks that its kind and labels are the ones the rules assume.
