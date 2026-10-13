"""`DbCalibratorRegistry` on Postgres with two pods (IMPL-072, ADR-003 §2-§5): a tenant artifact fitted by labeling
(noema_app, RLS lifecycle guard), activated by two operators through the CLI functions, reaches both pods' snapshots
on refresh and through the Redis `calibrators_changed` channel; the drift downgrade (noema_app grant of
admin.set_calibrator_status) removes it from both and bumps the epoch."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import anyio
import asyncpg
import pytest
from redis.asyncio import Redis
from tests.fixtures.db import PgTestDatabase, seed_tenant
from tests.integration.db._admin import Acting, operator

from noema.core.clock import SystemClock
from noema.core.protocols.labeling import CalibratorBindingKey
from noema.core.redis_keys import RedisKeys
from noema.db.engine import Engine, PoolSettings
from noema.db.repos import make_unit_of_work
from noema.db.uow import PgDatabase
from noema.labeling.artifact import artifact_row
from noema.labeling.deps import DRIFT_ACTOR
from noema.labeling.fitting import FittedParams
from noema.labeling.registry import DbCalibratorRegistry

pytestmark = pytest.mark.integration
type Conn = asyncpg.Connection[asyncpg.Record]


@pytest.fixture
async def pods(
    pg_database: PgTestDatabase,
) -> AsyncIterator[tuple[PgDatabase[Any], DbCalibratorRegistry, DbCalibratorRegistry]]:
    engines = [await Engine.create(PoolSettings(dsn=pg_database.app_dsn, max_size=2)) for _ in range(2)]
    try:
        dbs = [PgDatabase(e, make_unit_of_work) for e in engines]
        yield dbs[0], DbCalibratorRegistry(dbs[0]), DbCalibratorRegistry(dbs[1])
    finally:
        for e in engines:
            await e.close()


def key(signature: str) -> CalibratorBindingKey:
    return CalibratorBindingKey("router-default", "1", signature, ("m1", "m2", "m3"), "sha256:" + "6" * 64)


async def fitted(db: PgDatabase[Any], tenant: str, signature: str) -> str:
    cal_id = f"cal_{uuid.uuid4().hex[:20]}"
    params = FittedParams("temperature", {"m1": 1.4, "m2": 0.9, "m3": 1.1}, {"temperature": 1.2})
    row = artifact_row(
        calibrator_id=cal_id,
        key=key(signature),
        question_name="intent",
        params=params,
        holdout={"n": 400},
        acceptance={"passed": True, "failed_rules": []},
        reliability={"bins": [], "ci": {}},
        fitted_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    async with db.tenant_uow(tenant) as uow:
        await uow.calibrators.insert({k: v for k, v in row.items() if k != "scope"})
        await uow.calibrators.update(cal_id, status="accepted", accepted_at=datetime(2026, 10, 1, tzinfo=UTC))
    return cal_id


async def test_two_pods_see_activation_and_drift_downgrade(
    pods: tuple[PgDatabase[Any], DbCalibratorRegistry, DbCalibratorRegistry], pg_admin: Conn, pg_cli: Conn
) -> None:
    db, pod_a, pod_b = pods
    tenant = (await seed_tenant(pg_admin)).tenant_id
    signature = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex
    cal_id = await fitted(db, tenant, signature)
    await pod_a.refresh()
    await pod_b.refresh()
    assert pod_a.lookup(tenant, key(signature)) is None and pod_a.epoch(tenant).endswith(".0")
    calls = Acting(pg_cli)  # proves each operator's key (0003, Wave E)
    first, second = await operator(pg_admin), await operator(pg_admin)
    await calls.calibrator_approve(first, cal_id)
    await calls.calibrator_activate(second, cal_id)
    for pod in (pod_a, pod_b):
        await pod.refresh()
        found = pod.lookup(tenant, key(signature))
        assert found is not None and found.id == cal_id and found.member_T == {"m1": 1.4, "m2": 0.9, "m3": 1.1}
        assert pod.epoch(tenant).endswith(".1")
    async with db.system_uow() as system:
        await system.set_calibrator_status(DRIFT_ACTOR, cal_id, "downgraded", reason="drift_ece")
    for pod in (pod_a, pod_b):
        await pod.refresh()
        assert pod.lookup(tenant, key(signature)) is None and pod.epoch(tenant).endswith(".2")
    status = await pg_admin.fetchval("SELECT status FROM noema.calibrator WHERE id = $1", cal_id)
    assert status == "downgraded"


class Signalled(DbCalibratorRegistry):
    """Signals every completed refresh (a test hook: no sleeps)."""

    def __init__(self, db: PgDatabase[Any]) -> None:
        super().__init__(db)
        self.done = anyio.Event()

    async def refresh(self) -> None:
        await super().refresh()
        self.done.set()


async def test_calibrators_changed_messages_trigger_a_refresh(
    pods: tuple[PgDatabase[Any], DbCalibratorRegistry, DbCalibratorRegistry], noema_services: Any, noema_run_id: str
) -> None:
    db = pods[0]
    registry = Signalled(db)
    keys = RedisKeys(f"ns:{noema_run_id}:")
    redis = Redis.from_url(noema_services.redis_url)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: registry.listen(redis, keys.calibrators_changed(), clock=SystemClock()))
            with anyio.fail_after(5):
                await registry.done.wait()  # the refresh after subscribing
                before = registry.refreshes
                registry.done = anyio.Event()
                while not registry.done.is_set():
                    await redis.publish(keys.calibrators_changed(), b'{"scope_key": "platform", "n": 1}')
                    with anyio.move_on_after(0.2):
                        await registry.done.wait()
            assert registry.refreshes > before
            tg.cancel_scope.cancel()
    finally:
        await redis.aclose()
