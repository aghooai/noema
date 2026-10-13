"""F3b: the breakers' p95-TTFT rule (adapters `BreakerPolicy`: window p95 of TTFT > 3x the EWMA baseline of earlier
windows' p95, >= 20 samples, armed after 3 windows; the e2e stacks disarm it with NOEMA_BREAKER_TTFT_RULE=off, the
fault stack runs it armed as in production).

Phase 1 arms the baseline (Sol first text at 600 ms, paced sol-fast turns for BASELINE_S); phase 2 slows Sol's first
text to 4x (2,400 ms, every call still succeeds): the breaker must open on latency alone, on both pods, and refuse
turns without a model call; phase 3 restores 600 ms: the breaker closes again (time-to-recover reported).
"""

from __future__ import annotations

import time

import anyio
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.fault import kit
from tests.fault.stackctl import CircuitMonitor, any_open, both_closed

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]
BASELINE_S, SLOW_S, RECOVER_S = 80.0, 40.0, 45.0
PACE_S = 0.4  # 2.5 turns/s: ~37 Sol TTFT samples per pod in a 30 s window (>= 20)
SLOW_MS = 2400


def sol(first_text_ms: float) -> kit.Json:
    return kit.scenario("fault-F3b", [kit.gen(kit.SOL, first_text_ms=first_text_ms)], kit.members())


async def phase(stack: Stack, seconds: float, label: str) -> list[kit.TurnRecord]:
    n = int(seconds / PACE_S)
    return await kit.run_turns(stack, n, lambda i, t: kit.body(f"f03b-{label}-{t}"), pace_s=PACE_S, concurrency=40)


async def test_f03b_the_p95_ttft_rule_opens_the_breaker_on_latency_alone(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    monitor = CircuitMonitor(stack, kit.SOL)
    async with anyio.create_task_group() as tg:
        tg.start_soon(monitor.run)
        await mock.load(sol(kit.FIRST_TEXT_MS))
        t0 = time.monotonic()
        baseline = await phase(stack, BASELINE_S, "base")
        await mock.load(sol(SLOW_MS))
        t_slow = time.monotonic()
        slow = await phase(stack, SLOW_S, "slow")
        await mock.load(sol(kit.FIRST_TEXT_MS))
        t_fast = time.monotonic()
        recover = await phase(stack, RECOVER_S, "fast")
        tg.cancel_scope.cancel()
    kit.mark_budget_used()
    opened = monitor.first(t_slow, any_open)
    closed = monitor.first(t_fast, both_closed)
    open_during_baseline = monitor.first(t0, any_open)
    refused = [r for r in slow if not r.started]
    tail = [r for r in recover if closed is not None and r.t_send >= closed]
    kit.record_result(
        "F3b",
        turns=len(baseline) + len(slow) + len(recover),
        baseline_ttft=kit.summary([r.ttft_ms for r in baseline if r.ttft_ms is not None]),
        slow_ttft=kit.summary([r.ttft_ms for r in slow if r.ttft_ms is not None]),
        opened_during_baseline=open_during_baseline is not None and open_during_baseline < t_slow,
        time_to_open_after_slowdown_s=None if opened is None else round(opened - t_slow, 2),
        time_to_close_after_restore_s=None if closed is None else round(closed - t_fast, 2),
        transitions=monitor.transitions(t0),
        slow_outcomes=kit.counts(kit.outcome(r) for r in slow),
        slow_refused_without_model_call=len(refused),
        recover_outcomes=kit.counts(kit.outcome(r) for r in recover),
    )
    kit.assert_s1(baseline + slow + recover)
    assert all(r.completed for r in baseline), kit.counts(kit.outcome(r) for r in baseline)
    assert open_during_baseline is None or open_during_baseline >= t_slow, monitor.transitions(t0)
    assert opened is not None, monitor.transitions(t0)
    assert refused, kit.counts(kit.outcome(r) for r in slow)
    assert closed is not None and tail and all(r.completed for r in tail), monitor.transitions(t0)
