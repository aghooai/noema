"""`StsBroker` on FakeStsClient (ADR-001 §1-§6): SEC-STS-01 (unit), -02, -03, -04, -05/-06/-07/-09 (unit),
-13, -16, invalidate and the account check. The moto variants are in tests/integration/identity."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import anyio
import pytest
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from
from tests.fakes.credentials import FakeAssumeRoleResult
from tests.unit.identity.sts_rig import (
    ACME,
    ACME_ACCOUNT,
    ACME_EXTERNAL_ID,
    ACME_REF,
    ACME_ROLE,
    BETA,
    OverrideTenants,
    StsRig,
    beta_tenant,
    sts_rig,
)

from noema.core.errors import TenantAccessMisconfigured
from noema.core.protocols.credentials import TenantCredentials, assert_creds_for
from noema.telemetry import redact


@asynccontextmanager
async def running(rig: StsRig) -> AsyncIterator[StsRig]:
    await rig.runtime.open()
    try:
        await rig.broker.start()
        yield rig
    finally:
        await rig.broker.aclose()
        await rig.runtime.close()


async def cause_of(rig: StsRig, tenant_id: str = ACME) -> str:
    with pytest.raises(TenantAccessMisconfigured) as err:
        await rig.broker.get(tenant_id)
    assert not err.value.spec.retryable and err.value.spec.status == 503
    return err.value.cause


async def test_happy_path() -> None:
    """SEC-STS-01 (unit): role ARN, exact ExternalId, 3600 s, the session-name pattern, the tenant's account."""
    rig = sts_rig()
    async with running(rig):
        creds = await rig.broker.get(ACME)
    (call,) = rig.sts.calls
    assert call == {
        "role_arn": ACME_ROLE,
        "external_id": ACME_EXTERNAL_ID,
        "session_name": "noema-test-pod-1",
        "duration_s": 3600,
    }
    assert re.fullmatch(r"[\w+=,.@-]{1,64}", str(call["session_name"]))
    assert (creds.tenant_id, creds.account_id, creds.region, creds.source) == (ACME, ACME_ACCOUNT, "us-east-1", "sts")
    assert creds.expires_at == rig.clock.now() + timedelta(seconds=3600)
    assert_creds_for(creds, ACME, ACME_ACCOUNT)
    (span,) = [s for s in rig.telemetry.spans if s.name == "sts.assume_role"]
    assert span.attributes["aghoo.tenant"] == ACME and span.error is None


async def test_single_flight_cold_start() -> None:
    """SEC-STS-02: 1,000 concurrent cold gets -> exactly one AssumeRole."""
    rig = sts_rig()
    rig.sts.delay_s = 0.5
    results = []
    async with running(rig), rig.clock.autojump():

        async def one() -> None:
            results.append(await rig.broker.get(ACME))

        async with anyio.create_task_group() as tg:
            for _ in range(1000):
                tg.start_soon(one)
    assert len(rig.sts.calls) == 1 and len(results) == 1000
    assert len({c.aws.access_key_id for c in results}) == 1


async def test_soft_window_refreshes_in_the_background() -> None:
    """SEC-STS-03: at exp - 9 min the cached creds come back at once; exactly one background refresh."""
    rig = sts_rig()
    async with running(rig):
        first = await rig.broker.get(ACME)
        await rig.clock.advance(3600 - 9 * 60)
        rig.sts.delay_s = 1.0
        for _ in range(20):
            assert (await rig.broker.get(ACME)).aws.access_key_id == first.aws.access_key_id
        await rig.clock.advance(1.0)
        await rig.clock.settle()
        fresh = await rig.broker.get(ACME)
    assert len(rig.sts.calls) == 2
    assert fresh.aws.access_key_id != first.aws.access_key_id
    assert fresh.expires_at is not None and first.expires_at is not None and fresh.expires_at > first.expires_at


async def test_hard_window_blocks_on_one_refresh() -> None:
    """SEC-STS-04: at exp - 4 min callers wait; one refresh; new creds."""
    rig = sts_rig()
    async with running(rig):
        first = await rig.broker.get(ACME)
        await rig.clock.advance(3600 - 4 * 60)
        rig.sts.delay_s = 0.2
        got: list[TenantCredentials] = []
        async with rig.clock.autojump(), anyio.create_task_group() as tg:
            for _ in range(50):
                tg.start_soon(_append, rig, got)
    assert len(rig.sts.calls) == 2
    assert {c.aws.access_key_id for c in got} != {first.aws.access_key_id} and len(got) == 50
    assert len({c.aws.access_key_id for c in got}) == 1


async def _append(rig: StsRig, out: list[TenantCredentials]) -> None:
    out.append(await rig.broker.get(ACME))


async def test_wrong_external_id_is_denied_and_negative_cached() -> None:
    """SEC-STS-05 (unit): the trust table denies a wrong random part -> access_denied; 1 STS call for 50 requests
    within 10 s; the next call after the negative TTL tries STS again."""
    rig = sts_rig()
    rig.secrets.put(ACME_REF, "aghoo-t_acme-" + "Z" * 32)
    async with running(rig):
        for _ in range(50):
            assert await cause_of(rig) == "access_denied"
            await rig.clock.advance(0.1)
        assert len(rig.sts.calls) == 1
        await rig.clock.advance(5.1)
        assert await cause_of(rig) == "access_denied"
        assert len(rig.sts.calls) == 2
    assert rig.telemetry.counter("aghoo_tenant_access_failures_total", tenant=ACME, cause="access_denied") == 2
    assert rig.telemetry.counter("aghoo_security_violations_total", kind="tenant_access_misconfigured") == 2
    spans = [s for s in rig.telemetry.spans if s.name == "sts.assume_role"]
    assert [s.error for s in spans] == ["tenant_access_misconfigured"] * 2


async def test_external_id_of_another_tenant() -> None:
    """SEC-STS-06 (unit): t_beta's ref points at t_acme's secret -> external_id_invalid; 0 STS calls."""
    rig = sts_rig(patch={"tenants": {BETA: beta_tenant(external_id_ref=ACME_REF)}})
    async with running(rig):
        assert await cause_of(rig, BETA) == "external_id_invalid"
    assert rig.sts.calls == []


@pytest.mark.parametrize(
    "role_arn",
    ["arn:aws:iam::111122223333:role/Admin", "arn:aws:iam::999999999999:role/AghooBedrockInvoke", "not-an-arn"],
)
async def test_non_allowlisted_role(role_arn: str) -> None:
    """SEC-STS-07 (unit): role Admin in the right account, or AghooBedrockInvoke in another account (a config the
    checker already rejects) -> role_not_allowlisted; 0 STS calls."""
    inner = FakeConfigRegistry(snapshot_from(patched_example({})))
    rig = sts_rig(config=OverrideTenants(inner, {ACME: {"invoke_role_arn": role_arn}}))
    async with running(rig):
        assert await cause_of(rig) == "role_not_allowlisted"
    assert rig.sts.calls == []


async def test_secrets_down_and_unknown_tenant() -> None:
    """SEC-STS-09 (unit): Secrets Manager unavailable -> external_id_unavailable, no fallback; an unknown tenant
    fails closed without STS."""
    rig = sts_rig()
    rig.secrets.fail(ACME_REF)
    async with running(rig):
        assert await cause_of(rig) == "external_id_unavailable"
        assert await cause_of(rig, "t_unknown") == "role_not_allowlisted"
    assert rig.sts.calls == []


async def test_assumed_role_in_another_account() -> None:
    """An AssumedRoleUser.Arn of another account than the tenant's -> account_mismatch (security event)."""
    rig = sts_rig()
    honest = rig.sts.assume_role

    async def lying(*, role_arn: str, external_id: str, session_name: str, duration_s: int) -> FakeAssumeRoleResult:
        result = await honest(
            role_arn=role_arn, external_id=external_id, session_name=session_name, duration_s=duration_s
        )
        return FakeAssumeRoleResult(result.creds, result.assumed_role_arn.replace(ACME_ACCOUNT, "999999999999"))

    rig.sts.assume_role = lying  # type: ignore[method-assign]
    async with running(rig):
        assert await cause_of(rig) == "account_mismatch"
    assert rig.telemetry.counter("aghoo_security_violations_total", kind="account_mismatch") == 1
    assert rig.telemetry.counter("aghoo_tenant_access_failures_total", cause="account_mismatch") == 1


async def test_bearer_short_term_and_sigv4() -> None:
    """SEC-STS-13: the short-term token expires no later than the creds, never appears in telemetry and is
    re-minted after the creds refresh; sigv4 carries no token."""
    rig = sts_rig()
    async with running(rig):
        creds = await rig.broker.get(ACME)
        assert creds.mantle.mode == "bearer" and creds.mantle.bearer_token is not None
        token = creds.mantle.bearer_token.get_secret_value()
        assert token.startswith("bedrock-api-key-")
        assert creds.mantle.expires_at is not None and creds.expires_at is not None
        assert creds.mantle.expires_at <= creds.expires_at
        assert token not in redact(f"Authorization: Bearer {token} {token}")
        assert all(token not in repr(s.attributes) for s in rig.telemetry.spans)
        await rig.clock.advance(3600 - 4 * 60)
        fresh = await rig.broker.get(ACME)
        assert fresh.mantle.bearer_token is not None and fresh.mantle.bearer_token.get_secret_value() != token
    sig = sts_rig(mantle="sigv4")
    async with running(sig):
        creds = await sig.broker.get(ACME)
    assert creds.mantle.mode == "sigv4" and creds.mantle.bearer_token is None


async def test_sts_down_in_the_soft_window() -> None:
    """SEC-STS-16: STS throttles during the soft window; 200 calls over 30 s are served from the cache with at
    most 4 AssumeRole calls (1 per 10 s); then at exp - 4 min callers get sts_unavailable (negative-cached)."""
    rig = sts_rig()
    async with running(rig):
        first = await rig.broker.get(ACME)
        await rig.clock.advance(3600 - 9 * 60)
        rig.sts.fail_next("Throttling", times=1000)
        for _ in range(200):
            assert (await rig.broker.get(ACME)).aws.access_key_id == first.aws.access_key_id
            await rig.clock.advance(0.15)
            await rig.clock.settle()
        assert 1 + 1 <= len(rig.sts.calls) <= 1 + 4
        await rig.clock.advance(5 * 60 - 30 + 0.1)  # exp - 4 min
        calls = len(rig.sts.calls)
        for _ in range(20):
            assert await cause_of(rig) == "sts_unavailable"
        assert len(rig.sts.calls) == calls + 1


async def test_invalidate_only_evicts_old_sessions() -> None:
    rig = sts_rig()
    async with running(rig):
        await rig.broker.get(ACME)
        await rig.clock.advance(59.0)
        rig.broker.invalidate(ACME)
        await rig.broker.get(ACME)
        assert len(rig.sts.calls) == 1
        await rig.clock.advance(1.0)
        rig.broker.invalidate(ACME)
        await rig.broker.get(ACME)
        assert len(rig.sts.calls) == 2
        rig.broker.invalidate("t_unknown")  # no-op


async def test_sts_timeout_is_unavailable() -> None:
    rig = sts_rig()
    rig.sts.delay_s = 10.0
    async with running(rig), rig.clock.autojump():
        assert await cause_of(rig) == "sts_unavailable"


async def test_a_config_change_of_account_role_or_region_is_a_miss() -> None:
    """A cached session from before an account move (or a home_region change) is never handed out: the next get
    assumes the new role; an unchanged tenant keeps its session (no extra AssumeRole)."""
    body = patched_example({})
    registry = FakeConfigRegistry(snapshot_from(body))
    rig = sts_rig(config=registry)
    new_account = "444455556666"
    new_role = f"arn:aws:iam::{new_account}:role/AghooBedrockInvoke"
    rig.sts.allow(new_role, ACME_EXTERNAL_ID)
    async with running(rig):
        first = await rig.broker.get(ACME)
        assert first.account_id == ACME_ACCOUNT and rig.broker.assume_role_calls == 1
        version = body["config_version"]
        unrelated = patched_example({"config_version": version + 1})
        assert registry.activate(snapshot_from(unrelated))
        assert (await rig.broker.get(ACME)) is first and rig.broker.assume_role_calls == 1
        moved_body = {"aws_account_id": new_account, "invoke_role_arn": new_role}
        moved = patched_example({"config_version": version + 2, "tenants": {ACME: moved_body}})
        assert registry.activate(snapshot_from(moved))
        await rig.clock.advance(30 * 60)
        again = await rig.broker.get(ACME)
        assert again.account_id == new_account and rig.broker.assume_role_calls == 2
        assert_creds_for(again, ACME, registry.current().tenant(ACME).aws_account_id)  # no account_mismatch
        regional = {**moved_body, "home_region": "us-west-2"}
        assert registry.activate(
            snapshot_from(patched_example({"config_version": version + 3, "tenants": {ACME: regional}}))
        )
        relocated = await rig.broker.get(ACME)
        assert relocated.region == "us-west-2" and rig.broker.assume_role_calls == 3
    assert rig.telemetry.counter("aghoo_security_violations_total") == 0
