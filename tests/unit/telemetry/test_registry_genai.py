"""The closed telemetry registry (IMPL-006, 08 §7, spec-06 §16, spec-11) and the GenAI attribute mapper
(IMPL-029d: the cache-write attribute is verified against the pinned semconv package)."""

from __future__ import annotations

from typing import get_args

from opentelemetry.semconv._incubating.attributes import gen_ai_attributes

from noema.core.protocols.telemetry import SpanName
from noema.core.values import UsageRecord
from noema.telemetry import genai
from noema.telemetry.registry import METRICS, SPAN_ATTRIBUTES, SPAN_NAMES, check_metric, check_span

# 08 §7 table + IMPL-006 additions + spec-06 §16 + spec-11 §5, with their label sets.
EXPECTED_METRICS = {
    "aghoo_ttft_seconds": ("histogram", {"profile", "model", "strategy"}),
    "aghoo_first_answer_seconds": ("histogram", {"profile", "model", "strategy"}),
    "aghoo_e2e_seconds": ("histogram", {"profile", "model", "strategy"}),
    "aghoo_decision_seconds": ("histogram", {"profile", "stage", "member"}),
    "aghoo_decision_member_status_total": ("counter", {"member", "status"}),
    "aghoo_spec_restarts_total": ("counter", {"profile"}),
    "aghoo_spec_agreement_ratio": ("gauge", {"profile"}),
    "aghoo_conditioning_total": ("counter", {"answer_mode"}),
    "aghoo_escalations_total": ("counter", {"from_profile", "trigger", "outcome"}),
    "aghoo_verification_flags_total": ("counter", {"question"}),
    "aghoo_wasted_output_tokens_total": ("counter", {"profile", "reason"}),
    "aghoo_degradations_total": ("counter", {"reason", "model"}),
    "aghoo_cache_hits_total": ("counter", {"layer"}),
    "aghoo_bedrock_cached_input_ratio": ("gauge", {"model"}),
    "aghoo_bedrock_throttles_total": ("counter", {"tenant", "model"}),
    "aghoo_circuit_state": ("gauge", {"model", "region"}),
    "aghoo_calibration_ece": ("gauge", {"profile", "signature", "view_signature"}),
    "aghoo_label_consensus_ratio": ("gauge", {"profile", "question", "class"}),
    "aghoo_cost_usd_total": ("counter", {"tenant", "line"}),
    "aghoo_event_loop_lag_seconds": ("gauge", set()),
    "aghoo_queue_depth": ("gauge", set()),
    "aghoo_cache_misses_total": ("counter", {"layer", "reason"}),
    "aghoo_decision_parse_errors_total": ("counter", {"member", "code"}),
    "aghoo_decision_quorum_reached_seconds": ("histogram", {"profile"}),
    "aghoo_tenant_access_failures_total": ("counter", {"tenant", "cause"}),
    "aghoo_security_violations_total": ("counter", {"kind"}),
    "aghoo_sandbox_violations_total": ("counter", {"kind"}),
    "aghoo_outbox_backlog": ("gauge", {"sink"}),
    "aghoo_outbox_dropped_total": ("counter", set()),
    "aghoo_stuck_attempts_total": ("counter", set()),
    "aghoo_idempotency_attach_total": ("counter", {"result"}),
    "aghoo_reservation_overrun_total": ("counter", {"kind"}),
    "aghoo_decision_hierarchical_total": ("counter", {"profile", "outcome", "reason"}),
    "aghoo_idempotency_store_errors_total": ("counter", set()),
    # C2a: labeling, media jobs and admission (spec-02 §12 registry additions)
    "aghoo_labeling_items_total": ("counter", {"question", "outcome"}),
    "aghoo_calibrator_fits_total": ("counter", {"status"}),
    "aghoo_calibrator_downgrades_total": ("counter", {"reason"}),
    "aghoo_label_class_share": ("gauge", {"profile", "question", "class"}),
    "aghoo_media_jobs_total": ("counter", {"kind", "status"}),
    "aghoo_media_job_seconds": ("histogram", {"kind"}),
    "aghoo_inline_ingest_seconds": ("histogram", set()),
    "aghoo_sandbox_queue_seconds": ("histogram", {"kind"}),
    "aghoo_admission_rejections_total": ("counter", {"code"}),
    "aghoo_turns_shed_total": ("counter", set()),
    "aghoo_turn_slots_held": ("gauge", set()),
    "aghoo_admission_degraded": ("gauge", set()),
    "aghoo_admission_refund_failures_total": ("counter", set()),
    "aghoo_admission_pending_releases": ("gauge", set()),
    # Wave E3, IMPL-088 (C2b R4): the datastore pools
    "aghoo_db_pool_size": ("gauge", {"pool"}),
    "aghoo_db_pool_in_use": ("gauge", {"pool"}),
    "aghoo_db_pool_acquire_seconds": ("histogram", {"pool"}),
    "aghoo_db_command_seconds": ("histogram", {"pool"}),
    "aghoo_redis_pool_size": ("gauge", set()),
    "aghoo_redis_pool_in_use": ("gauge", set()),
    "aghoo_redis_pool_acquire_seconds": ("histogram", set()),
    "aghoo_redis_command_seconds": ("histogram", {"command"}),
}


def test_metric_registry_is_exactly_the_spec() -> None:
    got = {name: (spec.kind, set(spec.labels)) for name, spec in METRICS.items()}
    assert got == EXPECTED_METRICS
    for name, spec in METRICS.items():
        assert name.startswith("aghoo_") and spec.description
        assert (spec.kind == "counter") == name.endswith("_total")


def test_span_names_are_the_protocol_literal() -> None:
    assert frozenset(get_args(SpanName)) == SPAN_NAMES
    assert check_span("generation.attempt", {"gen_ai.request.model": "m"}) == []
    assert check_span("asr.transcribe", {}) != []  # Phase 2


def test_attribute_registry_has_the_named_keys() -> None:
    required = {
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.finish_reasons",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.cache_read.input_tokens",
        "gen_ai.usage.reasoning.output_tokens",
        "gen_ai.response.time_to_first_chunk",
        "gen_ai.conversation.id",
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
        "aghoo.member.id",
        "aghoo.decision.cache_hit",
        "aghoo.credentials.source",
        "aghoo.decision.round",
        "aghoo.decision.group",
    }
    assert required <= SPAN_ATTRIBUTES
    assert check_span("turn", {"aghoo.not_declared": 1}) == ["undeclared span attribute 'aghoo.not_declared'"]


def test_check_metric() -> None:
    assert check_metric("aghoo_cost_usd_total", "counter", {"tenant": "t", "line": "decision"}) == []
    assert check_metric("aghoo_cost_usd_total", "gauge", {}) == [
        "metric 'aghoo_cost_usd_total' is a counter, not a gauge"
    ]
    assert check_metric("aghoo_x_total", "counter", {}) == ["undeclared metric 'aghoo_x_total'"]


def test_cache_write_attribute_comes_from_the_pinned_semconv() -> None:
    pinned = getattr(gen_ai_attributes, "GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS", None)
    assert pinned == "gen_ai.usage.cache_creation.input_tokens"  # present in 0.66b1 (IMPL-029d)
    assert pinned == genai.CACHE_WRITE_ATTRIBUTE
    assert genai.CACHE_WRITE_ATTRIBUTE in SPAN_ATTRIBUTES


def test_usage_and_request_mapping() -> None:
    attrs = genai.usage_attributes(
        UsageRecord(100, 20, cached_input_tokens=30, cache_write_input_tokens=10, reasoning_tokens=5)
    )
    assert attrs == {
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 20,
        "gen_ai.usage.cache_read.input_tokens": 30,
        "gen_ai.usage.cache_creation.input_tokens": 10,
        "gen_ai.usage.reasoning.output_tokens": 5,
    }
    req = genai.request_attributes("us.openai.gpt-6.1-sol")
    assert req == {
        "gen_ai.provider.name": "aws.bedrock",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "us.openai.gpt-6.1-sol",
    }
    assert set(attrs) | set(req) <= SPAN_ATTRIBUTES


def test_finish_reasons() -> None:
    assert [genai.finish_reason(s) for s in ("end_turn", "stop_sequence", "max_tokens", "content_filter")] == [
        "stop",
        "stop",
        "length",
        "content_filter",
    ]
    assert genai.response_attributes("max_tokens", time_to_first_chunk_s=0.7) == {
        "gen_ai.response.finish_reasons": "length",
        "gen_ai.response.time_to_first_chunk": 0.7,
    }
    assert genai.response_attributes(None, time_to_first_chunk_s=None) == {}
