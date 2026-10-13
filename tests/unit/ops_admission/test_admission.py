"""`GcraAdmission` / `GcraReservation` on the in-process store (spec-11 §1.2): buckets, headers, 429 codes and
retry-after, reconcile/release math, labeling headroom, bulkheads, stream leases, cancellation, overruns."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from tests.contract_internal.kits import principal
from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from
from tests.fakes.telemetry import FakeTelemetry

from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import system_principal
from noema.core.protocols.ops import WorkGraph
from noema.core.redis_keys import RedisKeys
from noema.core.values import CallKind, UsageRecord
from noema.ops.admission import AdmissionRejected, GcraAdmission, GcraReservation

pytestmark = pytest.mark.unit
KEYS = RedisKeys("ns:unit:")
SOL = "sol-6.1"


def graph(
    calls: int = 4, tokens: int = 1000, streams: int = 0, alias: str = SOL, kind: CallKind = "generation"
) -> WorkGraph:
    return WorkGraph({(alias, kind): calls}, {alias: tokens}, calls, tokens // 2, tokens - tokens // 2, streams)


def admission(
    clock: FakeClock, telemetry: FakeTelemetry | None = None, env: str = "test", **limits: Any
) -> GcraAdmission:
    config = FakeConfigRegistry(snapshot_from(patched_example({"tenants": {"t_acme": {"limits": limits}}})))
    instruments = telemetry.instruments if telemetry is not None else None
    return GcraAdmission(clock, config, KEYS, None, env=env, instruments=instruments)


async def reserve(adm: GcraAdmission, g: WorkGraph, timeout_s: float = 1.0, **kw: Any) -> GcraReservation:
    return await adm.reserve(kw.pop("who", principal()), g, queue_timeout_s=timeout_s, **kw)


async def test_headers_come_from_the_most_constraining_buckets() -> None:
    adm = admission(FakeClock(), rpm_per_key=10, tpm_per_key=1000)
    res = await reserve(adm, graph(calls=4, tokens=400))
    assert dict(res.headers()) == {
        "x-ratelimit-limit-requests": "10",
        "x-ratelimit-remaining-requests": "6",
        "x-ratelimit-reset-requests": "24",
        "x-ratelimit-limit-tokens": "1000",
        "x-ratelimit-remaining-tokens": "600",
        "x-ratelimit-reset-tokens": "24",
    }
    assert res.graph.total_calls == 4 and not adm.degraded


async def test_model_quota_buckets_join_and_can_be_the_tightest() -> None:
    adm = admission(FakeClock(), model_quotas={SOL: {"rpm": 12, "tpm": 100_000}})
    res = await reserve(adm, graph(calls=3, tokens=1000))
    assert res.headers()["x-ratelimit-limit-requests"] == "12"
    assert res.headers()["x-ratelimit-remaining-requests"] == "9"
    with pytest.raises(NoemaError) as err:  # the model bucket has 9 calls left
        await reserve(adm, graph(calls=10, tokens=10), timeout_s=0)
    assert err.value.code is ErrorCode.RPM_EXCEEDED
    other = WorkGraph({("opus-5.5", "generation"): 10}, {"opus-5.5": 10}, 10, 5, 5, 0)
    await reserve(adm, other)  # no quota for opus: only key and project buckets


async def test_rpm_wins_over_tpm_and_retry_after_is_the_largest_wait() -> None:
    adm = admission(FakeClock(), rpm_per_key=10, tpm_per_key=1000)
    await reserve(adm, graph(calls=10, tokens=1000))
    with pytest.raises(AdmissionRejected) as err:
        await reserve(adm, graph(calls=1, tokens=100))  # rpm: 6 s, tpm: 6 s -> both fail
    assert err.value.code is ErrorCode.RPM_EXCEEDED and err.value.retry_after_s == 6
    assert err.value.headers["x-ratelimit-remaining-requests"] == "0"
    with pytest.raises(AdmissionRejected) as err:
        await reserve(adm, graph(calls=0, tokens=300))
    assert err.value.code is ErrorCode.TPM_EXCEEDED and err.value.retry_after_s == 18


async def test_a_rejection_debits_nothing() -> None:
    clock = FakeClock()
    adm = admission(clock, rpm_per_key=10, tpm_per_key=1000)
    with pytest.raises(AdmissionRejected):
        await reserve(adm, graph(calls=5, tokens=2000))  # tokens fail
    await reserve(adm, graph(calls=10, tokens=1000))  # the full capacity is still there


async def test_capacity_refills_at_the_emission_interval() -> None:
    clock = FakeClock()
    adm = admission(clock, rpm_per_key=60, tpm_per_key=10_000_000)
    await reserve(adm, graph(calls=60, tokens=10))
    with pytest.raises(AdmissionRejected) as err:
        await reserve(adm, graph(calls=1, tokens=0))
    assert err.value.retry_after_s == 1
    await clock.advance(1.0)  # one call per second comes back
    await reserve(adm, graph(calls=1, tokens=0))
    with pytest.raises(AdmissionRejected):
        await reserve(adm, graph(calls=1, tokens=0))


async def test_system_principal_has_no_key_or_project_bucket() -> None:
    adm = admission(FakeClock(), rpm_per_key=1, rpm_per_project=1)
    sysp = system_principal("t_acme", purpose="sweeper", request_id="req_sys")
    res = await reserve(adm, graph(calls=50), who=sysp)
    assert res.headers() == {}


async def test_reconcile_refunds_unused_calls_and_tokens_exactly() -> None:
    adm = admission(FakeClock(), rpm_per_key=10, tpm_per_key=1000)
    res = await reserve(adm, graph(calls=10, tokens=1000))
    for _ in range(3):
        res.consume("generation", SOL)
    await res.reconcile({SOL: UsageRecord(100, 50)})  # 7 calls and 850 tokens come back
    await res.reconcile({})  # idempotent: only the first counts
    await res.release()
    await reserve(adm, graph(calls=7, tokens=850))
    with pytest.raises(AdmissionRejected):
        await reserve(adm, graph(calls=1, tokens=0))


async def test_release_without_reconcile_refunds_only_unconsumed_calls() -> None:
    adm = admission(FakeClock(), rpm_per_key=10, tpm_per_key=1000)
    res = await reserve(adm, graph(calls=5, tokens=1000))
    res.consume("generation", SOL)
    await res.release()
    await res.release()  # idempotent
    await reserve(adm, graph(calls=9, tokens=0))  # 4 calls refunded: 1 + 9 = 10
    with pytest.raises(AdmissionRejected) as err:
        await reserve(adm, graph(calls=0, tokens=1))  # the tokens stay debited
    assert err.value.code is ErrorCode.TPM_EXCEEDED


async def test_release_then_reconcile_refunds_the_unused_calls_once() -> None:
    adm = admission(FakeClock(), rpm_per_key=10, tpm_per_key=1000)
    await reserve(adm, graph(calls=5, tokens=0))  # another request: 5 calls
    res = await reserve(adm, graph(calls=5, tokens=1000))
    res.consume("generation", SOL)
    await res.release()  # the slot ends with the SSE: 4 unused calls back
    await res.reconcile({SOL: UsageRecord(100, 100)})  # usage known later: only the 800 unused tokens back
    await reserve(adm, graph(calls=4, tokens=800))  # 5 + 1 calls and 200 tokens really used
    with pytest.raises(AdmissionRejected) as err:
        await reserve(adm, graph(calls=1, tokens=0))
    assert err.value.code is ErrorCode.RPM_EXCEEDED
    with pytest.raises(AdmissionRejected) as err:
        await reserve(adm, graph(calls=0, tokens=1))
    assert err.value.code is ErrorCode.TPM_EXCEEDED


async def test_reconcile_on_model_buckets_uses_the_alias_usage() -> None:
    adm = admission(FakeClock(), model_quotas={SOL: {"rpm": 10, "tpm": 1000}})
    g = WorkGraph({(SOL, "generation"): 4, ("opus-5.5", "generation"): 4}, {SOL: 1000, "opus-5.5": 50}, 8, 525, 525, 0)
    res = await reserve(adm, g)
    res.consume("generation", SOL)
    res.consume("generation", "opus-5.5")
    await res.reconcile({SOL: UsageRecord(300, 100), "opus-5.5": UsageRecord(40, 10)})
    await reserve(adm, graph(calls=9, tokens=600))  # sol bucket: 1 call and 400 tokens used
    with pytest.raises(AdmissionRejected):
        await reserve(adm, graph(calls=1, tokens=0))


async def test_twg5_reconcile_refunds_the_unexpanded_group() -> None:
    adm = admission(FakeClock(), rpm_per_key=4)
    res = await reserve(adm, graph(calls=4, tokens=10, alias="luna-6", kind="decision_member"))
    for _ in range(3):  # round 1 + two expanded groups
        res.consume("decision_member", "luna-6")
    await res.reconcile({"luna-6": UsageRecord(5, 5)})
    await reserve(adm, graph(calls=1, tokens=0))  # the refunded call
    with pytest.raises(AdmissionRejected):
        await reserve(adm, graph(calls=1, tokens=0))


async def test_labeling_keeps_twenty_percent_headroom() -> None:
    adm = admission(FakeClock(), model_quotas={SOL: {"rpm": 10, "tpm": 100_000}})
    labeler = system_principal("t_acme", purpose="labeling", request_id="req_lab")
    lab = graph(calls=8, tokens=10, kind="labeling")
    await reserve(adm, lab, who=labeler)  # 80% used: still 20% free
    with pytest.raises(AdmissionRejected) as err:
        await reserve(adm, graph(calls=1, tokens=10, kind="labeling"), who=labeler)
    assert err.value.code is ErrorCode.QUEUE_TIMEOUT and err.value.retry_after_s == 1
    await reserve(adm, graph(calls=2, tokens=10))  # user traffic may use the headroom


async def test_labeling_keeps_twenty_percent_of_the_bulkhead_free_and_never_queues() -> None:
    clock = FakeClock()
    adm = admission(clock, model_quotas={"luna-6": {"rpm": 60, "tpm": 10**9}})  # bulkhead: ceil(60 / 6) = 10
    labeler = system_principal("t_acme", purpose="labeling", request_id="req_lab")
    lab = graph(calls=1, tokens=10, alias="luna-6", kind="labeling")
    held = [await reserve(adm, lab, who=labeler) for _ in range(8)]  # floor(0.8 * 10) places
    with pytest.raises(AdmissionRejected) as err:  # at once, although it may wait 10 s
        await reserve(adm, lab, timeout_s=10, who=labeler)
    assert err.value.code is ErrorCode.QUEUE_TIMEOUT and err.value.retry_after_s == 1
    users = [await reserve(adm, graph(calls=1, alias="luna-6", kind="decision_member"), timeout_s=0) for _ in "ab"]
    async with clock.autojump(), anyio.create_task_group() as tg:  # the bulkhead is full: a user request queues

        async def user() -> None:
            await reserve(adm, graph(calls=1, alias="luna-6", kind="decision_member"), timeout_s=5)

        tg.start_soon(user)
        await clock.sleep(0.5)
        await held[0].release()  # 9 live: the place is the queued user's, labeling does not jump ahead
        with pytest.raises(AdmissionRejected) as err:
            await reserve(adm, lab, timeout_s=10, who=labeler)
        assert err.value.code is ErrorCode.QUEUE_TIMEOUT
    for res in (*users, held[1]):  # 10 live (7 labeling, 3 users) -> 7
        await res.release()
    await reserve(adm, lab, timeout_s=0, who=labeler)  # the 8th place is labeling's again


async def test_a_one_place_bulkhead_is_never_taken_by_labeling() -> None:
    adm = admission(FakeClock(), model_quotas={"luna-6": {"rpm": 6, "tpm": 10**9}})  # cap 1: floor(0.8) = 0
    labeler = system_principal("t_acme", purpose="labeling", request_id="req_lab")
    with pytest.raises(AdmissionRejected) as err:
        await reserve(adm, graph(calls=1, tokens=10, alias="luna-6", kind="labeling"), who=labeler)
    assert err.value.code is ErrorCode.QUEUE_TIMEOUT
    await reserve(adm, graph(calls=1, tokens=10, alias="luna-6"), timeout_s=0)  # the place stays for users


async def test_rate_exhaustion_behind_queued_waiters_is_a_429_at_once() -> None:
    clock = FakeClock()
    adm = admission(clock, concurrent_streams_per_project=1, rpm_per_key=10, tpm_per_key=10**6)
    key_2 = principal(key_id="key_2")
    await reserve(adm, graph(calls=10, tokens=0), who=key_2)  # key_2's minute is used up
    holder = await reserve(adm, graph(calls=1, streams=1))  # key_1 holds the project's only stream slot
    async with clock.autojump(), anyio.create_task_group() as tg:

        async def queued() -> None:
            await reserve(adm, graph(calls=1, streams=1), timeout_s=5)

        tg.start_soon(queued)
        await clock.sleep(0.01)
        start = clock.monotonic()
        with pytest.raises(AdmissionRejected) as err:
            await reserve(adm, graph(calls=1, streams=1), timeout_s=2, who=key_2)
        assert err.value.code is ErrorCode.RPM_EXCEEDED and err.value.retry_after_s == 6
        assert clock.monotonic() == start and err.value.headers["x-ratelimit-remaining-requests"] == "0"
        with pytest.raises(AdmissionRejected) as err:  # passing buckets behind a waiter: queue, debit nothing
            await reserve(adm, graph(calls=1, streams=1), timeout_s=0.5, who=principal(key_id="key_3"))
        assert err.value.code is ErrorCode.QUEUE_TIMEOUT
        await holder.release()
    await reserve(adm, graph(calls=10, tokens=0), who=principal(key_id="key_3"))  # key_3's check debited nothing


async def test_bulkhead_caps_live_reservations_per_model() -> None:
    clock = FakeClock()
    adm = admission(clock, model_quotas={SOL: {"rpm": 12, "tpm": 1_000_000}})  # ceil(12 / 6) = 2 live
    first, _second = await reserve(adm, graph(calls=1)), await reserve(adm, graph(calls=1))
    async with clock.autojump():
        with pytest.raises(NoemaError) as err:
            await reserve(adm, graph(calls=1), timeout_s=0.5)
    assert err.value.code is ErrorCode.QUEUE_TIMEOUT and err.value.retry_after_s == 1
    await first.release()
    await reserve(adm, graph(calls=1), timeout_s=0)


async def test_a_stream_slot_frees_itself_after_the_deadline_lease() -> None:
    clock = FakeClock()
    adm = admission(clock, concurrent_streams_per_project=1)
    deadline = Deadline(clock.monotonic() + 1.0, clock)
    await reserve(adm, graph(streams=1), deadline=deadline)  # never released: a dead pod
    with pytest.raises(NoemaError):
        await reserve(adm, graph(streams=1), timeout_s=0)
    await clock.advance(1.0 + 30.0)  # the deadline + 30 s
    await reserve(adm, graph(streams=1), timeout_s=0)


async def test_cancellation_while_queued_debits_nothing() -> None:
    clock = FakeClock()
    adm = admission(clock, concurrent_streams_per_project=1, rpm_per_key=10)
    holder = await reserve(adm, graph(calls=1, streams=1))
    async with clock.autojump(), anyio.create_task_group() as tg:

        async def queued() -> None:
            await reserve(adm, graph(calls=5, streams=1), timeout_s=10)

        tg.start_soon(queued)
        await clock.sleep(1.0)
        tg.cancel_scope.cancel()
    await holder.release()  # refunds its unconsumed call
    await reserve(adm, graph(calls=10, tokens=0))  # the cancelled waiter's 5 calls were never debited
    with pytest.raises(AdmissionRejected):
        await reserve(adm, graph(calls=1, tokens=0))


async def test_overrun_counts_and_asserts_only_in_test_env() -> None:
    telemetry = FakeTelemetry()
    res = await reserve(admission(FakeClock(), telemetry, env="prod"), graph(calls=1))
    res.consume("generation", SOL)
    res.consume("generation", SOL)  # above the reserved count: logged and counted, the call proceeds
    res.consume("verification_member", "haiku-5.5")  # never reserved
    assert telemetry.counter("aghoo_reservation_overrun_total", kind="generation") == 1
    assert telemetry.counter("aghoo_reservation_overrun_total", kind="verification_member") == 1
    strict = await reserve(admission(FakeClock(), env="test"), graph(calls=1))
    strict.consume("generation", SOL)
    with pytest.raises(AssertionError, match="overrun"):
        strict.consume("generation", SOL)


async def test_queue_depth_gauge() -> None:
    clock = FakeClock()
    telemetry = FakeTelemetry()
    adm = admission(clock, telemetry, concurrent_streams_per_project=1)
    await reserve(adm, graph(streams=1))
    async with clock.autojump():
        with pytest.raises(NoemaError):
            await reserve(adm, graph(streams=1), timeout_s=0.3)
    depths = [m.value for m in telemetry.metrics if m.name == "aghoo_queue_depth"]
    assert depths == [1, 0]


async def test_rejections_are_counted_by_code() -> None:
    telemetry = FakeTelemetry()
    adm = admission(FakeClock(), telemetry, rpm_per_key=10, tpm_per_key=1000, concurrent_streams_per_project=1)
    await reserve(adm, graph(calls=5, tokens=500, streams=1))
    for g, code in (
        (graph(calls=6, tokens=0), ErrorCode.RPM_EXCEEDED),
        (graph(calls=0, tokens=600), ErrorCode.TPM_EXCEEDED),
        (graph(calls=1, tokens=0, streams=1), ErrorCode.QUEUE_TIMEOUT),
    ):
        with pytest.raises(AdmissionRejected) as err:
            await reserve(adm, g, timeout_s=0)
        assert err.value.code is code
        assert telemetry.counter("aghoo_admission_rejections_total", code=code.value) == 1
    sysp = system_principal("t_acme", purpose="labeling", request_id="req_lab")
    with pytest.raises(AdmissionRejected):  # labeling short of slot headroom: queue_timeout at once
        await reserve(adm, graph(calls=1, tokens=0, streams=1, kind="labeling"), who=sysp)
    assert telemetry.counter("aghoo_admission_rejections_total", code="queue_timeout") == 2
    assert telemetry.counter("aghoo_admission_rejections_total") == 4
    assert [m.value for m in telemetry.metrics if m.name == "aghoo_admission_degraded"] == [0]
