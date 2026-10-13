"""`JobReaper` (spec-11 §6): every `jobs` process, every 30 s, `ops.reap_stuck('2 minutes')` requeues jobs whose
lock lapsed more than 2 minutes ago (their worker died or lost its lease; `dead` at `max_attempts`) and deletes
finished jobs (done > 7 days, dead > 30 days). Concurrent runs are harmless.

What needs no reaper (spec-11 §6, spec-04 §2): session leases (an expired lease is free at the next `acquire`; a
pending outbox finalize keeps the session busy through `session_pending`), idempotency leases (a lapsed in-flight
record is taken over at the next `begin`), stuck attempts (generation's in-process watchdog marks them
`cancelled(deadline)` when a turn task exits, `aghoo_stuck_attempts_total`), and abandoned turns (the orchestrator's
abandon watchdog). Turns and attempts are persisted only at finalize, so no running row is left in Postgres.
"""

from __future__ import annotations

import logging
from typing import Final

from noema.core.clock import Clock
from noema.core.protocols.storage import Database

log: Final = logging.getLogger("noema.ops.reapers")
EVERY_S: Final = 30.0
OLDER_THAN_S: Final = 120


class JobReaper:
    """See the module docstring."""

    def __init__(
        self, db: Database, clock: Clock, *, every_s: float = EVERY_S, older_than_s: int = OLDER_THAN_S
    ) -> None:
        self._db = db
        self._clock = clock
        self._every = every_s
        self._older_than = older_than_s

    async def run(self) -> None:
        """`reap_once()` every 30 s until cancelled."""
        while True:
            await self.reap_once()
            await self._clock.sleep(self._every)

    async def reap_once(self) -> int:
        """One `ops.reap_stuck`; the number of requeued jobs (0 on failure, which is logged)."""
        try:
            async with self._db.system_uow() as calls:
                requeued = await calls.reap_stuck(self._older_than)
        except Exception as exc:
            log.warning("job reaper failed (%s)", type(exc).__name__)
            return 0
        if requeued:
            log.info("job reaper: %d stuck jobs requeued", requeued)
        return requeued
