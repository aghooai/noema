"""The L2 decision cache (spec-06 §14, §17 T-L2-1…3; IMPL-004): key parts, gates, hits, fail-open."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

from tests.fakes.clock import FakeClock
from tests.fakes.telemetry import FakeTelemetry
from tests.unit.decision.checks import assert_valid, wire_request
from tests.unit.decision.conformance_impls import engine_deps
from tests.unit.decision.fake_redis import FakeRedis
from tests.unit.decision.helpers import DEPARTMENT
from tests.unit.decision.test_escalation import _image_input
from tests.unit.decision.world import EngineWorld, answer, engine_world, res, tel

from noema.core.protocols.media import InputView
from noema.core.redis_keys import RedisKeys
from noema.decision.cache import DecisionCacheImpl, l2_key
from noema.decision.engine import DecisionEngineImpl

DEPT = {"billing": 0.7, "technical": 0.1, "shipping": 0.1, "other": 0.1}


async def _decide(ew: EngineWorld, req: Any, **kw: Any) -> Any:
    async with ew.clock.autojump():
        return await ew.engine.decide(ew.context(**kw), req)


async def test_t_l2_1_miss_then_hit() -> None:
    ew = engine_world()
    ew.use([answer(None, None, DEPT, 300)])
    req = ew.request([DEPARTMENT])
    first = await _decide(ew, req)
    calls = len(ew.world.adapters.calls())
    second = await _decide(ew, req)
    assert len(ew.world.adapters.calls()) == calls  # served from L2
    assert first.decision_id != second.decision_id and second.final and second.status == "completed"
    r1: Any = first.result
    r2: Any = second.result
    assert r2["provenance"]["cache"] == {"hit": True} and r2["usage"]["input_tokens"] == 0
    assert all("usage" not in m for a in r2["answers"] for m in a.get("members", []))
    assert [a["candidates"] for a in r2["answers"]] == [a["candidates"] for a in r1["answers"]]
    assert r2["provenance"]["started_at"] == r1["provenance"]["started_at"]
    (savings,) = [ln for ln in ew.world.cost.lines if ln.line == "cache_savings"]
    assert savings.model_id is None and savings.decision_id == second.decision_id
    assert savings.usage.input_tokens == r1["usage"]["input_tokens"]
    rows = ew.world.db.store.rows("decision")
    assert [r["cache_hit"] for r in rows] == [False, True]
    assert tel(ew).counter("aghoo_cache_hits_total", layer="l2") == 1
    assert_valid(r2, wire_request(req))


async def test_t_l2_1_gates() -> None:
    ew = engine_world()
    ew.use([answer(None, None, DEPT, 300)])
    req = ew.request([DEPARTMENT])
    await _decide(ew, req, data_class="restricted")
    await _decide(ew, req, data_class="restricted")
    assert len(ew.world.adapters.calls()) == 6  # restricted: bypass both times
    assert tel(ew).counter("aghoo_cache_misses_total", reason="bypass") == 2
    await _decide(ew, req, store=False)
    await _decide(ew, req, purpose="verification")
    assert ew.world.decision_cache._data == {}
    newer = engine_world(patch={"routing": {"policy_version": 2}})
    k1 = l2_key(ew.world.keys, ew.context(), req, digest="d", view_signature="v", epoch="0.0")
    k2 = l2_key(
        newer.world.keys, newer.context(), newer.request([DEPARTMENT]), digest="d", view_signature="v", epoch="0.0"
    )
    assert k1 != k2


class MetadataOnlyForDeepSeek:
    async def resolve(self, member: Any, parts: Any, **kw: Any) -> InputView:
        kind = "metadata_only" if member.model == "deepseek-3.2" else "native"
        return InputView(kind, {"image": kind}, (), ())  # type: ignore[arg-type,dict-item]


async def test_t_l2_1_metadata_only_views_are_never_put() -> None:
    ew = engine_world()
    deps = replace(engine_deps(ew.world), views=MetadataOnlyForDeepSeek())
    ew.engine = DecisionEngineImpl(deps)
    ew.use([answer(None, None, DEPT, 300)])
    req = ew.request([DEPARTMENT], value=await _image_input(ew))
    snap = await _decide(ew, req)
    assert snap.status == "completed" and ew.world.decision_cache._data == {}


async def test_t_l2_1_a_cancelled_run_is_never_put() -> None:
    """IMPL-004's put gates (a hit equals a fresh computation): a run cancelled with members in flight (a turn cancel
    or abandon, W, the final grace) closes them `late`; even when the rest make quorum (`completed`) it is not put,
    so the next identical request computes the full answer (Wave E fix)."""
    haiku_dept = {"billing": 0.1, "technical": 0.7, "shipping": 0.1, "other": 0.1}
    ew = engine_world()
    ew.use([answer("us.anthropic.claude-haiku-5-5", None, haiku_dept, 800), answer(None, None, DEPT, 300)])
    req = ew.request([DEPARTMENT])
    async with ew.clock.autojump():
        async with ew.engine.run(ew.context(), req) as run:
            await ew.clock.sleep(0.5)  # DeepSeek and Luna answered at 300 ms; Haiku still in flight
            await run.cancel()
            cancelled = await run.finalize()
        assert cancelled.status == "completed"
        assert "late" in [m["status"] for m in res(cancelled)["answers"][0]["members"]]
        assert ew.world.decision_cache._data == {}
        fresh = await ew.engine.decide(ew.context(), req)
    assert res(fresh)["provenance"]["cache"]["hit"] is False
    assert [m["status"] for m in res(fresh)["answers"][0]["members"]] == ["ok", "ok", "ok"]


async def test_t_l2_2_tenant_isolation() -> None:
    patch = {
        "tenants": {
            "t_beta": {
                "aws_account_id": "444455556666",
                "invoke_role_arn": "arn:aws:iam::444455556666:role/AghooBedrockInvoke",
                "external_id_ref": "aws-sm://platform/tenants/t_beta/external-id",
                "home_region": "us-east-1",
                "defaults": {"decision_profile": "router-default"},
            }
        }
    }
    ew = engine_world(patch=patch)
    ew.use([answer(None, None, DEPT, 300)])
    req = ew.request([DEPARTMENT])
    await _decide(ew, req, tenant="t_acme")
    calls = len(ew.world.adapters.calls())
    await _decide(ew, req, tenant="t_beta")
    assert len(ew.world.adapters.calls()) == 2 * calls  # B misses
    keys = sorted(ew.world.decision_cache._data)
    assert len(keys) == 2 and [k.split(":")[4] for k in keys] == ["t_acme", "t_beta"]
    assert keys[0].rsplit(":", 1)[1] != keys[1].rsplit(":", 1)[1]


async def test_t_l2_3_no_budget_truncated_puts_then_a_full_computation() -> None:
    ew = engine_world()
    weak = {"billing": 0.48, "technical": 0.02, "shipping": 0.48, "other": 0.02}
    ew.use([answer("us.openai.gpt-6-luna", None, weak, 700), answer(None, None, DEPT, 300)])
    req = ew.request([DEPARTMENT], profile="lightweight-model-decide", escalation="profile_default")
    skipped = await _decide(ew, req, budget_s=1.5)  # 0.7 s + router-default 1.0 s > 1.5 s: skipped for budget
    assert res(skipped)["provenance"]["stages"] == []
    off = replace(req, escalation="off")
    capped = await _decide(ew, off, budget_s=0.9)  # the 1,000 ms stage-0 deadline capped by ctx.deadline
    assert capped.status == "completed"
    assert ew.world.decision_cache._data == {}
    full = await _decide(ew, req)
    assert res(full)["provenance"]["cache"]["hit"] is False
    assert len(res(full)["provenance"]["stages"]) == 1


async def test_t_l2_3_a_hanging_redis_is_a_miss_after_20_ms() -> None:
    ew = engine_world()
    ew.world.decision_cache.delay_s = 1.0
    ew.use([answer(None, None, DEPT, 300)])
    t0 = ew.clock.monotonic()
    snap = await _decide(ew, ew.request([DEPARTMENT]))
    assert snap.status == "completed" and round((ew.clock.monotonic() - t0) * 1000) <= 1000


async def test_cache_impl_round_trip_ttl_purge_and_fail_open() -> None:
    clock = FakeClock()
    redis = FakeRedis(clock)
    tel = FakeTelemetry()
    keys = RedisKeys("ns:t:")
    cache = DecisionCacheImpl(cast(Any, redis), keys, clock, tel.instruments)
    k = keys.l2("t_a", "d1")
    assert await cache.get(k) is None
    await cache.put(k, b"x", 10)
    assert await cache.get(k) == b"x"
    await clock.advance(11)
    assert await cache.get(k) is None
    for tenant in ("t_a", "t_a", "t_b"):
        for d in ("1", "2"):
            await cache.put(keys.l2(tenant, d), b"x", 60)
    assert await cache.purge_tenant("t_a") == 2 and await cache.get(keys.l2("t_b", "1")) == b"x"
    redis.down = True
    assert await cache.get(keys.l2("t_b", "1")) is None
    await cache.put(keys.l2("t_b", "1"), b"y", 60)
    redis.down, redis.delay_s = False, 1.0
    async with clock.autojump():
        t0 = clock.monotonic()
        assert await cache.get(keys.l2("t_b", "1")) is None
        assert round((clock.monotonic() - t0) * 1000) == 20
    assert tel.counter("aghoo_cache_misses_total", reason="error") == 3
