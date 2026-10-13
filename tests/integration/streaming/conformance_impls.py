"""Registers the Redis-mirrored `InProcessTurnEventLog` in the event_log conformance suite: two "pods" (two logs,
two mirrors, two clients) on the compose Valkey under a fresh namespace, each running its mirror writer.

The suite reads the other pod's mirror right after appending on the first pod. In production a duplicate arrives
long after the original's frames were mirrored; here `other_pod.attach_remote` first awaits the first pod's
`RedisEventMirror.drain()` so the (asynchronous, best-effort) mirror write has landed. Everything else is the real
read path."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import anyio
import pytest
from tests.contract_internal.kits import EventLogKit
from tests.contract_internal.registry import Harness, register

from noema.core.protocols.events import SseFrame
from noema.core.values import JsonValue
from noema.streaming import InProcessTurnEventLog, RedisEventMirror

from .redis_support import redis_namespace, second_client


class _AfterDrain:
    """`other_pod` of the kit: the real second log; attach_remote waits for the first pod's mirror to drain."""

    def __init__(self, log: InProcessTurnEventLog, first_mirror: RedisEventMirror) -> None:
        self._log = log
        self._first = first_mirror

    def append(self, turn_id: str, event: Mapping[str, JsonValue]) -> int:
        return self._log.append(turn_id, event)

    def subscribe(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame]:
        return self._log.subscribe(turn_id, after=after)

    def is_local(self, turn_id: str) -> bool:
        return self._log.is_local(turn_id)

    def seal(self, turn_id: str, ttl_s: int) -> None:
        self._log.seal(turn_id, ttl_s)

    async def attach_remote(self, turn_id: str, *, after: int = 0) -> AsyncIterator[SseFrame] | None:
        await self._first.drain()
        return await self._log.attach_remote(turn_id, after=after)

    async def idle_seconds(self, turn_id: str) -> float | None:
        return await self._log.idle_seconds(turn_id)


@register("event_log", "redis-mirror", marks=(pytest.mark.integration,))
@asynccontextmanager
async def redis_mirrored_event_log(h: Harness) -> AsyncIterator[EventLogKit]:
    url = h.fixture("noema_services").redis_url
    async with redis_namespace(url, h.run_id) as (client, keys):
        other = second_client(url)
        mirror_a = RedisEventMirror(client, keys, h.clock, block_ms=200)
        mirror_b = RedisEventMirror(other, keys, h.clock, block_ms=200)
        pod_a = InProcessTurnEventLog(h.clock, mirror=mirror_a)
        pod_b = InProcessTurnEventLog(h.clock, mirror=mirror_b)
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(mirror_a.run)
                tg.start_soon(mirror_b.run)
                yield EventLogKit(pod_a, h.clock, _AfterDrain(pod_b, mirror_a))
                tg.cancel_scope.cancel()
        finally:
            await other.aclose()
