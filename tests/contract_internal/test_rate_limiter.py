"""RateLimiter conformance (spec-09 §2 auth-failure bucket, spec-11 §1.2 GCRA): a full bucket admits `burst` units,
an empty one answers retry_after_s without debiting, `check` never debits, and capacity returns at `rate_per_s`."""

from __future__ import annotations

import pytest

from .kits import RateLimiterKit
from .registry import kit_fixture

kit = kit_fixture("rate_limiter")
BUCKET = "authfail:203.0.113.7"  # RedisKeys.gcra bucket name of spec-09 §2
RATE, BURST = 10.0, 50  # 10/s, burst 50


async def test_burst_then_retry_after(kit: RateLimiterKit) -> None:
    for _ in range(BURST):
        assert await kit.limiter.debit(BUCKET, rate_per_s=RATE, burst=BURST) is None
    retry = await kit.limiter.debit(BUCKET, rate_per_s=RATE, burst=BURST)
    assert retry is not None and 0 < retry <= 1 / RATE + 1e-6
    assert await kit.limiter.check(BUCKET, rate_per_s=RATE, burst=BURST) is not None
    assert await kit.limiter.check("authfail:198.51.100.1", rate_per_s=RATE, burst=BURST) is None  # per bucket


async def test_check_never_debits_and_capacity_returns(kit: RateLimiterKit) -> None:
    for _ in range(3 * BURST):
        assert await kit.limiter.check(BUCKET, rate_per_s=RATE, burst=BURST) is None
    for _ in range(BURST):
        await kit.limiter.debit(BUCKET, rate_per_s=RATE, burst=BURST)
    assert await kit.limiter.debit(BUCKET, rate_per_s=RATE, burst=BURST) is not None  # nothing debited by it
    await kit.clock.advance(1.0)  # 10 units come back
    for _ in range(10):
        assert await kit.limiter.debit(BUCKET, rate_per_s=RATE, burst=BURST) is None
    assert await kit.limiter.debit(BUCKET, rate_per_s=RATE, burst=BURST) is not None


async def test_multi_unit_debits_are_all_or_nothing(kit: RateLimiterKit) -> None:
    assert await kit.limiter.debit(BUCKET, rate_per_s=RATE, burst=BURST, n=BURST - 5) is None
    assert await kit.limiter.debit(BUCKET, rate_per_s=RATE, burst=BURST, n=10) is not None
    for _ in range(5):  # the refused 10 left the 5 remaining units in place
        assert await kit.limiter.debit(BUCKET, rate_per_s=RATE, burst=BURST) is None


@pytest.mark.parametrize(("rate", "burst", "n"), [(0.0, 50, 1), (10.0, 0, 1), (10.0, 50, 0)])
async def test_invalid_parameters(kit: RateLimiterKit, rate: float, burst: int, n: int) -> None:
    with pytest.raises(ValueError, match="rate_per_s"):
        await kit.limiter.debit(BUCKET, rate_per_s=rate, burst=burst, n=n)
