"""Attempt usage (IMPL-020: provider usage, else input estimate + counted text and reasoning + the overrun
allowance when the stream was cut), cost, breaker outcomes, metrics and the generation.attempt span."""

from __future__ import annotations

from decimal import Decimal

import pytest

from noema.core.errors import ErrorCode, ErrorKind, NoemaError
from noema.core.protocols.adapters import MessageStart, Stop, TextDelta
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.ops import UsageLine
from noema.core.values import Cost, UsageRecord

from .rig import GLM, SOL, cancel, commit, drive, make_rig
from .scripted import Call, error_at, failure, reasoning, text_stream

SOL_A, GLM_A = "sol-6.1", "glm-5.3"
PROMPT_TOKENS = 7 + 2  # FakeTokenEstimator: "You are a helpful assistant." (28 chars) + "Hello" (5 chars)


async def test_a_cut_stream_is_estimated_with_the_overrun_allowance() -> None:
    events = [(0.1, MessageStart(at=0)), reasoning(0.2, 401), (0.5, TextDelta("123456789"))]
    rig = make_rig({SOL_A: [Call(events, hang=True)]})
    out = await drive(rig, actions=[(1.0, cancel("spec_changed"))])
    [rec] = out.records
    assert rec.usage == UsageRecord(
        input_tokens=PROMPT_TOKENS, output_tokens=2 + 87 + 64, reasoning_tokens=87, tokens_estimated=True
    )
    assert rec.cost.estimated
    assert rig.telemetry.counter("aghoo_wasted_output_tokens_total", profile="sol-fast", reason="spec_changed") == 153


async def test_a_stream_that_ended_without_usage_has_no_overrun() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("abcd", "efgh"))]})
    out = await drive(rig, hold=False)
    [rec] = out.records
    assert rec.usage == UsageRecord(PROMPT_TOKENS, 2, tokens_estimated=True)
    usage = out.of("generation.completed")[0][1]["usage"]
    assert (usage["output_tokens"], usage["tokens_estimated"], usage["cost_estimated"]) == (2, True, True)


async def test_provider_usage_wins_and_a_pre_byte_failure_has_no_overrun() -> None:
    provider = UsageRecord(100, 20, cached_input_tokens=30, reasoning_tokens=5)
    rig = make_rig(
        {
            "opus-5.5": [Call(open_error=failure(ErrorKind.RETRYABLE, "503"))],
            SOL_A: [Call(text_stream("a", usage=provider))],
        }
    )
    out = await drive(rig, "opus-deep", effort="high", hold=False)
    failed, committed = out.records
    assert failed.usage == UsageRecord(PROMPT_TOKENS, 0, tokens_estimated=True)
    assert committed.usage == provider and not committed.usage.tokens_estimated


async def test_a_lost_race_attempt_is_waste_with_the_overrun() -> None:
    rig = make_rig(
        {SOL_A: [Call(text_stream("win", first_s=0.3))], GLM_A: [Call([(0.1, MessageStart(at=0))], hang=True)]}
    )
    out = await drive(rig, "race-sol-glm", actions=[(0.5, commit())])
    lost = next(r for r in out.records if r.model == GLM)
    assert (lost.cancel_reason, lost.usage.output_tokens, lost.usage.tokens_estimated) == ("lost_race", 64, True)
    assert rig.telemetry.counter("aghoo_wasted_output_tokens_total", profile="race-sol-glm", reason="lost_race") == 64


async def test_breaker_outcomes_per_attempt() -> None:
    rig = make_rig(
        {
            "opus-5.5": [Call([(0.1, MessageStart(at=0)), error_at(0.2, ErrorKind.RETRYABLE, "timeout:idle")])],
            SOL_A: [Call(text_stream("ok", first_s=0.4))],
        }
    )
    await drive(rig, "opus-deep", effort="high", hold=False)
    records = [(m, outcome, ttft) for m, _, _, outcome, ttft in rig.world.health.records]
    assert records == [("us.anthropic.claude-opus-5-5", "timeout", None), (SOL, "ok", 400)]

    rig = make_rig({SOL_A: [Call(open_error=failure(ErrorKind.THROTTLED, "ThrottlingException"))] * 2})
    await drive(rig, "sol-fast", hold=False, ctx=rig.ctx(deadline_s=1.0))
    assert [r[3] for r in rig.world.health.records] == ["throttled"]

    rig = make_rig({SOL_A: [Call(open_error=failure(ErrorKind.TENANT_ACCESS, "AccessDeniedException"))]})
    await drive(rig, "sol-fast", hold=False)
    assert rig.world.health.records == []  # tenant access is not a health signal

    rig = make_rig({SOL_A: [Call(hang=True)]})
    await drive(rig, actions=[(0.5, cancel("client_abort"))])
    assert rig.world.health.records == []  # cancelled before any upstream event


async def test_text_less_stops_are_service_breaker_failures() -> None:
    """Fault B1: a text-less stop (reasoning-only: `max_tokens` while reasoning, or an empty answer) is a provider
    failure for the shared breaker, so a model that keeps answering without text gets skipped instead of costing
    every turn a fallback; a reasoning-only stop under a cap the tenant lowered (`max_output_tokens` below the
    member's) is tenant-shaped and not recorded at all."""
    reasoning_only = Call([(0.05, MessageStart(at=0.0)), reasoning(0.1, 400), (0.3, Stop("max_tokens"))])
    empty = Call([(0.05, MessageStart(at=0.0)), (0.1, Stop("end_turn"))])
    for call in (reasoning_only, empty):
        rig = make_rig({GLM_A: [call], SOL_A: [Call(text_stream("ok"))]})
        await drive(rig, "glm-reason", hold=False)
        assert [(r[0], r[3], r[4]) for r in rig.world.health.records] == [(GLM, "error", None), (SOL, "ok", 200)]

    rig = make_rig({GLM_A: [reasoning_only], SOL_A: [Call(text_stream("ok"))]})
    await drive(rig, "glm-reason", max_output_tokens=64, hold=False)
    assert [r[0] for r in rig.world.health.records] == [SOL]


async def test_first_answer_metric_and_the_attempt_span() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("ok", first_s=0.72, usage=UsageRecord(9, 1)))]})
    out = await drive(rig, actions=[(0.93, commit())])
    [metric] = [m for m in rig.telemetry.metrics if m.name == "aghoo_first_answer_seconds"]
    assert metric.value == pytest.approx(0.72)
    assert dict(metric.labels) == {"profile": "sol-fast", "model": SOL, "strategy": "holdback"}
    [span] = [s for s in rig.telemetry.spans if s.name == "generation.attempt"]
    assert span.ended
    attrs = span.attributes
    assert attrs["gen_ai.request.model"] == SOL and attrs["gen_ai.provider.name"] == "aws.bedrock"
    assert attrs["aghoo.profile"] == "sol-fast" and attrs["aghoo.tenant"] == "t_acme"
    assert attrs["aghoo.attempt.spec_signature"] == out.run.spec.signature()
    assert attrs["aghoo.attempt.outcome"] == "committed" and attrs["gen_ai.conversation.id"] == "ses_1"
    assert attrs["gen_ai.usage.input_tokens"] == 9 and attrs["gen_ai.usage.output_tokens"] == 1
    assert attrs["gen_ai.response.finish_reasons"] == "stop"
    assert attrs["gen_ai.response.time_to_first_chunk"] == pytest.approx(0.72)


async def test_a_failed_attempt_span_carries_the_error_code() -> None:
    rig = make_rig({SOL_A: [Call(open_error=failure(ErrorKind.FATAL, "boom"))]})
    await drive(rig, hold=False)
    [span] = [s for s in rig.telemetry.spans if s.name == "generation.attempt"]
    assert span.error == "internal_error" and span.attributes["aghoo.attempt.outcome"] == "failed"


class FlatCost:
    """A CostMeter pricing every call at 1 USD, never estimated; `missing` aliases raise not_found. `calls` records
    (alias, region, snapshot) per price() call."""

    def __init__(self, missing: frozenset[str] = frozenset()) -> None:
        self.missing = missing
        self.calls: list[tuple[str, str, ConfigSnapshot | None]] = []

    def price(
        self, model_alias: str, region: str, usage: UsageRecord, *, snapshot: ConfigSnapshot | None = None
    ) -> Cost:
        self.calls.append((model_alias, region, snapshot))
        if model_alias in self.missing:
            raise NoemaError(ErrorCode.NOT_FOUND, "unknown model")
        return Cost(Decimal(1), estimated=False)

    def record(self, line: UsageLine) -> None:
        raise AssertionError("the engine records no usage lines")


async def test_estimated_usage_always_has_an_estimated_cost_and_unpriceable_models_cost_zero() -> None:
    for meter, usd in ((FlatCost(), Decimal(1)), (FlatCost(frozenset({SOL_A})), Decimal(0))):
        rig = make_rig({SOL_A: [Call(text_stream("abc"))]}, cost=meter)
        out = await drive(rig, hold=False)
        [rec] = out.records
        assert rec.usage.tokens_estimated and rec.cost == Cost(usd, estimated=True)
    rig = make_rig({SOL_A: [Call(text_stream("abc", usage=UsageRecord(5, 1)))]}, cost=FlatCost())
    out = await drive(rig, hold=False)
    assert out.records[0].cost == Cost(Decimal(1), estimated=False)  # provider usage keeps the meter's verdict


async def test_attempts_are_priced_with_the_pinned_snapshot_and_the_home_region() -> None:
    # IMPL-019: the turn's pinned config prices it, even if another snapshot is current by then; spec-11 §2 region
    meter = FlatCost()
    rig = make_rig(
        {"opus-5.5": [Call(open_error=failure(ErrorKind.RETRYABLE, "503"))], SOL_A: [Call(text_stream("a"))]},
        cost=meter,
    )
    ctx = rig.ctx()
    await drive(rig, "opus-deep", effort="high", hold=False, ctx=ctx)
    home = ctx.tenant.home_region
    assert meter.calls == [("opus-5.5", home, ctx.snapshot), (SOL_A, home, ctx.snapshot)]


async def test_tenant_shaped_latency_is_not_a_ttft_breaker_sample() -> None:
    """The shared per-(model, region) breaker's p95-TTFT rule must see only latency the tenant cannot shape: an
    attempt whose prompt exceeds `TTFT_SIGNAL_MAX_INPUT_TOKENS` (prefill) or that reasoned before its first text
    (effort) records `ok` without a TTFT sample (Wave E fix: one tenant's slow calls opened the circuit for all)."""
    from tests.fakes.generation import StaticPromptBuilder

    from noema.generation.run import TTFT_SIGNAL_MAX_INPUT_TOKENS

    rig = make_rig({SOL_A: [Call(text_stream("ok", first_s=4.0))]})
    long_prompt = StaticPromptBuilder("x" * 4 * (TTFT_SIGNAL_MAX_INPUT_TOKENS + 1))
    await drive(rig, "sol-fast", hold=False, builder=long_prompt)
    assert [(r[3], r[4]) for r in rig.world.health.records] == [("ok", None)]

    events = text_stream("answer", first_s=3.0)
    events.insert(1, reasoning(0.2, 400))  # it reasoned for 2.8 s before its first text
    rig = make_rig({SOL_A: [Call(events)]})
    await drive(rig, "sol-fast", hold=False)
    assert [(r[3], r[4]) for r in rig.world.health.records] == [("ok", None)]
