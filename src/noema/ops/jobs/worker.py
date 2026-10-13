"""`PgWorker`: the `Worker` of spec-02 §7 (spec-11 §4) over `jobs.claim` / `jobs.extend` / `jobs.finish`.

`run()` claims up to the free slots (`concurrency` minus the running handlers) through `SystemCalls.claim_jobs`
(`FOR UPDATE SKIP LOCKED`, priority order, lock 60 s, worker id `<pod>:<pid>`) on every wake-up: a `jobs`
notification for one of its kinds (LISTEN, when a listener is given), an enqueue in this process, a finished
handler, and every 5 s regardless. Each handler runs in its own task with its lease extended every 20 s; a refused
extension (the job was reaped and maybe claimed elsewhere) cancels the handler, and its result is never written.

Handler return -> `done`; `PermanentJobError` -> `dead` at once; any other exception -> retried with backoff
(30 s x 2^(attempt-1)), `dead` at `max_attempts` (the database applies the rule). The stored error is the
exception type and message, redacted, at most 200 characters. A dead job logs an ERROR (the alert rule counts it):
`finish_job` returns the state the database chose, so the per-job `max_attempts` (media.process: 2) is honored. A
finish the database ignored (None: the lease was lost, e.g. reaped and claimed elsewhere) logs a WARNING only.

Shutdown: `drain(grace_s)` (a `ProcessRuntime.on_drain` hook) stops claiming and waits for the running handlers;
cancelling `run()` gives them 10 s, then cancels them and leaves their jobs to the reaper.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Final

import anyio
import anyio.abc

from noema.core.clock import Clock
from noema.core.protocols.storage import Database, Job, PermanentJobError
from noema.db.repos import JOBS_CHANNEL
from noema.ops.jobs.timing import Wakeup

if TYPE_CHECKING:
    from noema.ops.notify_bridge.listener import PgListener

log: Final = logging.getLogger("noema.ops.jobs")
LOCK_S: Final = 60
EXTEND_EVERY_S: Final = 20.0
POLL_S: Final = 5.0
SHUTDOWN_GRACE_S: Final = 10.0
ERROR_MAX_CHARS: Final = 200

type Handler = Callable[[Job], Awaitable[None]]


class PgWorker:
    """Implements `Worker` (see the module docstring)."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        *,
        kinds: Sequence[str],
        handler: Handler,
        concurrency: int,
        worker_id: str,
        listener: PgListener | None,
        redact: Callable[[str], str],
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if not kinds:
            raise ValueError("a worker needs at least one job kind")
        self._db = db
        self._clock = clock
        self._kinds = tuple(kinds)
        self._handler = handler
        self._concurrency = concurrency
        self.worker_id = worker_id
        self._listener = listener
        self._redact = redact
        self._active: dict[str, anyio.CancelScope] = {}
        self._wake = Wakeup()
        self._freed = Wakeup()
        self._stopping = False

    @property
    def kinds(self) -> tuple[str, ...]:
        return self._kinds

    @property
    def running(self) -> int:
        """Handlers running now."""
        return len(self._active)

    def notify(self, kind: str) -> None:
        """Wake the claim loop if `kind` is one of this worker's kinds (sync)."""
        if kind in self._kinds:
            self._wake.set()

    async def run(self) -> None:
        """Claim and run jobs until cancelled (see the module docstring)."""
        async with anyio.create_task_group() as tg:
            if self._listener is not None:
                tg.start_soon(self._listener.listen, JOBS_CHANNEL, self.notify)
            try:
                while True:
                    if not self._stopping:
                        await self._claim(tg)
                    await self._wake.wait(self._clock, POLL_S)
            except anyio.get_cancelled_exc_class():
                with anyio.CancelScope(shield=True):
                    await self._wait_idle(SHUTDOWN_GRACE_S)
                    for scope in self._active.values():
                        scope.cancel()  # left running for the reaper
                raise
            finally:
                for scope in self._active.values():
                    scope.cancel()

    async def drain(self, grace_s: float) -> None:
        """Stop claiming and wait (at most `grace_s`) for the running handlers."""
        self._stopping = True
        await self._wait_idle(grace_s)

    async def _wait_idle(self, grace_s: float) -> None:
        give_up = self._clock.monotonic() + grace_s
        while self._active and (left := give_up - self._clock.monotonic()) > 0:
            await self._freed.wait(self._clock, left)

    async def _claim(self, tg: anyio.abc.TaskGroup) -> None:
        free = self._concurrency - len(self._active)
        if free <= 0:
            return
        try:
            async with self._db.system_uow() as calls:
                jobs = await calls.claim_jobs(self._kinds, free, self.worker_id, LOCK_S)
        except Exception as exc:
            log.warning("job claim failed (%s); retrying", type(exc).__name__)
            return
        for job in jobs:
            scope = anyio.CancelScope()
            self._active[job.id] = scope
            tg.start_soon(self._run_one, job, scope)

    async def _run_one(self, job: Job, scope: anyio.CancelScope) -> None:
        result: tuple[bool, str | None, bool] | None = None  # (ok, error, permanent); None = lease lost/cancelled
        try:
            with anyio.CancelScope(shield=True):
                with scope:
                    async with anyio.create_task_group() as inner:
                        inner.start_soon(self._extend, job, scope)
                        try:
                            await self._handler(job)
                            result = (True, None, False)
                        except PermanentJobError as exc:
                            result = (False, self._error(exc), True)
                        except Exception as exc:
                            result = (False, self._error(exc), False)
                        inner.cancel_scope.cancel()
                if result is not None:
                    await self._finish(job, *result)
        finally:
            self._active.pop(job.id, None)
            self._freed.set()
            self._wake.set()  # a slot is free: claim again now

    async def _extend(self, job: Job, scope: anyio.CancelScope) -> None:
        while True:
            await self._clock.sleep(EXTEND_EVERY_S)
            try:
                async with self._db.system_uow() as calls:
                    kept = await calls.extend_job(job.id, self.worker_id, LOCK_S)
            except Exception as exc:
                log.warning("job %s: lease extension failed (%s)", job.id, type(exc).__name__)
                continue
            if not kept:
                log.warning("job %s (%s): lease lost; the handler is cancelled", job.id, job.kind)
                scope.cancel()
                return

    def _error(self, exc: BaseException) -> str:
        text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return self._redact(text)[:ERROR_MAX_CHARS]

    async def _finish(self, job: Job, ok: bool, error: str | None, permanent: bool) -> None:
        try:
            async with self._db.system_uow() as calls:
                state = await calls.finish_job(job.id, self.worker_id, ok=ok, error=error, permanent=permanent)
        except Exception as exc:
            log.warning("job %s: finish failed (%s); the reaper requeues it", job.id, type(exc).__name__)
            return
        if state is None:
            log.warning("job %s (%s): finish ignored, the lease was lost", job.id, job.kind)
        elif state == "dead":
            log.error(
                "job dead: id=%s kind=%s tenant=%s attempt=%d error=%s",
                job.id,
                job.kind,
                job.tenant_id,
                job.attempt,
                error,
            )
        elif state == "queued":
            log.warning(
                "job failed (retried with backoff): id=%s kind=%s attempt=%d error=%s",
                job.id,
                job.kind,
                job.attempt,
                error,
            )
