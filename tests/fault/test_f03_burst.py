"""F3 (spec-10 §4, 06 §7): a 503 burst on Sol (`http_5xx{503}`, `window_ms: [0, 30000]`, `rate: 1.0`) under
sol-fast, with paced traffic on both replicas and both tenants.

Pass: the service breaker opens inside the 30 s window once >= 20 requests saw > 50% errors (both pods: breaker_events),
turns are refused without a model call while it is open, it goes half-open 15 s after opening (1-in-20 probes) and
closes after the window; time-to-open and time-to-recover are reported. Turns during the burst fail typed (sol-fast
has no other member: `all_routes_unavailable`), never a 500, never partial text.
"""

from __future__ import annotations

import time

import anyio
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite.kit import mock_calls, mock_seq
from tests.fault import kit
from tests.fault.stackctl import CircuitMonitor, any_open, both_closed

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]
WINDOW_S = 30.0
RUN_S = 55.0  # the window, then 25 s for open_s (15 s) plus the half-open probes to close it
PACE_S = 0.1  # 10 turns/s over both replicas


async def test_f03_a_503_burst_opens_the_breaker_and_it_recovers(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    burst = {"kind": "http_5xx", "status": 503, "rate": 1.0, "window_ms": [0, WINDOW_S * 1000]}
    monitor = CircuitMonitor(stack, kit.SOL)
    before = await mock_seq(mock)
    async with anyio.create_task_group() as tg:
        tg.start_soon(monitor.run)
        await mock.load(kit.scenario("fault-F3", [kit.gen(kit.SOL, fault=burst)], kit.members()))
        t0 = time.monotonic()  # the window starts at the scenario load
        records = await kit.run_turns(stack, int(RUN_S / PACE_S), lambda i, t: kit.body(f"f03-{t}"), pace_s=PACE_S)
        await anyio.sleep(1.0)
        tg.cancel_scope.cancel()
    kit.mark_budget_used()
    calls = [c for c in await mock_calls(mock, before) if c["model"] == kit.SOL and c["op"] == "converse-stream"]
    opened = monitor.first(t0, any_open)
    recovered = monitor.first(t0 + WINDOW_S, both_closed)
    in_open = [r for r in records if opened and r.t_send >= opened and r.t_send < t0 + WINDOW_S]
    refused = [r for r in in_open if not r.started]
    failed_before_open = sum(
        1 for c in calls if c.get("fault") and (opened is None or c["at_ms"] < (opened - t0) * 1000)
    )
    late = [r for r in records if r.t_send >= (recovered or 1e18)]
    kit.record_result(
        "F3",
        turns=len(records),
        pace_turns_per_s=1 / PACE_S,
        time_to_open_s=None if opened is None else round(opened - t0, 2),
        failed_requests_before_open=failed_before_open,
        time_to_recover_after_window_s=None if recovered is None else round(recovered - t0 - WINDOW_S, 2),
        transitions=monitor.transitions(t0),
        sol_requests=len(calls),
        sol_faulted=sum(1 for c in calls if c.get("fault")),
        turns_sent_while_open=len(in_open),
        refused_without_model_call=len(refused),
        outcomes=kit.counts(kit.outcome(r) for r in records),
        outcomes_after_recovery=kit.counts(kit.outcome(r) for r in late),
    )
    kit.assert_s1(records)
    assert opened is not None and opened - t0 < WINDOW_S, monitor.transitions(t0)
    assert recovered is not None, monitor.transitions(t0)
    assert refused and len(refused) >= 0.9 * len(in_open), (len(refused), len(in_open))
    assert all(kit.outcome(r) in ("completed", "error:all_routes_unavailable") for r in records)
    assert late and all(r.completed for r in late), kit.counts(kit.outcome(r) for r in late)
