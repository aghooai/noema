"""The deadline bounds the whole call, credentials included (spec-02 §4.1): `complete()` and `stream().__aenter__`
count a slow `broker.get` against the deadline (streams also against `connect_s`, from the call start), and no
request reaches the wire once the deadline has passed (the explicit pre-send check in `_send`)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from tests.unit.adapters.support import DEEPSEEK, SOL, TIMEOUTS, Rig, request, rig

from noema.core.errors import ErrorKind
from noema.core.protocols.adapters import AdapterError, ErrorEvent

ADAPTERS = [("sol-6.1", SOL), ("deepseek-3.2", DEEPSEEK)]


def assert_nothing_held(r: Rig, adapter: Any) -> None:
    assert adapter.open_streams == 0 and r.emulator.open_streams == 0
    assert [e.inflight for e in adapter._pool._entries.values()] in ([], [0])


def assert_timeout(err: pytest.ExceptionInfo[AdapterError], name: str) -> None:
    assert err.value.error.kind is ErrorKind.RETRYABLE and err.value.error.provider_code == f"timeout:{name}"


async def complete(r: Rig, alias: str, deadline_s: float) -> Any:
    async with r.clock.autojump():
        return await r.registry.for_model(alias).complete(
            request(alias, retry="none"), tenant=r.tenant, deadline=r.deadline(deadline_s)
        )


async def enter_stream(r: Rig, alias: str, deadline_s: float) -> list[tuple[int, Any]]:
    t0 = r.clock.monotonic()
    async with r.clock.autojump():
        cm = r.registry.for_model(alias).stream(
            request(alias), tenant=r.tenant, timeouts=TIMEOUTS, deadline=r.deadline(deadline_s)
        )
        async with cm as events:
            return [(round((r.clock.monotonic() - t0) * 1000), ev) async for ev in events]


@pytest.mark.parametrize(("alias", "model"), ADAPTERS)
async def test_complete_slow_broker_ends_at_the_deadline_with_no_request(alias: str, model: str) -> None:
    async with rig() as r:
        r.scenario({"match": {"model": model}, "answer": {"raw_text": "ok"}})
        r.broker.delay_s = 10.0
        t0 = r.clock.monotonic()
        with pytest.raises(AdapterError) as err:
            await complete(r, alias, deadline_s=1.0)
        assert r.clock.monotonic() - t0 == pytest.approx(1.0)
        assert r.emulator.requests == []
        assert_nothing_held(r, r.registry.for_model(alias))
    assert_timeout(err, "deadline")


@pytest.mark.parametrize(("alias", "model"), ADAPTERS)
async def test_complete_counts_the_broker_time_against_the_deadline(alias: str, model: str) -> None:
    async with rig() as r:
        r.scenario({"match": {"model": model}, "answer": {"raw_text": "ok"}, "latency_ms": 600})
        r.broker.delay_s = 0.6  # 0.6 s + 0.6 s > the 1 s deadline
        t0 = r.clock.monotonic()
        with pytest.raises(AdapterError) as err:
            await complete(r, alias, deadline_s=1.0)
        assert r.clock.monotonic() - t0 == pytest.approx(1.0)
        assert len(r.emulator.requests) == 1
    assert_timeout(err, "deadline")


@pytest.mark.parametrize(("alias", "model"), ADAPTERS)
async def test_complete_with_an_expired_deadline_sends_nothing(alias: str, model: str) -> None:
    async with rig() as r:
        r.scenario({"match": {"model": model}, "answer": {"raw_text": "ok"}})
        with pytest.raises(AdapterError) as err:
            await complete(r, alias, deadline_s=0.0)
        assert r.emulator.requests == []
    assert_timeout(err, "deadline")


@pytest.mark.parametrize("alias", ["sol-6.1", "deepseek-3.2"])
async def test_send_refuses_once_the_deadline_passed_also_for_the_connect_retry(alias: str) -> None:
    async with rig() as r:
        adapter: Any = r.registry.for_model(alias)
        sends: list[float] = []

        async def failing_send() -> None:
            sends.append(r.clock.monotonic())
            await r.clock.sleep(1.0)  # the deadline passes while the first attempt fails to connect
            raise httpx.ConnectError("reset")

        req = request(alias, retry="default")
        async with r.clock.autojump():
            with pytest.raises(AdapterError) as late:
                await adapter._send(req, r.tenant, "m", failing_send, deadline_at=r.clock.monotonic() + 0.5)
            with pytest.raises(AdapterError) as expired:
                await adapter._send(req, r.tenant, "m", failing_send, deadline_at=r.clock.monotonic())
        assert len(sends) == 1  # neither the retry nor the call past its deadline was sent
    assert_timeout(late, "deadline")
    assert_timeout(expired, "deadline")


@pytest.mark.parametrize(("alias", "model"), ADAPTERS)
@pytest.mark.parametrize(("deadline_s", "limit", "at_s"), [(2.0, "deadline", 2.0), (60.0, "connect", 3.0)])
async def test_stream_slow_broker_raises_on_enter_at_the_connect_window(
    alias: str, model: str, deadline_s: float, limit: str, at_s: float
) -> None:
    async with rig() as r:
        r.scenario({"match": {"model": model}, "stream": {"chunks": ["a"]}})
        r.broker.delay_s = 10.0
        t0 = r.clock.monotonic()
        with pytest.raises(AdapterError) as err:
            await enter_stream(r, alias, deadline_s)
        assert r.clock.monotonic() - t0 == pytest.approx(at_s)  # connect_s = 3, capped by the deadline
        assert r.emulator.requests == []
        assert_nothing_held(r, r.registry.for_model(alias))
    assert_timeout(err, limit)


@pytest.mark.parametrize(("alias", "model"), ADAPTERS)
async def test_stream_with_an_expired_deadline_sends_nothing(alias: str, model: str) -> None:
    async with rig() as r:
        r.scenario({"match": {"model": model}, "stream": {"chunks": ["a"]}})
        with pytest.raises(AdapterError) as err:
            await enter_stream(r, alias, 0.0)
        assert r.emulator.requests == []
        assert_nothing_held(r, r.registry.for_model(alias))
    assert_timeout(err, "deadline")


@pytest.mark.parametrize(("alias", "model"), ADAPTERS)
async def test_stream_first_event_counts_from_the_call_start(alias: str, model: str) -> None:
    async with rig() as r:
        r.scenario({"match": {"model": model}, "fault": {"kind": "hang_before_first_byte"}})
        r.broker.delay_s = 1.0  # inside the 3 s connect window
        timed = await enter_stream(r, alias, 60.0)
        assert len(r.emulator.requests) == 1
        assert_nothing_held(r, r.registry.for_model(alias))
    assert len(timed) == 1
    at, ev = timed[0]
    assert at == 5000 and isinstance(ev, ErrorEvent) and ev.error.provider_code == "timeout:first_event"
