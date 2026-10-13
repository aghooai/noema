"""Gate Q6 drift (ADR-003 §5; spec-10 §3): a weekly ECE of 0.0801 downgrades the artifact, 0.08 does not; a CI lower
bound above ece_max downgrades with drift_ci; platform artifacts are downgraded globally but are not judged on tenants
their own artifact serves; a thin week widens the slice up to four weeks, and a slice still too thin is reported, not
judged; a transient downgrade failure fails the job (retry) while a refused one is skipped; the downgrade bumps the
epoch so the registry stops serving the artifact."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from tests.fakes.world import build_world
from tests.unit.labeling.jobkit import SIG, VIEW, ScriptedAdapter, ScriptedRegistry, agree_on, deps, item_rows, run_row
from tests.unit.labeling.synth import labeled_set

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.labeling import CalibratorBindingKey
from noema.core.protocols.storage import Job
from noema.db.system import AdminError, AdminReason
from noema.labeling.dataset import Example, LabeledSet, MemberObs
from noema.labeling.deps import JOB_RUN
from noema.labeling.drift import DriftJob, verdict
from noema.labeling.registry import DbCalibratorRegistry

pytestmark = pytest.mark.unit
DRIFT = Job("job_d", "__platform__", "labeling.drift", {}, 1)
SIG2 = "sha256:" + "7" * 64


def test_verdict_boundaries_read_the_config_thresholds() -> None:
    from tests.fakes.config import example_snapshot

    acc = example_snapshot().labeling.acceptance
    assert (acc.drift_ece_max, acc.ece_max) == (0.08, 0.05)
    assert verdict(0.0801, 0.0, acc) == "drift_ece"
    assert verdict(0.08, 0.05, acc) is None
    assert verdict(0.06, 0.0501, acc) == "drift_ci"
    assert verdict(0.0, 0.0, acc) is None


def constant_slice(n: int, conf: float, positives_per_ten: int) -> LabeledSet:
    """P(true) = conf for every item; each run of 10 consecutive items has `positives_per_ten` trues, so every
    equal-mass bin (10 items at n = 150) observes positives_per_ten / 10 and ECE = |conf - that|."""
    from tests.unit.labeling.synth import T0

    examples = [
        Example(
            {"m1": MemberObs({"true": conf, "false": 1 - conf})},
            {"m1": 1.0},
            "true" if i % 10 < positives_per_ten else "false",
            T0 + timedelta(minutes=i),
        )
        for i in range(n)
    ]
    return LabeledSet("predicate", ("true", "false"), ("m1",), tuple(examples))


def calibrator(
    cid: str,
    *,
    scope: str = "tenant",
    subset: tuple[str, ...] = ("m1",),
    method: str = "platt",
    pooled: dict[str, float] | None = None,
    member_t: dict[str, float] | None = None,
) -> dict[str, Any]:
    return {
        "id": cid,
        "scope": scope,
        "tenant_id": "t_acme" if scope == "tenant" else None,
        "profile_id": "router-default",
        "profile_version": "1",
        "question_name": "intent",
        "question_signature": SIG,
        "member_subset": list(subset),
        "input_view_signature": VIEW,
        "choice_method": "flat",
        "method": method,
        "member_t": member_t or dict.fromkeys(subset, 1.0),
        "pooled": pooled or {"a": 1.0, "b": 0.0},
        "epsilon": 1e-4,
        "holdout": {"n": 300},
        "acceptance": {},
        "reliability": {},
        "status": "active",
    }


def world_with(ls: LabeledSet, *cals: dict[str, Any], tenants: tuple[str, ...] = ("t_acme",)) -> Any:
    world = build_world()
    # the slice was labeled during the last week
    shift = world.clock.now() - timedelta(days=3) - ls.examples[0].at
    shifted = LabeledSet(
        ls.kind,
        ls.options,
        ls.member_ids,
        tuple(Example(e.members, e.weights, e.label, e.at + shift) for e in ls.examples),
    )
    world.db.store.seed("labeling_run", run_row("lrun_0001", world.clock.now() - timedelta(days=5)))
    for row in item_rows(shifted, "lrun_0001"):
        world.db.store.seed("label_item", row)
    for cal in cals:
        world.db.store.seed("calibrator", cal)
    world.db.store.seed("calibrator_epoch", {"scope_key": "platform", "n": 1})
    for t in tenants:
        world.db.store.seed("calibrator_epoch", {"scope_key": t, "n": 1})
    return world


def handler(world: Any) -> DriftJob:
    return DriftJob(deps(world, ScriptedRegistry(ScriptedAdapter(agree_on({})))))


async def test_weekly_ece_0_0801_downgrades_and_bumps_the_epoch() -> None:
    world = world_with(constant_slice(150, 0.5801, 5), calibrator("cal_t"))
    registry = DbCalibratorRegistry(world.db)
    await registry.refresh()
    key = CalibratorBindingKey("router-default", "1", SIG, ("m1",), VIEW)
    assert registry.lookup("t_acme", key) is not None and registry.epoch("t_acme") == "1.1"
    await handler(world)(DRIFT)
    [cal] = world.db.store.rows("calibrator")
    assert cal["status"] == "downgraded" and cal["downgrade_reason"] == "drift_ece"
    await registry.refresh()
    assert registry.lookup("t_acme", key) is None and registry.epoch("t_acme") == "1.2"
    assert world.telemetry.counter("aghoo_calibration_ece", view_signature=VIEW) == pytest.approx(0.0801)
    assert world.telemetry.counter("aghoo_calibrator_downgrades_total", reason="drift_ece") == 1
    [span] = [s for s in world.telemetry.spans if s.name == "labeling.drift"]
    assert (
        span.ended
        and span.error is None
        and span.attributes
        == {
            "aghoo.profile": "router-default",
            "aghoo.profile_version": "1",
            "aghoo.member.question": "intent",
            "aghoo.tenant": "t_acme",
        }
    )


async def test_a_calibrated_weekly_slice_keeps_the_artifact() -> None:
    world = world_with(constant_slice(150, 0.5, 5), calibrator("cal_t"))
    await handler(world)(DRIFT)
    assert world.db.store.rows("calibrator")[0]["status"] == "active"


async def test_the_ci_rule_downgrades_a_consistently_miscalibrated_slice() -> None:
    world = world_with(constant_slice(4000, 0.67, 6), calibrator("cal_t"))  # ECE 0.07 <= 0.08, CI low > 0.05
    await handler(world)(DRIFT)
    [cal] = world.db.store.rows("calibrator")
    assert cal["status"] == "downgraded" and cal["downgrade_reason"] == "drift_ci"
    assert world.telemetry.counter("aghoo_calibrator_downgrades_total", reason="drift_ci") == 1
    assert world.telemetry.counter("aghoo_calibrator_downgrades_total", reason="drift_ece") == 0


async def test_a_thin_or_non_matching_slice_gives_no_verdict(caplog: pytest.LogCaptureFixture) -> None:
    thin = world_with(constant_slice(99, 0.9, 5), calibrator("cal_t"))
    with caplog.at_level("WARNING", logger="noema.labeling.drift"):
        await handler(thin)(DRIFT)
    [rec] = [r for r in caplog.records if r.getMessage().startswith("calibrator drift not evaluated")]
    assert rec.__dict__["n"] == 99 and rec.__dict__["min_n"] == 100
    other_subset = world_with(constant_slice(150, 0.9, 5), calibrator("cal_t", subset=("m1", "m2")))
    await handler(other_subset)(DRIFT)
    assert (
        thin.db.store.rows("calibrator")[0]["status"]
        == other_subset.db.store.rows("calibrator")[0]["status"]
        == "active"
    )


def age(world: Any, days: float) -> None:
    for row in world.db.store.rows("label_item"):
        world.db.store.seed("label_item", {**row, "created_at": world.clock.now() - timedelta(days=days)})


async def test_a_thin_week_widens_the_slice_up_to_four_weeks(caplog: pytest.LogCaptureFixture) -> None:
    world = world_with(constant_slice(150, 0.9, 5), calibrator("cal_t"))
    age(world, 8)  # nothing in the last week: the two-week slice holds the 150 labels
    with caplog.at_level("WARNING", logger="noema.labeling.drift"):
        await handler(world)(DRIFT)
    assert world.db.store.rows("calibrator")[0]["status"] == "downgraded"
    [rec] = [r for r in caplog.records if r.getMessage() == "calibrator downgraded by drift"]
    assert (rec.__dict__["n"], rec.__dict__["window_days"]) == (150, 14)


async def test_labels_older_than_four_weeks_are_not_evaluated_and_say_so(caplog: pytest.LogCaptureFixture) -> None:
    world = world_with(constant_slice(150, 0.9, 5), calibrator("cal_t"))
    age(world, 29)
    with caplog.at_level("WARNING", logger="noema.labeling.drift"):
        await handler(world)(DRIFT)
    assert world.db.store.rows("calibrator")[0]["status"] == "active"
    [rec] = [r for r in caplog.records if r.getMessage().startswith("calibrator drift not evaluated")]
    assert (rec.__dict__["calibrator_id"], rec.__dict__["n"], rec.__dict__["window_days"]) == ("cal_t", 0, 28)
    assert world.telemetry.counter("aghoo_calibration_ece", view_signature=VIEW) == 0


async def test_the_narrowest_sufficient_slice_is_judged() -> None:
    world = world_with(constant_slice(150, 0.5, 5), calibrator("cal_t"))  # calibrated this week
    stale = item_rows(constant_slice(150, 0.9, 5), "lrun_0001", tag="old")  # miscalibrated ten days ago
    for row in stale:
        world.db.store.seed("label_item", {**row, "created_at": world.clock.now() - timedelta(days=10)})
    await handler(world)(DRIFT)
    assert world.db.store.rows("calibrator")[0]["status"] == "active"


async def test_a_platform_artifact_is_downgraded_globally() -> None:
    world = world_with(constant_slice(150, 0.9, 5), calibrator("cal_p", scope="platform"), tenants=("t_acme", "t_beta"))
    await handler(world)(DRIFT)
    [cal] = world.db.store.rows("calibrator")
    assert cal["status"] == "downgraded"
    assert {r["scope_key"]: r["n"] for r in world.db.store.rows("calibrator_epoch")}["platform"] == 2
    [span] = [s for s in world.telemetry.spans if s.name == "labeling.drift"]
    assert "aghoo.tenant" not in span.attributes  # a platform artifact belongs to no tenant


async def test_the_slice_is_one_kept_items_read_and_counts_a_decision_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.fakes.db_repos import FakeLabelRepo

    world = world_with(constant_slice(150, 0.5801, 5), calibrator("cal_t"))
    world.db.store.seed("labeling_run", run_row("lrun_0002", world.clock.now() - timedelta(days=2)))
    for row in item_rows(constant_slice(150, 0.5, 5), "lrun_0002", tag="z"):  # a retry's copies of the decisions
        stored = {**row, "decision_id": row["decision_id"].replace("dec_z", "dec_a"), "id": "lab_z" + row["id"][5:]}
        world.db.store.seed("label_item", {**stored, "created_at": world.clock.now()})

    async def no_run_reads(self: Any, run_id: str, *, kept: bool | None = None) -> Any:
        raise AssertionError("the drift slice reads kept_items, not the runs' items")

    monkeypatch.setattr(FakeLabelRepo, "items", no_run_reads)
    with caplog.at_level("WARNING", logger="noema.labeling.drift"):
        await handler(world)(DRIFT)
    [rec] = [r for r in caplog.records if r.getMessage() == "calibrator downgraded by drift"]
    assert rec.__dict__["n"] == 150  # the earliest item of each decision; the copies are not counted again


async def test_a_failed_check_marks_its_span() -> None:
    world = world_with(constant_slice(150, 0.5801, 5), calibrator("cal_t"))
    with pytest.raises(NoemaError):
        await failing_handler(world, NoemaError(ErrorCode.INTERNAL_ERROR, "connection lost"), {"cal_t"})(DRIFT)
    [span] = [s for s in world.telemetry.spans if s.name == "labeling.drift"]
    assert span.ended and span.error == "internal_error"
    assert world.telemetry.counter("aghoo_calibrator_downgrades_total") == 0


async def test_the_artifact_is_applied_as_the_engine_applies_it() -> None:
    ls = labeled_set(600, kind="predicate", members=("m1", "m2"), temp=0.4, seed=4)
    good = calibrator("cal_t", subset=("m1", "m2"), member_t={"m1": 2.5, "m2": 2.5})
    world = world_with(ls, good)
    await handler(world)(DRIFT)
    assert world.db.store.rows("calibrator")[0]["status"] == "active"  # the right temperatures fix the slice
    raw = world_with(ls, calibrator("cal_t", subset=("m1", "m2")))
    await handler(raw)(DRIFT)
    assert raw.db.store.rows("calibrator")[0]["status"] == "downgraded"  # identity leaves it over-confident


async def test_stale_running_runs_are_scheduled_for_closing() -> None:
    world = world_with(constant_slice(10, 0.5, 5))
    world.db.store.seed("labeling_run", run_row("lrun_0002", world.clock.now() - timedelta(days=31)))
    await handler(world)(DRIFT)
    assert [j["payload"]["run_id"] for j in world.db.store.rows("job") if j["kind"] == JOB_RUN] == ["lrun_0002"]


class _FailingCalls:
    """SystemCalls whose `set_calibrator_status` raises `error` for the artifact ids in `ids`."""

    def __init__(self, inner: Any, error: Exception, ids: set[str]) -> None:
        self._inner, self._error, self._ids = inner, error, ids

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def set_calibrator_status(self, actor: str, calibrator_id: str, status: str, *, reason: str | None) -> None:
        if calibrator_id in self._ids:
            raise self._error
        await self._inner.set_calibrator_status(actor, calibrator_id, status, reason=reason)


class _FailingDb:
    def __init__(self, inner: Any, error: Exception, ids: set[str]) -> None:
        self._inner, self._error, self._ids = inner, error, ids

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @asynccontextmanager
    async def system_uow(self) -> AsyncIterator[Any]:
        async with self._inner.system_uow() as calls:
            yield _FailingCalls(calls, self._error, self._ids)


def failing_handler(world: Any, error: Exception, ids: set[str]) -> DriftJob:
    d = deps(world, ScriptedRegistry(ScriptedAdapter(agree_on({}))))
    return DriftJob(replace(d, db=_FailingDb(world.db, error, ids)))


async def test_a_transient_failure_to_downgrade_fails_the_job_after_checking_the_others() -> None:
    other = {**calibrator("cal_u"), "question_signature": SIG2}
    world = world_with(constant_slice(150, 0.5801, 5), calibrator("cal_t"), other)
    for row in item_rows(constant_slice(150, 0.5801, 5), "lrun_0002", tag="u"):
        world.db.store.seed("label_item", {**row, "created_at": world.clock.now() - timedelta(days=2)})
    world.db.store.seed(
        "labeling_run", {**run_row("lrun_0002", world.clock.now() - timedelta(days=4)), "question_signature": SIG2}
    )
    job = failing_handler(world, NoemaError(ErrorCode.INTERNAL_ERROR, "connection lost"), {"cal_t"})
    with pytest.raises(NoemaError):
        await job(DRIFT)  # the queue retries the job: the drifted artifact is not silently left active
    status = {c["id"]: c["status"] for c in world.db.store.rows("calibrator")}
    assert status == {"cal_t": "active", "cal_u": "downgraded"}
    await handler(world)(DRIFT)  # the retry
    assert {c["id"]: c["status"] for c in world.db.store.rows("calibrator")}["cal_t"] == "downgraded"


@pytest.mark.parametrize("reason", ["forbidden", "not_found", "invalid_state"])
async def test_a_refused_downgrade_is_skipped(reason: AdminReason) -> None:
    world = world_with(constant_slice(150, 0.5801, 5), calibrator("cal_t"))
    await failing_handler(world, AdminError(reason, "moved by someone else"), {"cal_t"})(DRIFT)  # returns


async def test_an_invalid_argument_refusal_is_not_swallowed() -> None:
    world = world_with(constant_slice(150, 0.5801, 5), calibrator("cal_t"))
    with pytest.raises(AdminError):
        await failing_handler(world, AdminError("invalid_argument", "bad reason"), {"cal_t"})(DRIFT)


async def test_a_platform_artifact_is_not_judged_where_a_tenant_artifact_serves() -> None:
    fixes = calibrator("cal_t", pooled={"a": 0.0, "b": 0.0})  # P(true) = 0.5 everywhere: calibrated on the slice
    world = world_with(
        constant_slice(150, 0.9, 5), fixes, calibrator("cal_p", scope="platform"), tenants=("t_acme", "t_beta")
    )
    await handler(world)(DRIFT)
    assert {c["id"]: c["status"] for c in world.db.store.rows("calibrator")} == {"cal_t": "active", "cal_p": "active"}


async def test_a_platform_artifact_is_judged_once_the_tenant_artifact_is_downgraded() -> None:
    world = world_with(
        constant_slice(150, 0.9, 5),
        calibrator("cal_t"),
        calibrator("cal_p", scope="platform"),
        tenants=("t_acme", "t_beta"),
    )
    await handler(world)(DRIFT)
    assert {c["id"]: c["status"] for c in world.db.store.rows("calibrator")} == {
        "cal_t": "downgraded",
        "cal_p": "downgraded",
    }
