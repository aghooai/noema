"""Clock-driven waits shared by the ledger loops (outbox, jobs, idempotency): every timeout goes through the injected
`Clock`, so the same code runs on `SystemClock` in production and on a virtual clock in tests."""

from __future__ import annotations

import anyio
import anyio.lowlevel

from noema.core.clock import Clock


async def wait_event(event: anyio.Event, clock: Clock, timeout_s: float) -> bool:
    """Wait until `event` is set or `timeout_s` passed on `clock`; True iff the event was set."""
    if event.is_set():
        return True
    if timeout_s <= 0:
        await anyio.lowlevel.checkpoint()
        return event.is_set()
    async with anyio.create_task_group() as tg:

        async def timer() -> None:
            await clock.sleep(timeout_s)
            tg.cancel_scope.cancel()

        tg.start_soon(timer)
        await event.wait()
        tg.cancel_scope.cancel()
    return event.is_set()


class Wakeup:
    """A re-armable wake-up signal: `set()` wakes the current waiter; `wait(clock, timeout_s)` re-arms."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = anyio.Event()

    def set(self) -> None:
        """Wake the waiter (sync; idempotent until the next `wait`)."""
        self._event.set()

    async def wait(self, clock: Clock, timeout_s: float) -> bool:
        """Until `set()` or `timeout_s`; True iff woken. The signal is consumed."""
        woken = await wait_event(self._event, clock, timeout_s)
        if woken:
            self._event = anyio.Event()
        return woken
