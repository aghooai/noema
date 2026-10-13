"""`FakeJobQueue` and `FakeWorker` (spec-02 §7, spec-11 §4) over `FakeDatabase`, driven by a `Clock`.

`enqueue` inserts under `tenant_uow(tenant)` (live duplicates by `dedupe_key` return the existing id) and wakes
workers (the LISTEN/NOTIFY stand-in). A worker claims through `system_uow().claim_jobs` on every wake-up and every
5 s, extends each lease every 20 s (a refused extension cancels the handler: lease lost), finishes jobs per
spec-11 §4 (return -> done; `PermanentJobError` -> dead; other errors -> backoff, dead at max_attempts). When
`run()` is cancelled, running handlers get 10 s (clock), then they are cancelled and their jobs are left running
for the reaper.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Final

import anyio
import anyio.abc

from noema.core.clock import Clock
from noema.core.ids import IdGen
from noema.core.protocols.storage import Job, PermanentJobError
from noema.core.values import JsonValue

from .db import FakeDatabase

LOCK_S: Final = 60
EXTEND_EVERY_S: Final = 20.0
POLL_S: Final = 5.0
SHUTDOWN_GRACE_S: Final = 10.0
_log: Final = logging.getLogger("tests.fakes.jobs")


class FakeJobQueue:
    """Implements `JobQueue`."""

    def __init__(self, clock: Clock, db: FakeDatabase, ids: IdGen) -> None:
        self._clock = clock
        self._db = db
        self._ids = ids
        self._wake = anyio.Event()
        self.enqueued: list[str] = []

    async def enqueue(
        self,
        kind: str,
        tenant_id: str,
        payload: Mapping[str, JsonValue],
        *,
        priority: int = 100,
        run_after: datetime | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 5,
    ) -> str:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        row = {
            "id": self._ids.new("job"),
            "kind": kind,
            "payload": dict(payload),
            "priority": priority,
            "dedupe_key": dedupe_key,
            "run_after": run_after or self._clock.now(),
            "max_attempts": max_attempts,
        }
        async with self._db.tenant_uow(tenant_id) as uow:
            job_id = await uow.jobs.insert(row)
        self.enqueued.append(job_id)
        self.notify()
        return job_id

    def notify(self) -> None:
        """Wake every waiting worker (pg_notify('jobs', kind))."""
        self._wake.set()
        self._wake = anyio.Event()

    async def wait(self, timeout_s: float) -> None:
        """Until the next notify or `timeout_s` on the clock."""
        wake = self._wake
        async with anyio.create_task_group() as tg:

            async def timer() -> None:
                await self._clock.sleep(timeout_s)
                tg.cancel_scope.cancel()

            tg.start_soon(timer)
            await wake.wait()
            tg.cancel_scope.cancel()

    def worker(
        self, kinds: Sequence[str], handler: Callable[[Job], Awaitable[None]], *, concurrency: int
    ) -> FakeWorker:
        return FakeWorker(self, kinds, handler, concurrency=concurrency, name=f"fake-worker-{id(handler):x}")


class FakeWorker:
    """Implements `Worker` (see the module docstring); `finished` logs (job id, outcome)."""

    def __init__(
        self,
        queue: FakeJobQueue,
        kinds: Sequence[str],
        handler: Callable[[Job], Awaitable[None]],
        *,
        concurrency: int,
        name: str,
    ) -> None:
        self._queue = queue
        self._kinds = tuple(kinds)
        self._handler = handler
        self._concurrency = concurrency
        self.name = name
        self._active: dict[str, anyio.CancelScope] = {}
        self.finished: list[tuple[str, str]] = []

    async def run(self) -> None:
        async with anyio.create_task_group() as tg:
            try:
                while True:
                    free = self._concurrency - len(self._active)
                    if free > 0:
                        async with self._queue._db.system_uow() as sys:
                            jobs = await sys.claim_jobs(self._kinds, free, self.name, LOCK_S)
                        for job in jobs:
                            scope = anyio.CancelScope()
                            self._active[job.id] = scope
                            tg.start_soon(self._run_one, job, scope)
                    await self._queue.wait(POLL_S)
            except anyio.get_cancelled_exc_class():
                with anyio.CancelScope(shield=True):
                    give_up = self._queue._clock.monotonic() + SHUTDOWN_GRACE_S
                    while self._active and self._queue._clock.monotonic() < give_up:
                        await self._queue._clock.sleep(min(0.1, give_up - self._queue._clock.monotonic()))
                    for scope in self._active.values():
                        scope.cancel()  # left running for the reaper
                raise

    async def _run_one(self, job: Job, scope: anyio.CancelScope) -> None:
        outcome = "left"
        try:
            with anyio.CancelScope(shield=True), scope:
                async with anyio.create_task_group() as inner:
                    inner.start_soon(self._extend, job, scope)
                    try:
                        await self._handler(job)
                        outcome = "done"
                    except PermanentJobError as exc:
                        outcome = "dead"
                        await self._finish(job, ok=False, error=f"permanent: {type(exc).__name__}", permanent=True)
                    except Exception as exc:
                        outcome = "failed"
                        await self._finish(job, ok=False, error=type(exc).__name__)
                    if outcome == "done":
                        await self._finish(job, ok=True, error=None)
                    inner.cancel_scope.cancel()
        finally:
            self._active.pop(job.id, None)
            self.finished.append((job.id, outcome))
            self._queue.notify()  # a slot is free: claim again now

    async def _extend(self, job: Job, scope: anyio.CancelScope) -> None:
        while True:
            await self._queue._clock.sleep(EXTEND_EVERY_S)
            async with self._queue._db.system_uow() as sys:
                if not await sys.extend_job(job.id, self.name, LOCK_S):
                    _log.warning("job lease lost: %s", job.id)
                    scope.cancel()
                    return

    async def _finish(self, job: Job, *, ok: bool, error: str | None, permanent: bool = False) -> None:
        async with self._queue._db.system_uow() as sys:
            await sys.finish_job(job.id, self.name, ok=ok, error=error, permanent=permanent)
