"""F4 (spec-10 §4, 06 §7): hang before the first byte (`hang_before_first_byte`: the mock sends nothing until the
client goes away).

- GLM under pinned glm-reason (first-event timeout 10 s, ROUTE_TIMEOUTS_V1) with `Aghoo-Deadline-Ms: 30000`: the
  attempt times out at 10 s and the turn falls back to Sol (spec-04 T10);
- Sol under sol-fast (first-event timeout 5 s; no member left, the default profile is sol-fast itself):
  `error{all_routes_unavailable}` at 5 s.
Pass: every turn's e2e <= its deadline + 1,000 ms; fallback success >= 99% (S1); the typed error otherwise.
"""

from __future__ import annotations

import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite.kit import data
from tests.fault import kit

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]


def failed_at_ms(r: kit.TurnRecord) -> float | None:
    """When the client saw the first attempt end `failed` (ms after the send)."""
    a1 = r.started[0]["attempt_id"] if r.started else None
    for at, event in zip(r.at_ms, r.events, strict=True):
        if event.event == "generation.cancelled" and data(event)["attempt_id"] == a1:
            return at
    return None


async def test_f04_glm_hang_falls_back_to_sol_at_the_first_event_timeout(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    hang = kit.gen_fault(kit.GLM, "hang_before_first_byte", 0)
    await mock.load(kit.scenario("fault-F4-glm", [hang, kit.gen(kit.SOL)], kit.members()))
    records = await kit.run_turns(
        stack, kit.TURNS // 2, lambda i, t: kit.body(f"f04a-{t}", "glm-reason"), deadline_ms=30_000, concurrency=50
    )
    kit.mark_budget_used()
    failed = [x for r in records if (x := failed_at_ms(r)) is not None]
    kit.record_result(
        "F4a",
        turns=len(records),
        deadline_ms=30_000,
        outcomes=kit.counts(kit.outcome(r) for r in records),
        committed_model=kit.counts(r.model_of(r.committed) for r in records),
        first_attempt_failed_at_ms=kit.summary(failed),
        e2e_ms=kit.summary([r.e2e_ms for r in records]),
        fallback=kit.fallback_numbers(records),
    )
    kit.assert_s1(records)
    assert max(r.e2e_ms for r in records) <= 30_000 + 1_000, kit.summary([r.e2e_ms for r in records])
    assert failed and min(failed) >= 9_500 and kit.pct(failed, 95) <= 11_000, kit.summary(failed)
    served, of = kit.fallback_success(records)
    assert served >= 0.99 * of, kit.fallback_numbers(records)
    assert all(r.completed for r in records), kit.counts(kit.outcome(r) for r in records)


async def test_f04_sol_hang_without_a_member_left_is_all_routes_unavailable(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    hang = kit.gen_fault(kit.SOL, "hang_before_first_byte", 0)
    await mock.load(kit.scenario("fault-F4-sol", [hang], kit.members()))
    records = await kit.run_turns(stack, kit.TURNS // 2, lambda i, t: kit.body(f"f04b-{t}"), concurrency=50)
    kit.mark_budget_used()
    attempted = [r for r in records if r.started]
    errors_at = [r.at_ms[r.kinds.index("error")] for r in attempted if "error" in r.kinds]
    kit.record_result(
        "F4b",
        turns=len(records),
        deadline_ms=kit.DEADLINE_MS,
        outcomes=kit.counts(kit.outcome(r) for r in records),
        attempted=len(attempted),
        refused_by_open_breaker=len(records) - len(attempted),
        error_at_ms_attempted=kit.summary(errors_at),
        e2e_ms=kit.summary([r.e2e_ms for r in records]),
    )
    kit.assert_s1(records)
    assert max(r.e2e_ms for r in records) <= kit.DEADLINE_MS + 1_000, kit.summary([r.e2e_ms for r in records])
    assert all(kit.outcome(r) == "error:all_routes_unavailable" for r in records), kit.counts(map(kit.outcome, records))
    assert errors_at and min(errors_at) >= 4_500 and kit.pct(errors_at, 95) <= 6_500, kit.summary(errors_at)
