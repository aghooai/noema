"""The notify bridge end to end (spec-11 §4): a definer function's pg_notify('noema_events', ...) reaches the Redis
channel through the dedicated LISTEN connection; the listener reconnects after its connection is killed."""

from __future__ import annotations

from typing import Any, cast

import anyio
import orjson
import pytest
from tests.fixtures.db import PgTestDatabase

from noema.core.clock import SystemClock
from noema.core.redis_keys import RedisKeys
from noema.ops.notify_bridge import NotifyBridge, PgListener

from .conftest import SwitchRedis

pytestmark = pytest.mark.integration


async def next_message(pubsub: Any) -> dict[str, Any]:
    while True:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if message is not None:
            return cast(dict[str, Any], orjson.loads(message["data"]))


async def test_pg_notify_reaches_redis_and_survives_a_reconnect(
    pg_database: PgTestDatabase, pg_admin: Any, valkey: SwitchRedis, keys: RedisKeys
) -> None:
    listener = PgListener(pg_database.app_dsn, SystemClock(), max_backoff_s=0.5)
    bridge = NotifyBridge(listener, cast(Any, valkey.real), keys)
    pubsub: Any = valkey.real.pubsub()
    await pubsub.subscribe(keys.config_changed(), keys.keys_revoked())
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(bridge.run)
            with anyio.fail_after(5):
                await listener.connected.wait()
            note = {"channel": "config_changed", "payload": {"version": 41}}
            await pg_admin.execute("SELECT pg_notify('noema_events', $1)", orjson.dumps(note).decode())
            with anyio.fail_after(5):
                assert await next_message(pubsub) == {"version": 41}
            await pg_admin.execute(  # kill the LISTEN session: the listener reconnects
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database()"
                " AND usename = 'noema_app' AND query LIKE 'LISTEN%'"
            )
            listener.connected = anyio.Event()
            with anyio.fail_after(10):
                await listener.connected.wait()
            note = {"channel": "keys_revoked", "payload": {"tenant_id": "t_acme", "key_id": "K"}}
            await pg_admin.execute("SELECT pg_notify('noema_events', $1)", orjson.dumps(note).decode())
            with anyio.fail_after(5):
                assert await next_message(pubsub) == {"tenant_id": "t_acme", "key_id": "K"}
            tg.cancel_scope.cancel()
    finally:
        await pubsub.aclose()


async def test_a_silently_dead_listen_connection_is_replaced(pg_database: PgTestDatabase, pg_admin: Any) -> None:
    """Wave E edge fix 22: a LISTEN connection that stops answering without FIN/RST is noticed by the periodic
    `SELECT 1` and replaced, so a notification sent afterwards is delivered."""
    from urllib.parse import urlsplit, urlunsplit

    from tests.integration.db.blackhole import blackhole_proxy

    from noema.core.secret import SecretStr

    parts = urlsplit(pg_database.app_dsn.get_secret_value())
    got: list[str] = []
    async with blackhole_proxy(int(parts.port or 5432)) as proxy:
        proxied = SecretStr(
            urlunsplit(parts._replace(netloc=f"{parts.netloc.rsplit('@', 1)[0]}@127.0.0.1:{proxy.port}"))
        )
        listener = PgListener(proxied, SystemClock(), max_backoff_s=0.2, check_s=0.3)
        async with anyio.create_task_group() as tg:
            tg.start_soon(listener.listen, "noema_events", got.append)
            with anyio.fail_after(5):
                await listener.connected.wait()
            accepted = proxy.accepted
            proxy.freeze()
            with anyio.fail_after(10):
                while proxy.accepted == accepted:  # noqa: ASYNC110 - polling the proxy's connection count
                    await anyio.sleep(0.05)
                while not got:  # sent until the new LISTEN session receives it
                    await pg_admin.execute("SELECT pg_notify('noema_events', 'after')")
                    await anyio.sleep(0.1)
            assert set(got) == {"after"}
            tg.cancel_scope.cancel()
