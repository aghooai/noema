"""`ScenarioHarness`: a `ScenarioRuntime` bound to a `Clock`, with awaitable barriers.

The fake adapter and the fake decision engine share one harness per test, so `nth` counters and barriers are
global to the scenario, exactly as in the wire mock. Barriers block until `release(name)` (or the caller's
deadline); `release_at(name, ms)` schedules a release on the clock ("virtual time" barriers, spec-10 §2.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anyio

from noema.core.clock import Clock, deadline_scope

from .scenario import RequestInfo, Resolution, Scenario, ScenarioRuntime, load_scenario


class ScenarioHarness:
    """One scenario run on one clock. `t0` is the clock's monotonic time at construction (window_ms origin)."""

    def __init__(self, scenario: Scenario | str | Path | Mapping[str, Any], clock: Clock) -> None:
        self.scenario = scenario if isinstance(scenario, Scenario) else load_scenario(scenario)
        self.clock = clock
        self.t0 = clock.monotonic()
        self.runtime = ScenarioRuntime(self.scenario, now_ms=lambda: (clock.monotonic() - self.t0) * 1000.0)
        self._barriers: dict[str, anyio.Event] = {}
        self._released: set[str] = set()

    def resolve(self, req: RequestInfo) -> Resolution:
        """`ScenarioRuntime.resolve` (raises `NoRuleMatched`)."""
        return self.runtime.resolve(req)

    def release(self, name: str) -> None:
        """Release a barrier: held and future requests on it proceed."""
        self._released.add(name)
        self.runtime.release(name)
        event = self._barriers.get(name)
        if event is not None:
            event.set()

    async def release_at(self, name: str, ms: float) -> None:
        """Sleep until `ms` after t0 on the clock, then release `name` (run it in a task group)."""
        await self.clock.sleep(max(0.0, self.t0 + ms / 1000.0 - self.clock.monotonic()))
        self.release(name)

    async def wait_barrier(self, name: str, *, until: float) -> bool:
        """Wait for `name` to be released; False if the clock reached `until` (monotonic) first."""
        if name in self._released:
            return True
        event = self._barriers.setdefault(name, anyio.Event())
        async with deadline_scope(self.clock, until):
            await event.wait()
        return name in self._released

    def unmatched(self) -> list[RequestInfo]:
        """Requests no rule answered (a test failure)."""
        return [req for req, rule in self.runtime.requests if rule is None]
