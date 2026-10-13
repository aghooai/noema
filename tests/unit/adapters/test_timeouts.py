"""Per-attempt stream timeouts (spec-02 §4.1, ROUTE_TIMEOUTS_V1): the `StreamWatchdog` limits and re-arming, and the
stream-level semantics: the connect window, `first_event` counting the response headers, errors after the connect
window as `ErrorEvent`s, the deadline cap, and timeouts after `Stop` ending the stream silently; idle_s as provider
silence (a slow consumer is not idleness) and the trailing usage after a text-less stop (no first_answer_s)."""

from __future__ import annotations

import anyio
import orjson
import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.scenario import PlannedEvent
from tests.unit.adapters.support import SOL, Scripted, collect, request, rig

from noema.adapters.resilience.timeouts import StreamWatchdog
from noema.config.models import route_timeouts_v1
from noema.core.clock import Deadline
from noema.core.errors import ErrorKind
from noema.core.protocols.adapters import (
    AdapterError,
    ErrorEvent,
    MessageStart,
    Stop,
    StreamEvent,
    StreamTimeouts,
    TextDelta,
    UsageEvent,
)

T = StreamTimeouts(connect_s=3, first_event_s=5, first_answer_s=8, idle_s=30)


def watchdog(clock: FakeClock, timeouts: StreamTimeouts = T, deadline_s: float = 100.0) -> StreamWatchdog:
    t0 = clock.monotonic()
    return StreamWatchdog(clock, timeouts, Deadline(t0 + deadline_s, clock), t0)


def test_limit_names_follow_the_state() -> None:
    clock = FakeClock()
    w, t0 = watchdog(clock), clock.monotonic()
    assert w.limit() == ("first_event", t0 + 5)
    w.on_event(text=False)
    assert w.limit() == ("first_answer", t0 + 8)
    w.on_event(text=True)
    assert w.limit() == ("idle", t0 + 30)
    assert watchdog(clock, deadline_s=2).limit() == ("deadline", t0 + 2)


async def waited(w: StreamWatchdog, clock: FakeClock) -> float:
    """Wait under the watchdog until it fires; the elapsed virtual seconds."""
    t0 = clock.monotonic()
    scope = w.begin_wait()
    assert scope is not None
    with scope:
        await clock.sleep(1000)
    w.end_wait()
    return clock.monotonic() - t0


async def test_fires_at_the_limit_while_waiting() -> None:
    clock = FakeClock()
    w = watchdog(clock)
    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(w.run)
        assert await waited(w, clock) == 5
    assert w.fired == "first_event"


async def test_rearms_when_an_event_moves_the_limit_earlier() -> None:
    clock = FakeClock()
    w = watchdog(clock, StreamTimeouts(connect_s=3, first_event_s=30, first_answer_s=60, idle_s=5))
    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(w.run)
        await clock.sleep(1)
        w.on_event(text=True)  # idle limit at 6 s, earlier than the 30 s the watchdog sleeps toward
        assert await waited(w, clock) == 5
    assert w.fired == "idle"


async def test_an_expired_limit_refuses_the_next_wait_and_close_stops_the_task() -> None:
    clock = FakeClock()
    w = watchdog(clock)
    async with clock.autojump(), anyio.create_task_group() as tg:
        tg.start_soon(w.run)
        await clock.sleep(6)  # the reader was busy: the watchdog parks instead of firing
        assert w.fired is None and w.expired() == "first_event" and w.begin_wait() is None
        w.close()


def test_route_table_rows() -> None:
    assert route_timeouts_v1("sol-6.1", "low") == T
    assert route_timeouts_v1("glm-5.3", None).first_event_s == 10


async def test_hang_before_headers_times_out_at_first_event_not_on_enter() -> None:
    async with rig() as r:
        r.scenario({"match": {"model": SOL}, "fault": {"kind": "hang_before_first_byte"}})
        timed = await collect(r, "sol-6.1")
        assert r.emulator.open_streams == 0
    assert len(timed) == 1
    at, ev = timed[0]
    assert at == 5000 and isinstance(ev, ErrorEvent) and ev.error.provider_code == "timeout:first_event"


async def test_error_response_after_the_connect_window_is_an_error_event() -> None:
    async with rig() as r:
        r.scenario({"match": {"model": SOL}, "fault": {"kind": "throttling"}, "latency_ms": 4000})
        timed = await collect(r, "sol-6.1")
    at, ev = timed[-1]
    assert at == 4000 and isinstance(ev, ErrorEvent) and ev.error.kind is ErrorKind.THROTTLED


async def test_error_response_inside_the_connect_window_raises_on_enter() -> None:
    async with rig() as r:
        r.scenario({"match": {"model": SOL}, "fault": {"kind": "access_denied"}, "latency_ms": 1000})
        with pytest.raises(AdapterError) as err:
            await collect(r, "sol-6.1")
    assert err.value.error.kind is ErrorKind.TENANT_ACCESS and err.value.error.cause == "model_access_denied"


async def test_deadline_caps_every_stream_timeout() -> None:
    async with rig() as r:
        r.scenario(
            {"match": {"model": SOL}, "stream": {"first_event_ms": 100, "chunks": ["a", "b"], "tokens_per_s": 1}}
        )
        timed = await collect(r, "sol-6.1", deadline_s=2.0)
    at, ev = timed[-1]
    assert at == 2000 and isinstance(ev, ErrorEvent) and ev.error.provider_code == "timeout:deadline"
    assert [type(e) for _, e in timed[:2]] == [MessageStart, TextDelta]


async def test_idle_after_the_stop_ends_silently() -> None:
    stop = b"data: " + orjson.dumps({"choices": [{"index": 0, "delta": {"content": "a"}, "finish_reason": "stop"}]})
    script = Scripted([stop + b"\n\n"], hang=True)  # neither usage nor [DONE] ever arrives
    async with rig(mantle_transport=script.transport()) as r:
        timed = await collect(r, "deepseek-3.2")
    assert [type(e) for _, e in timed] == [MessageStart, TextDelta, Stop]
    assert script.stream.closed


USAGE = {"input_tokens": 9, "output_tokens": 3}
BOTH = ["sol-6.1", "deepseek-3.2"]


@pytest.mark.parametrize("alias", BOTH)
async def test_a_slow_consumer_is_not_provider_idleness(alias: str) -> None:
    # adapters review: the provider sends everything within 50 ms; the consumer holds the first delta for 3 s, past
    # idle_s = 2. The buffered text must still arrive (no timeout:idle).
    timeouts = StreamTimeouts(connect_s=3, first_event_s=5, first_answer_s=8, idle_s=2)
    got: list[StreamEvent] = []
    async with rig() as r:
        r.emulator.plan_override = (
            PlannedEvent(0, "start"),
            *(PlannedEvent(10 * i, "text", text=t) for i, t in enumerate("abc", start=1)),
            PlannedEvent(40, "stop", stop="end_turn"),
            PlannedEvent(50, "usage", usage=USAGE),
        )
        async with r.clock.autojump():
            cm = r.registry.for_model(alias).stream(
                request(alias), tenant=r.tenant, timeouts=timeouts, deadline=r.deadline()
            )
            async with cm as events:
                async for ev in events:
                    got.append(ev)
                    if isinstance(ev, TextDelta) and ev.text == "a":
                        await r.clock.sleep(3.0)  # backpressure
    assert "".join(e.text for e in got if isinstance(e, TextDelta)) == "abc"
    assert [type(e) for e in got[-2:]] == [Stop, UsageEvent]
    assert not any(isinstance(e, ErrorEvent) for e in got)


@pytest.mark.parametrize("alias", BOTH)
async def test_usage_after_a_textless_stop_is_not_cut_by_first_answer(alias: str) -> None:
    # adapters review: a refusal at 1 s, the usage at 8.5 s (first_answer_s = 8, idle_s = 30): spec-02 "Stop, then
    # UsageEvent if the provider sent usage"
    async with rig() as r:
        r.emulator.plan_override = (
            PlannedEvent(0, "start"),
            PlannedEvent(1000, "stop", stop="content_filtered"),
            PlannedEvent(8500, "usage", usage=USAGE),
        )
        timed = await collect(r, alias)
    assert [type(e) for _, e in timed] == [MessageStart, Stop, UsageEvent]
    (_, stop), (at, usage) = timed[1], timed[2]
    assert isinstance(stop, Stop) and stop.reason == "content_filter" and at == 8500
    assert isinstance(usage, UsageEvent) and usage.usage.output_tokens == 3


@pytest.mark.parametrize("alias", BOTH)
async def test_idle_still_ends_the_wait_for_the_usage_after_the_stop(alias: str) -> None:
    timeouts = StreamTimeouts(connect_s=3, first_event_s=5, first_answer_s=8, idle_s=2)
    async with rig() as r:
        r.emulator.plan_override = (PlannedEvent(0, "start"), PlannedEvent(100, "stop", stop="content_filtered"))
        r.scenario({"match": {}, "stream": {"chunks": ["x"]}, "fault": {"kind": "stall_after", "n": 1}})  # hangs
        t0 = r.clock.monotonic()
        timed = await collect(r, alias, timeouts=timeouts)
        ended = r.clock.monotonic() - t0
    assert [type(e) for _, e in timed] == [MessageStart, Stop]  # silent end once idle passed (no usage)
    assert timed[-1][0] == 100 and ended == pytest.approx(2.1)
