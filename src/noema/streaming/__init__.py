"""noema.streaming (spec-01 §1): the per-turn event log (in-process, with the Redis Streams mirror) and the SSE
writer with backpressure (spec-02 §7, IMPL-021, IMPL-034)."""

from .event_log import InProcessTurnEventLog
from .frames import PING, encode_frame, frame_from_payload, merge_deltas, too_slow_frame
from .mirror import RedisEventMirror
from .sse_writer import BackpressureSseWriter

__all__ = [
    "PING",
    "BackpressureSseWriter",
    "InProcessTurnEventLog",
    "RedisEventMirror",
    "encode_frame",
    "frame_from_payload",
    "merge_deltas",
    "too_slow_frame",
]
