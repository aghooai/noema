"""The streaming call shared by both adapters (spec-02 §4.1 `Adapter.stream`).

`StreamSession` is the async context manager `Adapter.stream()` returns:
- `__aenter__` is bounded by the connect window: `connect_s` from the call start, capped by the deadline. It runs
  the adapter's `prepare()` (wire rules, credentials and the account assertion: failures raise `AdapterError`
  before any byte); a `prepare()` that does not finish inside the window raises `AdapterError(timeout:connect)`
  (`timeout:deadline` when the deadline caps the window) and sends nothing. It then starts the request in a
  background task and waits for its response headers until the window ends. A provider error in that window
  raises `AdapterError`; a request still pending then is treated as connected (its headers count against
  `first_event_s`, which like `first_answer_s` counts from the call start).
- The iterator yields normalized events under a `StreamWatchdog` (first_event, first_answer, idle, deadline). Any
  later failure (an error response, an exception event, a malformed frame or a provider event of the wrong shape,
  a timeout) becomes one `ErrorEvent`, the last event. A `UsageEvent` the provider sends before its stop is held
  until after `Stop`. After `Stop` only idle and the deadline apply to the wait for the usage, and a failure or
  timeout ends the stream silently (the answer is complete; usage may be missing).
- `__aexit__` cancels: the background tasks are cancelled and the provider stream is closed without draining. The
  close and the release of the call's resources always run, also when the consumer leaves because an enclosing
  cancel scope was cancelled (the cancellation is re-raised after them).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol

import anyio

from noema.adapters.classify import MSG_MALFORMED, StreamFault, stream_error, timeout_error
from noema.adapters.resilience.timeouts import StreamWatchdog
from noema.core.clock import Clock, Deadline, deadline_scope
from noema.core.errors import ClassifiedError
from noema.core.protocols.adapters import (
    AdapterError,
    ErrorEvent,
    Stop,
    StreamEvent,
    StreamTimeouts,
    TextDelta,
    UsageEvent,
)

Fed = StreamEvent | tuple[StreamEvent, ...] | None


class RawStream(Protocol):
    """One opened provider stream. `usage_ends_stream`: the usage event is the provider's last event (Converse)."""

    usage_ends_stream: bool

    async def next(self) -> object | None:
        """The next raw provider event; None at the end of the stream. May raise (classified by the session)."""
        ...

    def feed(self, raw: object) -> Fed:
        """Normalize one raw event (sync); may raise `StreamFault`."""
        ...

    def finish(self, stopped: bool) -> StreamEvent | None:
        """The event for the end of the stream (an `ErrorEvent` if it ended early), else None."""
        ...

    async def aclose(self) -> None:
        """Close without draining."""
        ...


@dataclass(slots=True)
class PreparedCall:
    """What `prepare()` returns: `open` sends the request (with the in-adapter connect retry) and returns the
    stream; `fail` classifies an exception (with the adapter's side effects); `release` frees the call's
    resources (connection pool lease, open-stream count)."""

    open: Callable[[], Awaitable[RawStream]]
    fail: Callable[[BaseException], ClassifiedError]
    release: Callable[[], Awaitable[None]]


class StreamSession:
    """Implements the `Adapter.stream()` context manager (see the module docstring)."""

    __slots__ = (
        "_call",
        "_clock",
        "_closed",
        "_deadline",
        "_gen",
        "_open_exc",
        "_opened",
        "_prepare",
        "_raw",
        "_stopped",
        "_tg",
        "_timeouts",
        "_watchdog",
    )

    def __init__(
        self,
        prepare: Callable[[], Awaitable[PreparedCall]],
        *,
        clock: Clock,
        timeouts: StreamTimeouts,
        deadline: Deadline,
    ) -> None:
        self._prepare = prepare
        self._clock = clock
        self._timeouts = timeouts
        self._deadline = deadline
        self._call: PreparedCall | None = None
        self._tg: anyio.abc.TaskGroup | None = None
        self._watchdog: StreamWatchdog | None = None
        self._opened = anyio.Event()
        self._raw: RawStream | None = None
        self._open_exc: Exception | None = None
        self._gen: AsyncGenerator[StreamEvent] | None = None
        self._stopped = False
        self._closed = False

    async def __aenter__(self) -> AsyncIterator[StreamEvent]:
        t0 = self._clock.monotonic()  # the call start: connect_s, first_event_s and first_answer_s count from here
        connect_at = min(t0 + self._timeouts.connect_s, self._deadline.at)
        try:
            async with deadline_scope(self._clock, connect_at) as scope:
                self._call = await self._prepare()  # raises AdapterError before any byte
            if scope.cancelled_caught or self._clock.monotonic() >= connect_at:  # no request after the window
                raise AdapterError(timeout_error("deadline" if connect_at >= self._deadline.at else "connect"))
            call = self._call
            assert call is not None
            self._watchdog = StreamWatchdog(self._clock, self._timeouts, self._deadline, t0)
            self._tg = anyio.create_task_group()
            await self._tg.__aenter__()
            self._tg.start_soon(self._open_task)
            async with deadline_scope(self._clock, connect_at):
                await self._opened.wait()
            if self._open_exc is not None:
                raise AdapterError(call.fail(self._open_exc)) from None
            self._tg.start_soon(self._watchdog.run)
        except BaseException:
            await self._shutdown()
            raise
        self._gen = self._events()
        return self._gen

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._shutdown()

    async def _open_task(self) -> None:
        assert self._call is not None
        try:
            self._raw = await self._call.open()
        except Exception as exc:  # classified by __aenter__ or the iterator
            self._open_exc = exc
        finally:
            self._opened.set()

    async def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._gen is not None:
                with anyio.CancelScope(shield=True):
                    await self._gen.aclose()
            if self._watchdog is not None:
                self._watchdog.close()
            if self._tg is not None:  # exited outside any newer cancel scope (LIFO), so it cannot be shielded
                self._tg.cancel_scope.cancel()
                # Waits for both tasks, then re-raises an outer cancellation (the consumer's scope was cancelled).
                await self._tg.__aexit__(None, None, None)
        finally:  # unconditional: the provider response and the pool lease are freed even when cancelled
            with anyio.CancelScope(shield=True):
                try:
                    if self._raw is not None:
                        await self._raw.aclose()
                finally:
                    if self._call is not None:
                        await self._call.release()

    def _timeout(self, w: StreamWatchdog) -> ErrorEvent | None:
        if self._stopped:
            return None
        return ErrorEvent(timeout_error(w.fired or w.expired() or "deadline"))

    async def _events(self) -> AsyncGenerator[StreamEvent]:
        w, call = self._watchdog, self._call
        assert w is not None
        assert call is not None
        try:
            if not self._opened.is_set():  # still waiting for the response headers (first_event counts)
                scope = w.begin_wait()
                if scope is not None:
                    with scope:
                        await self._opened.wait()
                    w.end_wait()
                if scope is None or w.fired is not None or not self._opened.is_set():
                    if (timed := self._timeout(w)) is not None:
                        yield timed
                    return
            if self._open_exc is not None:
                yield ErrorEvent(call.fail(self._open_exc))
                return
            raw_stream = self._raw
            assert raw_stream is not None
            held: UsageEvent | None = None
            while True:
                scope = w.begin_wait()
                if scope is None:
                    if (timed := self._timeout(w)) is not None:
                        yield timed
                    return
                with scope:
                    raw = await raw_stream.next()
                w.end_wait()
                if w.fired is not None:
                    if (timed := self._timeout(w)) is not None:
                        yield timed
                    return
                if raw is None:
                    end = raw_stream.finish(self._stopped)
                    if end is not None and not (self._stopped and isinstance(end, ErrorEvent)):
                        yield end
                    return
                try:
                    out = raw_stream.feed(raw)
                except (TypeError, AttributeError, KeyError, IndexError):  # a provider event of the wrong shape
                    raise StreamFault(stream_error("malformed:chunk", MSG_MALFORMED)) from None
                if out is None:
                    w.on_event(text=False)
                    continue
                if isinstance(out, tuple):
                    events = out
                    text = False
                    for e in out:
                        text = text or isinstance(e, TextDelta)
                else:
                    events = (out,)
                    text = isinstance(out, TextDelta)
                w.on_event(text=text)
                for ev in events:
                    if isinstance(ev, ErrorEvent):
                        if not self._stopped:
                            yield ev
                        return
                    if isinstance(ev, UsageEvent):
                        if not self._stopped:
                            held = ev
                            continue
                        yield ev
                        if raw_stream.usage_ends_stream:
                            return
                        continue
                    yield ev
                    if isinstance(ev, Stop):
                        self._stopped = True
                        w.on_stop()
                        if held is not None:
                            yield held
                            held = None
                            if raw_stream.usage_ends_stream:
                                return
        except Exception as exc:
            if not self._stopped:
                yield ErrorEvent(call.fail(exc))
        finally:
            w.close()
