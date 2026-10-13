"""`DbCalibratorRegistry` over the fake database (spec-02 §9, ADR-003 §2, IMPL-072): sync lookups on one snapshot,
tenant first then platform, exact binding keys, epochs, refresh of changed scopes only, malformed rows skipped,
failures keep the last snapshot, and the pub/sub listener."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.db import FakeDatabase

from noema.core.protocols.labeling import CalibratorBindingKey
from noema.labeling.calibrators.transforms import PlattTransform, TemperatureTransform
from noema.labeling.registry import DbCalibratorRegistry

pytestmark = pytest.mark.unit
KEY = CalibratorBindingKey("router-default", "1", "sha256:" + "a" * 64, ("m1", "m2", "m3"), "sha256:" + "b" * 64)


def row(
    cid: str, *, scope: str = "tenant", tenant: str | None = "t_acme", status: str = "active", **extra: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": cid,
        "scope": scope,
        "tenant_id": tenant if scope == "tenant" else None,
        "profile_id": KEY.profile_id,
        "profile_version": KEY.profile_version,
        "question_name": "intent",
        "question_signature": KEY.question_signature,
        "member_subset": ["m3", "m1", "m2"],  # stored order does not matter
        "input_view_signature": KEY.input_view_signature,
        "choice_method": "flat",
        "method": "temperature",
        "member_t": {"m1": 1.5, "m2": 0.8, "m3": 1.0},
        "pooled": {"temperature": 1.2},
        "epsilon": 1e-4,
        "holdout": {"n": 400, "ece": 0.03},
        "acceptance": {"passed": True, "failed_rules": []},
        "reliability": {"bins": [], "ci": {}},
        "status": status,
    }
    base.update(extra)
    return base


def setup(*rows: dict[str, Any], epochs: dict[str, int] | None = None) -> tuple[FakeDatabase, DbCalibratorRegistry]:
    db = FakeDatabase(FakeClock())
    for r in rows:
        db.store.seed("calibrator", r)
    for scope_key, n in (epochs or {"platform": 0, "t_acme": 0}).items():
        db.store.seed("calibrator_epoch", {"scope_key": scope_key, "n": n})
    return db, DbCalibratorRegistry(db)


def bump(db: FakeDatabase, scope_key: str) -> None:
    epochs = db.store.table("calibrator_epoch")
    current = epochs.get((scope_key,), {"scope_key": scope_key, "n": 0})
    db.store.seed("calibrator_epoch", {"scope_key": scope_key, "n": int(current["n"]) + 1})


async def test_before_the_first_refresh_nothing_is_served() -> None:
    _, registry = setup(row("cal_t"))
    assert registry.lookup("t_acme", KEY) is None and registry.epoch("t_acme") == ""


async def test_tenant_first_then_platform_and_exact_keys() -> None:
    db, registry = setup(row("cal_p", scope="platform"), epochs={"platform": 1, "t_acme": 0, "t_beta": 0})
    await registry.refresh()
    found = registry.lookup("t_acme", KEY)
    assert found is not None and found.id == "cal_p" and found.scope == "platform"
    db.store.seed("calibrator", row("cal_t"))
    bump(db, "t_acme")
    await registry.refresh()
    tenant = registry.lookup("t_acme", KEY)
    assert tenant is not None and tenant.id == "cal_t" and tenant.key == KEY
    assert (registry.lookup("t_beta", KEY) or tenant).id == "cal_p"
    for other in (
        replace(KEY, member_subset=("m1", "m2")),
        replace(KEY, input_view_signature="sha256:" + "c" * 64),
        replace(KEY, profile_id="router-plus"),
        replace(KEY, profile_version="2"),
        replace(KEY, choice_method="hierarchical"),
    ):
        assert registry.lookup("t_acme", other) is None
    assert registry.epoch("t_acme") == "1.1" and registry.epoch("t_beta") == "1.0" and registry.epoch("t_x") == ""
    assert registry.epoch("platform") == ""


async def test_artifact_transforms_are_applied_together() -> None:
    _, registry = setup(
        row("cal_t"),
        row(
            "cal_pp",
            scope="platform",
            method="platt",
            pooled={"a": 0.7, "b": -0.2},
            question_signature="sha256:" + "d" * 64,
        ),
    )
    await registry.refresh()
    cal = registry.lookup("t_acme", KEY)
    assert cal is not None and cal.member_T == {"m1": 1.5, "m2": 0.8, "m3": 1.0}
    assert cal.pooled == TemperatureTransform(1.2, 1e-4) and cal.member("m2") == TemperatureTransform(0.8, 1e-4)
    assert cal.member("m9").apply({"a": 0.3}, 0.7) == {"a": 0.3}  # identity outside the subset
    assert cal.holdout == {"n": 400, "ece": 0.03}
    platt = registry.lookup("t_acme", replace(KEY, question_signature="sha256:" + "d" * 64))
    assert platt is not None and platt.pooled == PlattTransform(0.7, -0.2, 1e-4) and platt.method == "platt"


async def test_only_changed_scopes_reload_and_downgrades_disappear() -> None:
    db, registry = setup(row("cal_t"), epochs={"platform": 0, "t_acme": 1})
    await registry.refresh()
    db.store.table("calibrator")[("cal_t",)]["status"] = "downgraded"
    await registry.refresh()  # epoch unchanged: the snapshot is kept
    assert registry.lookup("t_acme", KEY) is not None
    bump(db, "t_acme")
    await registry.refresh()
    assert registry.lookup("t_acme", KEY) is None and registry.epoch("t_acme") == "0.2"


async def test_malformed_rows_are_skipped_and_errors_keep_the_snapshot() -> None:
    db, registry = setup(
        row("cal_t"), row("cal_bad", tenant="t_acme", question_signature="sha256:" + "e" * 64, member_t={"m1": -1})
    )
    await registry.refresh()
    assert registry.lookup("t_acme", KEY) is not None
    assert registry.lookup("t_acme", replace(KEY, question_signature="sha256:" + "e" * 64)) is None
    db.store.available = False
    bump(db, "t_acme")
    await registry.refresh()  # never raises
    assert (
        registry.refresh_errors == 1
        and registry.lookup("t_acme", KEY) is not None
        and registry.epoch("t_acme") == "0.0"
    )
    db.store.available = True
    await registry.refresh()
    assert registry.epoch("t_acme") == "0.1"


async def test_a_row_whose_member_t_does_not_cover_the_subset_is_never_served() -> None:
    """ADR-003 §1: a T_m for every member of the subset; a partial artifact (e.g. a CLI import) stays uncalibrated
    instead of applying identity to the members it lacks."""
    partial = replace(KEY, question_signature="sha256:" + "c" * 64)
    extra = replace(KEY, question_signature="sha256:" + "d" * 64)
    _, registry = setup(
        row("cal_partial", question_signature=partial.question_signature, member_t={"m1": 1.5, "m2": 0.8}),
        row("cal_extra", question_signature=extra.question_signature, member_t={"m1": 1, "m2": 1, "m3": 1, "m4": 1}),
        row("cal_t"),
    )
    await registry.refresh()
    assert registry.lookup("t_acme", partial) is None and registry.lookup("t_acme", extra) is None
    assert registry.lookup("t_acme", KEY) is not None


async def test_a_deleted_tenant_scope_is_dropped() -> None:
    db, registry = setup(row("cal_t"), epochs={"platform": 0, "t_acme": 1})
    await registry.refresh()
    del db.store.table("calibrator_epoch")[("t_acme",)]
    await registry.refresh()
    assert registry.lookup("t_acme", KEY) is None and registry.epoch("t_acme") == ""


class FakePubSub:
    def __init__(self, messages: list[dict[str, Any]], fail: bool) -> None:
        self.messages, self.fail, self.closed, self.channels = messages, fail, False, list[str]()

    async def subscribe(self, channel: str) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.channels.append(channel)

    clock: FakeClock | None = None  # set: a silent connection (every read waits its timeout, PINGs unanswered)
    pings = 0

    async def get_message(self, *, ignore_subscribe_messages: bool, timeout: float) -> dict[str, Any] | None:  # noqa: ASYNC109
        if self.messages:  # read through `noema.core.pubsub.heartbeat_messages` (Wave E3)
            return self.messages.pop(0)
        if self.clock is None:
            raise ConnectionError("connection lost")
        await self.clock.sleep(timeout)
        return None

    async def ping(self) -> None:
        self.pings += 1

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    def __init__(self, sessions: list[FakePubSub]) -> None:
        self.sessions = sessions
        self.opened = 0

    def pubsub(self) -> FakePubSub:
        self.opened += 1
        return self.sessions.pop(0)


async def test_listener_refreshes_on_messages_and_resubscribes_with_backoff() -> None:
    import functools

    import anyio

    _db, registry = setup(row("cal_t"), epochs={"platform": 0, "t_acme": 1})
    clock = FakeClock()
    first = FakePubSub([{"type": "subscribe"}, {"type": "message", "data": b"{}"}], fail=False)
    down = FakePubSub([], fail=True)
    last = FakePubSub([], fail=False)
    redis = FakeRedis([first, down, last])
    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(registry.listen, redis, "ns:ch:calibrators_changed", clock=clock))  # type: ignore[arg-type]
        await clock.settle()
        assert registry.refreshes == 2 and first.closed and first.channels == ["ns:ch:calibrators_changed"]
        await clock.advance(1.0)
        await clock.settle()
        assert down.closed and redis.opened == 2
        await clock.advance(2.0)  # backoff doubled
        await clock.settle()
        assert redis.opened == 3 and last.closed
        tg.cancel_scope.cancel()
    assert registry.lookup("t_acme", KEY) is not None


async def test_a_silent_subscription_is_pinged_then_resubscribed() -> None:
    import functools

    import anyio

    _db, registry = setup(row("cal_t"), epochs={"platform": 0, "t_acme": 1})
    clock = FakeClock()
    silent = FakePubSub([], fail=False)
    silent.clock = clock
    redis = FakeRedis([silent, FakePubSub([], fail=False)])
    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(registry.listen, redis, "ns:ch:calibrators_changed", clock=clock))  # type: ignore[arg-type]
        await clock.settle()
        await clock.advance(14.0)
        await clock.settle()
        assert (silent.pings, silent.closed, redis.opened) == (2, False, 1)  # PINGs at 5 s and 10 s
        await clock.advance(1.0)  # 15 s without a reply: closed, resubscribed after the 1 s backoff
        await clock.settle()
        await clock.advance(1.0)
        await clock.settle()
        assert (silent.closed, redis.opened) == (True, 2)
        tg.cancel_scope.cancel()
