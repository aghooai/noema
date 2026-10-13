"""SEC-ISO-01 = spec-06 T-L2-2 on the compose Valkey: tenants A and B send identical input and questions; B misses,
and the keys differ in the tenant prefix and the per-tenant HMAC digest (IMPL-004, IMPL-059)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
import redis.asyncio
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from
from tests.fakes.world import build_world
from tests.unit.decision.conformance_impls import engine_deps
from tests.unit.decision.helpers import DEPARTMENT
from tests.unit.decision.world import EngineWorld, answer, res

from noema.core.clock import SystemClock
from noema.core.redis_keys import RedisKeys
from noema.decision.cache import DecisionCacheImpl
from noema.decision.compiler import QuestionCompilerImpl
from noema.decision.engine import DecisionEngineImpl

pytestmark = pytest.mark.integration
BETA = {
    "aws_account_id": "444455556666",
    "invoke_role_arn": "arn:aws:iam::444455556666:role/AghooBedrockInvoke",
    "external_id_ref": "aws-sm://platform/tenants/t_beta/external-id",
    "home_region": "us-east-1",
    "defaults": {"decision_profile": "router-default"},
}


async def test_no_cross_tenant_l2_hits(noema_services: Any, noema_run_id: str) -> None:
    client = redis.asyncio.Redis.from_url(noema_services.redis_url, decode_responses=False)
    keys = RedisKeys(f"ns:{noema_run_id}:iso:")
    world = build_world(
        clock=SystemClock(), config=FakeConfigRegistry(snapshot_from(patched_example({"tenants": {"t_beta": BETA}})))
    )
    cache = DecisionCacheImpl(client, keys, SystemClock(), world.telemetry.instruments)
    deps = replace(engine_deps(world), cache=cache, keys=keys)
    ew = EngineWorld(world, DecisionEngineImpl(deps), QuestionCompilerImpl(world.config), world.clock)  # type: ignore[arg-type]
    ew.use([answer(None, None, {"billing": 0.7, "technical": 0.1, "shipping": 0.1, "other": 0.1}, 10)])
    req = ew.request([DEPARTMENT])
    try:
        a1 = await ew.engine.decide(ew.context(tenant="t_acme"), req)
        a2 = await ew.engine.decide(ew.context(tenant="t_acme"), req)
        b1 = await ew.engine.decide(ew.context(tenant="t_beta"), req)
        assert res(a2)["provenance"]["cache"]["hit"] is True
        assert res(b1)["provenance"]["cache"]["hit"] is False
        assert res(a1)["provenance"]["input_digest"] != res(b1)["provenance"]["input_digest"]
        stored = sorted([k.decode() async for k in client.scan_iter(match=f"ns:{noema_run_id}:iso:*")])
        assert [k.split(":")[-2] for k in stored] == ["t_acme", "t_beta"]
        assert stored[0].rsplit(":", 1)[1] != stored[1].rsplit(":", 1)[1]
        assert await cache.purge_tenant("t_acme") == 1
        assert [k.decode() async for k in client.scan_iter(match=f"ns:{noema_run_id}:iso:*")] == [stored[1]]
    finally:
        async for key in client.scan_iter(match=f"ns:{noema_run_id}:iso:*"):
            await client.unlink(key)
        await client.aclose()
