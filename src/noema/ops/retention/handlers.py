"""The `retention.sweep` job handler (spec-11 §4, spec-01 §1 HANDLERS convention): exported for the `jobs`
entrypoint by `make_handlers(services)`."""

from __future__ import annotations

import logging
from typing import Final

from noema.core.protocols.services import JobHandlers, ServiceSet
from noema.core.protocols.storage import Job
from noema.ops.idempotency.bodies import BodyStore
from noema.ops.retention.sweeper import RetentionSweeper

log: Final = logging.getLogger("noema.ops.retention")
KIND: Final = "retention.sweep"


def make_handlers(services: ServiceSet) -> JobHandlers:
    """{"retention.sweep": handler}: one full sweep per job (a `__platform__` job that iterates tenants itself)."""
    bodies = BodyStore(services.s3, bucket=services.settings.s3_bucket, env=services.settings.env)
    sweeper = RetentionSweeper(services.db, services.files, bodies, services.outbox, services.ids, services.clock)

    async def sweep(job: Job) -> None:
        report = await sweeper.sweep()
        log.info("retention.sweep %s: %d deleted, %d failed", job.id, report.total, report.failed.total())

    return {KIND: sweep}
