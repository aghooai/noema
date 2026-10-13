"""P-SSE-1, generation part (spec-04 §4 invariants over one profile run): hypothesis over random scripted attempt
behaviors (answers, pre-byte and mid-stream failures, timeouts, reasoning-only and empty answers, content-filter
stops, hangs), profiles, hold/commit/cancel timings and deadlines. Every generation.started has exactly its one
terminal (the frame and reason its record implies; none only for T19 and the committed attempt's error event); at
most one commit; deltas only for the committed attempt, after its commit, seq 0, 1, 2, ...; the failure budget holds;
every payload is contract-valid. All times lie on a 50 ms grid, so events of different attempts, the commit, the
cancel and the deadline often fall on the same instant (the interleavings a continuous draw practically never hits).
"""

from __future__ import annotations

from typing import Any

import orjson
from hypothesis import given, settings
from hypothesis import strategies as st

from noema.contracts.validate import ContractValidator
from noema.core.errors import ErrorKind
from noema.core.protocols.adapters import MessageStart, ReasoningDelta, Stop, StreamEvent, TextDelta
from noema.core.protocols.generation import AttemptBudget
from noema.generation import StreamingProfileRun
from noema.generation.fsm import St

from .rig import cancel, commit, drive, make_rig
from .scripted import Call, error_at, failure

ALIASES = ("sol-6.1", "opus-5.5", "glm-5.3")
PROFILES = ("sol-fast", "opus-deep", "glm-reason", "race-sol-glm")
KINDS = (ErrorKind.RETRYABLE, ErrorKind.THROTTLED, ErrorKind.FATAL, ErrorKind.TENANT_ACCESS)
VALIDATOR = ContractValidator()


TICK = 0.05  # the 50 ms grid


def at(units: int) -> float:
    return units * TICK  # one multiplication per time, so equal unit counts give equal floats


@st.composite
def calls(draw: st.DrawFn) -> Call:
    shape = draw(st.sampled_from(["ok", "open_error", "mid_error", "reasoning_only", "empty", "filter", "hang"]))
    u0 = draw(st.integers(0, 30))
    if shape == "open_error":
        return Call(open_error=failure(draw(st.sampled_from(KINDS)), "x"), open_s=at(u0))
    events: list[tuple[float, StreamEvent]] = [(at(u0), MessageStart(at=0.0))]
    if shape in ("reasoning_only", "empty", "filter"):
        events.append((at(u0 + 2), ReasoningDelta(40)))
        stop = {"reasoning_only": "max_tokens", "empty": "end_turn", "filter": "content_filter"}[shape]
        events.append((at(u0 + 4), Stop(stop)))  # type: ignore[arg-type]
        return Call(events)
    chunks = draw(st.integers(1, 4))
    every = draw(st.integers(1, 16))
    events += [(at(u0 + 2 + i * every), TextDelta(f"c{i} ")) for i in range(chunks)]
    end = at(u0 + 2 + chunks * every)
    if shape == "mid_error":
        code = draw(st.sampled_from(["500", "timeout:idle", "ModelStreamErrorException"]))
        events.append(error_at(end, ErrorKind.RETRYABLE, code))
    elif shape == "ok":
        events.append((end, Stop(draw(st.sampled_from(["end_turn", "max_tokens", "stop_sequence"])))))
    return Call(events, hang=shape == "hang")


@st.composite
def runs(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "profile": draw(st.sampled_from(PROFILES)),
        "scripts": {a: draw(st.lists(calls(), max_size=4)) for a in ALIASES},
        "hold": draw(st.booleans()),
        "commit_at": draw(st.none() | st.integers(0, 80).map(at)),
        "cancel": draw(
            st.none() | st.tuples(st.integers(0, 120).map(at), st.sampled_from(["spec_changed", "client_abort"]))
        ),
        "deadline_s": draw(st.integers(20, 800).map(at)),
    }


def expected_terminal(rec: Any, state: St) -> tuple[str, str] | None:
    """The terminal frame (type, reason or finish_reason) the record implies (spec-04 §4.1); None = no frame."""
    if rec.outcome == "failed":
        return ("generation.cancelled", "failed")  # T10
    if rec.outcome == "cancelled":
        return None if rec.cancel_reason == "deadline" else ("generation.cancelled", rec.cancel_reason)  # T9/13/18/19
    if rec.error_code is not None:
        return None  # T16/T17: the run's error event ends the committed attempt
    if rec.finish_reason == "cancelled":
        return ("generation.cancelled", "client_abort") if state is St.X else None  # T18 / T19 (C -> Err)
    return ("generation.completed", rec.finish_reason)  # T15


def check_invariants(
    events: list[dict[str, Any]], records: tuple[Any, ...], budget: AttemptBudget, run: StreamingProfileRun
) -> None:
    started = [e["attempt_id"] for e in events if e["type"] == "generation.started"]
    assert len(started) == len(set(started)) == len(records)
    assert [r.attempt_id for r in records] == started
    committed = [e["attempt_id"] for e in events if e["type"] == "generation.committed"]
    assert len(committed) <= 1
    terminals: dict[str, list[tuple[str, str]]] = {a: [] for a in started}
    seen_commit, seq, ended = False, 0, set()
    for i, e in enumerate(events):
        kind, att = e["type"], e.get("attempt_id")
        assert att is None or att not in ended, f"event after the attempt's terminal frame: {e}"
        if kind == "generation.committed":
            seen_commit = True
        elif kind == "generation.delta":
            assert seen_commit and att == committed[0] and e["seq"] == seq
            seq += 1
        elif kind == "generation.cancelled":
            terminals[str(att)].append((kind, e["reason"]))
            ended.add(att)
        elif kind == "generation.completed":
            terminals[str(att)].append((kind, e["finish_reason"]))
            ended.add(att)
        elif kind == "error":
            assert i == len(events) - 1, "error is the run's last event"
        VALIDATOR.check_sse_frame("createTurn", str(i + 1), kind, orjson.dumps(e))
    states = {a.attempt_id: a.state for a in run.attempts}
    for rec in records:
        exp = expected_terminal(rec, states[rec.attempt_id])
        assert terminals[rec.attempt_id] == ([] if exp is None else [exp]), (rec, terminals[rec.attempt_id])
        if rec.outcome == "committed":
            assert committed == [rec.attempt_id]
            if rec.error_code is not None:
                assert (events[-1]["type"], events[-1].get("code")) == ("error", rec.error_code)
        elif rec.outcome == "failed":
            assert rec.cancel_reason == "failed" and rec.error_code is not None
        else:
            assert rec.cancel_reason in ("spec_changed", "lost_race", "client_abort", "deadline")
        assert rec.usage.output_tokens >= 0 and rec.cost.usd >= 0
    assert budget.remaining >= 0


@settings(max_examples=400)
@given(runs())
async def test_generation_run_invariants(case: dict[str, Any]) -> None:
    rig = make_rig(case["scripts"])
    rig.adapter.default = Call(open_error=failure(ErrorKind.RETRYABLE, "503"), open_s=0.1)
    actions = []
    if case["commit_at"] is not None:
        actions.append((case["commit_at"], commit()))
    if case["cancel"] is not None:
        actions.append((case["cancel"][0], cancel(case["cancel"][1])))
    budget = AttemptBudget()
    ctx = rig.ctx(deadline_s=case["deadline_s"])
    out = await drive(rig, case["profile"], hold=case["hold"], budget=budget, actions=actions, ctx=ctx)
    check_invariants([ev for _, ev in out.events], out.records, budget, out.run)
    if any(r.cancel_reason == "deadline" for r in out.records):
        assert ctx.deadline.expired()  # T19 only once ctx.deadline really passed (no cancel("deadline") here)
    race_width = 2 if case["profile"] == "race-sol-glm" else 1
    assert len(out.records) <= race_width + 2  # the first attempt(s) plus at most 2 failure-driven ones
