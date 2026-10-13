"""Generate the Noema Grafana dashboards (deploy/observability/grafana/dashboards/*.json).

    uv run --frozen --no-sync python deploy/observability/grafana/build_dashboards.py

The panels are declared below; the JSON files are generated and committed. tests/unit/observability checks that the
committed files equal this script's output, that every panel is well formed, and that every metric a panel queries
is declared in src/noema/telemetry/registry.py. docs/operations/observability.md explains every panel.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).resolve().parent / "dashboards"
DS = {"type": "prometheus", "uid": "${datasource}"}
Panel = dict[str, Any]


def ts(
    title: str,
    desc: str,
    *targets: tuple[str, str],
    unit: str = "short",
    thr: float | None = None,
    mappings: dict[str, str] | None = None,
    w: int = 12,
) -> Panel:
    """A time-series panel; `thr` draws the alert threshold as a red line."""
    defaults: dict[str, Any] = {"unit": unit, "min": 0, "custom": {"thresholdsStyle": {"mode": "off"}}}
    if thr is not None:
        defaults["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": "green", "value": None}, {"color": "red", "value": thr}],
        }
        defaults["custom"]["thresholdsStyle"] = {"mode": "line"}
    if mappings:
        defaults["mappings"] = [{"type": "value", "options": {k: {"text": v} for k, v in mappings.items()}}]
    return {
        "type": "timeseries",
        "title": title,
        "description": desc,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": [_target(i, expr, legend) for i, (expr, legend) in enumerate(targets)],
        "_size": (w, 8),
    }


def stat(title: str, desc: str, expr: str, *, unit: str = "short", thr: float | None = None) -> Panel:
    """A single-value panel (the last value); red at or above `thr`."""
    steps: list[dict[str, Any]] = [{"color": "green", "value": None}]
    if thr is not None:
        steps.append({"color": "red", "value": thr})
    return {
        "type": "stat",
        "title": title,
        "description": desc,
        "fieldConfig": {
            "defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}},
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "area",
            "textMode": "auto",
        },
        "targets": [_target(0, expr, "")],
        "_size": (6, 4),
    }


def row(title: str) -> Panel:
    return {"type": "row", "title": title, "collapsed": False, "panels": [], "_size": (24, 1)}


def _target(i: int, expr: str, legend: str) -> dict[str, Any]:
    return {"refId": chr(ord("A") + i), "datasource": DS, "expr": expr, "legendFormat": legend, "range": True}


def _layout(panels: list[Panel]) -> list[Panel]:
    """Left-to-right flow layout on Grafana's 24-column grid; sequential panel ids."""
    out, x, y, line_h = [], 0, 0, 0
    for pid, panel in enumerate(panels, start=1):
        p = dict(panel)
        w, h = p.pop("_size")
        if x + w > 24:
            x, y, line_h = 0, y + line_h, 0
        p = {"id": pid, "gridPos": {"h": h, "w": w, "x": x, "y": y}, **p}
        if p["type"] != "row":
            p["datasource"] = DS
        out.append(p)
        x, line_h = x + w, max(line_h, h)
    return out


def dashboard(uid: str, title: str, desc: str, panels: list[Panel]) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "description": desc,
        "tags": ["noema"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "graphTooltip": 1,
        "time": {"from": "now-6h", "to": "now"},
        "refresh": "1m",
        "links": [{"title": "Noema dashboards", "type": "dashboards", "tags": ["noema"], "asDropdown": True}],
        "annotations": {"list": []},
        "templating": {
            "list": [
                {"name": "datasource", "label": "Prometheus", "type": "datasource", "query": "prometheus", "hide": 0}
            ]
        },
        "panels": _layout(panels),
    }


def q(fn: str, metric: str, by: str, window: str = "5m") -> str:
    """`histogram_quantile(fn, sum by (by, le) (rate(metric_bucket[window])))`, fn like 0.95."""
    group = f"{by}, le" if by else "le"
    return f"histogram_quantile({fn}, sum by ({group}) (rate({metric}_bucket[{window}])))"


TTFT, FIRST, E2E = "aghoo_ttft_seconds", "aghoo_first_answer_seconds", "aghoo_e2e_seconds"


def overview() -> dict[str, Any]:
    return dashboard(
        "noema-overview",
        "Noema / Overview and SLO",
        "Phase 1 SLOs, traffic and the worst pod.",
        [
            row("SLO"),
            ts(
                "Availability error ratio",
                "Requests shed with 429 queue_timeout / (turns that ran + sheds). The SLO "
                "budget is 0.1% (99.9% monthly).",
                ("noema:availability_errors:ratio_rate5m", "5m"),
                ("noema:availability_errors:ratio_rate1h", "1h"),
                ("noema:availability_errors:ratio_rate6h", "6h"),
                unit="percentunit",
                thr=0.001,
            ),
            ts(
                "Availability budget burn rate",
                "Error ratio / 0.001. 14.4 over 1 h pages; 6 over 6 h opens a ticket.",
                ("noema:availability_errors:ratio_rate1h / 0.001", "1h"),
                ("noema:availability_errors:ratio_rate6h / 0.001", "6h"),
                thr=14.4,
            ),
            ts(
                "Gateway overhead (mean proxy)",
                "Mean TTFT minus mean model first text, holdback turns. SLO: p95 <= "
                "25 ms. Includes holdback and failover time.",
                ("noema:proxy_overhead_seconds:mean5m", "5m"),
                ("noema:proxy_overhead_seconds:mean1h", "1h"),
                unit="s",
                thr=0.025,
            ),
            ts(
                "TTFT p50 / p95",
                "Ingress to the first generation.delta, every turn (model time included).",
                (q("0.5", TTFT, ""), "p50"),
                (q("0.95", TTFT, ""), "p95"),
                unit="s",
            ),
            row("Traffic"),
            ts(
                "Turns per second",
                "Turns that ran, by commit strategy.",
                ("sum by (strategy) (rate(aghoo_e2e_seconds_count[5m]))", "{{strategy}}"),
                unit="reqps",
            ),
            ts(
                "Sheds per second",
                "429 queue_timeout at the per-process turn cap, per pod.",
                ("sum by (pod) (rate(aghoo_turns_shed_total[5m]))", "{{pod}}"),
                unit="reqps",
            ),
            ts(
                "E2E p50 / p95",
                "Ingress to turn.completed, every turn.",
                (q("0.5", E2E, ""), "p50"),
                (q("0.95", E2E, ""), "p95"),
                unit="s",
            ),
            ts(
                "Processes reporting",
                "Noema processes exporting metrics, per job.",
                ("count by (job) (aghoo_event_loop_lag_seconds)", "{{job}}"),
            ),
            row("Worst pod"),
            stat(
                "Max streams per pod",
                "Highest turn-slot count of any pod; plan 100 per pod.",
                "max(noema:pod_turn_slots_held:sum)",
                thr=100,
            ),
            stat(
                "Max pod CPU (cores)",
                "Busiest api pod; scale out above 0.62 vCPU.",
                "max(noema:pod_cpu_cores:rate5m)",
                thr=0.62,
            ),
            stat(
                "Max event-loop lag p99",
                "Worst api pod; C2 bound 50 ms.",
                "max(noema:pod_event_loop_lag_seconds:p99_5m)",
                unit="s",
                thr=0.05,
            ),
            stat(
                "Sheds (1 h)",
                "Requests shed in the last hour, all pods.",
                "sum(increase(aghoo_turns_shed_total[1h]))",
                thr=1,
            ),
        ],
    )


def decisions() -> dict[str, Any]:
    dq = "aghoo_decision_seconds"
    return dashboard(
        "noema-decisions",
        "Noema / Decisions",
        "Decision latency, members, escalation, cache, spec stability, calibration and labeling.",
        [
            row("Latency and quorum"),
            ts(
                "Decision time p95 by stage",
                "stage=quorum: quorum on every critical question; stage=all: every member.",
                (q("0.95", dq, "stage"), "{{stage}}"),
                unit="s",
            ),
            ts(
                "Quorum reached p95 by profile",
                "Commit-critical quorum; default deadline 1,000 ms.",
                ("noema:decision_quorum_seconds:p95_10m", "{{profile}}"),
                unit="s",
                thr=1,
            ),
            ts(
                "DeepSeek member p99",
                "No data until per-member decision times are exported (member is empty today).",
                (
                    "histogram_quantile(0.99, sum by (le) "
                    '(rate(aghoo_decision_seconds_bucket{member=~"deepseek.*"}[10m])))',
                    "p99",
                ),
                unit="s",
                thr=1.5,
            ),
            row("Members"),
            ts(
                "Member results by status",
                "ok, refused, timeout, error, late, skipped, per member model alias.",
                ("sum by (member, status) (rate(aghoo_decision_member_status_total[5m]))", "{{member}} {{status}}"),
                unit="ops",
            ),
            ts(
                "Parse-error ratio by member",
                "Parse errors / member results over 30 minutes; alert above 1%.",
                ("noema:member_parse_error_ratio:rate30m", "{{member}}"),
                unit="percentunit",
                thr=0.01,
            ),
            ts(
                "Parse errors by code",
                "Member parse errors by member and code.",
                ("sum by (member, code) (rate(aghoo_decision_parse_errors_total[5m]))", "{{member}} {{code}}"),
                unit="ops",
            ),
            row("Escalation, hierarchical, cache"),
            ts(
                "Escalations",
                "Escalation stages by source profile, trigger and outcome.",
                (
                    "sum by (from_profile, trigger, outcome) (rate(aghoo_escalations_total[5m]))",
                    "{{from_profile}} {{trigger}} {{outcome}}",
                ),
                unit="ops",
            ),
            ts(
                "Hierarchical answers",
                "Hierarchical method outcomes (scored, refusal, unavailable) and reasons.",
                (
                    "sum by (profile, outcome, reason) (rate(aghoo_decision_hierarchical_total[5m]))",
                    "{{profile}} {{outcome}} {{reason}}",
                ),
                unit="ops",
            ),
            ts(
                "L2 decision cache hit ratio",
                "L2 hits / (hits + misses); misses include bypass and error.",
                (
                    'sum(rate(aghoo_cache_hits_total{layer="l2"}[5m]))'
                    ' / (sum(rate(aghoo_cache_hits_total{layer="l2"}[5m]))'
                    ' + sum(rate(aghoo_cache_misses_total{layer="l2"}[5m])))',
                    "hit ratio",
                ),
                unit="percentunit",
            ),
            ts(
                "Cache misses by reason",
                "miss, bypass or error, per layer.",
                ("sum by (layer, reason) (rate(aghoo_cache_misses_total[5m]))", "{{layer}} {{reason}}"),
                unit="ops",
            ),
            row("Spec stability"),
            ts(
                "Spec restarts per turn",
                "spec_changed restarts / turns over 1 hour; alert above 10%.",
                ("noema:spec_restart_ratio:rate1h", "restarts per turn"),
                unit="percentunit",
                thr=0.10,
            ),
            ts(
                "Spec agreement ratio",
                "Share of turns whose decided spec equals spec0 (per-process running share).",
                ("avg by (profile) (aghoo_spec_agreement_ratio)", "{{profile}}"),
                unit="percentunit",
            ),
            ts(
                "Conditioning blocks",
                "Conditioning blocks rendered, by answer mode.",
                ("sum by (answer_mode) (rate(aghoo_conditioning_total[5m]))", "{{answer_mode}}"),
                unit="ops",
            ),
            row("Calibration and labeling"),
            ts(
                "Calibrator ECE",
                "Latest ECE per calibrator key (weekly drift job or fit holdout); alert above 0.08.",
                (
                    "max by (profile, signature, view_signature) (aghoo_calibration_ece)",
                    "{{profile}} {{signature}} {{view_signature}}",
                ),
                thr=0.08,
            ),
            ts(
                "Labeler consensus by class",
                "Consensus rate of the latest labeling run per class; alert on a drop of "
                "more than 10 points from the previous week.",
                ("max by (profile, question, class) (aghoo_label_consensus_ratio)", "{{question}} {{class}}"),
                unit="percentunit",
            ),
            ts(
                "Label class share",
                "Share of each label class in the latest run.",
                ("max by (profile, question, class) (aghoo_label_class_share)", "{{question}} {{class}}"),
                unit="percentunit",
            ),
            ts(
                "Labeled items per hour",
                "Labeled items by question and outcome (kept, disagreed, refused, ...).",
                ("sum by (question, outcome) (increase(aghoo_labeling_items_total[1h]))", "{{question}} {{outcome}}"),
            ),
            ts(
                "Calibrator fits and downgrades per day",
                "Fits by status (accepted, rejected) and active calibrators downgraded by reason.",
                ("sum by (status) (increase(aghoo_calibrator_fits_total[1d]))", "fit {{status}}"),
                ("sum by (reason) (increase(aghoo_calibrator_downgrades_total[1d]))", "downgrade {{reason}}"),
            ),
        ],
    )


def generation() -> dict[str, Any]:
    hb = 'strategy="holdback"'
    return dashboard(
        "noema-generation",
        "Noema / Generation and streaming",
        "Turn latency, upstream health, waste and idempotent replays.",
        [
            row("Latency"),
            ts(
                "TTFT p95 by route and model",
                "Ingress to the first delta, by served route (profile) and model.",
                (q("0.95", TTFT, "profile, model"), "{{profile}} {{model}}"),
                unit="s",
            ),
            ts(
                "Model first text p95 by model",
                "Attempt start to the first answer text, every attempt.",
                (q("0.95", FIRST, "model"), "{{model}}"),
                unit="s",
            ),
            ts(
                "E2E p95 by route",
                "Ingress to turn.completed, by served route.",
                (q("0.95", E2E, "profile"), "{{profile}}"),
                unit="s",
            ),
            ts(
                "Overhead proxy inputs",
                "The two means behind the overhead proxy (holdback turns): their gap is the "
                "Overview's gateway overhead.",
                (f"sum(rate({TTFT}_sum{{{hb}}}[5m])) / sum(rate({TTFT}_count{{{hb}}}[5m]))", "mean TTFT"),
                (f"sum(rate({FIRST}_sum{{{hb}}}[5m])) / sum(rate({FIRST}_count{{{hb}}}[5m]))", "mean model first text"),
                unit="s",
            ),
            row("Upstream health"),
            ts(
                "Circuit state",
                "Breaker state per model and region: 0 closed, 1 half-open, 2 open (alert).",
                ("max by (model, region) (aghoo_circuit_state)", "{{model}} {{region}}"),
                thr=2,
                mappings={"0": "closed", "1": "half-open", "2": "open"},
            ),
            ts(
                "Bedrock throttles by model",
                "ThrottlingException responses per second.",
                ("sum by (model) (rate(aghoo_bedrock_throttles_total[5m]))", "{{model}}"),
                unit="ops",
            ),
            ts(
                "Bedrock cached input ratio",
                "Cached share of Bedrock input tokens since process start, worst process.",
                ("min by (model) (aghoo_bedrock_cached_input_ratio)", "{{model}}"),
                unit="percentunit",
            ),
            ts(
                "Degradations",
                "Media and parameter degradations by reason and model.",
                ("sum by (reason, model) (rate(aghoo_degradations_total[5m]))", "{{reason}} {{model}}"),
                unit="ops",
            ),
            row("Waste and reliability"),
            ts(
                "Wasted output tokens",
                "Output tokens of attempts that did not commit, by reason.",
                ("sum by (reason) (rate(aghoo_wasted_output_tokens_total[5m]))", "{{reason}}"),
            ),
            ts(
                "Stuck attempts",
                "Attempts left non-terminal at turn exit (watchdog); expected 0.",
                ("sum by (pod) (increase(aghoo_stuck_attempts_total[15m]))", "{{pod}}"),
                thr=1,
            ),
            ts(
                "Idempotent duplicate attaches",
                "Duplicates of a keyed request by result (in_flight, completed, lost, deadline).",
                ("sum by (result) (rate(aghoo_idempotency_attach_total[5m]))", "{{result}}"),
                unit="ops",
            ),
            ts(
                "Idempotency store errors",
                "Response bodies that could not be stored (re-execution risk).",
                ("sum(increase(aghoo_idempotency_store_errors_total[1h]))", "per hour"),
                thr=1,
            ),
        ],
    )


def tenancy_cost() -> dict[str, Any]:
    spent = 'line!="cache_savings"'
    return dashboard(
        "noema-tenancy-cost",
        "Noema / Tenancy and cost",
        "Recorded cost, cost per turn, per-tenant throttles, access failures and security violations.",
        [
            row("Cost"),
            ts(
                "Cost per hour by tenant",
                "Recorded USD per hour (every line except cache savings).",
                (f"sum by (tenant) (increase(aghoo_cost_usd_total{{{spent}}}[1h]))", "{{tenant}}"),
                unit="currencyUSD",
            ),
            ts(
                "Cost per hour by line",
                "decision, generation, labeling, verification, waste and cache_savings (avoided).",
                ("sum by (line) (increase(aghoo_cost_usd_total[1h]))", "{{line}}"),
                unit="currencyUSD",
            ),
            ts(
                "Cost per turn",
                "Spent lines (decision, generation, verification, waste) / turns, 1 h; alert above the "
                "budget set in the rule.",
                ("noema:cost_usd_per_turn:rate1h", "USD per turn"),
                unit="currencyUSD",
                thr=0.05,
            ),
            ts(
                "Cache savings by tenant",
                "Avoided cost per hour (cache_savings lines).",
                ('sum by (tenant) (increase(aghoo_cost_usd_total{line="cache_savings"}[1h]))', "{{tenant}}"),
                unit="currencyUSD",
            ),
            row("Tenant health"),
            ts(
                "Bedrock throttles by tenant",
                "ThrottlingException responses per tenant and model; admission should throttle first.",
                ("sum by (tenant, model) (rate(aghoo_bedrock_throttles_total[5m]))", "{{tenant}} {{model}}"),
                unit="ops",
            ),
            ts(
                "Tenant access failures",
                "AssumeRole and account failures (tenant_access_misconfigured) by cause.",
                ("sum by (tenant, cause) (increase(aghoo_tenant_access_failures_total[15m]))", "{{tenant}} {{cause}}"),
            ),
            ts(
                "Security violations",
                "account_mismatch, pubsub_tenant_mismatch and other violations by kind.",
                ("sum by (kind) (increase(aghoo_security_violations_total[15m]))", "{{kind}}"),
                thr=1,
            ),
        ],
    )


def media() -> dict[str, Any]:
    return dashboard(
        "noema-media",
        "Noema / Media",
        "Sandbox media jobs, inline ingest and sandbox violations.",
        [
            row("Media jobs"),
            ts(
                "Media jobs by kind and status",
                "Sandbox jobs per second (status ok, rejected, failed, timeout, violation).",
                ("sum by (kind, status) (rate(aghoo_media_jobs_total[5m]))", "{{kind}} {{status}}"),
                unit="ops",
            ),
            ts(
                "Media job p95 by kind",
                "Sandbox job wall time.",
                (q("0.95", "aghoo_media_job_seconds", "kind"), "{{kind}}"),
                unit="s",
            ),
            ts(
                "Sandbox slot wait p95 by kind",
                "Wait for a sandbox slot before the job starts.",
                (q("0.95", "aghoo_sandbox_queue_seconds", "kind"), "{{kind}}"),
                unit="s",
            ),
            ts(
                "Inline image ingest p95",
                "Inline image ingest on the turn path; budget p95 < 400 ms (IMPL-074).",
                (q("0.95", "aghoo_inline_ingest_seconds", ""), "p95"),
                unit="s",
                thr=0.4,
            ),
            ts(
                "Sandbox violations",
                "Jail violations by kind (each is also a security violation).",
                ("sum by (kind) (increase(aghoo_sandbox_violations_total[15m]))", "{{kind}}"),
                thr=1,
            ),
        ],
    )


def admission() -> dict[str, Any]:
    return dashboard(
        "noema-admission",
        "Noema / Admission and overload",
        "Streams per pod, sheds, admission 429s, the Redis-down fallback and the scale signals.",
        [
            row("Scale signals"),
            ts(
                "Streams per pod",
                "Turn slots held, both api processes; plan 100 per pod (knee 125).",
                ("noema:pod_turn_slots_held:sum", "{{pod}}"),
                thr=100,
            ),
            ts(
                "Pod CPU (cores)",
                "api pod CPU (cAdvisor); scale out above 0.62 vCPU.",
                ("noema:pod_cpu_cores:rate5m", "{{pod}}"),
                thr=0.62,
            ),
            ts(
                "Event-loop lag p99 per pod",
                "Worst api process of each pod over 5 minutes; C2 bound 50 ms.",
                ("noema:pod_event_loop_lag_seconds:p99_5m", "{{pod}}"),
                unit="s",
                thr=0.05,
            ),
            ts(
                "Sheds per pod (5 min)",
                "429 queue_timeout at the per-process turn cap; any shed alerts.",
                ("sum by (pod) (increase(aghoo_turns_shed_total[5m]))", "{{pod}}"),
                thr=1,
            ),
            row("Admission"),
            ts(
                "Admission rejections by code",
                "Admission 429s: rpm_exceeded, tpm_exceeded, queue_timeout.",
                ("sum by (code) (rate(aghoo_admission_rejections_total[5m]))", "{{code}}"),
                unit="reqps",
            ),
            ts(
                "Admission queue depth",
                "Requests waiting in the per-pod weighted fair queue, worst process.",
                ("max by (pod) (aghoo_queue_depth)", "{{pod}}"),
            ),
            ts(
                "Redis-down fallback active",
                "1 while admission runs on the local buckets (Redis down).",
                ("max by (pod) (aghoo_admission_degraded)", "{{pod}}"),
                thr=1,
            ),
            ts(
                "Refund failures and pending releases",
                "Redis refunds or releases that failed, and the releases kept for a retry.",
                ("sum by (pod) (increase(aghoo_admission_refund_failures_total[15m]))", "{{pod}} failures"),
                ("max by (pod) (aghoo_admission_pending_releases)", "{{pod}} pending"),
            ),
            ts(
                "Reservation overruns",
                "Model calls above the reserved count, by kind (C1: 0).",
                ("sum by (kind) (increase(aghoo_reservation_overrun_total[15m]))", "{{kind}}"),
                thr=1,
            ),
        ],
    )


def datastores() -> dict[str, Any]:
    return dashboard(
        "noema-datastores",
        "Noema / Datastores",
        "Postgres and Redis pools and latency, and the write-behind outbox.",
        [
            row("Postgres"),
            ts(
                "Postgres pool in use / size",
                "Connections checked out and the pool maximum, per process.",
                ("sum by (pod, instance) (aghoo_db_pool_in_use)", "{{pod}} {{instance}} in use"),
                ("max(aghoo_db_pool_size)", "pool size"),
            ),
            ts(
                "Postgres pool utilisation",
                "In use / size, 5-minute average per process; alert at 90%.",
                ("noema:db_pool_utilization:avg5m", "{{pod}} {{instance}}"),
                unit="percentunit",
                thr=0.9,
            ),
            ts(
                "Postgres acquire wait p95",
                "Wait for a connection (sampled 1 in 16).",
                ("noema:db_pool_acquire_seconds:p95_5m", "{{pool}}"),
                unit="s",
            ),
            ts(
                "Postgres unit of work p50 / p95",
                "Acquire to release (sampled 1 in 16); alert p95 above 20 ms.",
                (q("0.5", "aghoo_db_command_seconds", "pool"), "{{pool}} p50"),
                ("noema:db_command_seconds:p95_5m", "{{pool}} p95"),
                unit="s",
                thr=0.02,
            ),
            row("Redis"),
            ts(
                "Redis pool utilisation",
                "In use / max connections, 5-minute average per process; alert at 90%.",
                ("noema:redis_pool_utilization:avg5m", "{{pod}} {{instance}}"),
                unit="percentunit",
                thr=0.9,
            ),
            ts(
                "Redis acquire wait p95",
                "Wait for a Redis connection (sampled 1 in 16).",
                ("noema:redis_pool_acquire_seconds:p95_5m", "p95"),
                unit="s",
            ),
            ts(
                "Redis command p99",
                "Every command or pipeline (sampled 1 in 16); 50 ms = admission's Redis-down timeout.",
                ("noema:redis_command_seconds:p99_5m", "p99"),
                unit="s",
                thr=0.05,
            ),
            ts(
                "Redis command p95 by command",
                "Per command name (MULTI and PIPELINE for batches).",
                (q("0.95", "aghoo_redis_command_seconds", "command"), "{{command}}"),
                unit="s",
            ),
            row("Outbox"),
            ts(
                "Outbox backlog by sink",
                "memory: records only in the process (both sinks down); redis: records waiting for the drainer.",
                ("max by (pod, sink) (aghoo_outbox_backlog)", "{{pod}} {{sink}}"),
            ),
            ts(
                "Outbox records dropped",
                "Write-behind records lost (deque bound with Redis down, or flush timeout).",
                ("sum by (pod) (increase(aghoo_outbox_dropped_total[5m]))", "{{pod}}"),
                thr=1,
            ),
        ],
    )


DASHBOARDS = (overview, decisions, generation, tenancy_cost, media, admission, datastores)


def render() -> dict[str, str]:
    """File name -> JSON text of every dashboard."""
    out = {}
    for build in DASHBOARDS:
        d = build()
        out[f"{d['uid']}.json"] = json.dumps(d, indent=2, ensure_ascii=False) + "\n"
    return out


def promql_rules() -> str:
    """Every panel query as a recording rule (JSON, which is YAML), so `promtool check rules` parses each one."""
    rules = []
    for build in DASHBOARDS:
        d = build()
        for p in d["panels"]:
            for t in p.get("targets", []):
                name = f"dashboard:{d['uid'].replace('-', '_')}:panel{p['id']}_{t['refId'].lower()}"
                rules.append({"record": name, "expr": t["expr"]})
    return json.dumps({"groups": [{"name": "dashboard-queries", "rules": rules}]}, indent=1) + "\n"


def main(argv: list[str]) -> int:
    """No arguments: write the dashboards. `--promql-rules PATH`: write the panel queries as a rule file."""
    if argv[:1] == ["--promql-rules"] and len(argv) == 2:
        Path(argv[1]).write_text(promql_rules())
        return 0
    if argv:
        sys.stderr.write("usage: build_dashboards.py [--promql-rules PATH]\n")
        return 2
    OUT_DIR.mkdir(exist_ok=True)
    for name, text in render().items():
        (OUT_DIR / name).write_text(text)
        sys.stdout.write(f"wrote {OUT_DIR / name}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
