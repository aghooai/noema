"""Admission + Reservation conformance (spec-02 §8, spec-11 §1.2): GCRA debits, 429 codes with retry-after,
headers, consume overrun, reconcile/release idempotency, stream slots with the queue timeout."""

from __future__ import annotations

import anyio
import pytest
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from

from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import system_principal
from noema.core.protocols.ops import WorkGraph
from noema.core.values import UsageRecord

from .kits import AdmissionKit, principal
from .registry import kit_fixture

kit = kit_fixture("admission")
RATE_HEADERS = {f"x-ratelimit-{a}-{b}" for a in ("limit", "remaining", "reset") for b in ("requests", "tokens")}


def graph(calls: int = 4, tokens: int = 1000, streams: int = 0) -> WorkGraph:
    return WorkGraph({("sol-6.1", "generation"): calls}, {"sol-6.1": tokens}, calls, tokens // 2, tokens // 2, streams)


def registry(**tenant_limits: int) -> FakeConfigRegistry:
    return FakeConfigRegistry(snapshot_from(patched_example({"tenants": {"t_acme": {"limits": tenant_limits}}})))


async def test_reserve_and_headers(kit: AdmissionKit) -> None:
    admission = kit.make(registry())
    res = await admission.reserve(principal(), graph(), queue_timeout_s=1)
    headers = res.headers()
    assert set(headers) == RATE_HEADERS
    assert int(headers["x-ratelimit-limit-requests"]) == 6000
    # every value is a non-negative integer string (openapi RateLimit* headers; ops-admission.md item 2)
    assert all(v.isascii() and v.isdigit() for v in headers.values()), headers
    await res.reconcile({"sol-6.1": UsageRecord(10, 10)})
    await res.reconcile({})  # idempotent
    await res.release()
    await res.release()


async def test_rpm_exhaustion_is_429_with_retry_after(kit: AdmissionKit) -> None:
    admission = kit.make(registry(rpm_per_key=10))
    await admission.reserve(principal(), graph(calls=10), queue_timeout_s=1)
    with pytest.raises(NoemaError) as err:
        await admission.reserve(principal(), graph(calls=1), queue_timeout_s=1)
    assert err.value.code is ErrorCode.RPM_EXCEEDED
    assert err.value.retry_after_s is not None and err.value.retry_after_s >= 1


async def test_more_than_the_limit_never_passes(kit: AdmissionKit) -> None:
    admission = kit.make(registry(rpm_per_key=10))
    with pytest.raises(NoemaError) as err:
        await admission.reserve(principal(), graph(calls=11), queue_timeout_s=1)
    assert err.value.code is ErrorCode.RPM_EXCEEDED and err.value.retry_after_s == 60


async def test_tpm_exhaustion(kit: AdmissionKit) -> None:
    admission = kit.make(registry(tpm_per_key=1000))
    with pytest.raises(NoemaError) as err:
        await admission.reserve(principal(), graph(tokens=5000), queue_timeout_s=1)
    assert err.value.code is ErrorCode.TPM_EXCEEDED


async def test_capacity_recovers_over_time(kit: AdmissionKit) -> None:
    admission = kit.make(registry(rpm_per_key=10))
    await admission.reserve(principal(), graph(calls=10), queue_timeout_s=1)
    await kit.clock.advance(61)
    await admission.reserve(principal(), graph(calls=10), queue_timeout_s=1)


async def test_system_principal_skips_key_buckets(kit: AdmissionKit) -> None:
    admission = kit.make(registry(rpm_per_key=1, rpm_per_project=1))
    sysp = system_principal("t_acme", purpose="labeling", request_id="req_sys")
    await admission.reserve(sysp, graph(calls=5), queue_timeout_s=1)


async def test_consume_overrun_is_an_assertion_in_test_mode(kit: AdmissionKit) -> None:
    res = await kit.make(registry()).reserve(principal(), graph(calls=1), queue_timeout_s=1)
    res.consume("generation", "sol-6.1")
    with pytest.raises(AssertionError):
        res.consume("generation", "sol-6.1")


async def test_stream_slots_queue_then_time_out(kit: AdmissionKit) -> None:
    admission = kit.make(registry(concurrent_streams_per_project=1))
    first = await admission.reserve(principal(), graph(streams=1), queue_timeout_s=1)
    async with kit.clock.autojump():
        with pytest.raises(NoemaError) as err:
            await admission.reserve(principal(), graph(streams=1), queue_timeout_s=0.5)
    assert err.value.code is ErrorCode.QUEUE_TIMEOUT
    await first.release()
    again = await admission.reserve(principal(), graph(streams=1), queue_timeout_s=0.5)
    await again.release()


async def test_release_wakes_a_queued_reservation(kit: AdmissionKit) -> None:
    admission = kit.make(registry(concurrent_streams_per_project=1))
    first = await admission.reserve(principal(), graph(streams=1), queue_timeout_s=1)
    got: list[object] = []
    async with kit.clock.autojump(), anyio.create_task_group() as tg:

        async def waiter() -> None:
            got.append(await admission.reserve(principal(), graph(streams=1), queue_timeout_s=5))

        tg.start_soon(waiter)
        await kit.clock.sleep(1.0)
        assert not got
        await first.release()
    assert len(got) == 1
