"""The live smoke's ambient broker against the compose moto STS (ADR-001 §7): one credentials-file read, one
`GetCallerIdentity`, the account mapping (`ambient_account_mismatch` otherwise), and both Mantle auth modes
(short-term bearer presigned offline, SigV4)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from tests.fakes.config import FakeConfigRegistry
from tests.live.ambient import LiveAmbientBroker
from tests.live.livecfg import LIVE_TENANT, live_snapshot

from noema.core.clock import SystemClock
from noema.core.errors import TenantAccessMisconfigured

pytestmark = pytest.mark.integration
MOTO_ACCOUNT = "123456789012"


@pytest.fixture
def moto_url(noema_services: object) -> str:
    url: str = noema_services.moto_url  # type: ignore[attr-defined]
    try:
        httpx.get(url, timeout=1)
    except httpx.HTTPError:
        pytest.skip("the compose moto server is not reachable")
    return url


def credentials_file(tmp_path: Path) -> str:
    path = tmp_path / "credentials"
    path.write_text("[default]\naws_access_key_id = AKIAMOTOAMBIENT00001\naws_secret_access_key = moto-secret-value\n")
    return str(path)


async def test_ambient_broker_maps_the_caller_account(tmp_path: Path, moto_url: str) -> None:
    clock = SystemClock()
    config = FakeConfigRegistry(live_snapshot(MOTO_ACCOUNT))
    broker = LiveAmbientBroker(clock, config, credentials_file=credentials_file(tmp_path), sts_endpoint=moto_url)
    await broker.start()
    assert broker.caller_account == MOTO_ACCOUNT
    creds = await broker.get(LIVE_TENANT)
    assert creds.source == "ambient" and creds.region == "us-east-1" and creds.account_id == MOTO_ACCOUNT
    assert creds.aws.access_key_id == "AKIAMOTOAMBIENT00001"
    token = creds.mantle.bearer_token
    assert creds.mantle.mode == "bearer" and token is not None
    assert token.get_secret_value().startswith("bedrock-api-key-")
    broker.mantle_mode = "sigv4"
    signed = await broker.get(LIVE_TENANT)
    assert signed.mantle.mode == "sigv4" and signed.mantle.bearer_token is None


async def test_ambient_broker_refuses_another_account(tmp_path: Path, moto_url: str) -> None:
    config = FakeConfigRegistry(live_snapshot("999999999999"))
    broker = LiveAmbientBroker(
        SystemClock(), config, credentials_file=credentials_file(tmp_path), sts_endpoint=moto_url
    )
    await broker.start()
    with pytest.raises(TenantAccessMisconfigured) as err:
        await broker.get(LIVE_TENANT)
    assert err.value.cause == "ambient_account_mismatch"
