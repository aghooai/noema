"""`PeriodicScheduler` (spec-11 §4): every `jobs` process, every 60 s, calls `jobs.ensure_periodic` for the Phase 1
periodic jobs, `retention.sweep` hourly and `labeling.drift` weekly. The `job_dedupe` index keeps at most one
queued or running instance (dedupe key = the kind, tenant `__platform__`, due at the next period boundary), so
several processes running the scheduler are safe."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

from noema.core.clock import Clock
from noema.core.protocols.storage import Database

log: Final = logging.getLogger("noema.ops.jobs")
EVERY_S: Final = 60.0
PERIODIC: Final[tuple[tuple[str, int], ...]] = (
    ("retention.sweep", 3600),
    ("labeling.drift", 7 * 86_400),
)


class PeriodicScheduler:
    """See the module docstring."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        *,
        periodic: Sequence[tuple[str, int]] = PERIODIC,
        every_s: float = EVERY_S,
    ) -> None:
        self._db = db
        self._clock = clock
        self._periodic = tuple(periodic)
        self._every = every_s

    async def run(self) -> None:
        """`ensure_once()` every 60 s until cancelled (failures are logged)."""
        while True:
            await self.ensure_once()
            await self._clock.sleep(self._every)

    async def ensure_once(self) -> int:
        """One `ensure_periodic` per periodic kind; returns how many calls succeeded."""
        done = 0
        for kind, every_s in self._periodic:
            try:
                async with self._db.system_uow() as calls:
                    await calls.ensure_periodic(kind, every_s)
                done += 1
            except Exception as exc:
                log.warning("periodic job %s: ensure failed (%s)", kind, type(exc).__name__)
        return done
