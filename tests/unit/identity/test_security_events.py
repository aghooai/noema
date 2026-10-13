"""spec-09 §1 security events (log, counter, `audit_event` outbox row), the `keys_revoked` listener reconnecting
after a Redis error, and the AmbientBroker mismatch event."""

from __future__ import annotations

import logging
from typing import Any

import anyio
import orjson
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from tests.fakes.clock import FakeClock, FakeIdGen
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from
from tests.fakes.outbox import FakeOutbox
from tests.fakes.telemetry import FakeTelemetry
from tests.unit.identity.helpers import KEY_ID, TENANT, authn_rig, bearer, seed_key

from noema.core.errors import TenantAccessMisconfigured
from noema.core.redis_keys import RedisKeys
from noema.core.secret import SecretStr
from noema.identity.ambient import AmbientBroker
from noema.identity.aws import StaticCredentials
from noema.identity.revocation import DEAD_S, RECONNECT_S, run_revocation_listener
from noema.identity.security import ACTOR, PLATFORM_TENANT, SecurityEvents


def events() -> tuple[SecurityEvents, FakeTelemetry, FakeOutbox, FakeClock]:
    clock = FakeClock()
    telemetry, outbox = FakeTelemetry(strict=True), FakeOutbox()
    return (
        SecurityEvents(telemetry=telemetry, outbox=outbox, ids=FakeIdGen(clock), clock=clock),
        telemetry,
        outbox,
        clock,
    )


def test_an_event_is_logged_counted_and_audited(caplog: pytest.LogCaptureFixture) -> None:
    security, telemetry, outbox, clock = events()
    with caplog.at_level(logging.WARNING, logger="noema.identity.security"):
        security.emit("account_mismatch", tenant_id="t_a", request_id="req_1", details={"source": "sts"})
        security.emit("pubsub_tenant_mismatch", tenant_id=None, job_id="job_1")
    assert telemetry.counter("aghoo_security_violations_total", kind="account_mismatch") == 1
    assert telemetry.counter("aghoo_security_violations_total", kind="pubsub_tenant_mismatch") == 1
    first, second = outbox.of_kind("audit_event")
    assert first.tenant_id == "t_a" and first.id == first.payload["id"] and str(first.id).startswith("aud_")
    assert first.payload == {
        "tenant_id": "t_a",
        "id": first.id,
        "actor_type": "system",
        "actor_id": ACTOR,
        "action": "security.account_mismatch",
        "resource_kind": "security",
        "resource_id": None,
        "details": {"source": "sts"},
        "request_id": "req_1",
        "at": clock.now().isoformat().replace("+00:00", "Z"),
    }
    assert second.tenant_id == PLATFORM_TENANT and second.payload["details"] == {}
    assert second.payload["action"] == "security.pubsub_tenant_mismatch" and second.payload["request_id"] is None
    records = [r for r in caplog.records if r.name == "noema.identity.security"]
    assert [r.getMessage() for r in records] == [
        "security name=account_mismatch tenant_id=t_a request_id=req_1 kind=account_mismatch",
        f"security name=pubsub_tenant_mismatch tenant_id={PLATFORM_TENANT} job_id=job_1 kind=pubsub_tenant_mismatch",
    ]


def test_without_an_outbox_nothing_is_audited() -> None:
    telemetry = FakeTelemetry(strict=True)
    SecurityEvents(telemetry=telemetry).emit("tenant_access_misconfigured", tenant_id="t_a", details={"cause": "x"})
    assert telemetry.counter("aghoo_security_violations_total", kind="tenant_access_misconfigured") == 1


class FlakyPubSub:
    def __init__(self, owner: FlakyRedis, fail: str | None) -> None:
        self.owner = owner
        self.fail = fail
        self.closed = False
        self.pings = 0

    async def subscribe(self, channel: str) -> None:
        self.owner.channels.append(channel)
        if self.fail == "subscribe":
            raise RedisConnectionError("Connection refused")

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool,
        timeout: float,  # noqa: ASYNC109 - the signature of redis.asyncio PubSub.get_message
    ) -> dict[str, Any] | None:
        if self.fail == "read":
            raise OSError("connection reset")
        if self.owner.queue:
            return self.owner.queue.pop(0)
        await self.owner.clock.sleep(timeout)
        return None

    async def ping(self) -> None:
        self.pings += 1  # a silent connection: the PONG never comes

    async def aclose(self) -> None:
        self.closed = True


class FlakyRedis:
    """`pubsub()` connections whose first ones fail (at subscribe, then while reading); later ones deliver."""

    def __init__(self, clock: FakeClock, failures: tuple[str, ...]) -> None:
        self.clock = clock
        self.failures = failures
        self.pubsubs: list[FlakyPubSub] = []
        self.channels: list[str] = []
        self.queue: list[dict[str, Any]] = []

    def pubsub(self) -> FlakyPubSub:
        n = len(self.pubsubs)
        pubsub = FlakyPubSub(self, self.failures[n] if n < len(self.failures) else None)
        self.pubsubs.append(pubsub)
        return pubsub


async def test_the_listener_reconnects_after_redis_errors_and_then_evicts() -> None:
    rig = authn_rig()
    token = seed_key(rig.db)
    await rig.authn.authenticate(bearer(token), "198.51.100.1")
    assert rig.authn.cache.peek(KEY_ID) is not None
    redis = FlakyRedis(rig.clock, ("subscribe", "read"))
    redis.queue.append({"type": "message", "data": orjson.dumps({"tenant_id": TENANT, "key_id": KEY_ID})})
    keys = RedisKeys("ns:test:")
    security = SecurityEvents(telemetry=FakeTelemetry(strict=True))
    async with anyio.create_task_group() as tg:
        tg.start_soon(run_revocation_listener, redis, keys, rig.authn, security, rig.clock)  # type: ignore[arg-type]
        await rig.clock.settle()
        assert len(redis.pubsubs) == 1 and redis.pubsubs[0].closed  # refused, closed, waiting to reconnect
        assert rig.authn.cache.peek(KEY_ID) is not None
        await rig.clock.advance(RECONNECT_S)
        assert len(redis.pubsubs) == 2 and redis.pubsubs[1].closed  # failed while reading
        await rig.clock.advance(RECONNECT_S)
        assert len(redis.pubsubs) == 3 and not redis.pubsubs[2].closed
        assert rig.authn.cache.peek(KEY_ID) is None  # the message after the reconnect evicted the key
        assert redis.channels == [keys.keys_revoked()] * 3
        tg.cancel_scope.cancel()
    assert redis.pubsubs[2].closed  # closed on cancellation too


async def test_ambient_mismatch_is_a_counted_audited_security_event() -> None:
    security, telemetry, outbox, clock = events()
    registry = FakeConfigRegistry(snapshot_from(patched_example({})))
    broker = AmbientBroker(
        config=registry,
        clock=clock,
        env="test",
        region="us-east-1",
        credentials=StaticCredentials("AKIATESTTESTTESTTEST", SecretStr("secret")),
        environ={},
        security=security,
    )
    broker._account = "999999999999"  # the caller account GetCallerIdentity reported (start() talks to STS)
    tenant = next(iter(registry.current().config.tenants))  # type: ignore[attr-defined]
    with pytest.raises(TenantAccessMisconfigured) as err:
        await broker.get(tenant)
    assert err.value.cause == "ambient_account_mismatch"
    assert telemetry.counter("aghoo_security_violations_total", kind="ambient_account_mismatch") == 1
    (audit,) = outbox.of_kind("audit_event")
    assert audit.tenant_id == tenant and audit.payload["action"] == "security.ambient_account_mismatch"


async def test_a_connection_that_never_answers_is_resubscribed() -> None:
    """Wave E edge fix 22: no message and no PONG for `DEAD_S` (a dead node or partition without FIN/RST): the
    listener closes the connection and subscribes again."""
    rig = authn_rig()
    redis = FlakyRedis(rig.clock, ())
    security = SecurityEvents(telemetry=FakeTelemetry(strict=True))
    async with anyio.create_task_group() as tg:
        tg.start_soon(run_revocation_listener, redis, RedisKeys("ns:test:"), rig.authn, security, rig.clock)  # type: ignore[arg-type]
        await rig.clock.advance(DEAD_S - 1.0)
        assert len(redis.pubsubs) == 1 and redis.pubsubs[0].pings >= 1  # PINGed while quiet, still waiting
        await rig.clock.advance(1.0 + RECONNECT_S)
        assert len(redis.pubsubs) == 2 and redis.pubsubs[0].closed and not redis.pubsubs[1].closed
        tg.cancel_scope.cancel()
