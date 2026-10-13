"""The generation events of every golden SSE fixture (spec-04 §4.2) from the real engine: the fixture's scenario
on the fake adapters, the documented commit / cancel / restart controls (`CONTROLS` of the fixture suite), and the
exact (time, type, attempt, reason|text|code) sequence of the fixture's generation.* and error events."""

from __future__ import annotations

from typing import Any

import anyio
import orjson
import pytest
from tests.contract_internal.kits import principal
from tests.contract_internal.test_golden_fixtures import CONTROLS, GEN_TYPES, USER_TEXT_FOR_ENGINE
from tests.fakes.clock import FakeClock
from tests.fakes.generation import StaticPromptBuilder
from tests.fakes.ops_admission import CountingReservation
from tests.fakes.world import build_world
from tests.golden.compare import Normalizer, fixture_paths, load_fixture
from tests.unit.generation.conformance_impls import real_engine

from noema.contracts.validate import ContractValidator
from noema.core.clock import Deadline
from noema.core.protocols.generation import AttemptBudget, AttemptRecord, AttemptSpec, TurnContext

FIXTURE_IDS = sorted(fixture_paths())


async def engine_events(
    fx: dict[str, Any],
) -> tuple[list[tuple[float, dict[str, Any]]], CountingReservation, list[AttemptRecord]]:
    profile, effort, max_tokens, hold, commit_at, arm_at, cancel, restart = CONTROLS[fx["id"]]
    clock = FakeClock()
    world = build_world(clock=clock, scenario=fx["scenario"])
    engine = real_engine(world)
    snap = world.config.current()
    reservation = CountingReservation()
    ctx = TurnContext(
        principal(),
        snap.tenant_ref("t_acme"),
        snap,
        Deadline(clock.monotonic() + 120, clock),
        reservation,
        "turn_1",
        "ses_1",
        "0" * 32,
    )
    budget = AttemptBudget()
    out: list[tuple[float, dict[str, Any]]] = []
    records: list[AttemptRecord] = []

    async def run_one(spec: AttemptSpec, *, held: bool, speculative: bool, start_at: float = 0) -> None:
        await clock.sleep(start_at / 1000)
        builder = StaticPromptBuilder(USER_TEXT_FOR_ENGINE)
        async with (
            engine.run(ctx, spec, builder, hold=held, speculative=speculative, failure_budget=budget) as run,
            anyio.create_task_group() as tg,
        ):

            async def read() -> None:
                out.extend([(round(clock.elapsed_ms(), 3), dict(ev)) async for ev in run.events()])

            tg.start_soon(read)
            if held:
                actions = [(commit_at, "commit"), (arm_at, "commit"), (cancel[0] if cancel else None, "cancel")]
                for at, action in sorted((t, a) for t, a in actions if t is not None):
                    await clock.sleep(max(0.0, at / 1000 - clock.elapsed_ms() / 1000))
                    if action == "commit":
                        await run.commit("holdback_deadline" if fx["id"].startswith("G03") else "spec_agrees")
                    else:
                        await run.cancel(cancel[1])
            records.extend(await run.result())

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(lambda: run_one(AttemptSpec(profile, effort, max_tokens, "none"), held=hold, speculative=True))
        if restart is not None:
            r_profile, r_effort, r_max, r_at = restart
            spec2 = AttemptSpec(r_profile, r_effort, r_max, "none")
            tg.start_soon(lambda: run_one(spec2, held=False, speculative=False, start_at=r_at))
    return sorted(out, key=lambda item: item[0]), reservation, sorted(records, key=lambda r: r.started_at)


def summary(at: float, ev: dict[str, Any], norm: Normalizer) -> tuple[Any, ...]:
    return (at, ev["type"], norm(ev.get("attempt_id")), ev.get("reason") or ev.get("text") or ev.get("code"))


@pytest.mark.parametrize("fixture_id", FIXTURE_IDS)
async def test_generation_events_match_the_fixture(fixture_id: str) -> None:
    fx = load_fixture(fixture_id)
    norm_want = Normalizer()
    want = [summary(e["at"], e["data"], norm_want) for e in fx["events"] if e["type"] in GEN_TYPES]
    events, reservation, _ = await engine_events(fx)
    norm = Normalizer()
    assert [summary(at, ev, norm) for at, ev in events] == want
    started = [ev for _, ev in events if ev["type"] == "generation.started"]
    assert len(reservation.consumed) == len(started)  # one Reservation.consume per attempt
    validator = ContractValidator()
    for i, (_, ev) in enumerate(events, start=1):  # every payload is a valid openapi TurnEvent
        validator.check_sse_frame("createTurn", str(i), ev["type"], orjson.dumps(ev))


@pytest.mark.parametrize("fixture_id", FIXTURE_IDS)
async def test_attempt_records_match_the_fixture_db(fixture_id: str) -> None:
    """The fixture's `db.attempts` rows (outcome, cancel_reason, finish_reason, line, estimated usage) from the
    real engine's records; ids normalized in start order."""
    fx = load_fixture(fixture_id)
    expected = fx.get("db", {}).get("attempts") or []
    _, _, records = await engine_events(fx)
    if expected:
        assert len(records) == len(expected)
    for row, rec in zip(expected, records, strict=False):
        assert row["outcome"] == rec.outcome, (fixture_id, row)
        for key in ("cancel_reason", "finish_reason", "error_code"):
            if key in row:
                assert getattr(rec, key) == row[key], (fixture_id, key)
        if "line" in row:
            assert row["line"] == ("generation" if rec.outcome == "committed" else "waste")
        if row.get("usage.tokens_estimated"):
            assert rec.usage.tokens_estimated and rec.cost.estimated
