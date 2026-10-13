"""The C2a interfaces in the `labeling.item` job: the exact daily cap (`RedisKeys.labeling_daily`, INCR + EXPIRE 2
days, first attempt only, fail-open), `ldrop`/`lab` ids from the IdGen, the `labeling.item` span, the
`aghoo_labeling_items_total{question, outcome}` counter, and prices with the job's snapshot and the tenant's home
region (spec-11 §2)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

import anyio
import pytest
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from
from tests.fakes.world import build_world
from tests.unit.labeling.jobkit import (
    INTENT,
    CounterRedis,
    ScriptedAdapter,
    ScriptedRegistry,
    agree_on,
    answer,
    decision_row,
    deps,
    letter_of,
    member,
)

from noema.core.errors import ClassifiedError, ErrorKind
from noema.core.ids import IdPrefix
from noema.core.protocols.storage import Job
from noema.labeling.deps import DAILY_COUNT_TTL_S
from noema.labeling.items import ItemJob, RetryLabeling

pytestmark = pytest.mark.unit
MEMBERS = [member("m1", {"billing": 0.6, "coding": 0.4}), member("m2", {"billing": 0.7, "other": 0.3})]


def job(decision_id: str = "dec_1", *, attempt: int = 1) -> Job:
    payload: dict[str, Any] = {
        "decision_id": decision_id,
        "turn_id": None,
        "question_names": ["intent"],
        "sample_reason": "uniform",
    }
    return Job(f"job_{decision_id}", "t_acme", "labeling.item", payload, attempt)


def world_with(n: int = 1, *, cap: int | None = 2000) -> Any:
    world = build_world(
        config=FakeConfigRegistry(
            snapshot_from(patched_example({"labeling": {"sample": {"max_per_day_per_tenant": cap}}}))
        )
    )
    for i in range(1, n + 1):
        world.db.store.seed("decision", decision_row(f"dec_{i}", [answer(INTENT, MEMBERS)]))
    return world


def handler(world: Any, script: Any = None, *, redis: Any = None) -> tuple[ItemJob, ScriptedAdapter]:
    adapter = ScriptedAdapter(script or agree_on({"intent": "billing"}))
    return ItemJob(deps(world, ScriptedRegistry(adapter), redis=redis)), adapter


def day_key(world: Any) -> str:
    return str(world.keys.labeling_daily("t_acme", world.clock.now().strftime("%Y%m%d")))


async def test_the_daily_cap_is_exact_across_pods() -> None:
    world, redis = world_with(3, cap=2), CounterRedis()
    pods = [handler(world, redis=redis) for _ in range(3)]  # one shared counter, three job processes
    for i, (h, _) in enumerate(pods, start=1):
        await h(job(f"dec_{i}"))
    assert sorted(i["decision_id"] for i in world.db.store.rows("label_item")) == ["dec_1", "dec_2"]
    assert len(pods[2][1].requests) == 0  # the third offer is not labeled (nor billed)
    key = day_key(world)
    assert key.endswith(f"labeling:daily:t_acme:{world.clock.now():%Y%m%d}") and redis.values == {key: 3}
    assert redis.ttls == {key: DAILY_COUNT_TTL_S} and DAILY_COUNT_TTL_S == 172_800


async def test_a_retry_is_not_counted_again() -> None:
    throttled = ClassifiedError(ErrorKind.THROTTLED, "slow down")
    calls = {"n": 0}

    def script(alias: str, aq: Any) -> Any:
        calls["n"] += 1
        return throttled if calls["n"] == 1 else letter_of(aq, "billing")

    world, redis = world_with(cap=1), CounterRedis()
    h, _ = handler(world, script, redis=redis)
    with pytest.raises(RetryLabeling):
        await h(job(attempt=1))
    await h(job(attempt=2))  # the offer was counted once, by the first attempt: it is not skipped now
    assert len(world.db.store.rows("label_item")) == 1 and redis.values == {day_key(world): 1}


async def test_the_counter_is_per_utc_day() -> None:
    world, redis = world_with(2, cap=1), CounterRedis()
    h, _ = handler(world, redis=redis)
    await h(job("dec_1"))
    first = day_key(world)
    await world.fake_clock.advance(86_400)
    await h(job("dec_2"))
    assert len(world.db.store.rows("label_item")) == 2 and redis.values == {first: 1, day_key(world): 1}


async def test_without_a_cap_nothing_is_counted() -> None:
    world, redis = world_with(cap=None), CounterRedis()
    h, _ = handler(world, redis=redis)
    await h(job())
    assert len(world.db.store.rows("label_item")) == 1 and redis.values == {}


async def test_a_failed_redis_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    world = world_with(cap=1)
    h, _ = handler(world, redis=CounterRedis(fail=True))
    await h(job())
    assert len(world.db.store.rows("label_item")) == 1
    assert "labeling daily cap not checked: Redis failed" in caplog.text


@dataclass
class HangingRedis:
    async def incr(self, key: str) -> int:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")


async def test_a_slow_redis_fails_open_after_the_timeout(caplog: pytest.LogCaptureFixture) -> None:
    world = world_with(cap=1)
    h, _ = handler(world, redis=HangingRedis())
    start = world.clock.monotonic()
    async with world.fake_clock.autojump():
        await h(job())
    assert len(world.db.store.rows("label_item")) == 1 and world.clock.monotonic() - start >= 1.0
    assert "labeling daily cap not checked: Redis timed out" in caplog.text


@dataclass
class SpyIds:
    inner: Any
    prefixes: list[str] = field(default_factory=list)

    def new(self, prefix: IdPrefix) -> str:
        self.prefixes.append(prefix)
        return str(self.inner.new(prefix))


async def test_kept_and_dropped_items_get_their_own_id_prefixes() -> None:
    world = world_with(2)
    adapter = ScriptedAdapter(agree_on({"intent": "billing"}))
    ids = SpyIds(world.ids)
    d = replace(deps(world, ScriptedRegistry(adapter)), ids=ids)
    await ItemJob(d)(job("dec_1"))
    adapter.script = lambda alias, aq: letter_of(aq, "billing" if alias == "sol-6.1" else "coding")
    await ItemJob(d)(job("dec_2"))
    kept, dropped = sorted(world.db.store.rows("label_item"), key=lambda i: i["decision_id"])
    assert re.fullmatch(r"lab_[0-9A-HJKMNP-TV-Z]{26}", kept["id"]) and kept["kept"] is True
    assert re.fullmatch(r"ldrop_[0-9A-HJKMNP-TV-Z]{26}", dropped["id"]) and dropped["drop_reason"] == "disagreed"
    assert ids.prefixes.count("lab") == 1 and ids.prefixes.count("ldrop") == 1  # straight from the IdGen


async def test_the_item_span_and_the_outcome_counter() -> None:
    world = world_with(2)
    h, adapter = handler(world)
    await h(job("dec_1"))
    adapter.script = lambda alias, aq: None if alias == "sonnet-5.5" else letter_of(aq, "billing")  # a refusal
    await h(job("dec_2"))
    spans = [s for s in world.telemetry.spans if s.name == "labeling.item"]
    assert [s.attributes for s in spans] == [
        {"aghoo.tenant": "t_acme", "aghoo.profile": "router-default", "aghoo.member.question": "intent"}
    ] * 2
    assert all(s.ended and s.error is None for s in spans)
    count = world.telemetry.counter
    assert count("aghoo_labeling_items_total", question="intent", outcome="kept") == 1
    assert count("aghoo_labeling_items_total", question="intent", outcome="refused") == 1
    assert count("aghoo_labeling_items_total") == 2


async def test_a_transient_retry_is_not_a_span_error() -> None:
    world = world_with()
    h, _ = handler(world, lambda alias, aq: ClassifiedError(ErrorKind.THROTTLED, "slow down"))
    with pytest.raises(RetryLabeling):
        await h(job())
    [span] = [s for s in world.telemetry.spans if s.name == "labeling.item"]
    assert span.ended and span.error is None
    assert world.telemetry.counter("aghoo_labeling_items_total") == 0  # nothing stored yet


@dataclass
class SpyCost:
    inner: Any
    priced: list[tuple[str, str, Any]] = field(default_factory=list)

    def price(self, model_alias: str, region: str, usage: Any, *, snapshot: Any = None) -> Any:
        self.priced.append((model_alias, region, snapshot))
        return self.inner.price(model_alias, region, usage, snapshot=snapshot)

    def record(self, line: Any) -> None:
        self.inner.record(line)


@dataclass
class OtherRegion:
    """A ScriptedAdapter served from another region (cross-region inference)."""

    inner: ScriptedAdapter

    async def complete(self, req: Any, *, tenant: Any, deadline: Any) -> Any:
        return replace(await self.inner.complete(req, tenant=tenant, deadline=deadline), region="us-west-2")


async def test_prices_use_the_job_snapshot_and_the_tenant_home_region() -> None:
    world = world_with()
    snapshot = world.config.current()
    home = snapshot.tenant_ref("t_acme").home_region
    assert home != "us-west-2"
    cost = SpyCost(world.cost)
    adapter = OtherRegion(ScriptedAdapter(agree_on({"intent": "billing"})))
    d = replace(deps(world, ScriptedRegistry(adapter)), cost=cost)  # type: ignore[arg-type]
    await ItemJob(d)(job())
    assert sorted(a for a, _, _ in cost.priced) == ["sol-6.1", "sonnet-5.5"]
    assert all(r == home and s is snapshot for _, r, s in cost.priced)  # the catalog key, never the serving region
    lines = [r for r in world.outbox.records if r.kind == "usage_line"]
    assert len(lines) == 2 and {r.payload["region"] for r in lines} == {"us-west-2"}  # the call region
