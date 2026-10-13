"""ConverseStream normalization through aiobotocore and real eventstream bytes (spec-02 §4.1/§4.2): event order,
reasoning counted and never forwarded, usage after Stop (cache tokens), exception events mid-stream,
reasoning-only and length stops, malformed frames and events of the wrong shape, refusals, and cancel = close
without draining."""

from __future__ import annotations

import pytest
from tests.fakes.clock import FakeClock
from tests.unit.adapters.emulator_wire import event_frame
from tests.unit.adapters.support import GLM, OPUS, SOL, TIMEOUTS, collect, request, rig

from noema.adapters._stream import PreparedCall, RawStream, StreamSession
from noema.adapters._unary import RawResponse
from noema.adapters.classify import classify
from noema.adapters.converse import _ConverseRaw
from noema.core.clock import Deadline
from noema.core.errors import ErrorKind
from noema.core.protocols.adapters import (
    ErrorEvent,
    MessageStart,
    ReasoningDelta,
    Stop,
    TextDelta,
    UsageEvent,
)


async def test_event_order_text_then_stop_then_usage() -> None:
    async with rig() as r:
        r.scenario(
            {
                "match": {"model": SOL, "op": "converse-stream"},
                "stream": {"first_event_ms": 200, "chunks": ["Su", "re"]},
            }
        )
        timed = await collect(r, "sol-6.1")
    events = [e for _, e in timed]
    assert isinstance(events[0], MessageStart) and timed[0][0] == 200
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Su", "re"]
    assert [type(e) for e in events[-2:]] == [Stop, UsageEvent]
    stop, usage = events[-2], events[-1]
    assert isinstance(stop, Stop) and stop.reason == "end_turn"
    assert isinstance(usage, UsageEvent) and usage.usage.input_tokens > 0 and usage.usage.output_tokens == 1


async def test_reasoning_is_counted_never_forwarded_as_text() -> None:
    async with rig() as r:
        r.scenario(
            {
                "match": {"model": OPUS},
                "stream": {"first_event_ms": 100, "first_text_ms": 300, "reasoning": {"deltas": 5}, "chunks": ["A"]},
            }
        )
        events = [e for _, e in await collect(r, "opus-5.5", request("opus-5.5", effort="low"))]
    reasoning = [e for e in events if isinstance(e, ReasoningDelta)]
    assert len(reasoning) == 5 and all(e.chars == 16 for e in reasoning)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "A"
    usage = next(e for e in events if isinstance(e, UsageEvent)).usage
    assert usage.reasoning_tokens == min(usage.output_tokens, 20)  # 80 reasoning chars / 4


async def test_cache_tokens_are_normalized_into_input() -> None:
    usage = {"input_tokens": 10, "output_tokens": 3, "cached_input_tokens": 100, "cache_write_input_tokens": 7}
    async with rig() as r:
        r.scenario({"match": {"model": SOL}, "stream": {"chunks": ["x"], "usage": usage}})
        events = [e for _, e in await collect(r, "sol-6.1")]
        ratio = r.metric("aghoo_bedrock_cached_input_ratio", model=SOL)
    got = next(e for e in events if isinstance(e, UsageEvent)).usage
    assert (got.input_tokens, got.cached_input_tokens, got.cache_write_input_tokens) == (117, 100, 7)
    assert got.output_tokens == 3 and not got.tokens_estimated
    assert ratio == 100 / 117


async def test_exception_event_mid_stream_is_one_retryable_error_last() -> None:
    async with rig() as r:
        r.scenario(
            {
                "match": {"model": SOL},
                "stream": {"chunks": ["a", "b", "c"]},
                "fault": {"kind": "exception_event", "type": "modelStreamErrorException", "after": 2},
            }
        )
        events = [e for _, e in await collect(r, "sol-6.1")]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["a", "b"]
    last = events[-1]
    assert isinstance(last, ErrorEvent) and last.error.kind is ErrorKind.RETRYABLE
    assert last.error.provider_code == "ModelStreamErrorException"
    assert sum(isinstance(e, ErrorEvent) for e in events) == 1 and not any(isinstance(e, Stop) for e in events)


async def test_throttling_exception_event_mid_stream_is_retryable() -> None:
    async with rig() as r:
        r.scenario(
            {
                "match": {"model": SOL},
                "stream": {"chunks": ["a", "b"]},
                "fault": {"kind": "exception_event", "type": "throttlingException", "after": 1},
            }
        )
        events = [e for _, e in await collect(r, "sol-6.1")]
    last = events[-1]
    assert isinstance(last, ErrorEvent) and last.error.kind is ErrorKind.RETRYABLE
    assert last.error.provider_code == "ThrottlingException"


async def test_reasoning_only_stops_at_max_tokens_without_text() -> None:
    async with rig() as r:
        r.scenario(
            {"match": {"model": GLM}, "fault": {"kind": "reasoning_only"}, "stream": {"reasoning": {"deltas": 3}}}
        )
        events = [e for _, e in await collect(r, "glm-5.3")]
    assert not any(isinstance(e, TextDelta) for e in events)
    assert sum(isinstance(e, ReasoningDelta) for e in events) == 3
    assert [e.reason for e in events if isinstance(e, Stop)] == ["max_tokens"]
    assert isinstance(events[-1], UsageEvent)


async def test_length_stop_with_text() -> None:
    async with rig() as r:
        r.scenario({"match": {"model": SOL}, "stream": {"chunks": ["abc"], "stop": "max_tokens"}})
        events = [e for _, e in await collect(r, "sol-6.1")]
    assert [e.reason for e in events if isinstance(e, Stop)] == ["max_tokens"]
    assert any(isinstance(e, TextDelta) for e in events)


async def test_refusal_stop_is_content_filter() -> None:
    async with rig() as r:
        r.scenario({"match": {"model": SOL}, "fault": {"kind": "refusal"}})
        events = [e for _, e in await collect(r, "sol-6.1")]
    assert [e.reason for e in events if isinstance(e, Stop)] == ["content_filter"]
    assert not any(isinstance(e, TextDelta) for e in events)


async def test_bad_crc_frame_is_a_malformed_retryable_error() -> None:
    async with rig() as r:
        r.scenario({"match": {"model": SOL}, "stream": {"chunks": ["a", "b"]}, "fault": {"kind": "malformed"}})
        events = [e for _, e in await collect(r, "sol-6.1")]
    last = events[-1]
    assert isinstance(last, ErrorEvent) and last.error.kind is ErrorKind.RETRYABLE
    assert last.error.provider_code == "malformed:eventstream"


async def test_cancel_closes_the_stream_without_draining() -> None:
    async with rig() as r:
        r.scenario({"match": {"model": SOL}, "stream": {"chunks": ["a", "b", "c", "d"], "tokens_per_s": 4}})
        seen = []
        async with r.clock.autojump():
            cm = r.converse.stream(request("sol-6.1"), tenant=r.tenant, timeouts=TIMEOUTS, deadline=r.deadline())
            async with cm as events:
                async for ev in events:
                    seen.append(ev)
                    if isinstance(ev, TextDelta):
                        break
                assert r.emulator.open_streams == 1 and r.converse.open_streams == 1
            elapsed = r.clock.elapsed_ms()
        assert r.emulator.open_streams == 0 and r.converse.open_streams == 0
        assert elapsed < 1000  # the remaining 3 chunks (1 s apart) were never read
    assert isinstance(seen[-1], TextDelta) and seen[-1].text == "a"


class _Scripted(_ConverseRaw):
    """A ConverseStream reader whose parsed events are scripted (the wrong shapes botocore's parser never returns
    are fed to `feed` directly)."""

    def __init__(self, events: list[object]) -> None:
        self.events, self.closed = list(events), False
        self._clock, self._on_usage, self._reasoning_chars = FakeClock(), lambda _: None, 0
        self._text_chars, self._hidden = 0, False

    async def next(self) -> object | None:
        return self.events.pop(0) if self.events else None

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "bad",
    [{"contentBlockDelta": "not an object"}, {"messageStop": ["end_turn"]}, {"contentBlockDelta": {"delta": 7}}],
)
async def test_a_wrong_shaped_event_is_one_retryable_malformed_error(bad: object) -> None:
    # adapters review (F8): a provider event of the wrong shape is malformed:chunk, never a fatal AttributeError
    clock = FakeClock()
    events = _Scripted([{"messageStart": {"role": "assistant"}}, {"contentBlockDelta": {"delta": {"text": "a"}}}, bad])

    async def open_stream() -> RawStream:
        return events

    async def release() -> None:
        return None

    async def prepare() -> PreparedCall:
        return PreparedCall(open_stream, classify, release)

    deadline = Deadline(clock.monotonic() + 60, clock)
    async with clock.autojump(), StreamSession(prepare, clock=clock, timeouts=TIMEOUTS, deadline=deadline) as it:
        got = [ev async for ev in it]
    assert [type(e) for e in got] == [MessageStart, TextDelta, ErrorEvent] and events.closed
    last = got[-1]
    assert isinstance(last, ErrorEvent) and last.error.kind is ErrorKind.RETRYABLE
    assert last.error.provider_code == "malformed:chunk"


class _ScriptedBody:
    """A 200 ConverseStream response serving one scripted eventstream body, then its end."""

    status, headers = 200, {"content-type": "application/vnd.amazon.eventstream"}

    def __init__(self, body: bytes) -> None:
        self._chunks = [body]

    async def chunk(self) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def read(self) -> bytes:
        return b""

    def close(self) -> None:
        pass


class _ScriptedTransport:
    """A `StreamTransport` whose `open()` answers with `body` (no network, no scenario)."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    async def post(self, *a: object, **k: object) -> RawResponse:
        raise AssertionError("ConverseStream only")

    async def open(self, *a: object, **k: object) -> _ScriptedBody:
        return _ScriptedBody(self.body)

    async def aclose(self) -> None:
        pass


async def scripted_stream(*frames: bytes) -> list[object]:
    body = b"".join(frames)
    async with rig(converse_unary=lambda _creds: _ScriptedTransport(body)) as r:
        cm = r.converse.stream(request("opus-5.5"), tenant=r.tenant, timeouts=TIMEOUTS, deadline=r.deadline())
        async with cm as events:
            return [e async for e in events]


def errors_of(events: list[object]) -> list[tuple[ErrorKind, str | None]]:
    return [(e.error.kind, e.error.provider_code) for e in events if isinstance(e, ErrorEvent)]


_START = event_frame("messageStart", {"role": "assistant"})


async def test_a_body_ending_before_message_stop_is_one_malformed_truncated() -> None:
    """Wave E (a test gap: `_ConverseRaw.finish` never ran): a clean EOF before `messageStop` is never a silently
    truncated answer; it ends with exactly one ErrorEvent(retryable, malformed:truncated) (F5/F8)."""
    events = await scripted_stream(_START, event_frame("contentBlockDelta", {"delta": {"text": "partial"}}))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["partial"]
    assert not any(isinstance(e, Stop) for e in events)
    assert errors_of(events) == [(ErrorKind.RETRYABLE, "malformed:truncated")] and isinstance(events[-1], ErrorEvent)


@pytest.mark.parametrize(
    "delta",
    [{"text": 5}, {"text": ["a", "b"]}, {"text": {"x": 1}}, {"reasoningContent": {"text": ["a", "b"]}}],
    ids=["text_int", "text_list", "text_object", "reasoning_text_list"],
)
async def test_non_string_text_is_malformed_chunk_never_a_text_delta(delta: dict[str, object]) -> None:
    """Wave E (F8): non-string content or reasoning is ErrorEvent(malformed:chunk); a TextDelta(5) used to reach the
    consumer, whose `len(text)` raised TypeError on the committed-attempt hot path."""
    events = await scripted_stream(_START, event_frame("contentBlockDelta", {"delta": delta}), STOP_FRAME)
    assert errors_of(events) == [(ErrorKind.RETRYABLE, "malformed:chunk")] and isinstance(events[-1], ErrorEvent)
    assert all(isinstance(e.text, str) for e in events if isinstance(e, TextDelta))
    assert not any(isinstance(e, ReasoningDelta) for e in events)


STOP_FRAME = event_frame("messageStop", {"stopReason": "end_turn"})


async def test_redacted_reasoning_is_estimated_from_the_output_left_after_the_text() -> None:
    """Wave E (live cassette 34-sol-6.1-stream): Sol at effort high streams `redactedContent` reasoning; output 393
    for about 166 tokens of text was reported with reasoning_tokens 0."""
    redacted = event_frame("contentBlockDelta", {"delta": {"reasoningContent": {"redactedContent": "AAEC"}}})
    usage = event_frame("metadata", {"usage": {"inputTokens": 7, "outputTokens": 393, "totalTokens": 400}})
    text = event_frame("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"text": "y" * 664}})
    events = await scripted_stream(_START, redacted, redacted, text, STOP_FRAME, usage)
    (usage_event,) = [e for e in events if isinstance(e, UsageEvent)]
    assert usage_event.usage.output_tokens == 393 and usage_event.usage.reasoning_tokens == 393 - 166
