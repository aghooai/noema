"""R4 (Wave E3; C2b R4, spec-10 C2) on the real composition against the seeded stack (conftest.py): the process's
Redis `BlockingConnectionPool` holds at most NOEMA_REDIS_MAX_CONNECTIONS connections and a caller waits about
0.5 s for a free one before the ConnectionError of an unreachable Redis; Redis commands and pipelines, the Postgres
units of work and both pools' acquire waits are observed (a random 1 in 16 of each; here all), and the pool gauges
are sampled every second."""

from __future__ import annotations

import time
import uuid
from typing import Any

import anyio
import pytest
import redis.exceptions
from tests.integration.composition.conftest import Seeded

from noema import composition

pytestmark = [pytest.mark.integration]


def _points(services: composition.Services) -> dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, int]]:
    """(metric, labels) -> (gauge value or histogram sum, histogram count; 0 for gauges)."""
    setup = services._lifecycle.telemetry_setup
    assert setup is not None and setup.metrics_memory is not None
    data = setup.metrics_memory.get_metrics_data()
    out: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, int]] = {}
    for rm in data.resource_metrics if data else ():
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    labels = tuple(sorted((k, str(v)) for k, v in (point.attributes or {}).items()))
                    p: Any = point
                    value = p.value if hasattr(p, "value") else p.sum
                    out[(metric.name, labels)] = (float(value), int(getattr(p, "count", 0)))
    return out


async def test_the_redis_pool_is_bounded_and_a_caller_waits_briefly(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOEMA_REDIS_MAX_CONNECTIONS", "8")
    services = composition.build("test")
    client, pool = services.redis, services.redis.connection_pool
    held = [await pool.get_connection() for _ in range(8)]  # type: ignore[no-untyped-call]
    try:
        began = time.monotonic()
        with anyio.fail_after(5), pytest.raises(redis.exceptions.ConnectionError, match="No connection available"):
            await client.ping()
        assert 0.45 <= time.monotonic() - began < 2.0  # REDIS_POOL_TIMEOUT_S, not 20 s nor MaxConnectionsError
    finally:
        for conn in held:
            await pool.release(conn)
    assert await client.ping()
    held = [await pool.get_connection() for _ in range(8)]  # type: ignore[no-untyped-call]

    async def ping() -> None:
        assert await client.ping()

    async with anyio.create_task_group() as tg:  # a burst past the bound waits for a released connection
        tg.start_soon(ping)
        await anyio.sleep(0.2)
        await pool.release(held.pop())
    for conn in held:
        await pool.release(conn)
    await client.aclose()


async def test_pool_and_command_metrics_are_observed(seeded: Seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOEMA_REDIS_MAX_CONNECTIONS", "64")
    monkeypatch.setattr(composition, "DATASTORE_SAMPLE_RATE", 1.0)  # time every operation (default 1 in 16)
    services = composition.build("test", process="jobs")
    await composition.start(services, "jobs")
    try:
        key = f"r4:{uuid.uuid4().hex}"
        await services.redis.set(key, b"1", px=10_000)
        assert await services.redis.get(key) == b"1"
        async with services.redis.pipeline(transaction=False) as pipe:
            pipe.get(key)
            pipe.delete(key)
            assert await pipe.execute() == [b"1", 1]
        async with services.db.system_uow() as calls:
            await calls.calibrator_epochs()
        await anyio.sleep(composition.POOL_GAUGES_EVERY_S + 0.5)
        m = _points(services)
    finally:
        await composition.stop(services)
    for command in ("SET", "GET", "PIPELINE"):
        assert m[("aghoo_redis_command_seconds", (("command", command),))][1] >= 1, command
    assert m[("aghoo_redis_pool_acquire_seconds", ())][1] >= 3
    assert m[("aghoo_db_pool_acquire_seconds", (("pool", "app"),))][1] >= 1
    assert m[("aghoo_db_command_seconds", (("pool", "app"),))][1] >= 1
    assert m[("aghoo_db_pool_size", (("pool", "app"),))][0] == composition.DB_POOL_MAX
    assert m[("aghoo_redis_pool_size", ())][0] == 64
    assert m[("aghoo_redis_pool_in_use", ())][0] >= 1  # the config watcher's subscription holds one
    assert m[("aghoo_db_pool_in_use", (("pool", "app"),))][0] >= 0
