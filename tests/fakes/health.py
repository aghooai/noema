"""`FakeHealthRegistry` and `FakeRetryBudget` (spec-02 §8, IMPL-078), clock-driven and in memory.

Breaker (per model and region, spec-10 F3): closed until at least `min_requests` outcomes in the last `window_s`
show an error ratio above `error_ratio` (`error` and `timeout` count as failures; `throttled` is a per-tenant quota
signal and does not trip the breaker); then open for `open_s`; then half-open, admitting one probe in
`probe_every` calls (5%); a successful probe closes it, a failed one re-opens it. `force` sets a state (tests).
Retry budget: a token bucket per (tenant account, model) with `capacity` tokens refilled at `refill_per_s`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from noema.core.clock import Clock

BreakerState = Literal["closed", "open", "half_open"]
Outcome = Literal["ok", "error", "throttled", "timeout"]


@dataclass
class _Breaker:
    state: BreakerState = "closed"
    opened_at: float = 0.0
    outcomes: deque[tuple[float, bool]] = field(default_factory=deque)  # (time, failed)
    half_open_calls: int = 0


class FakeHealthRegistry:
    """Implements `HealthRegistry` (see the module docstring); `records` logs every `record` call."""

    def __init__(
        self,
        clock: Clock,
        *,
        window_s: float = 30.0,
        min_requests: int = 20,
        error_ratio: float = 0.5,
        open_s: float = 15.0,
        probe_every: int = 20,
    ) -> None:
        self._clock = clock
        self._window_s = window_s
        self._min = min_requests
        self._ratio = error_ratio
        self._open_s = open_s
        self._probe_every = probe_every
        self._breakers: dict[tuple[str, str], _Breaker] = {}
        self.records: list[tuple[str, str, str, Outcome, int | None]] = []

    def _get(self, model: str, region: str) -> _Breaker:
        b = self._breakers.setdefault((model, region), _Breaker())
        if b.state == "open" and self._clock.monotonic() >= b.opened_at + self._open_s:
            b.state, b.half_open_calls = "half_open", 0
        return b

    def admit(self, model_id: str, region: str, tenant_account: str) -> bool:
        b = self._get(model_id, region)
        if b.state == "closed":
            return True
        if b.state == "open":
            return False
        b.half_open_calls += 1
        return (b.half_open_calls - 1) % self._probe_every == 0

    def record(self, model_id: str, region: str, tenant_account: str, outcome: Outcome, ttft_ms: int | None) -> None:
        self.records.append((model_id, region, tenant_account, outcome, ttft_ms))
        if outcome == "throttled":
            return
        b = self._get(model_id, region)
        now = self._clock.monotonic()
        failed = outcome in ("error", "timeout")
        if b.state == "half_open":
            if failed:
                b.state, b.opened_at = "open", now
            else:
                b.state = "closed"
                b.outcomes.clear()
            return
        b.outcomes.append((now, failed))
        while b.outcomes and b.outcomes[0][0] < now - self._window_s:
            b.outcomes.popleft()
        n = len(b.outcomes)
        if b.state == "closed" and n >= self._min and sum(f for _, f in b.outcomes) / n > self._ratio:
            b.state, b.opened_at = "open", now
            b.outcomes.clear()

    def state(self, model_id: str, region: str) -> BreakerState:
        return self._get(model_id, region).state

    def force(self, model: str, region: str, state: BreakerState) -> None:
        """Set a breaker state directly (an opened breaker re-opens its `open_s` timer now)."""
        b = self._get(model, region)
        b.state, b.opened_at, b.half_open_calls = state, self._clock.monotonic(), 0
        b.outcomes.clear()


class FakeRetryBudget:
    """Implements `RetryBudget` as a token bucket per (tenant account, model); `spent` counts granted retries."""

    def __init__(
        self, clock: Clock, *, capacity: float = 10.0, refill_per_s: float = 1.0, overrides: Mapping[str, float] = {}
    ) -> None:
        self._clock = clock
        self._capacity = capacity
        self._refill = refill_per_s
        self._overrides = dict(overrides)  # model -> capacity
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}  # key -> (tokens, at)
        self.spent = 0

    def note_request(self, tenant_account: str, model_id: str) -> None:
        """No-op: the fake's bucket does not depend on the request count."""

    def try_spend(self, tenant_account: str, model_id: str) -> bool:
        cap = self._overrides.get(model_id, self._capacity)
        now = self._clock.monotonic()
        tokens, at = self._buckets.get((tenant_account, model_id), (cap, now))
        tokens = min(cap, tokens + (now - at) * self._refill)
        if tokens < 1.0:
            self._buckets[(tenant_account, model_id)] = (tokens, now)
            return False
        self._buckets[(tenant_account, model_id)] = (tokens - 1.0, now)
        self.spent += 1
        return True

    def exhaust(self, tenant_account: str, model: str) -> None:
        """Empty one bucket (it refills over time)."""
        self._buckets[(tenant_account, model)] = (0.0, self._clock.monotonic())
