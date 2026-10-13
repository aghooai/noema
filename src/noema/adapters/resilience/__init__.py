"""noema.adapters.resilience (spec-01 §1): circuit breakers (`HealthRegistry`), the retry budget (`RetryBudget`)
and the per-attempt stream timeouts."""

from noema.adapters.resilience.breakers import BreakerPolicy, BreakerRegistry, BreakerState, Outcome
from noema.adapters.resilience.retry_budget import RetryBudgetPolicy, SlidingRetryBudget
from noema.adapters.resilience.timeouts import StreamWatchdog

__all__ = [
    "BreakerPolicy",
    "BreakerRegistry",
    "BreakerState",
    "Outcome",
    "RetryBudgetPolicy",
    "SlidingRetryBudget",
    "StreamWatchdog",
]
