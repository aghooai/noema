"""The `keys_revoked` listener (spec-09 §2, spec-01 §3 background task of the api process).

`noema-admin keys revoke` sets `revoked_at` and `admin.revoke_api_key` notifies `keys_revoked {tenant_id, key_id}`;
the notify bridge republishes it on `RedisKeys.keys_revoked()`. Every api pod evicts that key's LRU entry, so a
revocation takes effect at once with Redis up and within the 60 s cache TTL with Redis down. A payload whose
`tenant_id` differs from the cached row's tenant is dropped as `pubsub_tenant_mismatch` (SEC-ISO-02).

Liveness (Wave E): a subscribed connection only reads, so one that died without FIN/RST (a dead node, a partition, a
dropped NAT entry) would look idle forever. While quiet the listener sends a PING every `PING_S` on the subscribed
connection; when nothing (no message, no PONG) arrived for `DEAD_S` it closes the connection and resubscribes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

import orjson
from redis.exceptions import RedisError

from noema.core.clock import Clock
from noema.core.redis_keys import RedisKeys
from noema.identity.authn import ApiKeyAuthenticator
from noema.identity.security import SecurityEvents

if TYPE_CHECKING:
    from redis.asyncio import Redis

RECONNECT_S: Final = 1.0
POLL_S: Final = 1.0
PING_S: Final = 5.0  # a PING on the quiet subscribed connection
DEAD_S: Final = 15.0  # no message and no PONG this long: the connection is dead, resubscribe

log = logging.getLogger("noema.identity.revocation")


def handle_revocation(payload: bytes | str, authn: ApiKeyAuthenticator, security: SecurityEvents) -> bool:
    """Apply one `keys_revoked` message; True if a cached key was evicted. Malformed payloads are ignored."""
    try:
        message = orjson.loads(payload)
    except orjson.JSONDecodeError:
        message = None
    if not isinstance(message, dict):
        log.warning("keys_revoked: ignored a malformed payload")
        return False
    key_id, tenant_id = message.get("key_id"), message.get("tenant_id")
    if not isinstance(key_id, str) or not isinstance(tenant_id, str):
        log.warning("keys_revoked: ignored a payload without key_id/tenant_id")
        return False
    entry = authn.cache.peek(key_id)
    if entry is not None and entry.row is not None and entry.row.tenant_id != tenant_id:
        security.emit("pubsub_tenant_mismatch", tenant_id=entry.row.tenant_id, details={"channel": "keys_revoked"})
        return False
    return authn.evict(key_id)


async def run_revocation_listener(
    redis: Redis,
    keys: RedisKeys,
    authn: ApiKeyAuthenticator,
    security: SecurityEvents,
    clock: Clock,
    *,
    ping_s: float = PING_S,
    dead_s: float = DEAD_S,
) -> None:
    """Subscribe to `keys_revoked` until cancelled; reconnects every second while Redis is unreachable or the
    connection stopped answering (the LRU TTL bounds revocation meanwhile). Run it with `services.runtime.spawn` in
    the api process."""
    channel = keys.keys_revoked()
    while True:
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(channel)
            heard = pinged = clock.monotonic()
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=min(POLL_S, ping_s))
                now = clock.monotonic()
                if message is not None:
                    heard = now
                    if message.get("type") == "message":
                        handle_revocation(message["data"], authn, security)
                elif now - heard >= dead_s:
                    raise ConnectionError(f"no reply for {now - heard:.1f} s")
                if now - heard >= ping_s and now - pinged >= ping_s:
                    await pubsub.ping()
                    pinged = now
        except (OSError, RedisError) as exc:
            log.warning("keys_revoked listener: Redis unavailable (%s); retrying", type(exc).__name__)
        finally:
            await _close(pubsub)
        await clock.sleep(RECONNECT_S)


async def _close(pubsub: object) -> None:
    close = getattr(pubsub, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # closing a broken connection must not mask the reconnect
        log.debug("keys_revoked listener: pubsub close failed", exc_info=True)
