"""Leaving a stream because an enclosing cancel scope was cancelled (generation's `att.scope.cancel()` for race
losers, restarts, turn cancel and client disconnects) still closes the provider stream and frees the pool lease
(spec-02 §4.1 "exiting the context = cancel"), on the virtual and on the real clock, for both adapters."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from tests.unit.adapters.support import DEEPSEEK, SOL, TIMEOUTS, Rig, request, rig

from noema.adapters._stream import PreparedCall, RawStream, StreamSession
from noema.core.clock import Deadline, SystemClock
from noema.core.errors import ClassifiedError, ErrorKind
from noema.core.protocols.adapters import StreamEvent, TextDelta

ADAPTERS = [("sol-6.1", SOL), ("deepseek-3.2", DEEPSEEK)]


def leased(adapter: Any) -> list[int]:
    """The pool's in-flight counts (0 = every call released its client)."""
    return [entry.inflight for entry in adapter._pool._entries.values()]


def assert_released(r: Rig, adapter: Any) -> None:
    assert adapter.open_streams == 0
    assert r.emulator.open_streams == 0  # the provider response was closed
    assert leased(adapter) in ([], [0])


async def cancel_consumer(
    r: Rig, alias: str, deadline: Deadline, *, cancel_at: float | None = None
) -> tuple[anyio.CancelScope, list[StreamEvent]]:
    """Consume a stream inside a cancel scope that another task cancels: after `cancel_at` virtual seconds, or as
    soon as the first `TextDelta` arrived (the consumer then waits for the next event)."""
    adapter = r.registry.for_model(alias)
    scope = anyio.CancelScope()
    seen: list[StreamEvent] = []
    got_text = anyio.Event()

    async def consumer() -> None:
        with scope:
            async with adapter.stream(request(alias), tenant=r.tenant, timeouts=TIMEOUTS, deadline=deadline) as events:
                async for ev in events:
                    seen.append(ev)
                    if isinstance(ev, TextDelta):
                        got_text.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(consumer)
        if cancel_at is None:
            await got_text.wait()
        else:
            await r.clock.sleep(cancel_at)
        scope.cancel()
    return scope, seen


@pytest.mark.parametrize(("alias", "model"), ADAPTERS)
@pytest.mark.parametrize("cancel_at", [0.5, 1.5, 2.5])
async def test_outer_cancel_while_waiting_for_the_next_event_closes_and_releases(
    alias: str, model: str, cancel_at: float
) -> None:
    async with rig() as r:
        chunks = ["a", "b", "c", "d"]  # 1 s apart
        r.scenario({"match": {"model": model}, "stream": {"chunks": chunks, "tokens_per_s": 4}})
        async with r.clock.autojump():
            scope, seen = await cancel_consumer(r, alias, r.deadline(), cancel_at=cancel_at)
            await r.clock.sleep(60)  # nothing is closed later (no timer or gc involved)
        assert scope.cancelled_caught
        assert sum(isinstance(e, TextDelta) for e in seen) == int(cancel_at) + 1  # cancelled mid-stream
        assert_released(r, r.registry.for_model(alias))


@pytest.mark.parametrize(("alias", "model"), ADAPTERS)
async def test_outer_cancel_while_waiting_for_the_headers_releases(alias: str, model: str) -> None:
    async with rig() as r:
        r.scenario({"match": {"model": model}, "fault": {"kind": "hang_before_first_byte"}})
        async with r.clock.autojump():
            scope, seen = await cancel_consumer(r, alias, r.deadline(), cancel_at=1.0)  # inside the connect window
        assert scope.cancelled_caught and seen == []
        assert len(r.emulator.requests) == 1
        assert_released(r, r.registry.for_model(alias))


@pytest.mark.parametrize(("alias", "model"), ADAPTERS)
async def test_outer_cancel_while_fetching_credentials_sends_nothing(alias: str, model: str) -> None:
    async with rig() as r:
        r.scenario({"match": {"model": model}, "stream": {"chunks": ["a"]}})
        r.broker.delay_s = 2.0
        async with r.clock.autojump():
            scope, seen = await cancel_consumer(r, alias, r.deadline(), cancel_at=1.0)
        assert scope.cancelled_caught and seen == []
        assert r.emulator.requests == []
        assert_released(r, r.registry.for_model(alias))


@pytest.mark.parametrize(("alias", "model"), ADAPTERS)
async def test_repeated_outer_cancels_leave_the_pool_usable(alias: str, model: str) -> None:
    async with rig() as r:
        r.scenario({"match": {"model": model}, "stream": {"chunks": ["a", "b", "c"], "tokens_per_s": 4}})
        adapter: Any = r.registry.for_model(alias)
        async with r.clock.autojump():
            for _ in range(5):
                await cancel_consumer(r, alias, r.deadline(), cancel_at=0.5)
            assert_released(r, adapter)
            adapter._evict(r.tenant.tenant_id)  # an evicted idle client is closed at the next acquire
            async with adapter.stream(
                request(alias), tenant=r.tenant, timeouts=TIMEOUTS, deadline=r.deadline()
            ) as events:
                texts = [e.text async for e in events if isinstance(e, TextDelta)]
        assert texts == ["a", "b", "c"]
        assert_released(r, adapter)


@pytest.mark.parametrize(("alias", "model"), ADAPTERS)
async def test_outer_cancel_on_the_system_clock_always_releases(alias: str, model: str) -> None:
    clock = SystemClock()
    async with rig(adapter_clock=clock) as r:
        # every planned event at 0 ms on the emulator's frozen clock: one text chunk, then the provider stalls
        r.scenario(
            {"match": {"model": model}, "stream": {"chunks": ["a", "b"]}, "fault": {"kind": "stall_after", "n": 1}}
        )
        for _ in range(25):
            scope, seen = await cancel_consumer(r, alias, Deadline(clock.monotonic() + 60, clock))
            assert scope.cancelled_caught and isinstance(seen[-1], TextDelta)
            assert_released(r, r.registry.for_model(alias))
        assert len(r.emulator.requests) == 25


class _StallingRaw:
    """A provider stream: one text chunk, then nothing (the consumer waits); `aclose()` fails."""

    usage_ends_stream = False

    def __init__(self) -> None:
        self.reads = 0

    async def next(self) -> object | None:
        self.reads += 1
        if self.reads == 1:
            return "a"
        await anyio.sleep_forever()
        return None

    def feed(self, raw: object) -> StreamEvent | None:
        return TextDelta(str(raw))

    def finish(self, stopped: bool) -> StreamEvent | None:
        return None

    async def aclose(self) -> None:
        raise OSError("close failed")


async def test_release_runs_even_when_closing_the_provider_stream_fails() -> None:
    clock = SystemClock()
    released: list[bool] = []

    async def prepare() -> PreparedCall:
        async def open_stream() -> RawStream:
            return _StallingRaw()

        async def release() -> None:
            released.append(True)

        return PreparedCall(open_stream, lambda exc: ClassifiedError(ErrorKind.FATAL, "x"), release)

    session = StreamSession(prepare, clock=clock, timeouts=TIMEOUTS, deadline=Deadline(clock.monotonic() + 60, clock))

    async def consume() -> None:
        with anyio.CancelScope() as scope:
            async with session as events:
                async for _ in events:
                    scope.cancel()  # the next wait is cancelled from outside

    with pytest.raises(OSError, match="close failed"):
        await consume()
    assert released == [True]
