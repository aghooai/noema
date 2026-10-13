"""Cross-pod coordination of turns through Redis (spec-04 §2, §11; spec-02 §13; IMPL-021, IMPL-022).

`Coordinator` is what the orchestrator needs from Redis: the session FIFO queue and `session_pending` flag, the
running-turn owner registry, and the `session_released` / `turn_cancel` channels. `RedisCoordinator` implements
it on the process's bytes client. Every call is fail-soft (Redis down): the queue degrades to polling only, the
pending check reports False, owner lookups and cancel publishes raise `CoordinationDown` (the cancel route answers
500), and the listener retries with a backoff. The listener's subscribed connection is checked at the application
level (`noema.core.pubsub.heartbeat_messages`: a PING after 5 s quiet, resubscribe after 15 s without a reply).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol

import anyio
import orjson

from noema.core.clock import Clock, deadline_scope
from noema.core.pubsub import heartbeat_messages
from noema.core.redis_keys import RedisKeys
from noema.core.values import JsonValue

if TYPE_CHECKING:
    from redis.asyncio import Redis

log = logging.getLogger("noema.turns.coordination")

PENDING_TTL_S: Final = 86_400
OWNER_TTL_S: Final = 60
REDIS_TIMEOUT_S: Final = 0.25
LISTEN_BACKOFF_S: Final = 1.0

# One script so every queue score is Redis TIME in ms (spec-04 §2): prune entries older than the queue timeout + 1 s,
# optionally join (ZADD NX + PEXPIRE), and return the head ticket.
_QUEUE_LUA: Final = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local timeout = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - timeout - 1000)
if ARGV[3] == 'join' then
  redis.call('ZADD', KEYS[1], 'NX', now, ARGV[1])
  redis.call('PEXPIRE', KEYS[1], timeout + 1000)
end
local head = redis.call('ZRANGE', KEYS[1], 0, 0)
if head[1] then return head[1] end
return false
"""


class CoordinationDown(Exception):
    """Redis could not answer (down or timed out)."""


class Coordinator(Protocol):
    """See the module docstring. All methods are async and never raise unless documented."""

    async def session_pending(self, session_id: str) -> bool:
        """`EXISTS session_pending(sid)`; False when Redis is down (no pending check then, IMPL-022)."""
        ...

    async def mark_session_pending(self, session_id: str, turn_id: str) -> None:
        """`SETEX session_pending(sid) 86400 turn_id` before a turn_finalize outbox put (best effort)."""
        ...

    async def queue_join(self, session_id: str, turn_id: str, timeout_ms: int) -> bool:
        """Join the FIFO queue; False when Redis is down (the caller polls without fairness)."""
        ...

    async def queue_head(self, session_id: str, turn_id: str, timeout_ms: int) -> str | None:
        """Prune, then the head ticket; None when Redis is down or the queue is empty."""
        ...

    async def queue_leave(self, session_id: str, turn_id: str) -> None: ...

    async def wait_released(self, session_id: str, timeout_s: float) -> None:
        """Return on a `session_released(sid)` message or after `timeout_s` (the 50 ms poll)."""
        ...

    async def publish_released(self, tenant_id: str, session_id: str, last_seq: int | None) -> None: ...

    async def set_owner(self, turn_id: str, payload: Mapping[str, JsonValue]) -> None:
        """`SET turn_owner(turn) payload EX 60` (best effort)."""
        ...

    async def refresh_owners(self, payloads: Mapping[str, Mapping[str, JsonValue]]) -> None:
        """Re-SET every local owner entry (the 20 s refresh; best effort)."""
        ...

    async def delete_owner(self, turn_id: str) -> None: ...

    async def get_owner(self, turn_id: str) -> Mapping[str, JsonValue] | None:
        """Raises `CoordinationDown`."""
        ...

    async def publish_cancel(self, tenant_id: str, turn_id: str) -> None:
        """Raises `CoordinationDown`."""
        ...

    async def listen(self, on_cancel: Callable[[str, str], Awaitable[None]]) -> None:
        """Run forever: dispatch `turn_cancel` messages to `on_cancel(tenant_id, turn_id)` and wake
        `wait_released` waiters; reconnects with a backoff while Redis is down."""
        ...


class _Waiters:
    """Local waiters of `session_released`, keyed by session id."""

    def __init__(self) -> None:
        self._events: dict[str, anyio.Event] = {}

    def event(self, session_id: str) -> anyio.Event:
        ev = self._events.get(session_id)
        if ev is None:
            ev = self._events[session_id] = anyio.Event()
        return ev

    def wake(self, session_id: str) -> None:
        ev = self._events.pop(session_id, None)
        if ev is not None:
            ev.set()


async def wait_or_timeout(event: anyio.Event, clock: Clock, timeout_s: float) -> None:
    """Wait for `event` at most `timeout_s` on `clock`."""
    async with deadline_scope(clock, clock.monotonic() + max(0.0, timeout_s)):
        await event.wait()


def _glob_escape(s: str) -> str:
    return "".join("\\" + ch if ch in "*?[]\\" else ch for ch in s)


class RedisCoordinator:
    """Implements `Coordinator` on the process's bytes Redis client (see the module docstring)."""

    def __init__(self, redis: Redis, keys: RedisKeys, clock: Clock) -> None:
        self._redis = redis
        self._keys = keys
        self._clock = clock
        self._waiters = _Waiters()
        self._queue_script: Any = redis.register_script(_QUEUE_LUA)
        self._released_prefix = keys.session_released("")

    async def _call[T](self, fn: Callable[[], Awaitable[T]]) -> T:
        from redis.exceptions import RedisError

        try:
            with anyio.fail_after(REDIS_TIMEOUT_S):
                return await fn()
        except (RedisError, OSError, TimeoutError) as exc:
            raise CoordinationDown(type(exc).__name__) from exc

    async def session_pending(self, session_id: str) -> bool:
        try:
            return bool(await self._call(lambda: self._redis.exists(self._keys.session_pending(session_id))))
        except CoordinationDown:
            return False

    async def mark_session_pending(self, session_id: str, turn_id: str) -> None:
        key = self._keys.session_pending(session_id)
        try:
            await self._call(lambda: self._redis.set(key, turn_id, ex=PENDING_TTL_S))
        except CoordinationDown:
            log.warning("session_pending not set (Redis down): session %s", session_id)

    async def _queue(self, session_id: str, turn_id: str, timeout_ms: int, mode: str) -> str | None:
        key = self._keys.session_queue(session_id)
        head = await self._call(lambda: self._queue_script(keys=[key], args=[turn_id, timeout_ms, mode]))
        if head is None or head is False:
            return None
        return head.decode() if isinstance(head, bytes) else str(head)

    async def queue_join(self, session_id: str, turn_id: str, timeout_ms: int) -> bool:
        try:
            await self._queue(session_id, turn_id, timeout_ms, "join")
        except CoordinationDown:
            return False
        return True

    async def queue_head(self, session_id: str, turn_id: str, timeout_ms: int) -> str | None:
        try:
            return await self._queue(session_id, turn_id, timeout_ms, "head")
        except CoordinationDown:
            return None

    async def queue_leave(self, session_id: str, turn_id: str) -> None:
        key = self._keys.session_queue(session_id)
        with contextlib.suppress(CoordinationDown):
            await self._call(lambda: self._redis.zrem(key, turn_id))

    async def wait_released(self, session_id: str, timeout_s: float) -> None:
        await wait_or_timeout(self._waiters.event(session_id), self._clock, timeout_s)

    async def publish_released(self, tenant_id: str, session_id: str, last_seq: int | None) -> None:
        payload = orjson.dumps({"tenant_id": tenant_id, "session_id": session_id, "last_seq": last_seq})
        self._waiters.wake(session_id)
        with contextlib.suppress(CoordinationDown):
            await self._call(lambda: self._redis.publish(self._keys.session_released(session_id), payload))

    async def set_owner(self, turn_id: str, payload: Mapping[str, JsonValue]) -> None:
        key, value = self._keys.turn_owner(turn_id), orjson.dumps(payload)
        try:
            await self._call(lambda: self._redis.set(key, value, ex=OWNER_TTL_S))
        except CoordinationDown:
            log.warning("turn_owner not registered (Redis down): %s", turn_id)

    async def refresh_owners(self, payloads: Mapping[str, Mapping[str, JsonValue]]) -> None:
        if not payloads:
            return

        async def run() -> object:
            pipe = self._redis.pipeline(transaction=False)
            for turn_id, payload in payloads.items():
                pipe.set(self._keys.turn_owner(turn_id), orjson.dumps(payload), ex=OWNER_TTL_S)
            return await pipe.execute()

        try:
            await self._call(run)
        except CoordinationDown:
            log.warning("turn_owner refresh skipped (Redis down)")

    async def delete_owner(self, turn_id: str) -> None:
        with contextlib.suppress(CoordinationDown):
            await self._call(lambda: self._redis.delete(self._keys.turn_owner(turn_id)))

    async def get_owner(self, turn_id: str) -> Mapping[str, JsonValue] | None:
        raw = await self._call(lambda: self._redis.get(self._keys.turn_owner(turn_id)))
        if raw is None:
            return None
        value = orjson.loads(raw)
        return value if isinstance(value, dict) else None

    async def publish_cancel(self, tenant_id: str, turn_id: str) -> None:
        payload = orjson.dumps({"tenant_id": tenant_id, "turn_id": turn_id})
        await self._call(lambda: self._redis.publish(self._keys.turn_cancel(), payload))

    async def listen(self, on_cancel: Callable[[str, str], Awaitable[None]]) -> None:
        cancel_channel = self._keys.turn_cancel().encode()
        while True:
            try:
                await self._listen_once(cancel_channel, on_cancel)
            except Exception as exc:
                log.warning("turn channel listener reconnecting: %s", type(exc).__name__)
            await self._clock.sleep(LISTEN_BACKOFF_S)

    async def _listen_once(self, cancel_channel: bytes, on_cancel: Callable[[str, str], Awaitable[None]]) -> None:
        pubsub: Any = self._redis.pubsub()
        try:
            await pubsub.subscribe(cancel_channel)
            await pubsub.psubscribe(_glob_escape(self._released_prefix) + "*")
            async for msg in heartbeat_messages(pubsub, self._clock):
                channel = msg.get("channel")
                data = msg.get("data")
                if not isinstance(channel, bytes) or not isinstance(data, bytes):
                    continue
                if channel == cancel_channel:
                    decoded = _decode(data)
                    tenant, turn = decoded.get("tenant_id"), decoded.get("turn_id")
                    if isinstance(tenant, str) and isinstance(turn, str):
                        await on_cancel(tenant, turn)
                else:
                    self._waiters.wake(channel.decode()[len(self._released_prefix) :])
        finally:
            with anyio.CancelScope(shield=True):
                await pubsub.aclose()


def _decode(data: bytes) -> dict[str, JsonValue]:
    try:
        value = orjson.loads(data)
    except orjson.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
