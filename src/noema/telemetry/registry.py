"""The closed telemetry name registry (spec-02 §12, IMPL-006, 08 §7, spec-06 §16, spec-11).

Every span name, span attribute key and metric (with its kind and label keys) Noema may emit is declared here.
`Telemetry` rejects anything else: an error under `NOEMA_ENV=test`, dropped and logged otherwise. The registry is
frozen with the telemetry package; additions go through a change request.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, get_args

from noema.core.protocols.telemetry import SpanName

from .genai import CACHE_WRITE_ATTRIBUTE

SPAN_NAMES: Final[frozenset[str]] = frozenset(get_args(SpanName))

# --- span attributes -------------------------------------------------------------------------------------------------
_GEN_AI_ATTRIBUTES: Final = (
    # 08 §7 (GenAI semconv names through the mapping layer, genai.py)
    "gen_ai.provider.name",
    "gen_ai.operation.name",
    "gen_ai.request.model",
    "gen_ai.response.finish_reasons",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.reasoning.output_tokens",
    "gen_ai.response.time_to_first_chunk",
    "gen_ai.conversation.id",
    CACHE_WRITE_ATTRIBUTE,  # IMPL-029d, verified against the pinned semconv package
)
_AGHOO_ATTRIBUTES: Final = (
    # 08 §7
    "aghoo.tenant",
    "aghoo.profile",
    "aghoo.profile_version",
    "aghoo.decision.calibration_status",
    "aghoo.decision.band",
    "aghoo.member.elicitation",
    "aghoo.member.input_view",
    "aghoo.attempt.spec_signature",
    "aghoo.attempt.outcome",
    "aghoo.conditioning.answer_mode",
    "aghoo.degradation.reason",
    # IMPL-006
    "aghoo.member.id",
    "aghoo.decision.cache_hit",
    "aghoo.credentials.source",
    # spec-06 §16: decision.member (member id, question, status, backup; hierarchical round and group) and
    # decision.escalation (from, to, questions)
    "aghoo.member.question",
    "aghoo.member.status",
    "aghoo.member.backup",
    "aghoo.decision.round",
    "aghoo.decision.group",
    "aghoo.escalation.from",
    "aghoo.escalation.to",
    "aghoo.escalation.questions",
    # C2a (media-pipeline.md item 3): media.extract
    "aghoo.media.kind",
    "aghoo.media.status",
)
ERROR_ATTRIBUTE: Final = "error.type"  # OTel semconv; set by Span.error(code) with an ErrorCode or member error code

SPAN_ATTRIBUTES: Final[frozenset[str]] = frozenset((*_GEN_AI_ATTRIBUTES, *_AGHOO_ATTRIBUTES, ERROR_ATTRIBUTE))

# --- metrics ---------------------------------------------------------------------------------------------------------
MetricKind = Literal["counter", "histogram", "gauge"]


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One pre-declared instrument: `counter` (`Instruments.add`), `histogram` (`observe`, seconds) or `gauge`
    (`set`). `labels` is the closed set of label keys; a missing label is exported as ""."""

    name: str
    kind: MetricKind
    labels: tuple[str, ...]
    description: str


def _m(name: str, kind: MetricKind, labels: tuple[str, ...], description: str) -> MetricSpec:
    return MetricSpec(name=name, kind=kind, labels=labels, description=description)


_LATENCY_LABELS: Final = ("profile", "model", "strategy")
_METRIC_LIST: Final = (
    # 08 §7
    _m("aghoo_ttft_seconds", "histogram", _LATENCY_LABELS, "Ingress to the first generation.delta."),
    _m("aghoo_first_answer_seconds", "histogram", _LATENCY_LABELS, "Attempt start to the first answer text."),
    _m("aghoo_e2e_seconds", "histogram", _LATENCY_LABELS, "Ingress to turn.completed."),
    _m("aghoo_decision_seconds", "histogram", ("profile", "stage", "member"), "Decision time (stage=quorum|all)."),
    _m("aghoo_decision_member_status_total", "counter", ("member", "status"), "Member results by status."),
    _m("aghoo_spec_restarts_total", "counter", ("profile",), "spec_changed restarts."),
    _m("aghoo_spec_agreement_ratio", "gauge", ("profile",), "Share of turns whose decided spec equals spec0."),
    _m("aghoo_conditioning_total", "counter", ("answer_mode",), "Conditioning blocks rendered."),
    _m("aghoo_escalations_total", "counter", ("from_profile", "trigger", "outcome"), "Escalation stages."),
    _m("aghoo_verification_flags_total", "counter", ("question",), "Verification answers that flagged."),
    _m("aghoo_wasted_output_tokens_total", "counter", ("profile", "reason"), "Output tokens of waste attempts."),
    _m("aghoo_degradations_total", "counter", ("reason", "model"), "Media and parameter degradations."),
    _m("aghoo_cache_hits_total", "counter", ("layer",), "Cache hits."),
    _m("aghoo_bedrock_cached_input_ratio", "gauge", ("model",), "Cached share of Bedrock input tokens."),
    _m("aghoo_bedrock_throttles_total", "counter", ("tenant", "model"), "Bedrock throttling responses."),
    _m("aghoo_circuit_state", "gauge", ("model", "region"), "Breaker state (0 closed, 1 half-open, 2 open)."),
    _m("aghoo_calibration_ece", "gauge", ("profile", "signature", "view_signature"), "Calibrator ECE."),
    _m("aghoo_label_consensus_ratio", "gauge", ("profile", "question", "class"), "Labeler consensus rate."),
    _m("aghoo_cost_usd_total", "counter", ("tenant", "line"), "Recorded cost in USD."),
    _m("aghoo_event_loop_lag_seconds", "gauge", (), "Event-loop lag."),
    _m("aghoo_queue_depth", "gauge", (), "Admission queue depth."),
    # IMPL-006 additions
    _m("aghoo_cache_misses_total", "counter", ("layer", "reason"), "Cache misses (reason=miss|bypass|error)."),
    _m("aghoo_decision_parse_errors_total", "counter", ("member", "code"), "Member parse errors."),
    _m("aghoo_decision_quorum_reached_seconds", "histogram", ("profile",), "Time until quorum on every critical."),
    _m("aghoo_tenant_access_failures_total", "counter", ("tenant", "cause"), "Tenant access failures."),
    _m("aghoo_security_violations_total", "counter", ("kind",), "Security violations (e.g. account_mismatch)."),
    _m("aghoo_sandbox_violations_total", "counter", ("kind",), "Sandbox violations."),
    _m("aghoo_outbox_backlog", "gauge", ("sink",), "Outbox records waiting per sink."),
    _m("aghoo_outbox_dropped_total", "counter", (), "Outbox records dropped."),
    _m("aghoo_stuck_attempts_total", "counter", (), "Attempts left non-terminal at turn exit."),
    _m("aghoo_idempotency_attach_total", "counter", ("result",), "Idempotent duplicate attaches."),
    _m("aghoo_reservation_overrun_total", "counter", ("kind",), "Calls above the reserved count."),
    # spec-06 §16
    _m("aghoo_decision_hierarchical_total", "counter", ("profile", "outcome", "reason"), "Hierarchical answers."),
    # spec-11 §5
    _m("aghoo_idempotency_store_errors_total", "counter", (), "Idempotency bodies that could not be stored."),
    # C2a: labeling (labeling.md item 2; 04 §5 and 02 §5.5 label-distribution-shift alert input)
    _m("aghoo_labeling_items_total", "counter", ("question", "outcome"), "Labeled items (kept|disagreed|refused|...)."),
    _m("aghoo_calibrator_fits_total", "counter", ("status",), "Calibrator fits (accepted|rejected)."),
    _m("aghoo_calibrator_downgrades_total", "counter", ("reason",), "Active calibrators downgraded."),
    _m("aghoo_label_class_share", "gauge", ("profile", "question", "class"), "Share of each label class."),
    # C2a: media jobs (media-pipeline.md items 3 and 13; IMPL-074 inline ingest budget)
    _m("aghoo_media_jobs_total", "counter", ("kind", "status"), "Sandbox media jobs by kind and status."),
    _m("aghoo_media_job_seconds", "histogram", ("kind",), "Sandbox media job wall time."),
    _m("aghoo_inline_ingest_seconds", "histogram", (), "Inline image ingest time (p95 < 400 ms, IMPL-074)."),
    _m("aghoo_sandbox_queue_seconds", "histogram", ("kind",), "Wait for a sandbox slot."),
    # C2a: admission (ops-admission.md item 1; the C1/F10 gates)
    _m("aghoo_admission_rejections_total", "counter", ("code",), "Admission 429s (rpm|tpm_exceeded, queue_timeout)."),
    _m("aghoo_turns_shed_total", "counter", (), "createTurn 429 queue_timeout at the per-process turn cap."),
    _m("aghoo_turn_slots_held", "gauge", (), "Turn slots held by this process (running + admitted turns, decisions)."),
    _m("aghoo_admission_degraded", "gauge", (), "1 while the Redis-down local fallback is active."),
    _m("aghoo_admission_refund_failures_total", "counter", (), "Redis refunds or releases that failed."),
    _m("aghoo_admission_pending_releases", "gauge", (), "Slot releases kept for a retry."),
    # Wave E3: datastore pools (C2b R4; spec-10 C2 pool saturation and Redis/Postgres latency), composition
    _m("aghoo_db_pool_size", "gauge", ("pool",), "Connections the Postgres pool may open (max_size)."),
    _m("aghoo_db_pool_in_use", "gauge", ("pool",), "Postgres connections checked out."),
    _m("aghoo_db_pool_acquire_seconds", "histogram", ("pool",), "Wait for a Postgres connection (1/16 sampled)."),
    _m("aghoo_db_command_seconds", "histogram", ("pool",), "A unit of work, acquire to release (1/16 sampled)."),
    _m("aghoo_redis_pool_size", "gauge", (), "Connections the Redis pool may open (max_connections)."),
    _m("aghoo_redis_pool_in_use", "gauge", (), "Redis connections checked out (pub/sub ones included)."),
    _m("aghoo_redis_pool_acquire_seconds", "histogram", (), "Wait for a Redis connection (1/16 sampled)."),
    _m("aghoo_redis_command_seconds", "histogram", ("command",), "A Redis command or pipeline (1/16 sampled)."),
)
METRICS: Final[Mapping[str, MetricSpec]] = MappingProxyType({m.name: m for m in _METRIC_LIST})

# Histogram buckets (seconds) for every *_seconds histogram.
SECONDS_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 30.0, 60.0)

assert len(METRICS) == len(_METRIC_LIST), "duplicate metric declaration"


class UndeclaredTelemetry(AssertionError):
    """An undeclared span name, attribute key, metric, label key or instrument kind (NOEMA_ENV=test only; other
    environments drop the item and log a warning)."""


def check_span(name: str, attrs: Mapping[str, object]) -> list[str]:
    """Problems with a span name and its attribute keys (empty = valid)."""
    problems = [] if name in SPAN_NAMES else [f"undeclared span name {name!r}"]
    problems += [f"undeclared span attribute {k!r}" for k in attrs if k not in SPAN_ATTRIBUTES]
    return problems


def check_metric(name: str, kind: MetricKind, labels: Mapping[str, str]) -> list[str]:
    """Problems with one instrument use (empty = valid)."""
    spec = METRICS.get(name)
    if spec is None:
        return [f"undeclared metric {name!r}"]
    problems = [] if spec.kind == kind else [f"metric {name!r} is a {spec.kind}, not a {kind}"]
    problems += [f"undeclared label {k!r} on {name!r}" for k in labels if k not in spec.labels]
    return problems
