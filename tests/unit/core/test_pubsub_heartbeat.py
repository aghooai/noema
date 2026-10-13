"""`heartbeat_messages` (Wave E3, edge-fixer defect 22): a PING after 5 s without traffic, `ConnectionError` after 15 s
without any reply, PONGs keep the connection alive, only published messages are yielded, and the time a listener
spends on a message is not silence. On a fake PubSub over the fake clock (a read without data waits its timeout)."""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest
from tests.fakes.clock import FakeClock

from noema.core.pubsub import heartbeat_messages

pytestmark = pytest.mark.unit


class FakePubSub:
    """`get_message` pops queued replies, then delivers `scheduled` (at, message) pairs once the clock reached `at`,
    else waits `timeout` on the clock; `ping` records the time and, if `answers`, queues a PONG."""

    def __init__(self, clock: FakeClock, *, answers: bool) -> None:
        self.clock, self.answers = clock, answers
        self.queue: deque[dict[str, Any]] = deque()
        self.scheduled: deque[tuple[float, dict[str, Any]]] = deque()
        self.pings: list[float] = []

    async def get_message(self, *, ignore_subscribe_messages: bool, timeout: float) -> dict[str, Any] | None:  # noqa: ASYNC109
        assert ignore_subscribe_messages
        if self.queue:
            return self.queue.popleft()
        if self.scheduled and self.clock.monotonic() >= self.scheduled[0][0]:
            return self.scheduled.popleft()[1]
        await self.clock.advance(timeout)
        return None

    async def ping(self) -> None:
        self.pings.append(self.clock.monotonic())
        if self.answers:
            self.queue.append({"type": "pong", "channel": None, "data": b""})


async def test_a_silent_connection_is_pinged_then_declared_dead() -> None:
    clock = FakeClock()
    pubsub, t0 = FakePubSub(clock, answers=False), clock.monotonic()
    with pytest.raises(ConnectionError):
        await anext(heartbeat_messages(pubsub, clock))
    assert pubsub.pings == [t0 + 5.0, t0 + 10.0] and clock.monotonic() == t0 + 15.0


async def test_answered_pings_keep_the_connection_and_only_published_messages_are_yielded() -> None:
    clock = FakeClock()
    pubsub, t0 = FakePubSub(clock, answers=True), clock.monotonic()
    pubsub.queue.append({"type": "subscribe", "channel": b"c", "data": 1})
    pubsub.scheduled.append((t0 + 60.0, {"type": "message", "channel": b"c", "data": b"late"}))
    pubsub.scheduled.append((t0 + 60.0, {"type": "pmessage", "pattern": b"p*", "channel": b"p1", "data": b"p"}))
    got = []
    async for message in heartbeat_messages(pubsub, clock):  # a quiet minute, every PING answered
        got.append(message["data"])
        if len(got) == 2:
            break
    assert got == [b"late", b"p"]  # the subscribe confirmation and the PONGs were not yielded
    assert pubsub.pings == [t0 + 5.0 * k for k in range(1, 13)]


async def test_time_spent_on_a_message_is_not_silence() -> None:
    clock = FakeClock()
    pubsub = FakePubSub(clock, answers=False)
    stream = heartbeat_messages(pubsub, clock)
    pubsub.queue.append({"type": "message", "channel": b"c", "data": b"x"})
    assert (await anext(stream))["data"] == b"x"
    await clock.advance(20.0)  # the listener's own work on the message (e.g. a slow refresh)
    resumed = clock.monotonic()
    with pytest.raises(ConnectionError):
        await anext(stream)
    assert pubsub.pings == [resumed + 5.0, resumed + 10.0] and clock.monotonic() == resumed + 15.0
