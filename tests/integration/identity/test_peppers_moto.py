"""The pepper ring through `SecretsManagerProvider` on moto: a secret without an `AWSPREVIOUS` version reads as
"no previous pepper" (ResourceNotFound, not a partial ring); after a new value both versions load."""

from __future__ import annotations

import secrets

import pytest
from tests.fakes.clock import FakeClock
from tests.integration.identity.moto_support import CREDS, REGION, delete_secret, put_secret
from tests.unit.identity.helpers import PEPPER_V1, PEPPER_V2

from noema.identity.apikeys import load_peppers, pepper_value
from noema.identity.secrets import SecretsManagerProvider

pytestmark = pytest.mark.integration


async def test_missing_previous_version_is_not_a_backend_failure(moto: str) -> None:
    ref = f"noema/pepper-probe-{secrets.token_hex(6)}"
    await put_secret(moto, ref, pepper_value(1, PEPPER_V1).decode())
    provider = SecretsManagerProvider(FakeClock(), region=REGION, endpoint_url=moto, credentials=CREDS, ttl_s=0.0)
    try:
        first = await load_peppers(provider, ref)
        assert first.current.version == 1 and first.previous is None and not first.partial
        await put_secret(moto, ref, pepper_value(2, PEPPER_V2).decode())  # Secrets Manager moves AWSPREVIOUS
        rotated = await load_peppers(provider, ref, fallback=first)
        assert rotated.current.version == 2 and not rotated.partial
        assert rotated.previous is not None and rotated.previous.version == 1
    finally:
        await provider.aclose()
        await delete_secret(moto, ref)
