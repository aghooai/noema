"""ops-ledger integration fixtures: the production `Database` over the session clone (noema_app, RLS), a Valkey
client with a per-test key namespace (cleaned up), unreachable twins of both (Postgres on port 1, Redis on port 1)
and switches between them, a fresh tenant, moto S3, and a test-owned toxiproxy proxy in front of Valkey that can
be blackholed or slowed (skipped when the host toxiproxy is not running: `make up`)."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from aiobotocore.session import get_session
from redis.asyncio import Redis
from tests.contract_internal.registry import Harness
from tests.fixtures.db import SeededTenant, seed_tenant

from noema.core.principal import Principal
from noema.core.redis_keys import RedisKeys
from noema.core.secret import SecretStr
from noema.db.engine import Engine, PoolSettings
from noema.db.repos import PgUnitOfWork, make_unit_of_work
from noema.db.system import PgSystemCalls
from noema.db.uow import PgDatabase

from .ledger_harness import CountingDatabase, LedgerClock, emptied_database

UNREACHABLE_PG = SecretStr("postgresql://noema_app:unused@127.0.0.1:1/noema_down")


class SwitchDb:
    """A `Database` that is the real one, or (after `up = False`) one whose Postgres is unreachable."""

    def __init__(self, real: PgDatabase[PgUnitOfWork], dead: PgDatabase[PgUnitOfWork]) -> None:
        self.real, self.dead = real, dead
        self.up = True

    @property
    def _db(self) -> PgDatabase[PgUnitOfWork]:
        return self.real if self.up else self.dead

    def uow(self, principal: Principal) -> AbstractAsyncContextManager[PgUnitOfWork]:
        return self._db.uow(principal)

    def tenant_uow(self, tenant_id: str, *, project_id: str | None = None) -> AbstractAsyncContextManager[PgUnitOfWork]:
        return self._db.tenant_uow(tenant_id, project_id=project_id)

    def system_uow(self) -> AbstractAsyncContextManager[PgSystemCalls]:
        return self._db.system_uow()


class SwitchRedis:
    """Delegates to the real client, or (after `up = False`) to one pointed at a closed port."""

    def __init__(self, real: Redis, dead: Redis) -> None:
        self.real, self.dead = real, dead
        self.up = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real if self.up else self.dead, name)


@pytest.fixture
async def ledger_db(pg_app_engine: Engine) -> AsyncIterator[SwitchDb]:
    dead = await Engine.create(PoolSettings(dsn=UNREACHABLE_PG, max_size=1, acquire_timeout_s=0.5))
    try:
        yield SwitchDb(PgDatabase(pg_app_engine, make_unit_of_work), PgDatabase(dead, make_unit_of_work))
    finally:
        await dead.close()


@pytest.fixture
def keys(noema_run_id: str) -> RedisKeys:
    return RedisKeys(f"ns:{noema_run_id}:{uuid.uuid4().hex[:8]}:")


@pytest.fixture
async def valkey(noema_services: Any, keys: RedisKeys) -> AsyncIterator[SwitchRedis]:
    real = Redis.from_url(noema_services.redis_url, decode_responses=False)
    dead = Redis.from_url("redis://127.0.0.1:1/0", decode_responses=False, socket_connect_timeout=0.2)
    try:
        yield SwitchRedis(real, dead)
    finally:
        async for key in real.scan_iter(match=f"{keys.ns}*"):
            await real.unlink(key)
        await real.aclose()
        await dead.aclose()


@pytest.fixture
async def tenant(pg_admin: Any) -> SeededTenant:
    """A fresh tenant with projects prj_a and prj_b and one developer principal each."""
    return await seed_tenant(pg_admin, projects=("prj_a", "prj_b"))


@pytest.fixture
async def emptied(
    request: pytest.FixtureRequest, noema_run_id: str
) -> AsyncIterator[tuple[Any, CountingDatabase, LedgerClock]]:
    """(admin connection, counting Database, aging LedgerClock) on the emptied dedicated database: for tests that
    age the database clock or need fixed platform rows."""
    async with emptied_database(Harness(request, noema_run_id)) as triple:
        yield triple


@pytest.fixture
async def s3_bucket(noema_services: Any, noema_run_id: str) -> AsyncIterator[tuple[Any, str]]:
    """(aiobotocore S3 client on the compose moto, a fresh bucket), emptied and removed at exit."""
    bucket = "noema-t-" + re.sub(r"[^a-z0-9-]", "-", noema_run_id.lower())[:36] + "-" + uuid.uuid4().hex[:6]
    session = get_session()
    async with session.create_client(
        "s3",
        endpoint_url=noema_services.moto_url,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    ) as client:
        await client.create_bucket(Bucket=bucket)
        try:
            yield client, bucket
        finally:
            listed = await client.list_objects_v2(Bucket=bucket)
            for item in listed.get("Contents", []):
                await client.delete_object(Bucket=bucket, Key=item["Key"])
            await client.delete_bucket(Bucket=bucket)


@dataclass
class ValkeyProxy:
    """A toxiproxy proxy owned by one test (deleted at exit). `blackhole()`: replies are lost and nothing is
    refused (the timeout toxic with timeout 0); `latency(ms)`: every reply is delayed; `heal()` removes both."""

    api: httpx.Client
    name: str
    url: str

    def _toxic(self, name: str, kind: str, **attributes: int) -> None:
        body = {"name": name, "type": kind, "stream": "downstream", "attributes": attributes}
        self.api.post(f"/proxies/{self.name}/toxics", json=body).raise_for_status()

    def blackhole(self) -> None:
        self._toxic("blackhole", "timeout", timeout=0)

    def latency(self, ms: int) -> None:
        self._toxic("latency", "latency", latency=ms)

    def heal(self) -> None:
        for toxic in self.api.get(f"/proxies/{self.name}/toxics").raise_for_status().json():
            self.api.delete(f"/proxies/{self.name}/toxics/{toxic['name']}").raise_for_status()


@pytest.fixture
def valkey_proxy(noema_services: Any, noema_run_id: str) -> Iterator[ValkeyProxy]:
    api = httpx.Client(base_url=f"http://127.0.0.1:{os.environ.get('NOEMA_TOXIPROXY_PORT', '28474')}", timeout=5.0)
    try:
        api.get("/version").raise_for_status()
    except httpx.HTTPError:
        api.close()
        pytest.skip("the host toxiproxy is not running: make up")
    upstream = urlsplit(noema_services.redis_url)
    name = f"{noema_run_id}-ledger-{uuid.uuid4().hex[:8]}"
    body = {"name": name, "listen": "127.0.0.1:0", "upstream": f"{upstream.hostname}:{upstream.port}", "enabled": True}
    api.post("/proxies", json=body).raise_for_status()
    try:
        port = int(api.get(f"/proxies/{name}").raise_for_status().json()["listen"].rsplit(":", 1)[1])
        yield ValkeyProxy(api, name, f"redis://127.0.0.1:{port}/0")
    finally:
        api.delete(f"/proxies/{name}")
        api.close()
