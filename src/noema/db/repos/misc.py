"""`AuditRepo`, `ProjectRepo` and `JobRepo` (spec-02 §7.1, spec-03 §2, spec-11 §4)."""

from __future__ import annotations

from typing import Final

from noema.core.protocols.storage import Row
from noema.db.repos import _tables as t
from noema.db.repos._base import insert_sql, to_row
from noema.db.uow import Tx

JOBS_CHANNEL: Final = "jobs"  # pg_notify channel the job workers LISTEN on; the payload is the job kind
_ENQUEUE_SUFFIX: Final = (
    "ON CONFLICT (tenant_id, kind, dedupe_key) WHERE dedupe_key IS NOT NULL AND state IN ('queued','running')"
    " DO NOTHING RETURNING id"
)
_LIVE_DUPLICATE: Final = (
    "SELECT id FROM jobs.job WHERE tenant_id = $1 AND kind = $2 AND dedupe_key = $3 AND state IN ('queued','running')"
)
_NOTIFY: Final = "SELECT pg_catalog.pg_notify($1, $2)"
_PROJECT: Final = "SELECT * FROM noema.project WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL"
_ENQUEUE_TRIES: Final = 3


class PgAuditRepo:
    """`AuditRepo` over one unit of work (append-only: noema_app may not UPDATE or DELETE audit events)."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def insert(self, row: Row) -> None:
        """INSERT the audit event (details defaults to {}, at to now()). Never pass content in details."""
        sql, args = insert_sql(t.AUDIT_EVENT, self._tx, row, suffix="")
        await self._tx.conn.execute(sql, *args)


class PgProjectRepo:
    """`ProjectRepo` over one unit of work (read-only; projects come from admin.create_project)."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def get(self, project_id: str) -> Row | None:
        """The live project of the uow's tenant, or None (missing or deleted)."""
        record = await self._tx.conn.fetchrow(_PROJECT, self._tx.tenant_id, project_id)
        return None if record is None else to_row(record)


class PgJobRepo:
    """`JobRepo` over one unit of work (used only by the JobQueue implementation)."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def insert(self, row: Row) -> str:
        """INSERT a queued job (row: id, kind, payload, optionally priority, run_after, dedupe_key, max_attempts;
        tenant_id from the uow) and `pg_notify('jobs', kind)`, delivered when the uow commits. A live duplicate
        (same tenant, kind and dedupe_key, queued or running; the job_dedupe index) inserts nothing, sends no
        notification and returns the duplicate's id."""
        values = {"state": "queued", **row}
        sql, args = insert_sql(t.JOB, self._tx, values, suffix=_ENQUEUE_SUFFIX)
        for _ in range(_ENQUEUE_TRIES):  # the duplicate may finish between the conflict and the SELECT
            job_id = await self._tx.conn.fetchval(sql, *args)
            if job_id is not None:
                await self._tx.conn.execute(_NOTIFY, JOBS_CHANNEL, str(values["kind"]))
                return str(job_id)
            duplicate = await self._tx.conn.fetchval(
                _LIVE_DUPLICATE, self._tx.tenant_id, values["kind"], values.get("dedupe_key")
            )
            if duplicate is not None:
                return str(duplicate)
        raise RuntimeError("jobs.job: no insert and no live duplicate after retries")
