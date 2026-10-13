"""Per-attempt stream timeouts (spec-02 §4.1, spec-05 §1.4 ROUTE_TIMEOUTS_V1, design 04 §2).

`StreamWatchdog` enforces `first_event_s` and `first_answer_s` (from the call start `t0`), `idle_s` and the
caller's deadline, all on the injected `Clock`, with one watchdog task per stream instead of a timer per event: the
reader wraps each provider await in the scope returned by `begin_wait()`; the watchdog task (`run()`) sleeps until
the earliest limit and cancels that scope when it passes. Per event this costs one `CancelScope` and a few
comparisons.

`idle_s` is provider silence: each wait for the next provider event (after the first) starts a fresh idle window,
so the time a consumer spends holding an event (backpressure, a stalled loop) never counts, and data the provider
already sent is read, not dropped. After `Stop` (`on_stop()`) only `idle_s` and the deadline apply: the trailing
usage of a refusal or a reasoning-only stop is not cut by `first_answer_s`.
"""

from __future__ import annotations

from typing import Final

import anyio

from noema.core.clock import Clock, Deadline
from noema.core.protocols.adapters import StreamTimeouts

LimitName = str  # "first_event" | "first_answer" | "idle" | "deadline"
_INF: Final = float("inf")


class StreamWatchdog:
    """The timeout state of one stream (see the module docstring). `fired` is the name of the limit that expired
    while the reader was waiting, else None."""

    __slots__ = (
        "_clock",
        "_closed",
        "_deadline_at",
        "_kick",
        "_sleep",
        "_target",
        "_timeouts",
        "_wait",
        "fired",
        "got_event",
        "got_text",
        "last_at",
        "stopped",
        "t0",
    )

    def __init__(self, clock: Clock, timeouts: StreamTimeouts, deadline: Deadline, t0: float) -> None:
        self._clock = clock
        self._timeouts = timeouts
        self._deadline_at = deadline.at
        self.t0 = t0
        self.last_at = t0
        self.got_event = False
        self.got_text = False
        self.stopped = False
        self.fired: LimitName | None = None
        self._wait: anyio.CancelScope | None = None
        self._sleep: anyio.CancelScope | None = None
        self._kick: anyio.Event | None = None
        self._target = _INF
        self._closed = False

    def limit(self) -> tuple[LimitName, float]:
        """The earliest pending limit: (name, monotonic time)."""
        name, at = "deadline", self._deadline_at
        t = self._timeouts
        if not self.got_event:
            first = self.t0 + t.first_event_s
            if first < at:
                name, at = "first_event", first
        else:
            idle = self.last_at + t.idle_s
            if idle < at:
                name, at = "idle", idle
        if not self.got_text and not self.stopped:
            answer = self.t0 + t.first_answer_s
            if answer < at:
                name, at = "first_answer", answer
        return name, at

    def expired(self) -> LimitName | None:
        """The name of a limit that has already passed, else None."""
        name, at = self.limit()
        return name if self._clock.monotonic() >= at else None

    def begin_wait(self) -> anyio.CancelScope | None:
        """A scope for the next provider await (a fresh idle window starts now), or None when a limit has already
        passed (time out now)."""
        if self.got_event:
            self.last_at = self._clock.monotonic()
        if self.fired is not None or self.expired() is not None:
            return None
        scope = anyio.CancelScope()
        self._wait = scope
        if self._kick is not None:
            self._kick.set()
        return scope

    def end_wait(self) -> None:
        self._wait = None

    def on_event(self, *, text: bool) -> None:
        """A provider event arrived (`text`: it carried answer text); re-arms the watchdog when the limit moved
        earlier."""
        self.last_at = self._clock.monotonic()
        self.got_event = True
        if text:
            self.got_text = True
        if self._sleep is not None and self.limit()[1] < self._target:
            self._sleep.cancel()

    def on_stop(self) -> None:
        """The provider's stop arrived: first_event and first_answer no longer apply (only idle and the deadline,
        while the reader waits for the trailing usage)."""
        self.stopped = True
        self.got_event = True

    def close(self) -> None:
        """Stop the watchdog task (the stream ended)."""
        self._closed = True
        if self._sleep is not None:
            self._sleep.cancel()
        if self._kick is not None:
            self._kick.set()

    async def run(self) -> None:
        """The watchdog task body; returns after firing or `close()`."""
        while not self._closed:
            name, at = self.limit()
            delay = at - self._clock.monotonic()
            if delay > 0:
                self._target = at
                with anyio.CancelScope() as scope:
                    self._sleep = scope
                    await self._clock.sleep(delay)
                self._sleep, self._target = None, _INF
                continue
            if self._wait is not None:
                self.fired = name
                self._wait.cancel()
                return
            self._kick = anyio.Event()  # the reader is busy: it checks expiry itself before its next wait
            await self._kick.wait()
            self._kick = None
