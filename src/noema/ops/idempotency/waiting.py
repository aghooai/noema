"""`DoneSubscriber`: the best-effort `idem_done` wake-up of `IdempotencyStore.await_result` (spec-11 §5).

Wave E2 (edge defect 12): ONE pub/sub connection per process (per store) carries the wake-ups of every waiting
duplicate; each waiter opened its own before, so a burst of duplicates could drain the Redis pool. A waiter's
channel `RedisKeys.idem_done(scope_hash, key)` is subscribed while it has a waiter, the connection is open while
any channel is, and a waiting waiter reads it for all of them (one reader at a time), waking the waiters of the
message's channel and tenant (messages of another tenant are dropped, spec-09 §7). Redis being down or slow only
costs latency: SUBSCRIBE is bounded (by the caller's `subscribe_until`, at most 0.5 s), a failed or timed-out
subscription resets the connection (its waiters keep polling) and the poll alone decides. While read, a quiet
connection is PINGed every `PING_S` and reset after `DEAD_S` without any reply (a connection that stopped
answering, e.g. through a hung proxy, is not left in place). Closing is bounded by 0.5 s too.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Final

import anyio
import orjson

from noema.core.clock import Clock, deadline_scope
from noema.ops.jobs.timing import Wakeup

if TYPE_CHECKING:
    from redis.asyncio import Redis

log: Final = logging.getLogger("noema.ops.idempotency")
SUBSCRIBE_TIMEOUT_S: Final = 0.5
CLOSE_TIMEOUT_S: Final = 0.5
READ_S: Final = 1.0  # one read of the shared connection; the heartbeat is checked between reads
PING_S: Final = 5.0
DEAD_S: Final = 15.0
_Entry = tuple[str, Wakeup]  # (tenant_id, the waiter's wake-up)


async def _close(pubsub: Any, clock: Clock) -> None:
    with anyio.CancelScope(shield=True):
        try:
            async with deadline_scope(clock, clock.monotonic() + CLOSE_TIMEOUT_S):
                await pubsub.aclose()
        except Exception as exc:
            log.debug("idem_done unsubscribe failed (%s)", type(exc).__name__)


class DoneWaiter:
    """One waiter's wake-up: `wait(clock, timeout_s)` returns True when its original's `idem_done` arrived."""

    def __init__(self, subscriber: DoneSubscriber | None, wake: Wakeup) -> None:
        self._subscriber = subscriber  # None: the subscription failed, polling only
        self._wake = wake

    async def wait(self, clock: Clock, timeout_s: float) -> bool:
        """Until woken or `timeout_s`; meanwhile this waiter takes its turn reading the shared connection."""
        if self._subscriber is None:
            return await self._wake.wait(clock, timeout_s)
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._subscriber.pump)
            woken = await self._wake.wait(clock, timeout_s)
            tg.cancel_scope.cancel()
        return woken


class DoneSubscriber:
    """The process's shared `idem_done` subscription (see the module docstring)."""

    def __init__(self, redis: Redis, clock: Clock, *, ping_s: float = PING_S, dead_s: float = DEAD_S) -> None:
        self._redis = redis
        self._clock = clock
        self._ping_s = ping_s
        self._dead_s = dead_s
        self._pubsub: Any = None
        self._waiters: dict[str, list[_Entry]] = {}  # the channels subscribed on `_pubsub` -> their waiters
        self._commands = anyio.Lock()  # (un)subscribe in order on the shared connection
        self._reader = anyio.Lock()  # one reader of the shared connection at a time
        self._heard = self._pinged = 0.0

    @property
    def connected(self) -> bool:
        """True while the shared connection is open (some channel has a waiter)."""
        return self._pubsub is not None

    @asynccontextmanager
    async def signal(self, channel: str, tenant_id: str, *, subscribe_until: float) -> AsyncIterator[DoneWaiter]:
        """A waiter woken by `idem_done` messages of `tenant_id` on `channel` while the block runs; SUBSCRIBE gives up
        at `subscribe_until` (`clock.monotonic()` time) or after 0.5 s. Exceptions of the block propagate unchanged."""
        entry: _Entry = (tenant_id, Wakeup())
        until = min(subscribe_until, self._clock.monotonic() + SUBSCRIBE_TIMEOUT_S)
        if not await self._join(channel, entry, until):
            yield DoneWaiter(None, entry[1])
            return
        try:
            yield DoneWaiter(self, entry[1])
        finally:
            await self._leave(channel, entry)

    async def _join(self, channel: str, entry: _Entry, until: float) -> bool:
        try:
            async with deadline_scope(self._clock, until), self._commands:
                waiters = self._waiters.get(channel)
                if waiters is None:
                    try:
                        if self._pubsub is None:
                            self._pubsub = self._redis.pubsub()
                            self._heard = self._pinged = self._clock.monotonic()
                        await self._pubsub.subscribe(channel)
                    except BaseException:  # failed or timed out: the connection's state is unknown
                        await self._reset_locked()
                        raise
                    waiters = self._waiters[channel] = []
                waiters.append(entry)
                return True
        except Exception as exc:
            log.debug("idem_done subscription unavailable (%s); polling only", type(exc).__name__)
        return False

    async def _leave(self, channel: str, entry: _Entry) -> None:
        with anyio.CancelScope(shield=True):
            async with self._commands:
                waiters = self._waiters.get(channel)
                if waiters is None or entry not in waiters:
                    return  # the connection was reset meanwhile
                waiters.remove(entry)
                if waiters:
                    return
                del self._waiters[channel]
                if not self._waiters:
                    await self._reset_locked()  # nobody waits: close the connection
                    return
                try:
                    async with deadline_scope(self._clock, self._clock.monotonic() + CLOSE_TIMEOUT_S) as scope:
                        await self._pubsub.unsubscribe(channel)
                    if scope.cancelled_caught:
                        raise TimeoutError("UNSUBSCRIBE timed out")
                except Exception as exc:
                    log.debug("idem_done unsubscribe failed (%s); connection reset", type(exc).__name__)
                    await self._reset_locked()

    async def _reset_locked(self) -> None:
        """Close the shared connection (with `_commands` held); its waiters keep polling."""
        pubsub, self._pubsub = self._pubsub, None
        self._waiters.clear()
        if pubsub is not None:
            await _close(pubsub, self._clock)

    async def _reset(self, pubsub: Any) -> None:
        with anyio.CancelScope(shield=True):
            async with deadline_scope(self._clock, self._clock.monotonic() + CLOSE_TIMEOUT_S), self._commands:
                if self._pubsub is pubsub:  # not replaced meanwhile
                    await self._reset_locked()

    async def pump(self) -> None:
        """Read the shared connection for every waiter until cancelled (the reading waiter's wait ended; the next
        waiting waiter takes over), PINGing a quiet connection; a Redis error or `dead_s` without any reply resets
        it (the waiters' poll carries on, the next subscription reconnects)."""
        async with self._reader:
            while True:
                pubsub = self._pubsub
                if pubsub is None:
                    await anyio.sleep_forever()
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=READ_S)
                    now = self._clock.monotonic()
                    if message is not None:
                        self._heard = now
                        self._dispatch(message)
                    elif now - self._heard >= self._dead_s:
                        raise ConnectionError(f"no reply for {now - self._heard:.1f} s")
                    if now - self._heard >= self._ping_s and now - self._pinged >= self._ping_s:
                        self._pinged = now
                        await pubsub.ping()
                except Exception as exc:
                    log.debug("idem_done subscription lost (%s); polling only", type(exc).__name__)
                    await self._reset(pubsub)

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        if message.get("type") != "message":
            return  # a PONG
        channel = message.get("channel")
        waiters = self._waiters.get(channel.decode() if isinstance(channel, bytes) else str(channel))
        if not waiters:
            return
        try:
            doc = orjson.loads(message["data"])
        except (orjson.JSONDecodeError, KeyError, TypeError):
            return
        tenant = doc.get("tenant_id") if isinstance(doc, dict) else None
        for waiter_tenant, wake in waiters:
            if waiter_tenant == tenant:
                wake.set()
