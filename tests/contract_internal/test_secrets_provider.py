"""SecretsProvider conformance (spec-02 §3): versions, the 300 s cache, missing secrets -> internal_error
(`SecretMissing`, so a missing version is told from an unreadable backend)."""

from __future__ import annotations

import pytest

from noema.core.errors import ErrorCode, NoemaError, SecretMissing

from .kits import SecretsKit
from .registry import kit_fixture

kit = kit_fixture("secrets_provider")
REF = "noema/api-key-pepper"


async def test_current_and_versioned_reads(kit: SecretsKit) -> None:
    await kit.put(REF, b"pepper-version-one", "1")
    await kit.put(REF, b"pepper-version-two", "2")
    assert await kit.provider.get(REF) == b"pepper-version-two"
    assert await kit.provider.get(REF, version="1") == b"pepper-version-one"


async def test_missing_secret_is_internal_error(kit: SecretsKit) -> None:
    with pytest.raises(SecretMissing) as err:
        await kit.provider.get("noema/does-not-exist")
    assert err.value.code is ErrorCode.INTERNAL_ERROR
    await kit.put(REF, b"pepper-version-one", "1")
    with pytest.raises(SecretMissing):
        await kit.provider.get(REF, version="9")


async def test_cached_for_300_seconds(kit: SecretsKit) -> None:
    if kit.backend_reads is None:
        pytest.skip("no backend read counter")
    await kit.put(
        "aws-sm://platform/tenants/t_acme/external-id", b"aghoo-t_acme-0123456789abcdef0123456789abcdef", None
    )
    ref = "aws-sm://platform/tenants/t_acme/external-id"
    await kit.provider.get(ref)
    reads = kit.backend_reads()
    await kit.clock.advance(299)
    await kit.provider.get(ref)
    assert kit.backend_reads() == reads
    await kit.clock.advance(2)
    await kit.provider.get(ref)
    assert kit.backend_reads() == reads + 1


async def test_backend_failure_is_internal_error(kit: SecretsKit) -> None:
    if kit.break_backend is None:
        pytest.skip("no failure hook")
    await kit.put(REF, b"pepper-version-one", "1")
    kit.break_backend(REF)
    with pytest.raises(NoemaError) as err:
        await kit.provider.get(REF, version="1")
    assert err.value.code is ErrorCode.INTERNAL_ERROR
    assert not isinstance(err.value, SecretMissing)  # an outage is not "no such version" (identity.md item 6)
