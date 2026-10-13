"""identity's job handlers (spec-11 §4, spec-03 §5): `session.delete`.

`make_handlers(services)` is the `noema.identity.jobs` entry of the jobs process (`--kinds session.delete`). The
handler runs under `tenant_uow(job.tenant_id)` and is idempotent, so a retry after any partial failure finishes the
work: it deletes the session's turns (attempts and turn_files cascade), its decisions (label excerpts are cleared by
the database), purges its files through `FileService.purge` (S3 objects, then rows), then deletes turns finalized
meanwhile and the session row itself. Wave E2: before the turn and decision rows go, the keyed-request records of
those turns and decisions are expired with their inline body cleared, so no `Idempotency-Key` replays the
session's content after the deletion (the retention sweeper deletes the rows and any large body).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from noema.core.protocols.media import FileService
from noema.core.protocols.services import JobHandlers, ServiceSet
from noema.core.protocols.storage import Database, Job, PermanentJobError, UnitOfWork
from noema.identity.sessions import DELETE_JOB_KIND

if TYPE_CHECKING:
    from noema.db.repos.idempotency import PgIdempotencyRepo

log = logging.getLogger("noema.identity.jobs")


@dataclass(frozen=True, slots=True)
class SessionPurge:
    """What one `session.delete` run removed."""

    turns: int
    decisions: int
    files: int


async def delete_session_data(db: Database, files: FileService, tenant_id: str, session_id: str) -> SessionPurge:
    """Delete a session and everything it owns (see the module docstring)."""
    async with db.tenant_uow(tenant_id) as uow:
        await _expire_records(uow, session_id)
        turns = await uow.turns.delete_for_session(session_id)
        decisions = await uow.decisions.delete_for_session(session_id)
        file_ids = [str(row["id"]) for row in await uow.files.list_for_session(session_id)]
    for file_id in file_ids:
        await files.purge(tenant_id, file_id)
    async with db.tenant_uow(tenant_id) as uow:
        await _expire_records(uow, session_id)  # of a keyed turn that finalized meanwhile
        turns += await uow.turns.delete_for_session(session_id)  # a finalize that landed meanwhile
        await uow.sessions.delete(session_id)
    return SessionPurge(turns=turns, decisions=decisions, files=len(file_ids))


async def _expire_records(uow: UnitOfWork, session_id: str) -> None:
    """The keyed-request records of the session's turns and decisions are never replayed again (Wave E2)."""
    await cast("PgIdempotencyRepo", uow.idempotency).expire_for_session(session_id)


def make_handlers(services: ServiceSet) -> JobHandlers:
    """The job kinds identity handles: `session.delete` (payload `{session_id}`)."""

    async def session_delete(job: Job) -> None:
        session_id = job.payload.get("session_id")
        if not isinstance(session_id, str) or not session_id.startswith("ses_"):
            raise PermanentJobError("session.delete needs a session_id")
        purge = await delete_session_data(services.db, services.files, job.tenant_id, session_id)
        log.info(
            "session.delete done job_id=%s turns=%d decisions=%d files=%d",
            job.id,
            purge.turns,
            purge.decisions,
            purge.files,
            extra={"event": "session.delete", "tenant": job.tenant_id},
        )

    return {DELETE_JOB_KIND: session_delete}
