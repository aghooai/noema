"""L2 / L3 / Q8 (06 §4, §2.2; spec-10 §3): holdback TTFT bounds on the wire mock, `make qa-load-holdback`.

Cells T_d in {300, 900} ms x TTFA in {300, 720, 2400} ms, n >= 200 turns per cell at concurrency 50 over both api
replicas and two tenants, after a 50-turn ramp-in. T_d is the decision latency (every router-default member answers
every question at T_d, from the request's arrival at the mock); TTFA the first answer text of the route's stream.
Every stream then has L1's shape (spec-10 §5: 420 tokens at 60 tokens/s), so 50 concurrent streams are L1's load
shape (~7 turns/s), not a burst of short turns. Client TTFT = request sent -> the chunk holding the first
`generation.delta` (only committed text is ever sent), timed on the raw stream by `gatekit.drive` (the driver reads
the api containers directly; every response is contract-validated after the cell). A window is valid (spec-10 §5)
when the driver's loop lag p99 and the mock's lateness p99 are <= 2 ms; both only lengthen the measured TTFT, so a
bound met in any window passes, and a bound missed in an invalid window fails as "invalid, re-run". Every cell has a
premise (below); >= 99% of its turns must meet it, the others are reported (with T_d = 900 the decision has 100 ms to
the router deadline of 1,000 ms: on a loaded host a member can miss it and the turn then keeps spec0), and the TTFT
statistics are those of the steady turns that meet it. Every turn is served and passes S1 (no duplicated text,
P-SSE-1, no 500) in every cell.

- L2 `test_agree`: every pre-commit key pinned (sol-fast, low, 600, conditioning off), so the decided spec equals
  spec0. Pass: client TTFT p95 <= max(T_d, TTFA) + 50 ms in every cell (the nominal cell values: every Noema delay,
  ingress and decision overhead included, counts against the 50 ms). Premise: served by its one Sol attempt.
- L3 `test_disagree`: an unpinned first turn (spec0 = sol-fast) whose decision says coding/hard (decided opus-deep,
  high, 4000): a forced restart (the speculative Sol attempt's text comes only at T_d + 1,000 ms, so the hold cap
  never fires before S1), r* = Opus with its first text at TTFA. Pass: client TTFT p95 <= T_d + TTFA + 100 ms in
  every cell. Premise: restarted exactly once and served by Opus.
- Q8 `test_late_disagreement` (reported, G03b at load): Sol text at 300 ms, decision at 900 ms disagreeing. The hold
  cap (max_hold_ms 400) commits Sol at `holdback_deadline` before S1, and the stored turn has
  `routing.served_spec_differs = true`. Premise: one attempt committed at `holdback_deadline`; pass: every such
  turn stored with `served_spec_differs = true` (the rate is then 100%). The `holdback_deadline` share and the
  `served_spec_differs` value of every cell are reported.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import Final

import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import eventually, mock_calls, tag, turn_body
from tests.fault import kit
from tests.fault.kit import BodyFn, TurnRecord
from tests.load import gatekit

pytestmark = [pytest.mark.e2e, pytest.mark.perf, pytest.mark.timeout(1800)]

TURNS: Final = int(os.environ.get("NOEMA_HOLDBACK_TURNS", "200"))  # measured per cell, after the warm-up
WARMUP: Final = 50  # the ramp-in turns (one per stream), validated and checked but not in the statistics
CONC: Final = 50  # spec-10 §3 L2: concurrency 50
CELLS: Final = [(t_d, ttfa) for t_d in (300, 900) for ttfa in (300, 720, 2400)]
IDS: Final = [f"Td{t_d}-TTFA{ttfa}" for t_d, ttfa in CELLS]
MAX_HOLD_MS: Final = 400  # routing.speculation.max_hold_ms of the bootstrap config
CHUNKS: Final = sc.CHUNKS * 15  # 105 chunks of 4 tokens = 420 tokens
ANSWER: Final = "".join(CHUNKS)
STEP_MS: Final = 4000 / 60  # 60 tokens/s
PREMISE_MIN: Final = 0.99
DEADLINE_MS: Final = 30_000  # Aghoo-Deadline-Ms: the slowest cell's turn (900 + 2,400 + 7,000 ms of text) fits


def stream(model: str, first_text_ms: float) -> gatekit.Rule:
    return sc.stream(model, first_text_ms=first_text_ms, chunks=CHUNKS, step_ms=STEP_MS)


def coding(label: str) -> gatekit.Json:
    """An unpinned turn (spec0 = routing.default_profile sol-fast) with a unique text."""
    return turn_body(f"Write a sort function. ({tag(label)})")


def timings(r: TurnRecord) -> gatekit.Json:
    done = r.of("turn.completed")
    return dict(done[0]["timings"]) if done else {}


def committed_reason(r: TurnRecord) -> str | None:
    found = r.of("generation.committed")
    return str(found[0]["reason"]) if found else None


def miss(r: TurnRecord) -> gatekit.Json:
    """What a turn outside the cell's premise did (the decision, the routing reason, the attempts)."""
    decided = r.of("decision.completed")
    confirmed = r.of("route.confirmed")
    return {
        "decision": decided[0]["decision"]["status"] if decided else None,
        "routing_reason": confirmed[0]["routing"]["reason"] if confirmed else None,
        "committed": committed_reason(r),
        "routes": [a["route_id"] for a in r.started],
        "decision_ms": timings(r).get("decision_ms"),
    }


async def run_cell(
    stack: Stack,
    mock: Mock,
    name: str,
    rules: list[gatekit.Rule],
    make_body: BodyFn,
    bound_ms: float | None,
    premise: Callable[[TurnRecord], bool],
) -> tuple[list[TurnRecord], gatekit.Json]:
    """One cell: the scenario on a fresh mock ring, WARMUP + TURNS turns, S1 asserted on every turn. `premise` is
    what the cell measures (one Sol attempt; a restart served by Opus; a hold-cap commit): >= 99% of the turns must
    meet it (the rest, e.g. a decision closed at the router deadline on a loaded host, are reported), and the TTFT
    statistics are those of the steady turns that meet it."""
    scenario = kit.scenario(name, rules)
    seq = await gatekit.fresh_mock(mock, scenario)
    every, lag = await gatekit.drive(stack, WARMUP + TURNS, make_body, concurrency=CONC, deadline_ms=DEADLINE_MS)
    kit.assert_s1(every, expected=ANSWER)
    records = [r for r in gatekit.steady(every, WARMUP) if premise(r)]
    misses = [miss(r) for r in every if not premise(r)]
    reasons = kit.counts(committed_reason(r) for r in records)
    confirmed = [r.of("route.confirmed") for r in records]
    numbers: gatekit.Json = {
        "n": len(records),
        "warmup": WARMUP,
        "served": sum(r.completed for r in every),
        "premise_share": gatekit.share(len(every) - len(misses), len(every)),
        "premise_misses": misses[:5],
        "ttft_ms": gatekit.dist([r.ttft_ms for r in records if r.ttft_ms is not None]),
        "bound_ms": bound_ms,
        "decision_ms": gatekit.dist([float(t["decision_ms"]) for t in map(timings, records) if "decision_ms" in t]),
        "committed_reason": reasons,
        "holdback_deadline_share": gatekit.share(reasons.get("holdback_deadline", 0), len(records)),
        "route_confirmed_served_spec_differs": kit.counts(
            c[0]["routing"]["served_spec_differs"] if c else "none" for c in confirmed
        ),
        "spec_restarts": kit.counts(timings(r).get("spec_restarts") for r in every),
        "outcomes": kit.counts(kit.outcome(r) for r in every),
        **gatekit.validity(lag, gatekit.mock_lateness(scenario, await mock_calls(mock, seq))),
    }
    gatekit.record(name, **numbers)
    assert numbers["served"] == WARMUP + TURNS, numbers["outcomes"]
    assert numbers["premise_share"] >= PREMISE_MIN, numbers
    return records, numbers


def one_sol_attempt(r: TurnRecord) -> bool:
    return len(r.started) == 1 and r.model_of(r.committed) == sc.SOL


def restarted_on_opus(r: TurnRecord) -> bool:
    return timings(r).get("spec_restarts") == 1 and r.model_of(r.committed) == sc.OPUS


def hold_cap_commit(r: TurnRecord) -> bool:
    return committed_reason(r) == "holdback_deadline" and len(r.started) == 1


@pytest.mark.parametrize(("t_d", "ttfa"), CELLS, ids=IDS)
async def test_agree(stack: Stack, mock: Mock, gate_config: int, t_d: int, ttfa: int) -> None:
    bound = max(t_d, ttfa) + 50
    rules = [stream(sc.SOL, ttfa), *gatekit.router_at(t_d)]
    body: BodyFn = lambda i, _t: kit.body(f"L2-{i}", "sol-fast")  # noqa: E731
    _r, numbers = await run_cell(stack, mock, f"L2 agree T_d={t_d} TTFA={ttfa}", rules, body, bound, one_sol_attempt)
    gatekit.assert_within(numbers, numbers["ttft_ms"]["p95"], bound)


@pytest.mark.parametrize(("t_d", "ttfa"), CELLS, ids=IDS)
async def test_disagree(stack: Stack, mock: Mock, gate_config: int, t_d: int, ttfa: int) -> None:
    bound = t_d + ttfa + 100
    rules = [stream(sc.SOL, t_d + 1000), stream(sc.OPUS, ttfa)]
    rules += gatekit.router_at(t_d, intent=sc.CODING, level=sc.HARD)
    name = f"L3 disagree T_d={t_d} TTFA={ttfa}"
    _r, numbers = await run_cell(stack, mock, name, rules, lambda i, _t: coding(f"L3-{i}"), bound, restarted_on_opus)
    gatekit.assert_within(numbers, numbers["ttft_ms"]["p95"], bound)


async def test_late_disagreement(stack: Stack, mock: Mock, gate_config: int) -> None:
    t_d, ttfa = 900, 300
    rules = [stream(sc.SOL, ttfa), stream(sc.OPUS, ttfa)]
    rules += gatekit.router_at(t_d, intent=sc.CODING, level=sc.HARD)
    name = "Q8 late disagreement T_d=900 TTFA=300"
    records, numbers = await run_cell(stack, mock, name, rules, lambda i, _t: coding(f"Q8-{i}"), None, hold_cap_commit)
    stored = kit.counts(await stored_differs(stack, records))
    rate = gatekit.share(stored.get("True", 0), len(records))
    gatekit.record(name, **numbers, hold_cap_ms=ttfa + MAX_HOLD_MS, stored_served_spec_differs=stored,
                   served_spec_differs_rate=rate)  # fmt: skip
    assert stored == {"True": len(records)}, stored


async def stored_differs(stack: Stack, records: Sequence[TurnRecord]) -> list[object]:
    """`routing.served_spec_differs` of each stored turn (getTurn on the other replica, once stored)."""
    out: list[object] = []
    for r in records:
        client = stack.client(1 - r.replica, tenant=r.tenant)
        stored = await eventually(client, f"/v1/turns/{r.turn_id}", lambda b: True, timeout_s=30)
        out.append(stored["routing"]["served_spec_differs"])
    return out
