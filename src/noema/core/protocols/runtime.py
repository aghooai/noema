"""The process runtime shared by the composition root and the domains (spec-01 §3, spec-11 §7).

`composition.start()` opens it (the process's root task group) and `composition.stop()` drains and closes it. The
domains reach it as `services.runtime`: long-lived process state (the running-turn registry), fire-and-forget work
after a response (key touch/rehash, spec-09 §2), the draining flag behind `/readyz`, and shutdown hooks (running
turns are cancelled after the grace period, spec-11 §7 step 3).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol


class ProcessRuntime(Protocol):
    """One per process (see the module docstring). All methods are sync and must be called on the event loop."""

    @property
    def draining(self) -> bool:
        """True from the first shutdown signal (SIGTERM in the api process, or `composition.stop`) on; `/readyz`
        answers 503 while it is set (spec-11 §7 step 1). Never reset."""
        ...

    def spawn(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        """Run `fn()` in the root task group, detached from the caller (it outlives the request). An exception is
        logged (`noema.runtime`, ERROR, with `name` and the exception type) and never propagates; the task is
        cancelled when the runtime closes. Raises RuntimeError when the runtime is not open (before
        `composition.start` or after `composition.stop`)."""
        ...

    def on_drain(self, name: str, callback: Callable[[float], Awaitable[None]]) -> None:
        """Register a shutdown hook: when draining begins, `callback(grace_s)` runs in the root task group (all
        hooks concurrently; `grace_s` is the time running work may still take, 30 s in the api process).
        `composition.stop` waits for the hooks (at most the grace period) before the outbox flush. Registered while
        already draining: runs at once."""
        ...

    def local[T](self, key: str, factory: Callable[[], T]) -> T:
        """Process-local state owned by one domain, created by `factory()` on first use and the same object for the
        runtime's lifetime (e.g. `runtime.local("turns.registry", TurnRegistry)`). Keys start with the owning
        package name. Avoids module-level singletons, which tests running several apps in one process would share."""
        ...
