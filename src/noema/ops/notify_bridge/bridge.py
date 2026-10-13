"""`NotifyBridge` (spec-11 §4): `pg_notify('noema_events', '{"channel": c, "payload": p}')` from the `admin.*` and
labeling definer functions -> Redis pub/sub.

Every `jobs` process runs one bridge on its own LISTEN connection. Each notification is published, payload
unchanged, to the channel of `c`: `config_changed` -> `RedisKeys.config_changed()` ({version}),
`calibrators_changed` -> `calibrators_changed()` ({scope_key, n}), `keys_revoked` -> `keys_revoked()`
({tenant_id, key_id}). Several bridges publish duplicates, which receivers tolerate (each handler is idempotent);
a malformed notification or an unknown channel is logged and dropped; a Redis outage drops the message (pods also
poll config and calibrators every 5 s and key cache entries expire after 60 s, so changes are delayed, never lost).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Final

import anyio
import orjson

from noema.core.redis_keys import RedisKeys
from noema.ops.notify_bridge.listener import PgListener

if TYPE_CHECKING:
    from redis.asyncio import Redis

log: Final = logging.getLogger("noema.ops.notify_bridge")
PG_CHANNEL: Final = "noema_events"
BUFFER: Final = 1000


def redis_channels(keys: RedisKeys) -> Mapping[str, str]:
    """notification channel -> Redis pub/sub channel."""
    return {
        "config_changed": keys.config_changed(),
        "calibrators_changed": keys.calibrators_changed(),
        "keys_revoked": keys.keys_revoked(),
    }


class NotifyBridge:
    """See the module docstring."""

    def __init__(self, listener: PgListener, redis: Redis, keys: RedisKeys) -> None:
        self._listener = listener
        self._redis = redis
        self._channels = redis_channels(keys)
        self.forwarded = 0

    async def run(self) -> None:
        """LISTEN `noema_events` and publish until cancelled."""
        send, receive = anyio.create_memory_object_stream[str](BUFFER)
        async with anyio.create_task_group() as tg, send, receive:
            tg.start_soon(self._listener.listen, PG_CHANNEL, self._enqueue(send.send_nowait))
            async for raw in receive:
                await self.forward(raw)

    @staticmethod
    def _enqueue(push: Callable[[str], None]) -> Callable[[str], None]:
        def on_notify(raw: str) -> None:
            try:
                push(raw)
            except anyio.WouldBlock:
                log.warning("notify bridge: backlog full; notification dropped (pods poll)")

        return on_notify

    async def forward(self, raw: str) -> bool:
        """Publish one `noema_events` payload; True iff it was published."""
        try:
            doc = orjson.loads(raw)
        except orjson.JSONDecodeError:
            log.warning("notify bridge: malformed notification dropped")
            return False
        channel = doc.get("channel") if isinstance(doc, dict) else None
        target = self._channels.get(channel) if isinstance(channel, str) else None
        if target is None or not isinstance(doc, dict) or "payload" not in doc:
            log.warning("notify bridge: notification for unknown channel %r dropped", channel)
            return False
        try:
            await self._redis.publish(target, orjson.dumps(doc["payload"]))
        except Exception as exc:
            log.warning("notify bridge: publish to %s failed (%s); receivers poll", channel, type(exc).__name__)
            return False
        self.forwarded += 1
        return True
