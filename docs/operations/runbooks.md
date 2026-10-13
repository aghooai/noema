# Runbooks

One section per Prometheus alert of `deploy/observability/prometheus/rules/` (the heading is the alert name and the
anchor its `runbook_url` points at) and per runbook of design 08 §6. Each gives symptoms, what to check, the
commands, mitigations and escalation. Severity: `critical` pages, `warning` opens a ticket (the rules' convention).
Alert meanings and dashboards: [observability.md](observability.md).

## Common commands

The commands below are for the reference compose deployment; they were run on a stack while writing these docs. In
another orchestrator use its equivalents (logs, restart, exec).

```bash
C="docker compose -p <project> -f deploy/compose/docker-compose.yml"
make e2e-ps E2E_PROJECT=<project>                       # services and health
$C logs --no-log-prefix api-1 | grep '<text or request_id>'   # the whole log of one service (JSON lines)
make e2e-logs E2E_PROJECT=<project> SERVICE=api-1       # only the last 200 lines: seconds of history under load
curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_(circuit_state|turns_shed_total|event_loop_lag_seconds)'
# jobs and media-worker publish no metrics port: read it inside the container
$C exec -T jobs python -I -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9464/metrics').read().decode())" | grep '^aghoo_label'
$C exec -T postgres psql -U postgres -d noema -Atc "SELECT state, kind, count(*) FROM jobs.job GROUP BY 1, 2"
$C exec -T postgres psql -U postgres -d noema -Atc "SELECT count(*) FROM ops.outbox_dead_letter"
$C exec -T postgres psql -U postgres -d noema -Atc "SELECT usename, state, count(*) FROM pg_stat_activity WHERE datname = 'noema' GROUP BY 1, 2"
$C exec -T valkey valkey-cli info clients
$C restart api-1                                        # SIGTERM, drain (5 s delay, 30 s turn grace), start
```

`$A` is `noema-admin` with its environment ([configuration.md](configuration.md) section 1). Every api pod serves
one metrics port per worker process (9464 and 9465 in compose; the host ports are the state file's
`endpoints.metrics`); scrape or read both. A counter appears in `/metrics` only after its first increment, so a
missing `aghoo_*_total` line means zero. Logs are one JSON object per line with `ts`, `level`, `logger`, `message`,
`trace_id`, `tenant`, `event`.

Escalation, in general: a code defect goes to the owning role of the module (`OWNERS.yaml`); a Bedrock incident or
quota to AWS Support for the tenant account; a tenant account change to the tenant's administrator; anything with
a `security` event (`account_mismatch`, `pubsub_tenant_mismatch`, `sandbox.violation`) to security at once.

## Index

Alerts (25, from the rule files):

| Alert | Severity | Rule file |
|---|---|---|
| [noema-decision-quorum-slow](#noema-decision-quorum-slow) | warning | `noema-alerts-decisions.rules.yml` |
| [noema-spec-restart-rate-high](#noema-spec-restart-rate-high) | warning | `noema-alerts-decisions.rules.yml` |
| [noema-member-parse-errors-high](#noema-member-parse-errors-high) | warning | `noema-alerts-decisions.rules.yml` |
| [noema-deepseek-p99-high](#noema-deepseek-p99-high) | warning | `noema-alerts-decisions.rules.yml` |
| [noema-calibrator-ece-drift](#noema-calibrator-ece-drift) | warning | `noema-alerts-decisions.rules.yml` |
| [noema-label-consensus-drop](#noema-label-consensus-drop) | warning | `noema-alerts-decisions.rules.yml` |
| [noema-availability-slo-fast-burn](#noema-availability-slo-fast-burn) | critical | `noema-alerts-slo-capacity.rules.yml` |
| [noema-availability-slo-slow-burn](#noema-availability-slo-slow-burn) | warning | `noema-alerts-slo-capacity.rules.yml` |
| [noema-proxy-overhead-high](#noema-proxy-overhead-high) | warning | `noema-alerts-slo-capacity.rules.yml` |
| [noema-overhead-slo-burn](#noema-overhead-slo-burn) | warning | `noema-alerts-slo-capacity.rules.yml` |
| [noema-pod-streams-high](#noema-pod-streams-high) | warning | `noema-alerts-slo-capacity.rules.yml` |
| [noema-pod-cpu-high](#noema-pod-cpu-high) | warning | `noema-alerts-slo-capacity.rules.yml` |
| [noema-event-loop-lag-high](#noema-event-loop-lag-high) | critical | `noema-alerts-slo-capacity.rules.yml` |
| [noema-turns-shed](#noema-turns-shed) | warning | `noema-alerts-slo-capacity.rules.yml` |
| [noema-circuit-open](#noema-circuit-open) | warning | `noema-alerts-upstream-ops.rules.yml` |
| [noema-bedrock-throttling](#noema-bedrock-throttling) | warning | `noema-alerts-upstream-ops.rules.yml` |
| [noema-stuck-attempts](#noema-stuck-attempts) | warning | `noema-alerts-upstream-ops.rules.yml` |
| [noema-cost-per-turn-high](#noema-cost-per-turn-high) | warning | `noema-alerts-upstream-ops.rules.yml` |
| [noema-outbox-records-dropped](#noema-outbox-records-dropped) | critical | `noema-alerts-upstream-ops.rules.yml` |
| [noema-outbox-memory-backlog](#noema-outbox-memory-backlog) | critical | `noema-alerts-upstream-ops.rules.yml` |
| [noema-outbox-redis-backlog](#noema-outbox-redis-backlog) | warning | `noema-alerts-upstream-ops.rules.yml` |
| [noema-postgres-pool-saturated](#noema-postgres-pool-saturated) | warning | `noema-alerts-upstream-ops.rules.yml` |
| [noema-postgres-latency-high](#noema-postgres-latency-high) | warning | `noema-alerts-upstream-ops.rules.yml` |
| [noema-redis-pool-saturated](#noema-redis-pool-saturated) | warning | `noema-alerts-upstream-ops.rules.yml` |
| [noema-redis-latency-high](#noema-redis-latency-high) | warning | `noema-alerts-upstream-ops.rules.yml` |

| Design 08 §6 runbook | Section |
|---|---|
| `ThrottlingException` rate up for one tenant | [noema-bedrock-throttling](#noema-bedrock-throttling) |
| `tenant_access_misconfigured` | [tenant-access-misconfigured](#tenant-access-misconfigured) |
| Circuit breaker open | [noema-circuit-open](#noema-circuit-open) |
| Mantle errors (DeepSeek member) | [mantle-errors](#mantle-errors) |
| DeepSeek p99 > 1.5 s | [noema-deepseek-p99-high](#noema-deepseek-p99-high) |
| Verbalized parse-error rate > 1% | [noema-member-parse-errors-high](#noema-member-parse-errors-high) |
| Calibrator drift (ECE > 0.08) | [noema-calibrator-ece-drift](#noema-calibrator-ece-drift) |
| Consensus rate drops | [noema-label-consensus-drop](#noema-label-consensus-drop) |
| Spec-restart rate > 10% | [noema-spec-restart-rate-high](#noema-spec-restart-rate-high) |
| Opus first-text p95 rises | [opus-first-text-slow](#opus-first-text-slow) |
| Model deprecation notice | [model-deprecation](#model-deprecation) |
| Tenant onboarding | [tenant-onboarding](#tenant-onboarding) |
| Tenant offboarding | [tenant-offboarding](#tenant-offboarding) |
| Deletion job stuck | [deletion-job-stuck](#deletion-job-stuck) |

Further procedures: [redis-down](#redis-down), [postgres-down](#postgres-down),
[api-key-compromised](#api-key-compromised), [config-rollback](#config-rollback).

## SLO and capacity

### noema-availability-slo-fast-burn

- **Symptoms**: critical. The share of turn requests shed with 429 `queue_timeout` burns the 99.9% monthly budget
  at 14.4x or more over 1 h (and 5 min). Clients see 429 with `retry-after: 1`. The SLI is a proxy: it counts
  only these sheds, not 5xx or failed turns, which are not exported ([observability.md](observability.md)).
- **Check**: `aghoo_turns_shed_total` per pod, the turn slots held per pod (`aghoo_turn_slots_held`, summed over the
  pod's processes), pod CPU, event-loop lag; the api log line `edge shed: <n> requests refused by the per-process
  cap`. Is one tenant flooding? Route-level sheds have an access line with its `tenant`; edge sheds are summarized in
  one line per second without a tenant.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_(turns_shed_total|turn_slots_held|event_loop_lag_seconds)'` on every worker port of each pod; `$C logs --no-log-prefix api-1 | grep 'edge shed'`; `docker stats --no-stream <api container>`.
- **Mitigate**: add api pods (sizing: [sizing.md](sizing.md)). A single flooding tenant is contained by the fair
  share (C5 victim p95 ratio 1.021 and 1.022, [reports/qa/wave-e-summary.md](../../reports/qa/wave-e-summary.md));
  lower its `tenants.<t>.limits` with a config change if it should not get that capacity. Do not raise
  `NOEMA_MAX_TURNS_PER_PROCESS`: 240 per pod is already 1.9x the knee ([sizing.md](sizing.md) section 1).
- **Escalate**: if pods are added and shedding continues at low CPU, to the api owners.

### noema-availability-slo-slow-burn

- **Symptoms**: warning; the same signal at 6x over 6 h (and 30 min).
- **Commands**: as for the fast burn.
- **Check and mitigate**: as for the fast burn; plan capacity before the next peak.
- **Escalate**: capacity planning (the deployment owner).

### noema-proxy-overhead-high

- **Symptoms**: warning; the mean gateway overhead proxy (TTFT minus the model's first text on holdback turns) is
  above 25 ms for 10 min. Users see slower first tokens. The proxy over-reads: it also counts the designed
  holdback wait (first delta at max(S1, first text), up to the router's 1,000 ms deadline; live E3: 282 and 407 ms
  in 2 of 6 turns, [reports/qa/e3/live-smoke.md](../../reports/qa/e3/live-smoke.md) §3), spec-restart time and
  failovers ([observability.md](observability.md) section 6).
- **Check**: decision latency first (`noema:decision_quorum_seconds:p95_10m`, the Decisions dashboard): slow
  commit-critical members raise the proxy without any gateway cost; then per-pod streams, CPU and event-loop lag
  (if any is high, it is capacity: see [noema-pod-streams-high](#noema-pod-streams-high)); the fallback rate
  (`aghoo_circuit_state`, `aghoo_decision_member_status_total`); Postgres and Redis latency.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_(event_loop_lag_seconds|turn_slots_held|circuit_state)'`; `docker stats --no-stream <api container>`.
- **Mitigate**: scale out; fix the upstream cause (open breakers, throttles).
- **Escalate**: sustained with healthy capacity and upstreams: api and perf owners (sizing risk 1: the p50 budget is
  most of the bound at low load, [reports/perf/sizing.md](../../reports/perf/sizing.md) §7).

### noema-overhead-slo-burn

- **Symptoms**: warning (a ticket, not a page, until a gateway-overhead histogram exists: the proxy over-reads); the
  overhead proxy is above 25 ms for the last hour and the last 5 min.
- **Commands**: as for [noema-proxy-overhead-high](#noema-proxy-overhead-high).
- **Check and mitigate**: as [noema-proxy-overhead-high](#noema-proxy-overhead-high); rule out slow decisions
  before treating it as gateway time.
- **Escalate**: the deployment owner; then api and perf owners.

### noema-pod-streams-high

- **Symptoms**: warning; a pod holds more than 100 turn slots for 5 min (`aghoo_turn_slots_held` summed over its
  two processes): the Wave F plan of 100 per pod, 80% of the 125-stream knee ([sizing.md](sizing.md)). If you
  re-size, keep the rule threshold equal to the plan.
- **Check**: the same pod's CPU and lag; whether load is spread evenly (a load balancer pinning connections).
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep '^aghoo_turn_slots_held'` on both worker ports of the pod (sum them).
- **Mitigate**: scale out (N = ceil(peak concurrent streams / the planned streams per pod)).
- **Escalate**: none if scaling works; otherwise the deployment owner.

### noema-pod-cpu-high

- **Symptoms**: warning; a pod's CPU is above 0.62 vCPU for 10 min: 100 streams x ~6.2 millicores at the L1
  workload (workload-dependent, [sizing.md](sizing.md)). If you re-size, keep the threshold equal to the plan.
  Needs cAdvisor's `container_cpu_usage_seconds_total`; on the compose stack the rule has no data.
- **Check**: streams per pod; a heavier turn mix (more decision calls, media, verification) raises CPU per stream.
- **Commands**: `docker stats --no-stream <api container>`; `curl -s http://127.0.0.1:<metrics port>/metrics | grep '^aghoo_turn_slots_held'`.
- **Mitigate**: scale out. If CPU is high at low stream counts, the workload differs from L1: re-measure the knee
  for it ([sizing.md](sizing.md) section 4).
- **Escalate**: perf owners for a re-sizing.

### noema-event-loop-lag-high

- **Symptoms**: critical; the worst api process of a pod has event-loop lag p99 above 50 ms for 5 min (C2 bound;
  4.0-4.1 ms at the knee, [sizing.md](sizing.md) section 2). Every stream on that process is delayed.
- **Check**: `aghoo_event_loop_lag_seconds` per process; pod CPU; streams per pod; a full GC pause pattern
  ([reports/perf/sizing.md](../../reports/perf/sizing.md) §2.1: 31-38 ms p50 every 30-38 s per process at the knee);
  one process stuck in CPU work (the two processes of the pod differ widely).
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep '^aghoo_event_loop_lag_seconds'` on both worker ports; `$C restart <service>`.
- **Mitigate**: scale out; restart the pod (`$C restart api-1`) if one process is stuck while the other is idle.
- **Escalate**: page the deployment owner; persistent lag at low load goes to the api owners.

### noema-turns-shed

- **Symptoms**: warning; any 429 `queue_timeout` from the per-process cap in 5 min. A process is at its cap (120
  turns and in-flight decisions by default).
- **Check**: as [noema-availability-slo-fast-burn](#noema-availability-slo-fast-burn); `aghoo_turn_slots_held`.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_(turns_shed_total|turn_slots_held)'`; `$C logs --no-log-prefix api-1 | grep 'edge shed'`.
- **Mitigate**: scale out before the availability burn alerts fire; contain a flooding tenant by its limits.
- **Escalate**: as the availability alerts.

## Decisions and labeling

### noema-decision-quorum-slow

- **Symptoms**: warning; the commit-critical quorum p95 of a decision profile is above its 1,000 ms deadline for
  10 min. Turns commit on the hold cap (`holdback_deadline`) or on spec0 more often; `/v1/decisions` answers
  `partial` with `unavailable` answers.
- **Check**: `aghoo_decision_quorum_reached_seconds` and `aghoo_decision_member_status_total{status}` per member
  (timeouts, errors); open breakers ([noema-circuit-open](#noema-circuit-open)); Bedrock throttles; DeepSeek tail
  ([noema-deepseek-p99-high](#noema-deepseek-p99-high)).
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_(decision_member_status_total|decision_quorum_reached_seconds_(sum|count)|circuit_state)'`.
- **Mitigate**: the members degrade by themselves (a member that times out does not block the quorum when others
  answer). Deadlines and quorum sizes are config values set at their design numbers until Phase 0 measures them
  (IMPL-025); changing them is a config change with two operators.
- **Escalate**: decision owners; AWS Support if one model's latency rose.

### noema-spec-restart-rate-high

- **Symptoms**: warning; spec restarts (`generation.cancelled{spec_changed}`) above 10% of turns over 1 h. Each
  restart wastes the cancelled attempt's tokens and delays the first text.
- **Check**: `aghoo_spec_restarts_total` and `aghoo_wasted_output_tokens_total` by profile; the turns' matched
  bindings (getTurn, the Web UI inspector); a recent config change to `routing.bindings` or conditioning.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_(spec_restarts_total|wasted_output_tokens_total)'`; `$A config list` (the last activation time).
- **Mitigate**: design 08 §6: make conditioning sparser, check stickiness, consider `gate` for that route; roll back
  a config change that caused it (`$A config rollback <n>`, then approve by a second operator).
- **Escalate**: orchestrator owners. The Phase 1 replay measured a 0.23 restart share on synthetic agreement
  (R4, reported against the Phase 0 10% threshold, not gated).

### noema-member-parse-errors-high

- **Symptoms**: warning; a decision member's parse errors above 1% of its calls for 15 min. Its answers are member
  errors; quorums rest on fewer members.
- **Check**: `aghoo_decision_parse_errors_total{member,code}` (the code says which parse rule failed); a model
  update notice for that model; a template change in the last config version.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep '^aghoo_decision_parse_errors_total'`.
- **Mitigate**: design 08 §6: enable `textFormat` structured output for that member (slower) or ship a fixed template
  version (a new version needs recalibration). Removing the member from the profile is a config change.
- **Escalate**: decision owners.

### noema-deepseek-p99-high

- **Symptoms**: warning; the DeepSeek member's p99 above 1.5 s for 10 min (gate L5 measured 929.5 ms with backups,
  [reports/qa/wave-e-summary.md](../../reports/qa/wave-e-summary.md)).
- **Check**: the backup rate (no metric of its own in Phase 1: compare the member's calls in
  `aghoo_decision_member_status_total` with the number of decisions); Mantle errors
  ([mantle-errors](#mantle-errors)); `aghoo_circuit_state{model=~"deepseek.*"}`.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_(decision_member_status_total|circuit_state)' | grep deepseek`.
- **Mitigate**: design 08 §6: lower `backup_after_ms` (config); swapping in another member needs owner approval.
- **Escalate**: decision owners; AWS Support for a Mantle latency regression.

### noema-calibrator-ece-drift

- **Symptoms**: warning, for a day; the weekly drift job downgraded an active calibrator
  (`aghoo_calibrator_downgrades_total{reason}`): `drift_ece` = its ECE above 0.08 (ADR-003 `drift_ece_max`),
  `drift_ci` = the ECE lower bound above `ece_max`. The answers it covered are now served `uncalibrated`. The alert
  does not read the `aghoo_calibration_ece` gauge: a rejected fit sets that gauge too (the Calibrator ECE panel
  shows both); `$A calibrators list --status rejected` lists those fits.
- **Check**: `$A calibrators list --status downgraded`; the consensus rate and label distribution
  (`aghoo_label_consensus_ratio`, `aghoo_label_class_share`); a model update; a traffic shift; the jobs log line
  `calibrator downgraded by drift`.
- **Commands**: `$A calibrators list --status downgraded`; `$A calibrators list --scope platform`;
  `$C logs --no-log-prefix jobs | grep 'calibrator downgraded by drift'`.
- **Mitigate**: refit from fresh labels, then two-person activation (`$A calibrators approve <id>`, then
  `$A calibrators activate <id>` by another operator). In Phase 1 no calibrator is active, so this alert cannot
  fire until an artifact is activated.
- **Escalate**: labeling owners.

### noema-label-consensus-drop

- **Symptoms**: warning; the labelers' consensus rate for a (profile, question, class) is more than 10 points below
  its 7-day average.
- **Check**: the dropped items (W4: `GET /v1/admin/labeling/dropped`, the Web UI calibration page); labeler model
  updates; a class definition change; label-shift (`aghoo_label_class_share`); poisoning attempts (SE5 is
  NOT-RUN [P0]).
- **Commands**: on compose (the jobs process publishes no metrics port): `$C exec -T jobs python -I -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9464/metrics').read().decode())" | grep -E '^aghoo_(label_consensus_ratio|label_class_share|labeling_items_total)'`; elsewhere scrape the jobs process's `NOEMA_METRICS_PORT`; `$A calibrators list`.
- **Mitigate**: refine the question instructions (a new signature, so new calibrators); retire the affected
  calibrator (`$A calibrators retire <id>`; its answers are then served `uncalibrated`).
- **Escalate**: labeling owners; security if poisoning is suspected.

### noema-cost-per-turn-high

- **Symptoms**: warning; recorded cost per turn above the budget for 1 h (`aghoo_cost_usd_total` over turns).
- **Check**: `GET /v1/usage` grouped by line and model (tenant_admin key); `aghoo_wasted_output_tokens_total`
  (restarts, fallbacks); verification and escalation counts; the price catalog pinned by the active config's
  `platform.cost.prices_ref`. Phase 1 prices are placeholders: with TBD prices the cost is 0
  and `cost_estimated` (see [configuration.md](configuration.md) section 4).
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_(cost_usd_total|wasted_output_tokens_total)'`; `curl -s "$API/v1/usage?group_by=model" -H "Authorization: Bearer <tenant_admin key>"` (not run while writing this page).
- **Mitigate**: lower output budgets or effort through bindings; reduce verification; correct a wrong price import.
- **Escalate**: ops-ledger owners for metering defects; the product owner for budgets.

### mantle-errors

Design runbook (no alert of its own; see also [noema-circuit-open](#noema-circuit-open) and
[noema-deepseek-p99-high](#noema-deepseek-p99-high)).
- **Symptoms**: DeepSeek member errors or timeouts; a breaker open for the DeepSeek model; decisions with fewer
  responding members (`quorum.responded`).
- **Check**: `aghoo_decision_member_status_total{member=~"deepseek.*"}` by status; the api logs for the Mantle
  adapter's error kinds.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_(decision_member_status_total|circuit_state)' | grep deepseek`;
  `$C logs --no-log-prefix api-1 | grep 'adapter call failed'`.
- **Mitigate**: none needed for availability: the quorum continues with Haiku and Luna (answers `uncalibrated` if
  that subset has no calibrator). Verify the Mantle route and auth mode outside Noema; Phase 1 runs no live probe
  from the service.
- **Escalate**: adapters owners; AWS Support for a Mantle outage.

### opus-first-text-slow

Design runbook: Opus first-text p95 rises (no alert of its own).
- **Symptoms**: slower first text on the reasoning route (`opus-deep`); `aghoo_first_answer_seconds` for the Opus
  model rises.
- **Check**: the effort the bindings chose (getTurn, the Web UI inspector); a model update.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep '^aghoo_first_answer_seconds' | grep opus`.
- **Mitigate**: lower the effort for `moderate` complexity in the bindings (a config change, two operators).
- **Escalate**: orchestrator owners.

## Upstreams (Bedrock, STS)

### noema-circuit-open

- **Symptoms**: warning; a breaker is open for (model, region) for 1 min (`aghoo_circuit_state` 2). Turns fall back
  to other members or profiles automatically; decision members of that model are skipped.
- **Check**: which model and region; `aghoo_decision_member_status_total` and the api logs (`adapter call failed`,
  error kinds); the AWS Health Dashboard; whether the trip is the failure-ratio rule or the p95-TTFT rule (a slow,
  not failing, model). Both pods share breaker state through Redis pub/sub.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep '^aghoo_circuit_state'` on every pod (2 = open); `$C logs --no-log-prefix api-1 | grep 'adapter call failed'`.
- **Mitigate**: wait for the half-open probes; the state moves only on a call, so an idle model stays at 2 until
  traffic probes it (F3 measured open 4.29 s and recovered 5.13 s after the window,
  [reports/qa/wave-e-summary.md](../../reports/qa/wave-e-summary.md)). Announce degraded routing if a generation
  model is out for long. Design 08 §6.
- **Escalate**: AWS Support (tenant account) for a Bedrock incident; adapters owners if the breaker trips with a
  healthy upstream.

### noema-bedrock-throttling

- **Symptoms**: warning; Bedrock `ThrottlingException`s for a (tenant, model) over 10 min. Pre-commit, turns fall
  back at once (F1: 52/52); the tenant sees slower or degraded answers.
- **Check**: `aghoo_bedrock_throttles_total{tenant,model}`; the tenant's Service Quotas for the model; whether the
  decision fan-out grew (questions x members per turn, design 08 §6); whether `tenants.<t>.limits.model_quotas`
  mirrors the real quota (admission should throttle with 429 before Bedrock does: C1 had 0 mock throttles at
  rpm 600).
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_(bedrock_throttles_total|admission_rejections_total)'`.
- **Mitigate**: request a quota increase (Service Quotas in the tenant account); set or lower the quota mirror in
  the config so admission throttles first.
- **Escalate**: the tenant's administrator and AWS Support.

### tenant-access-misconfigured

Design runbook; no alert of its own yet (metric `aghoo_tenant_access_failures_total{tenant,cause}`).
- **Symptoms**: 503 `tenant_access_misconfigured`, `retryable: false`, with `details.cause`; the log line
  `sts.assume_role failed tenant=<t> error=<e>`.
- **Check**: the cause table in [tenant-onboarding.md](tenant-onboarding.md#3-when-the-first-call-fails);
  CloudTrail in the tenant account; the ExternalId secret; the role's model list.
- **Mitigate**: fix the account side (re-run the StackSet, enable model access); the broker caches a failure 10 s
  per tenant and pod, so recovery is automatic after the fix.
- **Escalate**: the tenant's administrator; security at once for `account_mismatch`.

## Ops pipeline (outbox, jobs, attempts)

### noema-stuck-attempts

- **Symptoms**: warning; generation attempts were left non-terminal when their turn task exited
  (`aghoo_stuck_attempts_total`); the watchdog marked them `cancelled(deadline)` with estimated usage. The 1 h soak
  measured 0 (C4).
- **Check**: the api log `generation attempts left live at turn exit` and the turn ids around it; a deploy or a
  process restart at that time.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep '^aghoo_stuck_attempts_total'`; `$C logs --no-log-prefix api-1 | grep 'left live at turn exit'`.
- **Mitigate**: none at runtime (the watchdog already settled the rows); collect the turn ids.
- **Escalate**: generation owners (a defect).

### noema-outbox-records-dropped

- **Symptoms**: critical; a process dropped outbox records (`aghoo_outbox_dropped_total`): usage lines, audit rows
  or finalize records are lost. It happens only when Redis is unreachable and the in-memory queue (100,000) is full,
  or at shutdown (`outbox: <n> records lost at shutdown`).
- **Check**: Redis and Postgres health; the log `outbox record dropped (<reason>): kind=<k> id=<id>`.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_outbox_(dropped_total|backlog)'`; `$C logs --no-log-prefix api-1 | grep 'outbox'`.
- **Mitigate**: restore Redis or Postgres first. Billing gaps need reconciliation from the turn and decision rows.
- **Escalate**: page; ops-ledger owners.

### noema-outbox-memory-backlog

- **Symptoms**: critical; records stayed in a process's memory for 5 min: Postgres rejected or is down and Redis
  is unreachable (`outbox: Redis unavailable (...); records stay in memory`). A restart of that process loses them.
- **Check**: Redis reachability from the pod; `$C exec -T valkey valkey-cli info clients`.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep '^aghoo_outbox_backlog'` (wait for `sink="memory"` 0); `$C exec -T valkey valkey-cli info clients`.
- **Mitigate**: restore Redis; **do not restart** the affected pod until `aghoo_outbox_backlog{sink="memory"}` is 0.
- **Escalate**: page; ops-ledger owners.

### noema-outbox-redis-backlog

- **Symptoms**: warning; records waited in the Redis outbox streams for 15 min: the drainer cannot apply them
  (Postgres down, or rejected records). Usage reports lag.
- **Check**: Postgres health; the drainer logs (`outbox drainer: Postgres unavailable; <n> entries stay pending`,
  `outbox: <kind> <id> rejected (...)`); the dead letter:
  `$C exec -T postgres psql -U postgres -d noema -Atc "SELECT kind, count(*), max(attempts) FROM ops.outbox_dead_letter GROUP BY 1"`.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep '^aghoo_outbox_backlog'`; the dead-letter query under Check.
- **Mitigate**: restore Postgres; the drainer resumes by itself (`outbox drainer: Postgres reachable again`). A
  record that failed 10 deliveries is dead-lettered with an ERROR log `outbox record dead-lettered after <n>
  deliveries`; a dead-lettered `turn_finalize` keeps its session busy until released.
- **Escalate**: ops-ledger owners for dead-lettered records (no replay command exists in Phase 1).

### deletion-job-stuck

Design runbook (08 §6; the design's "deletion job > 24 h" alert has no rule yet).
- **Symptoms**: a `session.delete`, `file.purge` or `retention.sweep` job stays `queued` or `running`, or ends `dead`.
- **Check**: `$C exec -T postgres psql -U postgres -d noema -Atc "SELECT state, kind, count(*) FROM jobs.job GROUP BY 1, 2"`,
  and for one kind `"SELECT id, state, attempts, last_error, updated_at FROM jobs.job WHERE kind = 'session.delete'
  AND state <> 'done' ORDER BY updated_at"`; the jobs or media-worker log `job dead: id=... kind=... error=...`.
- **Mitigate**: a lapsed lock is re-queued by the reaper within 2 minutes; check S3 access and lifecycle for
  object deletes; failed jobs back off 30 s x 2^(attempt-1) up to `max_attempts` (5).
- **Escalate**: ops-ledger or media-pipeline owners after 24 h (the design's compliance SLA).

## Datastores

### noema-postgres-pool-saturated

- **Symptoms**: warning; a process's Postgres pool is at 90% or more of its 20 connections for 5 min
  (`aghoo_db_pool_in_use` / `aghoo_db_pool_size`). Callers wait for a connection; ingress latency rises. At the
  ramp to 1,000 streams per pod at most 1 of 20 was in use (C2, [reports/qa/wave-e-summary.md](../../reports/qa/wave-e-summary.md)),
  so saturation means slow statements or locks, not load.
- **Check**: `aghoo_db_pool_acquire_seconds`, `aghoo_db_command_seconds`; long or blocked statements:
  `$C exec -T postgres psql -U postgres -d noema -Atc "SELECT usename, state, count(*) FROM pg_stat_activity WHERE datname = 'noema' GROUP BY 1, 2"`.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_db_pool_(in_use|size)'`; the `pg_stat_activity` query under Check.
- **Mitigate**: statements are bounded (10 s statement timeout, 5 s lock timeout, [modules/db.md](../modules/db.md)),
  so a hung database surfaces as retryable 500s rather than piling up. Fix the database side; the per-pod
  connection count (2 x 20 per api pod plus the workers) must fit the server's `max_connections`
  ([sizing.md](sizing.md)).
- **Escalate**: the database owner; db owners for a slow query.

### noema-postgres-latency-high

- **Symptoms**: warning; a unit of work (acquire to release, sampled 1/16) has p95 above 20 ms for 10 min (the design
  08 §9 Postgres scale signal). Measured 2.3 to 5.1 ms mean from 50 to 1,000 streams per pod ([sizing.md](sizing.md) section 3).
- **Check**: the database's CPU, I/O and connections; autovacuum; replication lag after a failover.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep '^aghoo_db_command_seconds_(sum|count)'`.
- **Mitigate**: scale the database (08 §9 starts at 2 vCPU / 8 GiB, Multi-AZ).
- **Escalate**: the database owner.

### noema-redis-pool-saturated

- **Symptoms**: warning; a process's Redis pool is at 90% or more of `NOEMA_REDIS_MAX_CONNECTIONS` (256) for 5 min.
  Past the bound a caller waits up to 0.5 s, then fails like an unreachable Redis (fail-soft: local buckets,
  resume disabled). 5 to 7 were in use per process at the ramp ([sizing.md](sizing.md) section 3).
- **Check**: `aghoo_redis_pool_in_use`, `aghoo_redis_pool_acquire_seconds`; turn-event mirror readers (up to 128 per
  process) during many cross-pod attaches; `$C exec -T valkey valkey-cli info clients`.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep -E '^aghoo_redis_pool_(in_use|size)'`; `$C exec -T valkey valkey-cli info clients`.
- **Mitigate**: find the holder (attach storms, a slow Valkey); raise `NOEMA_REDIS_MAX_CONNECTIONS` only with
  Valkey's `maxclients` headroom (at most 2 x 256 per api pod by default).
- **Escalate**: the Valkey owner; api or streaming owners for a connection leak.

### noema-redis-latency-high

- **Symptoms**: warning; a Redis command or pipeline p99 above 50 ms for 10 min. Every turn's ingress does Redis work
  (~14.5 commands or pipelines per turn, [sizing.md](sizing.md) section 3), so the TTFT overhead rises.
- **Check**: Valkey CPU and memory (Noema runs it with `maxmemory-policy noeviction`: once a `maxmemory` is
  reached, writes fail); `$C exec -T valkey valkey-cli info clients`.
- **Commands**: `curl -s http://127.0.0.1:<metrics port>/metrics | grep '^aghoo_redis_command_seconds_(sum|count)'`; `$C exec -T valkey valkey-cli info clients`.
- **Mitigate**: scale Valkey (08 §9: 1 GiB with a replica).
- **Escalate**: the Valkey owner.

### redis-down

No alert of its own (`aghoo_admission_degraded` = 1 while active; log `admission: Redis unavailable (...); local
buckets at 1/<n> of each limit`).
- **Behavior** (F10 PASS): turns keep completing; admission uses local buckets at limit / (`NOEMA_EXPECTED_PODS` x
  `WEB_CONCURRENCY`); cross-pod cancel fails (500) and a cross-pod attach ends with 410 `resume_window_expired`;
  key revocation falls back to the 60 s cache TTL; config changes arrive by the 5 s poll.
- **Mitigate**: restore Valkey; set `NOEMA_EXPECTED_PODS` to the real api pod count so the local shares stay right.
  Production requirements (`noeviction`, persistence, replica): [deployment.md](deployment.md#9-datastores-in-production).

### postgres-down

- **Behavior** (F11 PASS): `store: true` turns fail fast with a retryable 500 `internal_error`; `store: false` turns
  continue; billing and audit queue in the Redis outbox and drain once Postgres is back; finalized turns are stored
  exactly once.
- **Mitigate**: restore Postgres; watch [noema-outbox-redis-backlog](#noema-outbox-redis-backlog) drain to 0. A
  restore from backup: [deployment.md](deployment.md#9-datastores-in-production) (Multi-AZ, point-in-time
  recovery, the restore steps).

## Lifecycle (design 08 §6)

### model-deprecation

- **Symptoms**: a Bedrock lifecycle notice for a model in `models`.
- **Check**: which profiles use it (the config's `decision_profiles` and `generation_profiles`); pinned clients
  (`request_overrides`).
- **Commands**: `$A config validate <file>`, `$A config propose <file>`, `config approve <version>` by a second
  operator ([configuration.md](configuration.md) section 3).
- **Mitigate**: add the replacement model (design 08 §4), run old and new side by side in new profile versions,
  migrate pinned clients, then remove the old model. A new profile version starts `uncalibrated`.
- **Escalate**: decision and orchestrator owners for capability checks; the owner for a member swap.

### tenant-onboarding

The steps are in [tenant-onboarding.md](tenant-onboarding.md#1-steps).

### tenant-offboarding

The steps are in [tenant-onboarding.md](tenant-onboarding.md#5-offboarding).

### api-key-compromised

- **Action**: `$A keys revoke <key_id>`: refused by every pod within about 1 s with Redis up, within 60 s without.
  Mint a replacement with `$A keys create` and hand it over. Check the access log for the `key_id`
  (`... key_id=<id>` in every access line).
- **Escalate**: security.

### config-rollback

- **Action**: `$A config list` (find the last good version), `$A config rollback <version>`, then
  `config approve <version>` by a second operator. Pods load it within 5 s; running turns finish on their pinned
  snapshot. A version a pod cannot load is kept out by the pod itself (ERROR `platform config v<n> rejected;
  keeping the current one`).
- **Escalate**: the author of the change.
