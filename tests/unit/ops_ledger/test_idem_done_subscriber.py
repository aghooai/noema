"""Wave E2 (edge defect 22 for the shared `idem_done` connection): while a waiter reads it, a quiet connection is
PINGed every `ping_s` and reset after `dead_s` without any reply (a hung proxy keeps TCP alive, so keepalives never
notice); a PONG keeps it. A reset connection leaves the waiter polling. Virtual clock, scripted pub/sub."""

from __future__ import annotations

from typing import Any

import pytest
from tests.fakes.clock import FakeClock

from noema.ops.idempotency.waiting import DoneSubscriber

pytestmark = pytest.mark.unit


class ScriptedPubSub:
    """A pub/sub connection that never delivers a message; `answers` makes it reply PONG to each PING."""

    def __init__(self, clock: FakeClock, *, answers: bool) -> None:
        self.clock, self.answers = clock, answers
        self.pings, self.closed, self.pongs_due = 0, False, 0

    async def subscribe(self, channel: str) -> None:
        return None

    async def unsubscribe(self, channel: str) -> None:
        return None

    async def get_message(self, **kwargs: Any) -> dict[str, Any] | None:  # timeout=, as redis-py's
        if self.pongs_due:
            self.pongs_due -= 1
            return {"type": "pong", "channel": None, "data": b""}
        await self.clock.sleep(kwargs["timeout"])
        return None

    async def ping(self) -> None:
        self.pings += 1
        self.pongs_due += int(self.answers)

    async def aclose(self) -> None:
        self.closed = True


class ScriptedRedis:
    def __init__(self, clock: FakeClock, *, answers: bool) -> None:
        self.clock, self.answers = clock, answers
        self.made: list[ScriptedPubSub] = []

    def pubsub(self) -> ScriptedPubSub:
        self.made.append(ScriptedPubSub(self.clock, answers=self.answers))
        return self.made[-1]


@pytest.mark.parametrize("answers", [False, True])
async def test_a_quiet_connection_is_pinged_and_reset_only_when_nothing_answers(answers: bool) -> None:
    clock, pings = FakeClock(), 7 if answers else 2  # every 5 s of silence; dead after 15 s without a reply
    redis = ScriptedRedis(clock, answers=answers)
    subscriber = DoneSubscriber(redis, clock, ping_s=5.0, dead_s=15.0)  # type: ignore[arg-type]
    until = clock.monotonic() + 1
    async with clock.autojump(), subscriber.signal("idem-done", "t_acme", subscribe_until=until) as waiter:
        assert subscriber.connected
        assert await waiter.wait(clock, 40.0) is False  # no idem_done message: the poll decides
        assert subscriber.connected is answers
    (pubsub,) = redis.made
    assert pubsub.pings >= pings
    assert pubsub.closed  # reset (dead), or closed when its last waiter left
