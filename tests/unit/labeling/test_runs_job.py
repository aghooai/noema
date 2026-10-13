"""The `labeling.run` job (ADR-003 §1, §3; 06 §2.3): aggregates from items, closing, and fitting + the gate on
synthetic labels with known calibration, so both acceptance and rejection run end to end on the fake world; runs
closed early are never fitted, the same labels never mint a second artifact, a decision counts once, and the fit
runs off the event loop over a window that stops paging at its start; the span, the fit counter and the uniform
label class shares are emitted."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

import pytest
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from
from tests.fakes.db_repos import FakeLabelRepo
from tests.fakes.world import build_world
from tests.unit.labeling.jobkit import (
    SIG,
    SOL,
    SONNET,
    VIEW,
    ScriptedAdapter,
    ScriptedRegistry,
    agree_on,
    deps,
    dropped_row,
    item_rows,
    run_row,
)
from tests.unit.labeling.synth import labeled_set

from noema.core.protocols.labeling import CalibratorBindingKey
from noema.core.protocols.storage import Job, PermanentJobError
from noema.labeling import runs as runs_mod
from noema.labeling.evaluation import fit_and_evaluate
from noema.labeling.runs import RunJob, calibrator_id

pytestmark = pytest.mark.unit
FULL = ("m1", "m2", "m3")


def world_with(*rows: tuple[str, dict[str, Any]]) -> tuple[Any, RunJob]:
    world = build_world()
    for table, row in rows:
        world.db.store.seed(table, row)
    return world, RunJob(deps(world, ScriptedRegistry(ScriptedAdapter(agree_on({})))))


def run_job(run_id: str = "lrun_0001") -> Job:
    return Job("job_r", "t_acme", "labeling.run", {"run_id": run_id}, 1)


def seeded(
    n: int, *, members: tuple[str, ...] = FULL, age_days: float = 1.0, patch: dict[str, Any] | None = None, **kw: Any
) -> tuple[Any, RunJob]:
    ls = labeled_set(n, members=members, **kw)
    world = build_world(config=FakeConfigRegistry(snapshot_from(patched_example(patch or {}))))
    started = world.clock.now() - timedelta(days=age_days)
    rows = [("labeling_run", run_row("lrun_0001", started))] + [("label_item", r) for r in item_rows(ls, "lrun_0001")]
    rows += [("label_item", dropped_row("lrun_0001", i)) for i in range(3)]
    for table, row in rows:
        world.db.store.seed(table, row)
    return world, RunJob(deps(world, ScriptedRegistry(ScriptedAdapter(agree_on({})))))


async def test_aggregates_are_recomputed_and_a_young_small_run_stays_open() -> None:
    world, handler = seeded(40, temp=0.4, seed=1)
    await handler(run_job())
    [run] = world.db.store.rows("labeling_run")
    assert run["status"] == "running" and run["completed_at"] is None
    assert run["counts"] == {
        "sampled": 43,
        "sampled_uniform": 40,
        "sampled_uncertainty": 3,
        "kept": 40,
        "dropped_disagreed": 3,
        "dropped_refused": 0,
        "dropped_parse_error": 0,
        "dropped_labeler_error": 0,
    }
    assert run["consensus_rate"] == pytest.approx(40 / 43, abs=1e-6)
    assert sum(run["n_by_class"].values()) == 40 and set(run["n_by_class"]) == {"opt0", "opt1", "opt2", "opt3"}
    assert run["usage"]["input_tokens"] == 40 * 2 * 10 and run["usage"]["cost_usd"] == pytest.approx(0.08)
    assert world.db.store.rows("calibrator") == []
    assert world.telemetry.counter("aghoo_label_consensus_ratio", question="intent") > 0
    shares = {m.labels["class"]: m.value for m in world.telemetry.metrics if m.name == "aghoo_label_class_share"}
    assert shares == {
        c: pytest.approx(run["n_by_class"].get(c, 0) / 40, abs=1e-6) for c in ("opt0", "opt1", "opt2", "opt3")
    }
    assert {m.labels["profile"] for m in world.telemetry.metrics if m.name == "aghoo_label_class_share"} == {
        "router-default"
    }
    [span] = [s for s in world.telemetry.spans if s.name == "labeling.run"]
    assert (
        span.ended
        and span.error is None
        and span.attributes
        == {
            "aghoo.tenant": "t_acme",
            "aghoo.profile": "router-default",
            "aghoo.profile_version": "1",
            "aghoo.member.question": "intent",
        }
    )


async def test_the_class_share_reads_the_uniform_slice_only() -> None:
    world, handler = world_with(("labeling_run", run_row("lrun_0001", None)))
    ls = labeled_set(20, members=FULL, temp=0.4, seed=3)
    for row in item_rows(ls, "lrun_0001", reason="uncertainty"):
        world.db.store.seed("label_item", row)  # kept, but from the uncertainty slice
    await handler(run_job())
    assert [m for m in world.telemetry.metrics if m.name == "aghoo_label_class_share"] == []


async def test_a_full_correctable_run_closes_and_is_accepted() -> None:
    world, handler = seeded(4000, temp=0.4, seed=21)
    await handler(run_job())
    [run] = world.db.store.rows("labeling_run")
    assert run["status"] == "completed" and run["completed_at"] == world.clock.now()
    [cal] = world.db.store.rows("calibrator")
    newest = max(r["id"] for r in world.db.store.rows("label_item") if r["kept"] and r["sample_reason"] == "uniform")
    key = CalibratorBindingKey("router-default", "1", SIG, FULL, VIEW)
    assert cal["id"] == calibrator_id("t_acme", key, newest) and cal["status"] == "accepted"
    assert cal["acceptance"] == {"passed": True, "failed_rules": []} and cal["accepted_at"] == world.clock.now()
    assert cal["scope"] == "tenant" and cal["tenant_id"] == "t_acme" and cal["method"] == "temperature"
    assert (cal["profile_id"], cal["profile_version"], cal["question_signature"], cal["member_subset"]) == (
        "router-default",
        "1",
        SIG,
        list(FULL),
    )
    assert set(cal["member_t"]) == set(FULL) and all(2.0 < t < 3.0 for t in cal["member_t"].values())
    h = cal["holdout"]
    assert h["dataset_id"] == "lrun_0001" and h["labelers"] == [SOL, SONNET] and h["label_source"] == "llm_consensus"
    assert h["ece"] <= 0.05 and h["nll"] <= h["nll_uncalibrated"] and h["n"] >= 2000
    assert len(cal["reliability"]["bins"]) == 15 and set(cal["reliability"]["ci"]) == {"ece", "brier", "nll"}
    assert world.telemetry.counter("aghoo_calibration_ece", view_signature=VIEW) == h["ece"]
    assert world.telemetry.counter("aghoo_calibrator_fits_total", status="accepted") == 1
    assert world.telemetry.counter("aghoo_calibrator_fits_total", status="rejected") == 0


async def test_confidence_anticorrelated_with_correctness_is_rejected() -> None:
    world, handler = seeded(1300, inverted=True, seed=2)
    await handler(run_job())
    [cal] = world.db.store.rows("calibrator")
    assert cal["status"] == "rejected" and cal.get("accepted_at") is None
    assert "ece_max" in cal["acceptance"]["failed_rules"] and cal["acceptance"]["passed"] is False
    assert world.telemetry.counter("aghoo_calibrator_fits_total", status="rejected") == 1


async def test_fitting_is_idempotent_per_run_and_binding() -> None:
    world, handler = seeded(1300, temp=0.4, seed=5)
    await handler(run_job())
    await handler(run_job())
    assert len(world.db.store.rows("calibrator")) == 1


async def test_a_rare_partial_subset_fails_a8_while_the_full_set_passes() -> None:
    world, handler = seeded(4000, temp=0.4, seed=21, patch={"labeling": {"acceptance": {"subset_min_share": 0.2}}})
    rare = labeled_set(700, members=("m1", "m2"), temp=0.4, seed=8)
    for row in item_rows(rare, "lrun_0001", tag="r"):
        world.db.store.seed("label_item", row)
    await handler(run_job())
    by_subset = {tuple(c["member_subset"]): c for c in world.db.store.rows("calibrator")}
    assert by_subset[FULL]["status"] == "accepted"
    partial = by_subset[("m1", "m2")]
    assert (
        partial["status"] == "rejected" and "member_subset_share" in partial["acceptance"]["failed_rules"]
    )  # 700/4700 < 20%


async def test_an_old_run_closes_without_enough_labels_and_nothing_is_fitted() -> None:
    world, handler = seeded(100, temp=0.4, seed=1, age_days=31)
    await handler(run_job())
    assert world.db.store.rows("labeling_run")[0]["status"] == "completed"
    assert world.db.store.rows("calibrator") == []


async def test_a_retired_profile_version_is_not_fitted() -> None:
    world, handler = seeded(1300, temp=0.4, seed=1)
    world.db.store.seed(
        "labeling_run", {**world.db.store.rows("labeling_run")[0], "profile_version": "2", "status": "completed"}
    )
    await handler(run_job())
    assert world.db.store.rows("calibrator") == []


async def test_unknown_or_malformed_runs() -> None:
    world, handler = world_with()
    await handler(run_job("lrun_missing"))
    with pytest.raises(PermanentJobError):
        await handler(Job("j", "t_acme", "labeling.run", {"run_id": 3}, 1))
    assert world.db.store.rows("calibrator") == []


def completed(run_id: str, started: Any, done: Any) -> dict[str, Any]:
    return {**run_row(run_id, started, status="completed"), "completed_at": done}


async def test_runs_closed_below_their_limits_are_refreshed_but_never_fitted() -> None:
    """One due run plus ten one-item runs closed early (a labeler-set change, or parked by the pre-0002 rotation)."""
    world, handler = world_with()
    now = world.clock.now()
    world.db.store.seed("labeling_run", completed("lrun_0000", now - timedelta(days=10), now - timedelta(days=9)))
    for r in item_rows(labeled_set(1300, members=FULL, temp=0.4, seed=5), "lrun_0000", tag="b"):
        world.db.store.seed("label_item", {**r, "created_at": now - timedelta(days=9)})
    tiny = item_rows(labeled_set(10, members=FULL, temp=0.4, seed=6), "lrun_x", tag="c")
    for i in range(10):
        rid = f"lrun_{i + 1:04d}"
        world.db.store.seed("labeling_run", completed(rid, now - timedelta(days=1), now))
        world.db.store.seed("label_item", {**tiny[i], "id": f"lab_c{i:08d}", "run_id": rid, "created_at": now})
    for i in range(10):
        await handler(run_job(f"lrun_{i + 1:04d}"))
    assert world.db.store.rows("calibrator") == []  # (before: one near-duplicate artifact per one-item run)
    assert all(r["counts"]["sampled"] == 1 for r in world.db.store.rows("labeling_run") if r["id"] != "lrun_0000")
    await handler(run_job("lrun_0000"))
    [cal] = world.db.store.rows("calibrator")
    assert cal["holdout"]["n"] >= 600  # the due run's fit also reads the closed runs' labels (same definition)


async def test_the_same_labels_never_mint_a_second_artifact() -> None:
    world, handler = seeded(1300, temp=0.4, seed=5)
    await handler(run_job())
    now = world.clock.now()
    world.db.store.seed("labeling_run", completed("lrun_0000", now - timedelta(days=31), now))  # age-due, no items
    await handler(run_job("lrun_0000"))  # its window holds exactly lrun_0001's labels
    assert len(world.db.store.rows("calibrator")) == 1


async def test_a_decision_stored_in_two_runs_counts_once() -> None:
    world, handler = seeded(1300, temp=0.4, seed=5)
    twin, _ = seeded(1300, temp=0.4, seed=5)
    now = twin.clock.now()
    twin.db.store.seed("labeling_run", completed("lrun_0002", now - timedelta(hours=1), now))
    for r in item_rows(labeled_set(1300, members=FULL, temp=0.4, seed=5), "lrun_0001")[:400]:
        twin.db.store.seed("label_item", {**r, "id": "lab_z" + r["id"][5:], "run_id": "lrun_0002"})  # a retry's copy
    await handler(run_job())
    await RunJob(deps(twin, ScriptedRegistry(ScriptedAdapter(agree_on({})))))(run_job())
    [one], [other] = world.db.store.rows("calibrator"), twin.db.store.rows("calibrator")
    assert one["holdout"]["n"] == other["holdout"]["n"] and one["member_t"] == other["member_t"]


async def test_the_fit_runs_in_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[bool] = []
    real = fit_and_evaluate

    def spy(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread() is threading.main_thread())
        return real(*args, **kwargs)

    monkeypatch.setattr(runs_mod, "fit_and_evaluate", spy)
    world, handler = seeded(1300, temp=0.4, seed=5)
    await handler(run_job())
    assert threads == [False] and len(world.db.store.rows("calibrator")) == 1


async def test_the_fit_window_stops_paging_at_runs_older_than_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    read: list[str] = []
    real = FakeLabelRepo.items

    async def spy(self: Any, run_id: str, *, kept: bool | None = None) -> Any:
        read.append(run_id)
        return await real(self, run_id, kept=kept)

    world, handler = seeded(1300, temp=0.4, seed=5)
    now = world.clock.now()
    world.db.store.seed("labeling_run", completed("lrun_0000", now - timedelta(days=200), now - timedelta(days=190)))
    monkeypatch.setattr(FakeLabelRepo, "items", spy)
    await handler(run_job())
    assert "lrun_0000" not in read and len(world.db.store.rows("calibrator")) == 1


async def test_an_item_committed_while_the_run_job_runs_is_in_its_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `labeling.run` refresh of an item committed after this job read the items is deduped against this
    running job (one live job per run), so the job re-reads the items in the transaction that writes the aggregate
    (Wave E fix: the threshold-crossing item could wait for the next item or RUN_MAX_AGE)."""
    world, handler = seeded(40, temp=0.4, seed=1)
    real = FakeLabelRepo.items
    reads = []

    async def items(self: Any, run_id: str) -> Any:
        out = await real(self, run_id)
        reads.append(len(out))
        if len(reads) == 1:  # another worker's labeling.item commits right after this read
            world.db.store.seed("label_item", dropped_row("lrun_0001", 99))
        return out

    monkeypatch.setattr(FakeLabelRepo, "items", items)
    await handler(run_job())
    [run] = world.db.store.rows("labeling_run")
    assert reads[:2] == [43, 44]
    assert run["counts"]["sampled"] == 44 and run["counts"]["dropped_disagreed"] == 4
