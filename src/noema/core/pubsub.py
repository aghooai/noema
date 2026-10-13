"""Application-level liveness of the Redis pub/sub listeners (Wave E3; the loop of `identity/revocation.py`).

A subscribed connection only reads, so one that died without FIN/RST (a dead node, a partition, a dropped NAT entry)
would look idle forever: TCP keepalives notice it only after minutes. `heartbeat_messages` reads a subscribed
`redis.asyncio` PubSub (duck-typed: `get_message(ignore_subscribe_messages=True, timeout=...)` and `ping()`), sends a
PING on the subscribed connection after `ping_s` without traffic (again every `ping_s` while quiet), and raises
`ConnectionError` once nothing (no message, no PONG) arrived for `dead_s`; the listener then closes the connection and
resubscribes. Used by the `turn_cancel` / `session_released`, `breaker_events`, `calibrators_changed` and
`config_changed` listeners. The time a listener spends on a message does not count as silence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Final

from noema.core.clock import Clock

PING_S: Final = 5.0  # a PING on the quiet subscribed connection
DEAD_S: Final = 15.0  # no message and no PONG this long: the connection is dead, resubscribe
POLL_S: Final = 1.0  # the longest single read
PUBLISHED: Final = frozenset({"message", "pmessage"})


async def heartbeat_messages(
    pubsub: Any, clock: Clock, *, ping_s: float = PING_S, dead_s: float = DEAD_S, poll_s: float = POLL_S
) -> AsyncIterator[dict[str, Any]]:
    """Yield every published message (`message`, `pmessage`) of the subscribed `pubsub` until the connection fails
    (the PubSub's own errors propagate) or stays silent for `dead_s` (`ConnectionError`)."""
    heard = pinged = clock.monotonic()
    while True:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=min(poll_s, ping_s))
        now = clock.monotonic()
        if message is not None:
            heard = now
            if message.get("type") in PUBLISHED:
                yield message
                heard = now = clock.monotonic()
        elif now - heard >= dead_s:
            raise ConnectionError(f"pub/sub silent for {now - heard:.1f} s")
        if now - heard >= ping_s and now - pinged >= ping_s:
            await pubsub.ping()
            pinged = now
