"""The golden fixtures (spec-04 §4.2, spec-02 §4.2): committed files equal the builders' output; every SSE frame
passes the ContractValidator (TurnEvent schema, plus `result_invariants` for the decision payloads); the
P-SSE-1 invariants hold; the embedded scenarios load; the comparator accepts each fixture against itself; and the
wire goldens follow the reference mapping."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import orjson
import pytest
from tests.fakes.config import example_snapshot
from tests.fakes.scenario import load_scenario
from tests.golden.compare import compare, fixture_paths, load_fixture, sse_invariants
from tests.golden.regenerate import all_files
from tests.golden.wire import expected_wire, load_wire_goldens, request_from

from noema.contracts.validate import ContractValidator
from noema.core.values import Effort

FIXTURE_IDS = sorted(fixture_paths())
SPEC_IDS = {f"G{n:02d}" for n in range(1, 13)}


def questions(profile: str, names: list[str] | None = None) -> dict[str, Any]:
    from tests.fakes.decision import platform_wire

    prof = example_snapshot().decision_profile(profile)
    wires = [platform_wire(q) for q in prof.questions if names is None or q.name in names]
    return {"questions": wires, "input": "x", "include": ["members"]}


ROUTER = questions("router-default")
VERIFIER = questions("answer-verifier", ["answers_question", "unsupported_claims"])


def test_committed_files_are_up_to_date() -> None:
    stale = [str(p) for p, text in all_files().items() if not p.exists() or p.read_text() != text]
    assert stale == [], "run: PYTHONPATH=. python -m tests.golden.regenerate"


def test_every_spec_scenario_has_a_fixture() -> None:
    assert set(FIXTURE_IDS) >= SPEC_IDS
    assert {"G03b", "G04b", "G05b", "G10b", "G12b"} <= set(FIXTURE_IDS)


@pytest.mark.parametrize("fixture_id", FIXTURE_IDS)
def test_frames_pass_the_contract(fixture_id: str) -> None:
    fx = load_fixture(fixture_id)
    validator = ContractValidator()
    body = b""
    for ev in fx["events"]:
        payload = orjson.dumps(ev["data"])
        assert ev["data"]["type"] == ev["type"]
        request = VERIFIER if ev["type"] == "turn.verified" else ROUTER
        validator.check_sse_frame("createTurn", str(ev["id"]), ev["type"], payload, decision_request=request)
        body += b"id: %d\nevent: %s\ndata: %s\n\n" % (ev["id"], ev["type"].encode(), payload)
    parsed = validator.parse_sse(body)
    assert [int(i or 0) for i, _, _ in parsed] == list(range(1, len(fx["events"]) + 1))


@pytest.mark.parametrize("fixture_id", FIXTURE_IDS)
def test_sse_invariants_and_self_comparison(fixture_id: str) -> None:
    fx = load_fixture(fixture_id)
    events = [e["data"] for e in fx["events"]]
    assert sse_invariants(events) == []
    assert compare([(e["at"], e["data"]) for e in fx["events"]], fx) == []
    times = [e["at"] for e in fx["events"]]
    assert times == sorted(times), "events are in time order"
    load_scenario(fx["scenario"])


def _ev(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, **fields}


def _race_turn(*middle: dict[str, Any]) -> list[dict[str, Any]]:
    head = [_ev("turn.created"), _ev("preflight.completed"), _ev("route.selected", attempt_id="att_1")]
    starts = [_ev("generation.started", attempt_id=a) for a in ("att_1", "att_2")]
    return [*head, *starts, _ev("decision.completed"), *middle, _ev("turn.completed")]


def test_sse_invariants_count_a_race_restart_once() -> None:
    # orchestrator.md item 9: T9 cancels every live race member with spec_changed at once
    restart = [_ev("generation.cancelled", attempt_id=a, reason="spec_changed") for a in ("att_1", "att_2")]
    events = _race_turn(*restart, _ev("generation.started", attempt_id="att_3"))
    events[-1:-1] = [_ev("route.confirmed", attempt_id="att_3"), _ev("generation.committed", attempt_id="att_3")]
    events[-1:-1] = [_ev("generation.completed", attempt_id="att_3")]
    assert sse_invariants(events) == []
    twice = [*events[:6], restart[0], _ev("decision.updated"), restart[1], *events[8:]]
    assert "at most one spec_changed" in sse_invariants(twice)


def test_sse_invariants_exempt_every_attempt_the_deadline_stopped() -> None:
    # orchestrator.md item 9: under error{deadline_exceeded} every live race member ends with the turn's error
    events = _race_turn(_ev("error", code="deadline_exceeded"))
    assert sse_invariants(events) == []
    other = _race_turn(_ev("error", code="all_routes_unavailable"))
    assert sse_invariants(other) == ["att_1: needs exactly one terminal event"]


def test_sse_invariants_before_t_d0() -> None:
    # orchestrator.md item 13: a turn that ends before t_d0 owes no decision.completed
    events = [_ev("turn.created"), _ev("route.selected"), _ev("error", code="tenant_access_misconfigured")]
    events.append(_ev("turn.completed"))
    assert sse_invariants(events, reached_t_d0=False) == []
    assert "decision.completed exactly once" in sse_invariants(events)
    late = [*events[:2], _ev("decision.completed"), *events[2:]]
    assert "no decision.completed before t_d0" in sse_invariants(late, reached_t_d0=False)


def test_conditioning_golden_signature_is_over_the_exact_block() -> None:
    # T-COND-1 (spec-04 §5.2, IMPL-014; orchestrator.md item 5): sha256 of the template id line plus the block
    fx = orjson.loads((Path(__file__).parents[1] / "golden" / "conditioning" / "T-COND-1.json").read_bytes())
    digest = hashlib.sha256(f"{fx['template']}\n{fx['block']}".encode()).hexdigest()
    assert fx["signature"] == f"sha256:{digest}" and not fx["block"].endswith("\n")


def test_comparator_catches_drift() -> None:
    fx = load_fixture("G01")
    actual = [(e["at"], dict(e["data"])) for e in fx["events"]]
    swapped = [*actual[:4], actual[5], actual[4], *actual[6:]]
    assert compare(swapped, fx) != []
    wrong = [(at, {**ev, "reason": "gate"} if ev["type"] == "generation.committed" else ev) for at, ev in actual]
    assert any("reason" in p for p in compare(wrong, fx))
    late = [(at + (500 if ev["type"] == "decision.updated" else 0), ev) for at, ev in actual]
    assert any("decision.updated" in p for p in compare(late, fx))


def test_wire_goldens_follow_the_reference_mapping() -> None:
    snap = example_snapshot()
    goldens = load_wire_goldens()
    assert len(goldens) >= 10
    for g in goldens:
        req = request_from(g["request"])
        assert expected_wire(req, snap.model(req.model_alias), g["op"], "us-east-1") == g["expected"], g["name"]
        if g["model_alias"] in ("sol-6.1", "opus-5.5", "haiku-5.5", "luna-6") and "body" in g["expected"]:
            assert "temperature" not in g["expected"]["body"].get("inferenceConfig", {})  # never sent to them


# --- the fixtures' generation events are what the fake generation engine produces -------------------------------------
GEN_TYPES = (
    "generation.started",
    "generation.cancelled",
    "generation.committed",
    "generation.delta",
    "generation.completed",
    "error",
)
# fixture -> (profile, effort, max tokens, hold, commit_at | None, arm_at | None, cancel (at, reason) | None, restart)
CONTROLS: dict[str, tuple[str, Effort | None, int | None, bool, float | None, float | None, Any, Any]] = {
    "G01": ("sol-fast", None, None, True, 930, None, None, None),
    "G02": ("sol-fast", None, None, True, None, None, (930, "spec_changed"), ("opus-deep", "high", 4000, 930)),
    "G03": ("sol-fast", None, None, True, 700, None, None, None),
    "G03b": ("sol-fast", None, None, True, 700, None, None, None),
    "G04": ("opus-deep", "high", 4000, True, None, 1000, None, None),
    "G04b": ("opus-deep", "high", 4000, True, None, 1050, None, None),
    "G05": ("opus-deep", "high", 4000, True, None, 930, None, None),
    "G05b": ("opus-deep", "high", 4000, True, None, 930, None, None),
    "G06": ("race-sol-glm", None, None, True, 930, None, None, None),
    "G07": ("sol-fast", None, None, True, 930, None, (950, "client_abort"), None),
    "G08": ("sol-fast", None, None, True, 930, None, None, None),
    "G09": ("sol-fast", None, None, True, None, 50, None, None),
    "G10": ("sol-fast", None, None, True, 930, None, None, None),
    "G10b": ("sol-fast", None, None, True, 930, None, None, None),
    "G11": ("sol-fast", None, None, True, 930, None, None, None),
    "G12": ("sol-fast", None, None, True, 930, None, None, None),
    "G12b": ("sol-fast", None, None, True, 930, None, None, None),
}


async def _engine_events(fx: dict[str, Any]) -> list[tuple[float, dict[str, Any]]]:
    import anyio
    from tests.fakes.clock import FakeClock
    from tests.fakes.generation import StaticPromptBuilder
    from tests.fakes.ops_admission import CountingReservation
    from tests.fakes.world import build_world

    from noema.core.clock import Deadline
    from noema.core.protocols.generation import AttemptBudget, AttemptSpec, TurnContext

    from .kits import principal

    profile, effort, max_tokens, hold, commit_at, arm_at, cancel, restart = CONTROLS[fx["id"]]
    clock = FakeClock()
    world = build_world(clock=clock, scenario=fx["scenario"])
    snap = world.config.current()
    ctx = TurnContext(
        principal(),
        snap.tenant_ref("t_acme"),
        snap,
        Deadline(clock.monotonic() + 120, clock),
        CountingReservation(),
        "turn_1",
        "ses_1",
        "0" * 32,
    )
    budget = AttemptBudget()
    out: list[tuple[float, dict[str, Any]]] = []

    def ms() -> float:
        return round(clock.elapsed_ms(), 3)

    async def run_one(spec: AttemptSpec, *, held: bool, speculative: bool, start_at: float = 0) -> None:
        await clock.sleep(start_at / 1000)
        async with (
            world.generation.run(
                ctx,
                spec,
                StaticPromptBuilder(USER_TEXT_FOR_ENGINE),
                hold=held,
                speculative=speculative,
                failure_budget=budget,
            ) as run,
            anyio.create_task_group() as tg,
        ):

            async def read() -> None:
                out.extend([(ms(), dict(ev)) async for ev in run.events()])

            tg.start_soon(read)
            if held:
                for at, action in sorted(
                    [
                        (t, a)
                        for t, a in (
                            (commit_at, "commit"),
                            (arm_at, "commit"),
                            (cancel[0] if cancel else None, "cancel"),
                        )
                        if t is not None
                    ]
                ):
                    await clock.sleep(max(0.0, at / 1000 - clock.elapsed_ms() / 1000))
                    if action == "commit":
                        await run.commit("holdback_deadline" if fx["id"].startswith("G03") else "spec_agrees")
                    else:
                        await run.cancel(cancel[1])
            await run.result()

    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(lambda: run_one(AttemptSpec(profile, effort, max_tokens, "none"), held=hold, speculative=True))
        if restart is not None:
            r_profile, r_effort, r_max, r_at = restart
            tg.start_soon(
                lambda: run_one(
                    AttemptSpec(r_profile, r_effort, r_max, "none"), held=False, speculative=False, start_at=r_at
                )
            )
    return out


USER_TEXT_FOR_ENGINE = "What is the capital of France?"


@pytest.mark.parametrize("fixture_id", FIXTURE_IDS)
async def test_generation_events_match_the_fake_engine(fixture_id: str) -> None:
    from tests.golden.compare import Normalizer

    fx = load_fixture(fixture_id)
    want = [
        (
            e["at"],
            e["type"],
            e["data"].get("attempt_id"),
            e["data"].get("reason") or e["data"].get("text") or e["data"].get("code"),
        )
        for e in fx["events"]
        if e["type"] in GEN_TYPES
    ]
    norm = Normalizer()
    got = [
        (at, ev["type"], norm(ev.get("attempt_id")), ev.get("reason") or ev.get("text") or ev.get("code"))
        for at, ev in sorted(await _engine_events(fx), key=lambda item: item[0])
    ]
    assert got == want


@pytest.mark.parametrize("fixture_id", FIXTURE_IDS)
def test_fixture_requests_pass_the_gate(fixture_id: str) -> None:
    from noema.contracts.gate import RequestGate

    request = load_fixture(fixture_id)["request"]
    RequestGate.from_bundle().check_body("createTurn", "application/json", orjson.dumps(request["body"]))
