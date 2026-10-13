"""F9 (spec-10 §4, 06 §7): every decision member down (`http_5xx{503}` on Haiku, Luna and DeepSeek; their breakers
open on the way, so later turns find the members `circuit_open` without a call).

Pass: 100% of the turns are served (`completed`) on the default route (spec0, sol-fast), each decision snapshot is
`unavailable` for every answer, and the routing says why (G09: `decision unavailable: <reason>`).
"""

from __future__ import annotations

import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite.kit import mock_calls, mock_seq
from tests.fault import kit
from tests.fault.stackctl import circuit_states

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]


async def test_f09_all_decision_members_down_still_serves_every_turn(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    down = kit.fault_rule({}, "http_5xx", 50, status=503)
    await mock.load(kit.scenario("fault-F9", [kit.gen(kit.SOL)], [kit.members()[0], down]))
    before = await mock_seq(mock)
    records = await kit.run_turns(stack, kit.TURNS, lambda i, t: kit.body(f"f09-{t}"))
    calls = [c for c in await mock_calls(mock, before) if c["op"] in ("converse", "chat")]
    kit.mark_budget_used()
    reasons = [r.of("route.confirmed")[0]["routing"]["reason"] for r in records if r.of("route.confirmed")]
    snapshots = [r.of("decision.completed")[0]["decision"] for r in records if r.of("decision.completed")]
    member_circuits = {m: await circuit_states(stack, m) for m in (kit.HAIKU, kit.LUNA, kit.DEEPSEEK)}
    kit.record_result(
        "F9",
        turns=len(records),
        outcomes=kit.counts(kit.outcome(r) for r in records),
        committed_route=kit.counts(r.started[0]["route_id"] if r.started else None for r in records),
        routing_reason=kit.counts(reasons),
        answer_types=kit.counts(a["type"] for s in snapshots for a in s["answers"]),
        member_requests=len(calls),
        member_requests_per_turn=round(len(calls) / len(records), 2),
        member_circuits_after=member_circuits,
        ttft_ms=kit.summary([r.ttft_ms for r in records if r.ttft_ms is not None]),
    )
    kit.assert_s1(records)
    assert all(r.completed for r in records), kit.counts(kit.outcome(r) for r in records)
    assert all(a["type"] == "unavailable" for s in snapshots for a in s["answers"])
    assert len(reasons) == len(records) and all(x.startswith("decision unavailable: ") for x in reasons), reasons[:5]
