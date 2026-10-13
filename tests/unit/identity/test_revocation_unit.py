"""`handle_revocation` (keys_revoked payloads, SEC-ISO-02) and the lookup/revocation race of the key cache."""

from __future__ import annotations

import anyio
import orjson
from tests.fakes.telemetry import FakeTelemetry
from tests.unit.identity.helpers import KEY_ID, TENANT, authn_rig, bearer, seed_key

from noema.core.protocols.storage import Row
from noema.identity.revocation import handle_revocation
from noema.identity.security import SecurityEvents


async def test_payload_handling() -> None:
    rig = authn_rig()
    token = seed_key(rig.db)
    telemetry = FakeTelemetry(strict=True)
    security = SecurityEvents(telemetry=telemetry)
    await rig.authn.authenticate(bearer(token), "198.51.100.1")
    for junk in (b"not json", b"[]", b'{"key_id": 1}', orjson.dumps({"key_id": KEY_ID})):
        assert handle_revocation(junk, rig.authn, security) is False
    assert len(rig.authn.cache) == 1
    assert handle_revocation(orjson.dumps({"tenant_id": "t_x", "key_id": KEY_ID}), rig.authn, security) is False
    assert telemetry.counter("aghoo_security_violations_total", kind="pubsub_tenant_mismatch") == 1
    assert handle_revocation(orjson.dumps({"tenant_id": TENANT, "key_id": KEY_ID}), rig.authn, security) is True
    assert len(rig.authn.cache) == 0
    assert handle_revocation(orjson.dumps({"tenant_id": TENANT, "key_id": "unknown"}), rig.authn, security) is False


async def test_a_revocation_during_the_lookup_is_not_cached_over() -> None:
    rig = authn_rig()
    token = seed_key(rig.db)
    calls = type(rig.db).system_uow
    gate = anyio.Event()
    started = anyio.Event()

    from contextlib import asynccontextmanager
    from typing import Any

    @asynccontextmanager
    async def slow_uow(db: Any) -> Any:
        async with calls(db) as system:
            original = system.lookup_api_key

            async def lookup(key_id: str) -> Row | None:
                row = await original(key_id)
                started.set()
                await gate.wait()
                return row

            system.lookup_api_key = lookup  # type: ignore[method-assign]
            yield system

    rig.db.system_uow = slow_uow.__get__(rig.db)  # type: ignore[method-assign]
    async with anyio.create_task_group() as tg:
        tg.start_soon(rig.authn.authenticate, bearer(token), "198.51.100.1")
        await started.wait()
        rig.authn.evict(KEY_ID)  # keys_revoked arrives while the lookup read the pre-revocation row
        gate.set()
    assert len(rig.authn.cache) == 0
