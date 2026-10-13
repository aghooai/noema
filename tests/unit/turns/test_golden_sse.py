"""G01-G12 (spec-04 §4.2, §13): the real orchestrator on the scenario-driven fakes reproduces every golden SSE
fixture: event order, the exact fields, the delta text runs, `decision.updated` at its FakeClock time, every frame
valid against the contract, the P-SSE-1 invariants, and the fixture's DB assertions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio
import orjson
import pytest
from tests.golden.compare import Normalizer, compare, fixture_paths, load_fixture, sse_invariants

from noema.contracts.validate import ContractValidator

from .harness import TurnHarness, make_harness, principal, running

pytestmark = pytest.mark.unit

FIXTURES = sorted(f for f in fixture_paths() if not f.startswith("G12"))  # G12/G12b: test_golden_attach.py


def _setup(fx: dict[str, Any], wrap: Callable[[TurnHarness], TurnHarness] | None = None) -> TurnHarness:
    setup = fx.get("setup") or {}
    hold = bool((setup.get("decision_engine") or {}).get("hold_until_cancel"))
    h = make_harness(scenario=fx["scenario"], hold_decisions_until_cancel=hold)
    if wrap is not None:
        h = wrap(h)
    session = setup.get("session")
    if session is not None:
        h.seed_session(session["id"], last_seq=session["last_seq"], sticky_spec=session.get("sticky_spec"))
    return h


async def run_fixture(
    fx: dict[str, Any], wrap: Callable[[TurnHarness], TurnHarness] | None = None
) -> tuple[TurnHarness, Any, list[dict[str, Any]]]:
    """Run one fixture's turn (`wrap` may swap engines); returns the harness, the runner and the cancel answers."""
    h = _setup(fx, wrap)
    answers: list[dict[str, Any]] = []
    cancel = (fx.get("setup") or {}).get("cancel")
    async with running(h):
        runner = await h.start(fx["request"]["body"])
        frames: list[bytes] = []

        async def consume() -> None:
            frames.extend([frame.wire async for frame in h.world.events.subscribe(runner.turn_id)])

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            if cancel is not None:
                await h.clock.sleep(max(0.0, cancel["at_ms"] / 1000 - _since_d0(h, runner)))
                answers.append(await h.service.cancel(principal(), runner.turn_id))
            await runner.done.wait()
            if cancel is not None:
                answers.append(await h.service.cancel(principal(), runner.turn_id))
        h.frames = frames  # type: ignore[attr-defined]
    return h, runner, answers


def _since_d0(h: TurnHarness, runner: Any) -> float:
    return h.clock.monotonic() - (runner.t_d0 if runner.t_d0 is not None else h.clock.monotonic())


def _get(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def check_db(h: TurnHarness, runner: Any, fx: dict[str, Any], norm: Normalizer) -> None:
    db = fx.get("db") or {}
    store = h.world.db.store
    turns = [r for r in store.rows("turn") if r["id"] == runner.turn_id]
    for path, want in (db.get("turn") or {}).items():
        assert turns, "the turn row exists"
        assert norm(_get(turns[0], path)) == want, f"turn.{path}"
    attempts = {norm(a["id"]): a for a in store.rows("attempt") if a["turn_id"] == runner.turn_id}
    lines = {norm(line.attempt_id): line for line in h.world.cost.lines if line.attempt_id}
    for want in db.get("attempts") or []:
        got = attempts[want["id"]]
        for key, value in want.items():
            if key == "line":
                assert lines[want["id"]].line == value
            elif key == "usage.tokens_estimated":
                assert got["usage"]["tokens_estimated"] is value
            elif key != "id":
                assert got[key] == value, f"attempt {want['id']}.{key}"
    session = db.get("session")
    if session is not None:
        row = next(r for r in store.rows("session") if r["id"] == "ses_1")
        for key, value in session.items():
            assert row[key] == value, f"session.{key}"
        assert row["active_turn_id"] is None and row["lease_expires_at"] is None
    for table, count in (db.get("rows") or {}).items():
        assert len(store.rows(table)) == count, table
    types = [e["type"] for e in h.events(runner.turn_id)]
    for absent in db.get("absent_events") or []:
        assert absent not in types


def _questions(profile: str, names: list[str] | None = None) -> dict[str, Any]:
    from tests.fakes.config import example_snapshot
    from tests.fakes.decision import platform_wire

    prof = example_snapshot().decision_profile(profile)
    wires = [platform_wire(q) for q in prof.questions if names is None or q.name in names]
    return {"questions": wires, "input": "x", "include": ["members"]}


ROUTER = _questions("router-default")
VERIFIER = _questions("answer-verifier", ["answers_question", "unsupported_claims"])


def assert_frames_valid(body: bytes, types: list[str]) -> None:
    """Every frame passes the ContractValidator (TurnEvent schema + result invariants); ids 1, 2, 3, ..."""
    validator = ContractValidator()
    parsed = validator.parse_sse(body)
    for frame_id, event, data in parsed:
        assert event is not None
        request = VERIFIER if event == "turn.verified" else ROUTER
        validator.check_sse_frame("createTurn", frame_id, event, data, decision_request=request)
    assert [int(i or 0) for i, _, _ in parsed] == list(range(1, len(parsed) + 1))
    assert [e for _, e, _ in parsed] == types


@pytest.mark.parametrize("fixture_id", FIXTURES)
async def test_golden_fixture(fixture_id: str) -> None:
    fx = load_fixture(fixture_id)
    h, runner, answers = await run_fixture(fx)
    timed = h.timed(runner)
    assert compare(timed, fx) == [], "\n".join(compare(timed, fx))
    payloads = [e for _, e in timed]
    assert sse_invariants(payloads) == []
    assert_frames_valid(b"".join(h.frames), [e["type"] for e in payloads])  # type: ignore[attr-defined]
    norm = Normalizer()
    norm([e for _, e in timed])
    check_db(h, runner, fx, norm)
    cancel = (fx.get("setup") or {}).get("cancel")
    if cancel is not None:
        assert norm(answers[0]) == cancel["response"]["body"]
        assert norm(answers[1]) == cancel["repeat_response"]["body"]
    assert orjson.dumps(runner.result.turn)  # the TurnObject payload is serializable


def invariants_ok(events: list[dict[str, Any]]) -> bool:
    """P-SSE-1 over one turn (for the variants of this test)."""
    return sse_invariants(events) == []
