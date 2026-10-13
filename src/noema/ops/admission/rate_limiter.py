"""`RateLimiter` (spec-02 §8, spec-09 §2): a bare GCRA bucket outside the request work graph, e.g. the per-IP
auth-failure bucket `authfail:<ip>` (10/s, burst 50), on the admission script and its Redis-down fallback."""

from __future__ import annotations

from typing import TYPE_CHECKING

from noema.core.clock import Clock
from noema.core.redis_keys import RedisKeys
from noema.ops.admission.gcra import denied, rate_bucket, wall_ms
from noema.ops.admission.stores import PROBE_INTERVAL_S, REDIS_TIMEOUT_S, FailoverStore, RedisStore

if TYPE_CHECKING:
    import redis.asyncio


def _validate(rate_per_s: float, burst: int, n: int) -> None:
    if not (rate_per_s > 0 and burst >= 1 and n >= 1):
        raise ValueError(f"rate_per_s must be > 0, burst >= 1 and n >= 1 (got {rate_per_s}, {burst}, {n})")


class GcraRateLimiter:
    """Implements `RateLimiter`: `T = 1 / rate_per_s`, `τ = burst · T` per bucket (`RedisKeys.gcra(bucket)`).
    Backend failures never raise: the per-pod fallback answers at 1/`expected_pods` of the rate and burst."""

    def __init__(
        self,
        clock: Clock,
        keys: RedisKeys,
        redis: redis.asyncio.Redis | None,
        *,
        expected_pods: int = 1,
        redis_timeout_s: float = REDIS_TIMEOUT_S,
        probe_interval_s: float = PROBE_INTERVAL_S,
    ) -> None:
        self._clock = clock
        primary = RedisStore(redis, keys, timeout_s=redis_timeout_s) if redis is not None else None
        self._store = FailoverStore(primary, clock, expected_pods=expected_pods, probe_interval_s=probe_interval_s)

    async def check(self, bucket: str, *, rate_per_s: float, burst: int) -> float | None:
        _validate(rate_per_s, burst, 1)
        now = wall_ms(self._clock)
        tat, counted = await self._store.peek(rate_bucket(bucket, rate_per_s, burst, 1), now)
        after = tat + counted.interval_ms
        return (after - now - counted.tau_ms) / 1000.0 if denied(counted, after, now) else None

    async def debit(self, bucket: str, *, rate_per_s: float, burst: int, n: int = 1) -> float | None:
        _validate(rate_per_s, burst, n)
        now = wall_ms(self._clock)
        verdict, debit = await self._store.reserve([rate_bucket(bucket, rate_per_s, burst, n)], [], now, now)
        if verdict.status == "ok":
            return None
        return (verdict.after[0] - now - debit.buckets[0].tau_ms) / 1000.0
