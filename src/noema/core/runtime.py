"""`TaskGroupRuntime`: the `ProcessRuntime` of every process (spec-01 §3, spec-11 §7).

Lifecycle (driven by the composition root, or by a test): `await open()` enters the root task group; `begin_drain()`
(sync, idempotent; safe to schedule from a signal handler through `loop.call_soon_threadsafe`) sets `draining` and
starts every drain hook; `await drain(timeout_s)` begins draining and waits for the hooks, at most `timeout_s` on the
runtime's clock; `await close()` cancels everything still running and leaves the task group. `open` and `close` must
run in the same task (the app lifespan).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any, Final, cast

import anyio
import anyio.abc

from noema.core.clock import Clock, deadline_scope

_log: Final = logging.getLogger("noema.runtime")
DRAIN_GRACE_S: Final = 30.0  # running turns continue up to 30 s after SIGTERM (spec-11 §7 step 3)


class TaskGroupRuntime:
    """Implements `ProcessRuntime` over one anyio task group (see the module docstring)."""

    def __init__(self, clock: Clock, *, grace_s: float = DRAIN_GRACE_S) -> None:
        self._clock = clock
        self._grace_s = grace_s
        self._stack: AsyncExitStack | None = None
        self._tg: anyio.abc.TaskGroup | None = None
        self._closed = False
        self._draining = False
        self._hooks: list[tuple[str, Callable[[float], Awaitable[None]]]] = []
        self._pending_hooks = 0
        self._hooks_done = anyio.Event()
        self._locals: dict[str, object] = {}

    # --- ProcessRuntime ------------------------------------------------------------------------------------------
    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def is_open(self) -> bool:
        """True between `open()` and `close()`."""
        return self._tg is not None and not self._closed

    def spawn(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        if self._tg is None or self._closed:
            raise RuntimeError(f"the process runtime is not open (spawn {name!r})")
        self._tg.start_soon(self._guarded, name, fn, name=name)

    def on_drain(self, name: str, callback: Callable[[float], Awaitable[None]]) -> None:
        self._hooks.append((name, callback))
        if self._draining:
            self._start_hook(name, callback)

    def local[T](self, key: str, factory: Callable[[], T]) -> T:
        if key not in self._locals:
            self._locals[key] = factory()
        return cast("T", self._locals[key])

    # --- lifecycle -------------------------------------------------------------------------------------------------
    async def open(self) -> None:
        """Enter the root task group. RuntimeError if already opened (a runtime opens once)."""
        if self._stack is not None or self._closed:
            raise RuntimeError("the process runtime was already opened")
        stack = AsyncExitStack()
        self._tg = await stack.enter_async_context(anyio.create_task_group())
        self._stack = stack

    def begin_drain(self) -> None:
        """Set `draining` and start every registered drain hook (once). Without an open task group only the flag
        is set; the hooks then never run."""
        if self._draining:
            return
        self._draining = True
        if self._tg is not None and not self._closed:
            for name, callback in self._hooks:
                self._start_hook(name, callback)
        if self._pending_hooks == 0:
            self._hooks_done.set()

    async def drain(self, timeout_s: float | None = None) -> bool:
        """`begin_drain()`, then wait for the drain hooks, at most `timeout_s` (default: the grace period) on the
        clock. True when every hook finished in time."""
        self.begin_drain()
        if self._hooks_done.is_set():
            return True
        limit = self._grace_s if timeout_s is None else timeout_s
        async with deadline_scope(self._clock, self._clock.monotonic() + limit) as scope:
            await self._hooks_done.wait()
        return not scope.cancelled_caught

    async def close(self) -> None:
        """Cancel the spawned tasks and leave the task group; idempotent. Must run in the task that opened it."""
        if self._closed:
            return
        self._closed = True
        self.begin_drain()
        if self._tg is not None and self._stack is not None:
            self._tg.cancel_scope.cancel()
            await self._stack.aclose()

    # --- internals -------------------------------------------------------------------------------------------------
    def _start_hook(self, name: str, callback: Callable[[float], Awaitable[None]]) -> None:
        if self._tg is None or self._closed:
            return
        self._pending_hooks += 1
        self._hooks_done = anyio.Event() if self._hooks_done.is_set() else self._hooks_done

        async def run_hook() -> None:
            try:
                await self._guarded(name, lambda: callback(self._grace_s))
            finally:
                self._pending_hooks -= 1
                if self._pending_hooks == 0:
                    self._hooks_done.set()

        self._tg.start_soon(run_hook, name=f"drain:{name}")

    @staticmethod
    async def _guarded(name: str, fn: Callable[[], Awaitable[Any]]) -> None:
        try:
            await fn()
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as exc:  # a detached task never takes the process down
            _log.error("runtime task %s failed: %s", name, type(exc).__name__, exc_info=exc)
