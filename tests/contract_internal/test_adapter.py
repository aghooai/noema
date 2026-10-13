"""Adapter conformance (spec-02 §4, §14 `test_adapter.py`): template-aware answers, the §4.3 error
classification, `retry="none"` vs `"default"`, the §4.1 stream event order and timeouts, cancel = aclose, and
(with wire capture) the golden requests of `tests/golden/{decision,generation}_requests/`."""

from __future__ import annotations

import json
from typing import Any, Literal

import anyio
import pytest

from noema.core.clock import Deadline
from noema.core.errors import ErrorKind
from noema.core.protocols.adapters import (
    AdapterError,
    ErrorEvent,
    MessageStart,
    ModelRequest,
    StreamEvent,
    StreamTimeouts,
    TextDelta,
    UsageEvent,
)
from noema.core.protocols.adapters import Stop as StopEv
from noema.core.values import CallKind, ImageBlock, Message, TextBlock

from .kits import AdapterKit
from .registry import kit_fixture

kit = kit_fixture("adapter")
TIMEOUTS = StreamTimeouts(connect_s=3, first_event_s=5, first_answer_s=8, idle_s=30)
AQ = (
    '<app_question>{"instructions": "What is the user\'s primary intent?", "options": [{"letter": "A", "value": '
    '"chitchat", "description": null}, {"letter": "B", "value": "coding", "description": null}, {"letter": "C", '
    '"value": "factual_qa", "description": null}]}</app_question>\n'
)
DIST = {"chitchat": 0.1, "coding": 0.7, "factual_qa": 0.2}


def req(
    alias: str,
    *,
    text: str = AQ,
    logprobs: bool = False,
    kind: CallKind = "decision_member",
    retry: Literal["none", "default"] = "none",
) -> ModelRequest:
    return ModelRequest(
        alias,
        ("You classify an input.",),
        (Message("user", (TextBlock(text),)),),
        64,
        kind,
        retry,
        logprobs=logprobs,
        top_logprobs=20 if logprobs else None,
    )


def deadline(kit: AdapterKit, s: float = 60) -> Deadline:
    return Deadline(kit.clock.monotonic() + s, kit.clock)


def scenario(*rules: dict[str, Any]) -> dict[str, Any]:
    return {"scenario": "adapter-suite", "seed": 3, "defaults": {"latency": {"fixed_ms": 0}}, "rules": list(rules)}


async def collect(
    kit: AdapterKit, alias: str, request: ModelRequest | None = None, timeouts: StreamTimeouts = TIMEOUTS
) -> list[tuple[float, StreamEvent]]:
    out: list[tuple[float, StreamEvent]] = []
    t0 = kit.clock.monotonic()
    async with kit.clock.autojump():
        cm = kit.adapters.for_model(alias).stream(
            request or req(alias, text="Hi", kind="generation", retry="default"),
            tenant=kit.tenant,
            timeouts=timeouts,
            deadline=deadline(kit),
        )
        async with cm as events:
            out.extend([(round((kit.clock.monotonic() - t0) * 1000), ev) async for ev in events])
    return out


async def test_capabilities_follow_the_config(kit: AdapterKit) -> None:
    caps = kit.adapters.for_model("deepseek-3.2").capabilities("deepseek-3.2")
    assert caps.logprobs and caps.top_logprobs_max == 20 and caps.temperature
    assert caps.modalities == frozenset({"text"}) and caps.chars_per_token == 4.0
    opus = kit.adapters.for_model("opus-5.5").capabilities("opus-5.5")
    assert opus.reasoning and "high" in opus.effort and not opus.temperature
    assert "document" in opus.modalities and opus.context_window > 0


async def test_verbalized_answer_honors_the_letters(kit: AdapterKit) -> None:
    kit.use_scenario(
        scenario({"match": {"model": "us.anthropic.claude-haiku-5-5"}, "answer": {"dist": DIST}, "latency_ms": 900})
    )
    async with kit.clock.autojump():
        resp = await kit.adapters.for_model("haiku-5.5").complete(
            req("haiku-5.5"), tenant=kit.tenant, deadline=deadline(kit)
        )
    obj = json.loads(resp.text)
    assert obj == pytest.approx({"A": 0.1, "B": 0.7, "C": 0.2})
    assert not resp.refused and resp.stop_reason == "end_turn" and resp.region == kit.tenant.home_region
    assert resp.usage.input_tokens > 0
    if kit.virtual_time:
        assert resp.latency_ms == 900


async def test_logprob_answer_on_mantle(kit: AdapterKit) -> None:
    kit.use_scenario(
        scenario({"match": {"model": "deepseek.v3.2", "op": "chat"}, "answer": {"dist": DIST}, "latency_ms": 500})
    )
    async with kit.clock.autojump():
        resp = await kit.adapters.for_model("deepseek-3.2").complete(
            req("deepseek-3.2", logprobs=True), tenant=kit.tenant, deadline=deadline(kit)
        )
    assert resp.logprobs is not None and resp.text.strip() == "B"
    tops = dict(resp.logprobs[0].top)
    assert set(tops) >= {"A", "B", "C"} and tops["B"] > tops["C"] > tops["A"]


async def test_refusal(kit: AdapterKit) -> None:
    kit.use_scenario(scenario({"match": {"model": "us.anthropic.claude-haiku-5-5"}, "fault": {"kind": "refusal"}}))
    async with kit.clock.autojump():
        resp = await kit.adapters.for_model("haiku-5.5").complete(
            req("haiku-5.5"), tenant=kit.tenant, deadline=deadline(kit)
        )
    assert resp.refused and resp.stop_reason == "content_filter"


async def test_wire_rules_reject_before_any_request(kit: AdapterKit) -> None:
    kit.use_scenario(scenario({"match": {}, "answer": {"dist": DIST}}))
    with pytest.raises(AdapterError) as err:
        await kit.adapters.for_model("haiku-5.5").complete(
            req("haiku-5.5", logprobs=True), tenant=kit.tenant, deadline=deadline(kit)
        )
    assert err.value.error.kind is ErrorKind.INVALID_REQUEST
    image = ModelRequest(
        "deepseek-3.2",
        (),
        (Message("user", (TextBlock("x"), ImageBlock("png", b"p", 1, 1))),),
        8,
        "decision_member",
        "none",
    )
    with pytest.raises(AdapterError) as err:
        await kit.adapters.for_model("deepseek-3.2").complete(image, tenant=kit.tenant, deadline=deadline(kit))
    assert err.value.error.kind is ErrorKind.INVALID_REQUEST


@pytest.mark.parametrize(
    ("fault", "kind", "cause"),
    [
        ({"kind": "throttling"}, ErrorKind.THROTTLED, None),
        ({"kind": "access_denied"}, ErrorKind.TENANT_ACCESS, "model_access_denied"),
        ({"kind": "validation", "message": "bad"}, ErrorKind.INVALID_REQUEST, None),
        ({"kind": "http_5xx", "status": 503}, ErrorKind.RETRYABLE, None),
        ({"kind": "model_timeout"}, ErrorKind.RETRYABLE, None),
    ],
)
async def test_error_classification(kit: AdapterKit, fault: dict[str, Any], kind: ErrorKind, cause: str | None) -> None:
    kit.use_scenario(scenario({"match": {"model": "us.anthropic.claude-haiku-5-5"}, "fault": fault}))
    async with kit.clock.autojump():
        with pytest.raises(AdapterError) as err:
            await kit.adapters.for_model("haiku-5.5").complete(
                req("haiku-5.5"), tenant=kit.tenant, deadline=deadline(kit)
            )
    assert err.value.error.kind is kind and err.value.error.cause == cause
    assert kit.adapters.for_model("haiku-5.5").classify_error(err.value).kind is kind


async def test_complete_deadline_is_retryable_timeout(kit: AdapterKit) -> None:
    kit.use_scenario(scenario({"match": {}, "answer": {"dist": DIST}, "latency_ms": 5000}))
    async with kit.clock.autojump():
        with pytest.raises(AdapterError) as err:
            await kit.adapters.for_model("haiku-5.5").complete(
                req("haiku-5.5"), tenant=kit.tenant, deadline=deadline(kit, 1.0)
            )
    assert err.value.error.kind is ErrorKind.RETRYABLE and err.value.error.provider_code == "timeout:deadline"


SOL = "us.openai.gpt-6.1-sol"


def needs_virtual_time(kit: AdapterKit) -> None:
    if not kit.virtual_time:
        pytest.skip("timing assertions need a virtual-time backend")


async def test_stream_event_order(kit: AdapterKit) -> None:
    kit.use_scenario(
        scenario(
            {
                "match": {"model": SOL, "op": "converse-stream"},
                "stream": {"first_event_ms": 200, "first_text_ms": 720, "chunks": ["Sure, ", "done."]},
            }
        )
    )
    events = [ev for _, ev in await collect(kit, "sol-6.1")]
    assert isinstance(events[0], MessageStart)
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert "".join(texts) == "Sure, done."
    stops = [i for i, e in enumerate(events) if isinstance(e, StopEv)]
    assert len(stops) == 1 and all(isinstance(e, UsageEvent) for e in events[stops[0] + 1 :])
    assert not any(isinstance(e, ErrorEvent) for e in events)


async def test_stream_times(kit: AdapterKit) -> None:
    needs_virtual_time(kit)
    kit.use_scenario(
        scenario(
            {
                "match": {"model": SOL},
                "stream": {"first_event_ms": 200, "first_text_ms": 720, "chunks": ["a", "b"], "tokens_per_s": 40},
            }
        )
    )
    timed = await collect(kit, "sol-6.1")
    assert timed[0][0] == 200 and next(t for t, e in timed if isinstance(e, TextDelta)) == 720


@pytest.mark.parametrize(
    ("rule", "code", "at_ms"),
    [
        ({"fault": {"kind": "hang_before_first_byte"}}, "timeout:first_event", 5000),
        (
            {
                "stream": {"first_event_ms": 100, "first_text_ms": 500, "chunks": ["x", "y"]},
                "fault": {"kind": "stall_after", "n": 1},
            },
            "timeout:idle",
            30_500,
        ),
        (
            {"stream": {"first_event_ms": 100, "first_text_ms": 9000, "reasoning": {"deltas": 400, "every_ms": 20}}},
            "timeout:first_answer",
            8000,
        ),
    ],
)
async def test_stream_timeouts_end_with_one_error(kit: AdapterKit, rule: dict[str, Any], code: str, at_ms: int) -> None:
    needs_virtual_time(kit)
    kit.use_scenario(scenario({"match": {"model": SOL}, **rule}))
    timed = await collect(kit, "sol-6.1")
    at, last = timed[-1]
    assert isinstance(last, ErrorEvent) and last.error.kind is ErrorKind.RETRYABLE
    assert last.error.provider_code == code and at == at_ms
    assert sum(isinstance(e, ErrorEvent) for _, e in timed) == 1


async def test_exception_event_is_the_last_event(kit: AdapterKit) -> None:
    kit.use_scenario(
        scenario(
            {
                "match": {"model": SOL},
                "stream": {"chunks": ["a", "b", "c"]},
                "fault": {"kind": "exception_event", "type": "modelStreamErrorException", "after": 2},
            }
        )
    )
    events = [e for _, e in await collect(kit, "sol-6.1")]
    assert isinstance(events[-1], ErrorEvent) and events[-1].error.kind is ErrorKind.RETRYABLE
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["a", "b"]


async def test_reasoning_only_stops_at_max_tokens(kit: AdapterKit) -> None:
    kit.use_scenario(
        scenario(
            {
                "match": {"model": "us.zai.glm-5.3"},
                "fault": {"kind": "reasoning_only"},
                "stream": {"first_event_ms": 50, "reasoning": {"deltas": 3}},
            }
        )
    )
    events = [e for _, e in await collect(kit, "glm-5.3")]
    assert not any(isinstance(e, TextDelta) for e in events)
    assert [e.reason for e in events if isinstance(e, StopEv)] == ["max_tokens"]


async def test_pre_byte_failure_raises_on_enter(kit: AdapterKit) -> None:
    kit.use_scenario(scenario({"match": {"model": SOL}, "fault": {"kind": "http_5xx", "status": 503}}))
    with pytest.raises(AdapterError) as err:
        await collect(kit, "sol-6.1")
    assert err.value.error.kind is ErrorKind.RETRYABLE


@pytest.mark.parametrize(("retry", "succeeds"), [("default", True), ("none", False)])
async def test_connect_retry_policy(kit: AdapterKit, retry: Literal["none", "default"], succeeds: bool) -> None:
    kit.use_scenario(
        scenario(
            {"match": {"model": SOL, "nth": 1}, "fault": {"kind": "connect_reset"}},
            {"match": {"model": SOL}, "stream": {"chunks": ["ok"]}},
        )
    )
    request = req("sol-6.1", text="Hi", kind="generation", retry=retry)
    if succeeds:
        events = [e for _, e in await collect(kit, "sol-6.1", request)]
        assert any(isinstance(e, TextDelta) for e in events)
    else:
        with pytest.raises(AdapterError) as err:
            await collect(kit, "sol-6.1", request)
        assert err.value.error.kind is ErrorKind.RETRYABLE


async def test_exiting_the_context_cancels_the_stream(kit: AdapterKit) -> None:
    kit.use_scenario(scenario({"match": {"model": SOL}, "stream": {"chunks": ["a", "b", "c", "d"]}}))
    seen = []
    async with kit.clock.autojump():
        cm = kit.adapters.for_model("sol-6.1").stream(
            req("sol-6.1", text="Hi", kind="generation"), tenant=kit.tenant, timeouts=TIMEOUTS, deadline=deadline(kit)
        )
        async with cm as events:
            async for ev in events:
                seen.append(ev)
                if isinstance(ev, TextDelta):
                    break
    assert isinstance(seen[-1], TextDelta)
    if kit.open_streams is not None:
        assert kit.open_streams() == 0


async def test_an_outer_cancel_mid_stream_closes_the_stream(kit: AdapterKit) -> None:
    # adapters.md item 6: generation leaves a stream through its attempt scope (`att.scope.cancel()`), not `break`
    kit.use_scenario(
        scenario({"match": {"model": SOL}, "stream": {"chunks": ["a", "b"]}, "fault": {"kind": "stall_after", "n": 1}})
    )
    got_text = anyio.Event()
    scope = anyio.CancelScope()

    async def consume() -> None:
        with scope:
            cm = kit.adapters.for_model("sol-6.1").stream(
                req("sol-6.1", text="Hi", kind="generation"),
                tenant=kit.tenant,
                timeouts=TIMEOUTS,
                deadline=deadline(kit),
            )
            async with cm as events:
                async for ev in events:
                    if isinstance(ev, TextDelta):
                        got_text.set()  # the stream stalls now: the consumer waits for the next event

    async with kit.clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await got_text.wait()
        scope.cancel()
    assert scope.cancelled_caught
    if kit.open_streams is not None:
        assert kit.open_streams() == 0


async def test_golden_wire_requests(kit: AdapterKit) -> None:
    if kit.wire_requests is None:
        pytest.skip("no wire capture (fake adapters never build wire requests)")
    from tests.golden.wire import check_adapter_against_goldens

    await check_adapter_against_goldens(kit)
