"""`PgJobQueue`: the `JobQueue` of spec-02 §7 (spec-11 §4) over `jobs.job`.

`enqueue` inserts under `tenant_uow(tenant_id)` through `JobRepo.insert`, which sends `pg_notify('jobs', kind)` at
commit and returns the live duplicate's id for a repeated `dedupe_key` (queued or running, same tenant and kind).
Workers of this process are also woken directly, so a local enqueue never waits for the 5 s poll. `run_after`
None means "now" on the database clock.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from noema.core.clock import Clock
from noema.core.ids import IdGen
from noema.core.protocols.storage import Database
from noema.core.values import JsonValue
from noema.ops.jobs.worker import Handler, PgWorker

if TYPE_CHECKING:
    from noema.ops.notify_bridge.listener import PgListener


class PgJobQueue:
    """Implements `JobQueue` (see the module docstring). `listener` (None: poll only) gives every worker its own
    LISTEN connection; `redact` scrubs stored job errors (`Telemetry.redact`)."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        ids: IdGen,
        *,
        pod_id: str,
        listener: PgListener | None,
        redact: Callable[[str], str],
    ) -> None:
        self._db = db
        self._clock = clock
        self._ids = ids
        self._listener = listener
        self._redact = redact
        self.worker_id = f"{pod_id}:{os.getpid()}"
        self._workers: list[PgWorker] = []

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
        """The new (or live duplicate's) job id. ValueError for `max_attempts` < 1 or an empty kind/tenant."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not kind or not tenant_id:
            raise ValueError("a job needs a kind and a tenant_id")
        row: dict[str, object] = {
            "id": self._ids.new("job"),
            "kind": kind,
            "payload": dict(payload),
            "priority": priority,
            "max_attempts": max_attempts,
        }
        if run_after is not None:
            row["run_after"] = run_after
        if dedupe_key is not None:
            row["dedupe_key"] = dedupe_key
        async with self._db.tenant_uow(tenant_id) as uow:
            job_id = await uow.jobs.insert(row)
        for worker in self._workers:
            worker.notify(kind)
        return job_id

    def worker(self, kinds: Sequence[str], handler: Handler, *, concurrency: int) -> PgWorker:
        """A worker for exactly `kinds` (expand globs first: `core.protocols.services.expand_kinds`)."""
        worker = PgWorker(
            self._db,
            self._clock,
            kinds=kinds,
            handler=handler,
            concurrency=concurrency,
            worker_id=self.worker_id,
            listener=self._listener,
            redact=self._redact,
        )
        self._workers.append(worker)
        return worker
