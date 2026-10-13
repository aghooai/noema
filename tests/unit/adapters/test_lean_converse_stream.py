"""E sprint A: the lean ConverseStream reader (botocore's `EventStreamBuffer` + `parse_event`) yields what the
aiobotocore client's event stream yielded for the same body bytes, however they are chunked: the same normalized
events, and the same classification of the failure that ends the stream (exception and error messages of every
type, corrupt CRCs); a partial message at the end is dropped by both. One deliberate difference: a payload
botocore's parser crashes on is `malformed:chunk` (F8), where aiobotocore surfaced a fatal AttributeError."""

from __future__ import annotations

from typing import Any

import orjson
import pytest
from tests.fakes.clock import FakeClock
from tests.unit.adapters.emulator_wire import encode_message, event_frame, exception_frame
from tests.unit.adapters.lean_reference import converse_model, reference

from noema.adapters.classify import MSG_MALFORMED, StreamFault, classify, stream_error
from noema.adapters.converse import _ConverseRaw
from noema.core.errors import ErrorKind
from noema.core.protocols.adapters import MessageStart

START = event_frame("messageStart", {"role": "assistant"})
STOP = event_frame("messageStop", {"stopReason": "end_turn"})
USAGE = event_frame("metadata", {"usage": {"inputTokens": 7, "outputTokens": 40, "totalTokens": 47}, "metrics": {}})


def delta(text: str, index: int = 0, **extra: Any) -> bytes:
    return event_frame("contentBlockDelta", {"contentBlockIndex": index, "delta": {"text": text}, **extra})


def reasoning(text: str) -> bytes:
    return event_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"reasoningContent": {"text": text}}})


def frame(event_type: str, payload: bytes) -> bytes:
    headers = {":message-type": "event", ":event-type": event_type, ":content-type": "application/json"}
    return encode_message(headers, payload)


ERROR = encode_message({":message-type": "error", ":error-code": "InternalFailure", ":error-message": "x"}, b"")
CASES: dict[str, list[bytes]] = {
    "text": [START, delta("Par"), delta("is."), event_frame("contentBlockStop", {"contentBlockIndex": 0}), STOP, USAGE],
    "reasoning_then_text": [
        START,
        reasoning("think" * 20),
        event_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"reasoningContent": {"signature": "s"}}}),
        event_frame("contentBlockStop", {"contentBlockIndex": 0}),
        delta("A", 1),
        STOP,
        USAGE,
    ],
    "refusal": [START, event_frame("messageStop", {"stopReason": "guardrail_intervened"}), USAGE],
    "padded_and_unknown_members": [START, delta("a", p="zz"), event_frame("messageStop", {"stopReason": "x", "p": 1})],
    "unknown_event_type": [START, event_frame("citationsDelta", {"x": 1}), delta("b"), STOP],
    "usage_strings": [START, delta("c"), STOP, event_frame("metadata", {"usage": {"inputTokens": "7"}})],
    "usage_null": [START, delta("c"), STOP, event_frame("metadata", {"usage": None})],
    "redacted_reasoning": [
        START,
        event_frame("contentBlockDelta", {"delta": {"reasoningContent": {"redactedContent": "AAEC"}}}),
        STOP,
    ],
    "non_json_payload": [START, frame("contentBlockDelta", b"not json"), delta("d"), STOP],
    "delta_not_an_object": [START, frame("contentBlockDelta", orjson.dumps({"delta": 7}))],
    "payload_a_list": [START, frame("messageStop", b"[1]")],
    "throttling": [START, delta("e"), exception_frame("throttlingException", "Too many tokens")],
    "model_stream_error": [START, exception_frame("modelStreamErrorException")],
    "internal": [exception_frame("internalServerException")],
    "unavailable": [START, exception_frame("serviceUnavailableException")],
    "validation": [START, exception_frame("validationException")],
    "unknown_exception": [START, exception_frame("weirdException")],
    "error_message": [START, ERROR],
    "corrupt_crc": [START, event_frame("contentBlockDelta", {"delta": {"text": "f"}}, corrupt=True), STOP],
    "truncated": [START, delta("g"), delta("h")[:-5]],
    "empty": [],
}


def chunked(frames: list[bytes], size: int) -> list[bytes]:
    data = b"".join(frames)
    return [data[i : i + size] for i in range(0, len(data), size)] if size else [data] if data else []


def feeder() -> _ConverseRaw:
    raw = _ConverseRaw.__new__(_ConverseRaw)
    raw._clock, raw._on_usage, raw._reasoning_chars = FakeClock(), lambda _: None, 0
    raw._text_chars, raw._hidden = 0, False
    return raw


def _classified(exc: BaseException) -> tuple[Any, ...]:
    error = classify(exc)
    return error.kind, error.message, error.provider_code


def outcome(events: list[object], failure: BaseException | None) -> tuple[list[Any], Any]:
    """What the stream session sees: each event normalized by `feed` (or the feed's own fault), then the failure
    that ended the stream, classified."""
    feed = feeder()
    normalized: list[Any] = []
    for event in events:
        try:
            normalized.append(feed.feed(event))
        except Exception as exc:
            normalized.append(_classified(exc))
    return normalized, None if failure is None else _classified(failure)


class _Body:
    """A `StreamedResponse` serving the scripted chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.status, self._chunks, self.closed = 200, list(chunks), False
        self.headers: dict[str, str] = {}

    async def chunk(self) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def read(self) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True


async def lean(chunks: list[bytes]) -> tuple[list[Any], Any]:
    raw = _ConverseRaw(_Body(chunks), converse_model(), FakeClock(), lambda _: None)
    events: list[object] = []
    try:
        while (event := await raw.next()) is not None:
            events.append(event)
    except Exception as exc:
        return outcome(events, exc)
    return outcome(events, None)


async def botocore_side(chunks: list[bytes]) -> tuple[list[Any], Any]:
    params = {
        "modelId": "m",
        "messages": [{"role": "user", "content": [{"text": "a"}]}],
        "inferenceConfig": {"maxTokens": 8},
    }
    events: list[object] = []
    async with reference() as ref:
        ref.stream_chunks = chunks
        response = await ref.client.converse_stream(**params)
        try:
            async for event in response["stream"]:
                events.append(event)  # noqa: PERF401  # the events before a failure count too
        except (AttributeError, TypeError):  # botocore's parser crashed on a wrong-shaped payload: F8 says
            return outcome(events, StreamFault(stream_error("malformed:chunk", MSG_MALFORMED)))  # malformed:chunk
        except Exception as exc:
            return outcome(events, exc)
    return outcome(events, None)


@pytest.mark.parametrize("name", ["delta_not_an_object", "payload_a_list"])
async def test_a_payload_the_parser_cannot_take_is_malformed_chunk_not_a_fatal(name: str) -> None:
    # aiobotocore surfaced botocore's AttributeError (fatal); the lean reader keeps the F8 contract
    events, failure = await lean(chunked(CASES[name], 0))
    assert events[:1] == [MessageStart(at=1000.0)]
    assert failure == (ErrorKind.RETRYABLE, MSG_MALFORMED, "malformed:chunk")


@pytest.mark.parametrize("size", [0, 1, 7, 64])
@pytest.mark.parametrize("name", list(CASES))
async def test_the_stream_reads_as_aiobotocore_read_it(name: str, size: int) -> None:
    chunks = chunked(CASES[name], size)
    assert await lean(chunks) == await botocore_side(chunks)
