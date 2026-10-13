"""SEC-STS-17: aiobotocore refreshable credentials backed by the broker, with the broker's refresh windows
(advisory 600 s, mandatory 300 s), on a fake clock stepping from exp - 16 min to exp - 4 min."""

from __future__ import annotations

from datetime import datetime, timedelta

from botocore.exceptions import ClientError
from tests.fakes.credentials import FakeAssumeRoleResult, FakeStsClient
from tests.unit.identity.sts_rig import ACME, ACME_ACCOUNT, StsRig, sts_rig

from noema.core.errors import TenantAccessMisconfigured
from noema.identity.sts import StsBroker
from noema.identity.sts_aws import broker_credentials


class FlakySts:
    """Wraps the fake STS: records call times and throttles while `failing`."""

    def __init__(self, inner: FakeStsClient, rig: StsRig) -> None:
        self.inner = inner
        self.rig = rig
        self.failing = False
        self.calls: list[datetime] = []

    async def assume_role(
        self, *, role_arn: str, external_id: str, session_name: str, duration_s: int
    ) -> FakeAssumeRoleResult:
        self.calls.append(self.rig.clock.now())
        if self.failing:
            raise ClientError({"Error": {"Code": "Throttling", "Message": "slow down"}}, "AssumeRole")
        return await self.inner.assume_role(
            role_arn=role_arn, external_id=external_id, session_name=session_name, duration_s=duration_s
        )


def flaky_broker(rig: StsRig) -> tuple[StsBroker, FlakySts]:
    sts = FlakySts(rig.sts, rig)
    broker = StsBroker(
        config=rig.config,
        secrets=rig.secrets,
        sts=sts,
        clock=rig.clock,
        runtime=rig.runtime,
        deployment="t",
        pod_id="p",
    )
    return broker, sts


async def step_until(rig: StsRig, creds: object, until: datetime, keys: list[str]) -> None:
    get = getattr(creds, "get_frozen_credentials")  # noqa: B009 - aiobotocore's async accessor
    while rig.clock.now() < until:
        await rig.clock.advance(30.0)
        frozen = await get()
        keys.append(frozen.access_key)
        await rig.clock.settle()


async def test_healthy_sts_refreshes_in_the_background_only() -> None:
    rig = sts_rig()
    broker, sts = flaky_broker(rig)
    await rig.runtime.open()
    try:
        creds = broker_credentials(broker, ACME, ACME_ACCOUNT, time_fetcher=rig.clock.now)
        first = (await creds.get_frozen_credentials()).access_key
        exp = rig.clock.now() + timedelta(hours=1)
        await rig.clock.advance((exp - timedelta(minutes=16) - rig.clock.now()).total_seconds())
        keys: list[str] = []
        await step_until(rig, creds, exp - timedelta(minutes=4), keys)
    finally:
        await rig.runtime.close()
    assert len(sts.calls) == 2  # the cold fetch + one background refresh in the soft window
    assert exp - timedelta(minutes=10) <= sts.calls[1] < exp - timedelta(minutes=5)
    assert keys[0] == first and keys[-1] != first


async def test_failed_soft_refresh_leads_to_one_blocking_refresh_at_exp_minus_5() -> None:
    rig = sts_rig()
    broker, sts = flaky_broker(rig)
    await rig.runtime.open()
    try:
        creds = broker_credentials(broker, ACME, ACME_ACCOUNT, time_fetcher=rig.clock.now)
        first = (await creds.get_frozen_credentials()).access_key
        exp = rig.clock.now() + timedelta(hours=1)
        await rig.clock.advance((exp - timedelta(minutes=16) - rig.clock.now()).total_seconds())
        sts.failing = True
        keys: list[str] = []
        await step_until(rig, creds, exp - timedelta(minutes=5, seconds=30), keys)
        assert set(keys) == {first}  # botocore kept the cached creds through every failed soft refresh
        soft_attempts = len(sts.calls) - 1
        assert soft_attempts >= 1
        sts.failing = False
        await step_until(rig, creds, exp - timedelta(minutes=4), keys)
    finally:
        await rig.runtime.close()
    blocking = sts.calls[1 + soft_attempts :]
    assert blocking == [exp - timedelta(minutes=5)]  # exactly one refresh, at the hard window
    assert keys[-1] != first


async def test_refresh_asserts_the_tenant_account() -> None:
    rig = sts_rig()
    await rig.runtime.open()
    try:
        creds = broker_credentials(rig.broker, ACME, "999999999999", time_fetcher=rig.clock.now)
        try:
            await creds.get_frozen_credentials()
        except TenantAccessMisconfigured as err:
            assert err.cause == "account_mismatch"
        else:
            raise AssertionError("account_mismatch expected")
    finally:
        await rig.runtime.close()
