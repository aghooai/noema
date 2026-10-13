"""Member calls wait `DISPATCH_YIELDS` loop passes before their CPU-heavy first step (C2b perf smoke): a task that a
turn starts right after the decision entry (its generation attempt, about 4 task hops to the network) is dispatched
before the 15 router-default member calls instead of after them. Ordering only: no virtual time passes."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from tests.contract_internal.test_decision_engine import d_std
from tests.fakes.adapters import FakeAdapter
from tests.unit.decision.test_run import turn_request
from tests.unit.decision.world import engine_world

from noema.decision.coordinator import DISPATCH_YIELDS

pytestmark = [pytest.mark.unit]
GENERATION_HOPS = 4  # profile run -> attempt -> stream open -> the adapter call (noema.generation, adapters._stream)


async def test_a_generation_attempt_started_at_t_d0_is_dispatched_before_the_member_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ew = engine_world()
    ew.world.use_scenario(d_std())
    order: list[str] = []
    real = FakeAdapter.complete

    async def complete(self: FakeAdapter, *args: Any, **kwargs: Any) -> Any:
        order.append("member")
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(FakeAdapter, "complete", complete)
    t0 = ew.clock.monotonic()
    ctx = ew.context(turn_id="turn_1", escalate=False)
    async with ew.clock.autojump(), ew.engine.run(ctx, turn_request(ew)) as run:
        async with anyio.create_task_group() as tg:

            async def hop(n: int) -> None:
                if n == 0:
                    order.append("generation")
                else:
                    tg.start_soon(hop, n - 1)

            await hop(GENERATION_HOPS)
        await run.commit_critical()
        assert ew.clock.monotonic() > t0
    assert order[0] == "generation", order[:3]
    assert order.count("member") >= 15  # 3 members x 5 questions (+ DeepSeek backups in this scenario)


def test_the_yields_cover_the_generation_hops() -> None:
    assert DISPATCH_YIELDS > GENERATION_HOPS
