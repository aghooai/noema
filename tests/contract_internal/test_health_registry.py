"""HealthRegistry + RetryBudget conformance (spec-02 §8, IMPL-078, spec-10 F3): breaker transitions per
(model, region) and the retry budget (R8: 10 % of the noted first sends), all sync and on the clock."""

from __future__ import annotations

from .kits import HealthKit
from .registry import kit_fixture

kit = kit_fixture("health_registry")
M, R, A = "us.openai.gpt-6.1-sol", "us-east-1", "111122223333"


async def test_starts_closed_and_admits(kit: HealthKit) -> None:
    assert kit.health.state(M, R) == "closed"
    assert kit.health.admit(M, R, A)


async def test_opens_after_errors_then_half_opens_and_closes(kit: HealthKit) -> None:
    for _ in range(20):
        kit.health.record(M, R, A, "error", None)
    assert kit.health.state(M, R) == "open"
    assert not kit.health.admit(M, R, A)
    await kit.clock.advance(15.0)
    assert kit.health.state(M, R) == "half_open"
    assert kit.health.admit(M, R, A)  # a probe
    kit.health.record(M, R, A, "ok", 700)
    assert kit.health.state(M, R) == "closed"


async def test_failed_probe_reopens(kit: HealthKit) -> None:
    for _ in range(20):
        kit.health.record(M, R, A, "timeout", None)
    await kit.clock.advance(15.0)
    assert kit.health.admit(M, R, A)
    kit.health.record(M, R, A, "error", None)
    assert kit.health.state(M, R) == "open"


async def test_mostly_ok_traffic_stays_closed(kit: HealthKit) -> None:
    for i in range(40):
        kit.health.record(M, R, A, "error" if i % 3 == 0 else "ok", 500)
    assert kit.health.state(M, R) == "closed"


async def test_breakers_are_per_model_and_region(kit: HealthKit) -> None:
    for _ in range(20):
        kit.health.record(M, R, A, "error", None)
    assert kit.health.state(M, "us-west-2") == "closed"
    assert kit.health.state("us.zai.glm-5.3", R) == "closed"


def _first_sends(kit: HealthKit, n: int, account: str = A, model: str = M) -> None:
    for _ in range(n):  # owner ruling R8: the budget is 10 % of first sends, no floor
        kit.retry_budget.note_request(account, model)


async def test_retry_budget_is_bounded_and_refills(kit: HealthKit) -> None:
    _first_sends(kit, 100)
    granted = [kit.retry_budget.try_spend(A, M) for _ in range(200)]
    assert granted[0] and not granted[-1]
    assert sum(granted) < 200
    await kit.clock.advance(120.0)
    _first_sends(kit, 100)
    assert kit.retry_budget.try_spend(A, M)


async def test_noted_requests_never_shrink_the_retry_budget(kit: HealthKit) -> None:
    for _ in range(1000):
        kit.retry_budget.note_request(A, M)  # adapters.md item 2: every wire request is noted
    assert kit.retry_budget.try_spend(A, M)


async def test_retry_budget_is_per_tenant_account_and_model(kit: HealthKit) -> None:
    for account, model in ((A, M), ("222222222222", M), (A, "us.zai.glm-5.3")):
        _first_sends(kit, 100, account, model)
    for _ in range(10_000):
        if not kit.retry_budget.try_spend(A, M):
            break
    assert kit.retry_budget.try_spend("222222222222", M)
    assert kit.retry_budget.try_spend(A, "us.zai.glm-5.3")
