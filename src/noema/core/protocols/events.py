"""The per-turn event log and the SSE writer (spec-02 §7, IMPL-021)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from noema.core.values import JsonValue


@dataclass(frozen=True, slots=True)
class SseFrame:
    """One pre-serialized SSE frame. `id` is 1, 2, ... per turn; `type` = event["type"]; `payload` = orjson(event);
    `wire` = b"id: {id}\\nevent: {type}\\ndata: {payload}\\n\\n"."""

    id: int
    type: str
    payload: bytes
    wire: bytes


class TurnEventLog(Protocol):
    """In-process per-turn log with a best-effort Redis Streams mirror (5-minute window)."""

    def append(self, turn_id: str, event: Mapping[str, JsonValue]) -> int:
        """Sync. `event` has "type"; assigns the next id (first = 1), serializes once and mirrors to
        `RedisKeys.turn_events` (XADD MAXLEN ~20000) asynchronously. Raises RuntimeError after seal (a bug)."""
        ...

    def subscribe(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame]:
        """Local frames with id > after, then live frames; ends after the turn.completed frame; empty if the turn
        is not local."""
        ...

    def is_local(self, turn_id: str) -> bool:
        """Sync."""
        ...

    def seal(self, turn_id: str, ttl_s: int) -> None:
        """Sync; after turn.completed. The local log is kept ttl_s; the mirror gets EXPIRE ttl_s."""
        ...

    async def attach_remote(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame] | None:
        """The Redis mirror (XREAD BLOCK); None if the mirror doesn't exist (expired or Redis down)."""
        ...

    async def idle_seconds(self, turn_id: str) -> float | None:
        """Abandon watchdog: seconds since the last frame was delivered to any subscriber (local, or remote via
        `RedisKeys.turn_attach`); None while a subscriber is attached."""
        ...


class SseWriter(Protocol):
    """Writes frames to one client with backpressure (04:L281)."""

    async def run(
        self,
        frames: AsyncIterator[SseFrame],
        send: Callable[[bytes], Awaitable[None]],
        *,
        queue_events: int = 256,
        queue_bytes: int = 1 << 20,
        too_slow_s: float = 30,
        ping_s: float = 15,
    ) -> Literal["completed", "disconnected", "client_too_slow"]:
        """Frames go through a bounded queue; at the high-water mark consecutive generation.delta frames of one
        attempt are merged (text concatenated; the last frame's id and seq). Still full after too_slow_s: one
        best-effort error{client_too_slow, retryable: false} frame (no id line, 1 s timeout), then
        "client_too_slow" (the caller cancels the turn, client_abort). `: ping\\n\\n` after ping_s without a write.
        `send()` raising gives "disconnected"."""
        ...
