"""T-IDEM-2 (spec-11 §8, IMPL-085) on the compose Postgres and moto S3: a 3 MiB body replays byte-identical from
`<tenant>/idempotency/<id>`; exactly 1 MiB stays inline; PutObject failing twice -> not_stored + metric; a GetObject
failure -> 500 and the record stays completed; a take-over removes the old object and the new owner's body
overwrites the key; another tenant never reaches the object."""

from __future__ import annotations

from typing import Any

import pytest
from redis.asyncio import Redis
from tests.fakes.config import FakeConfigRegistry
from tests.fakes.telemetry import FakeTelemetry
from tests.fixtures.db import new_tenant_id

from noema.core.clock import SystemClock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import UlidIdGen
from noema.core.principal import ALL_SCOPES, Principal
from noema.core.protocols.storage import New, Replay, StoredResponse
from noema.core.redis_keys import RedisKeys
from noema.db.engine import Engine
from noema.db.repos import IDEM_INLINE_MAX_BYTES, make_unit_of_work
from noema.db.uow import PgDatabase
from noema.ops.idempotency import BodyStore, PgIdempotencyStore

pytestmark = pytest.mark.integration
CLOCK = SystemClock()
ROUTE = "/v1/decisions"
BIG = bytes(range(256)) * (3 * 4096)  # 3 MiB, not compressible to a pattern by accident


class FlakyS3:
    """The real client, failing the next `fail_puts` PutObject and `fail_gets` GetObject calls."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.fail_puts = 0
        self.fail_gets = 0
        self.puts = 0

    async def put_object(self, **kw: Any) -> Any:
        self.puts += 1
        if self.fail_puts:
            self.fail_puts -= 1
            raise OSError("s3 unavailable")
        return await self._client.put_object(**kw)

    async def get_object(self, **kw: Any) -> Any:
        if self.fail_gets:
            self.fail_gets -= 1
            raise OSError("s3 unavailable")
        return await self._client.get_object(**kw)

    async def delete_object(self, **kw: Any) -> Any:
        return await self._client.delete_object(**kw)


def who(tenant: str, request_id: str) -> Principal:
    return Principal(tenant, "prj_a", "prn_1", "key_1", frozenset({"developer"}), ALL_SCOPES, request_id)


@pytest.fixture
async def setup(pg_app_engine: Engine, s3_bucket: tuple[Any, str], keys: RedisKeys, noema_services: Any) -> Any:
    client, bucket = s3_bucket
    flaky = FlakyS3(client)
    telemetry = FakeTelemetry()
    redis = Redis.from_url(noema_services.redis_url, decode_responses=False)
    store = PgIdempotencyStore(
        PgDatabase(pg_app_engine, make_unit_of_work), redis, keys, BodyStore(flaky, bucket=bucket, env="test"),  # type: ignore[arg-type]
        CLOCK, UlidIdGen(CLOCK), FakeConfigRegistry(), telemetry.instruments, pod_id="pod",
    )  # fmt: skip
    yield store, flaky, client, bucket, telemetry
    await redis.aclose()


async def objects(client: Any, bucket: str) -> dict[str, bytes]:
    listed = await client.list_objects_v2(Bucket=bucket)
    out = {}
    for item in listed.get("Contents", []):
        got = await client.get_object(Bucket=bucket, Key=item["Key"])
        async with got["Body"] as body:
            out[item["Key"]] = await body.read()
    return out


async def row(pg_admin: Any, tenant: str) -> Any:
    return await pg_admin.fetchrow(
        "SELECT id, state, response_body, response_body_key FROM noema.idempotency_record WHERE tenant_id = $1", tenant
    )


async def test_large_body_replays_byte_identical_from_s3(setup: Any, pg_admin: Any) -> None:
    store, _, client, bucket, _ = setup
    t = new_tenant_id()
    new = await store.begin(who(t, "r1"), "POST", ROUTE, "k", "fp")
    await store.complete(new.token, StoredResponse(200, (("content-type", "application/json"),), BIG, None))
    rec = await row(pg_admin, t)
    key = f"{t}/idempotency/{rec['id']}"
    assert rec["state"] == "completed" and rec["response_body"] is None and rec["response_body_key"] == key
    stored = await objects(client, bucket)
    assert list(stored) == [key] and stored[key] == BIG
    head = await client.head_object(Bucket=bucket, Key=key)
    assert head["ServerSideEncryption"] == "AES256"  # dev/test; SSE-KMS with the tenant key in staging/prod
    replay = await store.begin(who(t, "r2"), "POST", ROUTE, "k", "fp")
    assert isinstance(replay, Replay) and replay.response.body == BIG
    assert replay.response.headers == (("content-type", "application/json"),)


async def test_exactly_one_mib_stays_inline(setup: Any, pg_admin: Any) -> None:
    store, flaky, client, bucket, _ = setup
    t = new_tenant_id()
    body = b"y" * IDEM_INLINE_MAX_BYTES
    new = await store.begin(who(t, "r1"), "POST", ROUTE, "k", "fp")
    await store.complete(new.token, StoredResponse(200, (), body, None))
    rec = await row(pg_admin, t)
    assert rec["response_body"] == body and rec["response_body_key"] is None and flaky.puts == 0
    assert await objects(client, bucket) == {}


async def test_put_failing_twice_is_not_stored(setup: Any, pg_admin: Any) -> None:
    store, flaky, _, _, telemetry = setup
    t = new_tenant_id()
    flaky.fail_puts = 2
    new = await store.begin(who(t, "r1"), "POST", ROUTE, "k", "fp")
    await store.complete(new.token, StoredResponse(200, (), BIG, None))
    assert (await row(pg_admin, t))["state"] == "not_stored" and flaky.puts == 2
    assert telemetry.counter("aghoo_idempotency_store_errors_total") == 1.0
    assert isinstance(await store.begin(who(t, "r2"), "POST", ROUTE, "k", "fp"), New)  # the retry re-executes


async def test_one_put_retry_succeeds(setup: Any, pg_admin: Any) -> None:
    store, flaky, _, _, telemetry = setup
    t = new_tenant_id()
    flaky.fail_puts = 1
    new = await store.begin(who(t, "r1"), "POST", ROUTE, "k", "fp")
    await store.complete(new.token, StoredResponse(200, (), BIG, None))
    assert (await row(pg_admin, t))["state"] == "completed" and flaky.puts == 2
    assert telemetry.counter("aghoo_idempotency_store_errors_total") == 0.0


async def test_get_failure_is_500_and_the_record_stays_completed(setup: Any, pg_admin: Any) -> None:
    store, flaky, _, _, _ = setup
    t = new_tenant_id()
    new = await store.begin(who(t, "r1"), "POST", ROUTE, "k", "fp")
    await store.complete(new.token, StoredResponse(200, (), BIG, None))
    flaky.fail_gets = 1
    with pytest.raises(NoemaError) as err:
        await store.begin(who(t, "r2"), "POST", ROUTE, "k", "fp")
    assert err.value.code is ErrorCode.INTERNAL_ERROR and err.value.spec.retryable
    assert (await row(pg_admin, t))["state"] == "completed"
    replay = await store.begin(who(t, "r3"), "POST", ROUTE, "k", "fp")  # the client's retry replays
    assert isinstance(replay, Replay) and replay.response.body == BIG


async def test_take_over_removes_then_overwrites_the_object(setup: Any, pg_admin: Any) -> None:
    store, _, client, bucket, _ = setup
    t = new_tenant_id()
    first = await store.begin(who(t, "r1"), "POST", ROUTE, "k", "fp")
    await store.complete(first.token, StoredResponse(200, (), BIG, None))
    key = (await row(pg_admin, t))["response_body_key"]
    await pg_admin.execute(  # the record expired: it counts as absent
        "UPDATE noema.idempotency_record SET expires_at = now() - interval '1 second' WHERE tenant_id = $1", t
    )
    second = await store.begin(who(t, "r2"), "POST", ROUTE, "k", "other-request")
    assert isinstance(second, New) and second.token.record_id == first.token.record_id
    assert await objects(client, bucket) == {}  # the previous owner's object is gone
    newer = BIG[::-1]
    await store.complete(second.token, StoredResponse(201, (), newer, None))
    assert (await objects(client, bucket)) == {key: newer}
    replay = await store.begin(who(t, "r3"), "POST", ROUTE, "k", "other-request")
    assert isinstance(replay, Replay) and replay.response.body == newer and replay.response.status == 201


async def test_another_tenant_never_reaches_the_object(setup: Any, pg_admin: Any) -> None:
    store, _, client, bucket, _ = setup
    a, b = new_tenant_id(), new_tenant_id()
    first = await store.begin(who(a, "r1"), "POST", ROUTE, "k", "fp")
    await store.complete(first.token, StoredResponse(200, (), BIG, None))
    other = await store.begin(who(b, "r2"), "POST", ROUTE, "k", "fp")  # same project id, key and request
    assert isinstance(other, New) and other.token.record_id != first.token.record_id
    await store.complete(other.token, StoredResponse(200, (), b"tenant b", None))
    assert list(await objects(client, bucket)) == [f"{a}/idempotency/{first.token.record_id}"]
    replay = await store.begin(who(b, "r3"), "POST", ROUTE, "k", "fp")
    assert isinstance(replay, Replay) and replay.response.body == b"tenant b"
