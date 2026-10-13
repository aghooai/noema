"""CredentialBroker conformance (spec-02 §3, ADR-001 §4-§5): tenant-bound credentials, caching, invalidate,
fail-closed `TenantAccessMisconfigured`, and `assert_creds_for`."""

from __future__ import annotations

from datetime import timedelta

import anyio
import pytest

from noema.core.errors import ErrorCode, TenantAccessMisconfigured
from noema.core.protocols.credentials import assert_creds_for
from noema.core.secret import SecretStr
from noema.telemetry import redact

from .kits import BrokerKit
from .registry import kit_fixture

kit = kit_fixture("credential_broker")


async def test_credentials_belong_to_the_tenant(kit: BrokerKit) -> None:
    creds = await kit.broker.get(kit.tenant.tenant_id)
    assert creds.tenant_id == kit.tenant.tenant_id
    assert creds.account_id == kit.tenant.aws_account_id
    assert creds.region == kit.tenant.home_region
    assert isinstance(creds.aws.secret_access_key, SecretStr)
    assert creds.source in ("sts", "ambient")
    assert_creds_for(creds, kit.tenant.tenant_id, kit.tenant.aws_account_id)
    if creds.mantle.mode == "bearer":
        assert creds.mantle.bearer_token is not None
        assert creds.mantle.expires_at is not None and creds.expires_at is not None
        assert creds.mantle.expires_at <= creds.expires_at


async def test_secrets_never_print_and_are_redacted(kit: BrokerKit) -> None:
    creds = await kit.broker.get(kit.tenant.tenant_id)
    assert "REDACTED" not in str(creds.aws.secret_access_key)
    assert str(creds.aws.secret_access_key) == "**********"
    leaked = f"key={creds.aws.access_key_id} secret={creds.aws.secret_access_key.get_secret_value()}"
    assert creds.aws.secret_access_key.get_secret_value() not in redact(leaked)
    assert creds.aws.access_key_id not in redact(leaked)


async def test_account_mismatch_is_detected(kit: BrokerKit) -> None:
    creds = await kit.broker.get(kit.tenant.tenant_id)
    with pytest.raises(TenantAccessMisconfigured) as err:
        assert_creds_for(creds, kit.tenant.tenant_id, "999999999999")
    assert err.value.cause == "account_mismatch"
    assert err.value.code is ErrorCode.TENANT_ACCESS_MISCONFIGURED
    assert not err.value.spec.retryable


async def test_unknown_tenant_fails_closed(kit: BrokerKit) -> None:
    with pytest.raises(TenantAccessMisconfigured):
        await kit.broker.get(kit.unknown_tenant)


async def test_warm_get_is_cached_and_single_flight(kit: BrokerKit) -> None:
    first = await kit.broker.get(kit.tenant.tenant_id)
    async with anyio.create_task_group() as tg:
        for _ in range(50):
            tg.start_soon(kit.broker.get, kit.tenant.tenant_id)
    again = await kit.broker.get(kit.tenant.tenant_id)
    assert again.aws.access_key_id == first.aws.access_key_id
    if kit.issued is not None:
        assert kit.issued(kit.tenant.tenant_id) == 1


async def test_invalidate_then_refresh(kit: BrokerKit) -> None:
    await kit.broker.get(kit.tenant.tenant_id)
    kit.broker.invalidate(kit.tenant.tenant_id)
    creds = await kit.broker.get(kit.tenant.tenant_id)
    assert creds.tenant_id == kit.tenant.tenant_id


async def test_refreshes_before_expiry(kit: BrokerKit) -> None:
    creds = await kit.broker.get(kit.tenant.tenant_id)
    if creds.expires_at is None:
        pytest.skip("static credentials (AmbientBroker)")
    await kit.clock.advance((creds.expires_at - kit.clock.now() - timedelta(minutes=4)).total_seconds())
    fresh = await kit.broker.get(kit.tenant.tenant_id)
    assert fresh.expires_at is not None and fresh.expires_at > creds.expires_at


async def test_injected_failure_keeps_its_cause(kit: BrokerKit) -> None:
    if kit.fail is None:
        pytest.skip("no failure hook")
    kit.fail(kit.tenant.tenant_id, "external_id_unavailable")
    with pytest.raises(TenantAccessMisconfigured) as err:
        await kit.broker.get(kit.tenant.tenant_id)
    assert err.value.cause == "external_id_unavailable"
    assert err.value.details == {"cause": "external_id_unavailable"}
