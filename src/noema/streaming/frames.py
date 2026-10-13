"""Pre-serialized SSE frames (spec-02 §7 `SseFrame`, IMPL-034): `id: <n>\\nevent: <type>\\ndata: <json>\\n\\n`,
serialized once (orjson escapes control characters, so the payload is always one `data:` line)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import orjson

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.events import SseFrame
from noema.core.values import JsonValue

PING: Final = b": ping\n\n"
TERMINAL_EVENT: Final = "turn.completed"
DELTA_EVENT: Final = "generation.delta"
_EVENT_PREFIX: dict[str, bytes] = {}  # event type -> b"\nevent: <type>\ndata: " (a handful of closed types)


def _prefix(kind: str) -> bytes:
    prefix = _EVENT_PREFIX.get(kind)
    if prefix is None:
        if not kind or "\n" in kind or "\r" in kind:
            raise ValueError("an SSE event type must be a non-empty single-line string")
        prefix = _EVENT_PREFIX[kind] = b"\nevent: " + kind.encode() + b"\ndata: "
    return prefix


def frame_from_payload(frame_id: int, kind: str, payload: bytes) -> SseFrame:
    """The frame of an already serialized event (the Redis mirror stores id, type and payload). The frame keeps a
    right-sized copy of `payload` sliced from the wire: an `orjson.dumps` result is backed by a ~4 KiB allocation
    whatever its length, and the event log retains every frame of a turn for the resume window."""
    head = b"".join((b"id: ", str(frame_id).encode(), _prefix(kind)))
    wire = b"".join((head, payload, b"\n\n"))
    return SseFrame(id=frame_id, type=kind, payload=wire[len(head) : -2], wire=wire)


def encode_frame(frame_id: int, event: Mapping[str, JsonValue]) -> SseFrame:
    """Serialize one turn event (it has a string "type"). Raises ValueError for an id < 1 or a bad type."""
    if frame_id < 1:
        raise ValueError("SSE frame ids start at 1")
    kind = event.get("type")
    if not isinstance(kind, str):
        raise ValueError('a turn event needs a string "type"')
    return frame_from_payload(frame_id, kind, orjson.dumps(event))


def merge_deltas(frames: list[SseFrame]) -> SseFrame:
    """Consecutive generation.delta frames of one attempt as one frame: the texts concatenated, the last frame's id,
    seq and other fields (spec-02 §7 backpressure)."""
    if len(frames) == 1:
        return frames[0]
    last = orjson.loads(frames[-1].payload)
    last["text"] = "".join(orjson.loads(f.payload)["text"] for f in frames)
    return frame_from_payload(frames[-1].id, DELTA_EVENT, orjson.dumps(last))


def delta_attempt(frame: SseFrame) -> str | None:
    """The attempt id of a generation.delta frame (None for any other frame or an unreadable payload)."""
    if frame.type != DELTA_EVENT:
        return None
    try:
        value = orjson.loads(frame.payload).get("attempt_id")
    except (orjson.JSONDecodeError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def too_slow_frame() -> bytes:
    """The best-effort `error{client_too_slow, retryable: false}` frame: no id line, never in the event log."""
    event = NoemaError(ErrorCode.CLIENT_TOO_SLOW, "The client is reading the stream too slowly.").sse_event()
    return b"event: error\ndata: " + orjson.dumps(event) + b"\n\n"
