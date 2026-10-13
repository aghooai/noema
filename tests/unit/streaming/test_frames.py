"""SSE framing (spec-02 §7 `SseFrame`, IMPL-034): pre-serialized frames, one data line, delta merging and the
id-less client_too_slow frame; the retained size of a frame (the event log keeps every frame of a turn for the
resume window)."""

from __future__ import annotations

import tracemalloc

import orjson
import pytest

from noema.api import sse_frame
from noema.contracts.validate import ContractValidator
from noema.core.values import JsonValue
from noema.streaming.frames import (
    PING,
    delta_attempt,
    encode_frame,
    frame_from_payload,
    merge_deltas,
    too_slow_frame,
)


def delta(i: int, text: str, attempt: str = "att_1") -> dict[str, JsonValue]:
    return {"type": "generation.delta", "attempt_id": attempt, "seq": i, "text": text}


def test_encode_frame_is_the_spec_wire_format() -> None:
    event = delta(0, "a\nb\r\u2028")  # newlines and a line separator inside the text
    frame = encode_frame(7, event)
    assert (frame.id, frame.type, frame.payload) == (7, "generation.delta", orjson.dumps(event))
    assert frame.wire == b"id: 7\nevent: generation.delta\ndata: " + orjson.dumps(event) + b"\n\n"
    assert frame.wire.count(b"\n") == 4  # control characters stay escaped inside the single data line
    assert frame_from_payload(7, frame.type, frame.payload) == frame


def test_encode_frame_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="start at 1"):
        encode_frame(0, {"type": "turn.created"})
    with pytest.raises(ValueError, match="type"):
        encode_frame(1, {"no": "type"})
    with pytest.raises(ValueError, match="single-line"):
        encode_frame(1, {"type": "bad\ntype"})


def test_merge_deltas_keeps_the_last_id_and_seq() -> None:
    frames = [encode_frame(i + 3, delta(i, t)) for i, t in enumerate(["a", "b", "c"])]
    merged = merge_deltas(frames)
    assert merged.id == 5 and orjson.loads(merged.payload) == delta(2, "abc")
    assert merge_deltas(frames[:1]) is frames[0]
    ContractValidator().check_sse_frame("createTurn", "5", merged.type, merged.payload)


def test_delta_attempt() -> None:
    assert delta_attempt(encode_frame(1, delta(0, "x", "att_9"))) == "att_9"
    assert delta_attempt(encode_frame(1, {"type": "turn.created"})) is None
    assert delta_attempt(frame_from_payload(1, "generation.delta", b"not json")) is None


def test_too_slow_frame_has_no_id_line() -> None:
    raw = too_slow_frame()
    assert raw.startswith(b"event: error\ndata: ") and raw.endswith(b"\n\n") and b"id:" not in raw
    payload = orjson.loads(raw.split(b"data: ", 1)[1])
    assert (payload["code"], payload["retryable"]) == ("client_too_slow", False)
    assert PING == b": ping\n\n"


def test_a_retained_frame_holds_only_right_sized_bytes() -> None:
    # orjson.dumps returns bytes backed by a ~4 KiB allocation whatever the payload size; a frame must not keep it
    events = [delta(i, f"word {i} ") for i in range(2000)]
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        frames = [encode_frame(i + 1, e) for i, e in enumerate(events)]
        merged = [merge_deltas(frames[i : i + 2]) for i in range(0, 200, 2)]
        per_frame = (tracemalloc.get_traced_memory()[0] - base) / (len(frames) + len(merged))
    finally:
        tracemalloc.stop()
    wire = len(frames[-1].wire)
    assert per_frame < 3 * wire + 256, per_frame  # wire + payload + the SseFrame object, not 4 KiB
    assert frames[-1].payload == orjson.dumps(events[-1]) and merged[0].payload == orjson.dumps(
        delta(1, "word 0 word 1 ")
    )


def test_frames_are_byte_identical_to_the_api_encoder() -> None:
    # the api's encode_frame / encode_unnumbered (spec-02 §7) keep the same wire and right-sized payload; streaming
    # cannot import noema.api (spec-01 §2 layers), so the two encoders are pinned against each other here
    events: list[dict[str, JsonValue]] = [delta(0, "a\nb\u2028"), {"type": "turn.completed", "turn_id": "turn_1"}]
    for i, event in enumerate(events, start=1):
        assert encode_frame(i, event) == sse_frame.encode_frame(i, event)
    assert too_slow_frame() == sse_frame.encode_unnumbered(orjson.loads(too_slow_frame().split(b"data: ", 1)[1]))
