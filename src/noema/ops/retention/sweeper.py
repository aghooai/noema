"""`RetentionSweeper`: the hourly `retention.sweep` job (spec-11 §6, spec-03 §5, IMPL-085).

Pages `SystemCalls.retention_page(1000, after=<the previous page's last candidate>)` (rows with `expires_at < now()`
in (expires_at, kind, id, tenant_id) order, a keyset cursor) until a page comes back empty, and deletes each row
under `tenant_uow(tenant)` (RLS: a delete in the wrong scope touches nothing). A row that fails stays behind the
cursor, so failing rows never starve newer ones and each row is tried at most once per run. The first 10,000
failed keys of a run are remembered; rows that fail in two consecutive runs log an ERROR (the alert: expired data
outlives its `expires_at`; the order is stable, so a persistent cause fails the same oldest rows every run).
- `session`: its turns (attempts and turn files cascade), its decisions, its files (`FileService.purge`: S3 objects
  and rows), then the session row (the session.delete order of spec-03 §5);
- `turn`, `decision` (the database nulls the `content_excerpt` of label items with that decision), `file`
  (`FileService.purge`);
- `idempotency_record`: in one unit of work, the row iff it is still expired (`delete_expired`), then the
  record's S3 object (`<tenant>/idempotency/<id>`, a no-op when absent) before the commit: an S3 failure rolls the
  row back for the next sweep (a large body never outlives its record), and a record a new request took over
  since the page was read (same id and object key, IMPL-077) keeps its row and its new owner's body (Wave E).
Each tenant gets one `audit_event` per run through the outbox (`action = retention.sweep`, counts only). A failing
row is logged, counted and skipped (retried by the next sweep). L2 entries expire on their own; `usage_line` and
`audit_event` rows are kept (billing and audit records); legal hold is Phase 2.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

from noema.core.clock import Clock
from noema.core.ids import IdGen
from noema.core.protocols.media import FileService
from noema.core.protocols.storage import Database, Outbox, OutboxRecord, RetentionCandidate, UnitOfWork
from noema.ops.cost.lines import rfc3339
from noema.ops.idempotency.bodies import BodyStore

if TYPE_CHECKING:
    from noema.db.repos.idempotency import PgIdempotencyRepo

log: Final = logging.getLogger("noema.ops.retention")
PAGE: Final = 1000
MAX_REMEMBERED: Final = 10 * PAGE  # failed keys remembered per run, for the two-consecutive-runs ERROR
ACTOR: Final = "system:retention"

type Key = tuple[str, str, str]  # (tenant_id, kind, id) of a `RetentionCandidate`


@dataclass
class SweepReport:
    """Per tenant: deleted rows by kind; `failed` counts rows left for the next sweep."""

    deleted: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    failed: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(sum(c.values()) for c in self.deleted.values())


class RetentionSweeper:
    """See the module docstring."""

    def __init__(
        self,
        db: Database,
        files: FileService,
        bodies: BodyStore,
        outbox: Outbox,
        ids: IdGen,
        clock: Clock,
        *,
        page: int = PAGE,
        max_remembered: int = MAX_REMEMBERED,
    ) -> None:
        self._db = db
        self._files = files
        self._bodies = bodies
        self._outbox = outbox
        self._ids = ids
        self._clock = clock
        self._page = page
        self._max_remembered = max_remembered
        self._last_failed: frozenset[Key] = frozenset()  # the previous run's failures (at most max_remembered)

    async def sweep(self) -> SweepReport:
        """One full run. Raises `NoemaError(internal_error)` if Postgres is down (the job retries)."""
        report = SweepReport()
        failed: set[Key] = set()  # the first max_remembered failures of this run
        after: RetentionCandidate | None = None
        while True:
            async with self._db.system_uow() as calls:  # strictly after the cursor: failed rows stay behind it
                page = await calls.retention_page(self._page, after=after)
            if not page:
                break
            after = page[-1]
            by_tenant: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for tenant_id, kind, row_id, _ in page:
                by_tenant[tenant_id].append((kind, row_id))
            for tenant_id, items in by_tenant.items():
                for kind, row_id in await self._sweep_tenant(tenant_id, items, report):
                    if len(failed) < self._max_remembered:
                        failed.add((tenant_id, kind, row_id))
        self._report_repeated(failed)
        for tenant_id, counts in report.deleted.items():
            self._audit(tenant_id, counts, report.failed[tenant_id])
        if report.total or report.failed:
            log.info("retention sweep: %d rows deleted, %d left for the next run", report.total, report.failed.total())
        return report

    def _report_repeated(self, failed: set[Key]) -> None:
        """ERROR (alerting) when rows that failed in the previous run failed again: a persistent cause (e.g. a
        missing s3:DeleteObject permission) keeps expired data past its `expires_at`."""
        repeated = failed & self._last_failed
        self._last_failed = frozenset(failed)
        if repeated:
            tenant_id, kind, row_id = min(repeated)
            log.error(
                "retention sweep: %d expired rows failed in two consecutive runs (e.g. %s %s of tenant %s)",
                len(repeated),
                kind,
                row_id,
                tenant_id,
            )

    async def _sweep_tenant(
        self, tenant_id: str, items: Sequence[tuple[str, str]], report: SweepReport
    ) -> list[tuple[str, str]]:
        """Delete `items` of one tenant; returns the (kind, id) that failed (counted in `report.failed`)."""
        deleted, failed = report.deleted[tenant_id], list[tuple[str, str]]()
        rows: list[tuple[str, str]] = []  # deleted together in one unit of work
        for kind, row_id in items:
            if kind == "session":
                ok = await self._try(tenant_id, kind, report, self._session(tenant_id, row_id))
            elif kind == "file":
                ok = await self._try(tenant_id, kind, report, self._files.purge(tenant_id, row_id))
            elif kind == "idempotency_record":  # one row per unit of work: its object is deleted inside it
                gone: list[bool] = []
                if not await self._try(tenant_id, kind, report, self._idempotency(tenant_id, row_id, gone)):
                    failed.append((kind, row_id))
                elif gone:
                    deleted[kind] += 1
                continue
            elif kind in _ROW_DELETES:
                rows.append((kind, row_id))
                continue
            else:
                log.warning("retention sweep: unknown candidate kind %r skipped", kind)
                report.failed[tenant_id] += 1
                ok = False
            if ok:
                deleted[kind] += 1
            else:
                failed.append((kind, row_id))
        if not rows:
            return failed
        if await self._try(tenant_id, "batch", report, self._delete_rows(tenant_id, rows), count_failure=False):
            deleted.update(kind for kind, _ in rows)
            return failed
        for kind, row_id in rows:  # isolate the failing row
            if await self._try(tenant_id, kind, report, self._delete_rows(tenant_id, [(kind, row_id)])):
                deleted[kind] += 1
            else:
                failed.append((kind, row_id))
        return failed

    async def _try(
        self, tenant_id: str, what: str, report: SweepReport, work: Awaitable[object], *, count_failure: bool = True
    ) -> bool:
        try:
            await work
        except Exception as exc:
            log.warning("retention sweep: %s of tenant %s failed (%s)", what, tenant_id, type(exc).__name__)
            if count_failure:
                report.failed[tenant_id] += 1
            return False
        return True

    async def _delete_rows(self, tenant_id: str, rows: Sequence[tuple[str, str]]) -> None:
        async with self._db.tenant_uow(tenant_id) as uow:
            for kind, row_id in rows:
                await _ROW_DELETES[kind](uow, row_id)

    async def _idempotency(self, tenant_id: str, record_id: str, gone: list[bool]) -> None:
        """Delete a still-expired record and then its object, in one transaction (see the module docstring);
        appends True to `gone` when the record was deleted."""
        async with self._db.tenant_uow(tenant_id) as uow:
            if await cast("PgIdempotencyRepo", uow.idempotency).delete_expired(record_id):
                await self._bodies.delete(tenant_id, record_id)
                gone.append(True)

    async def _session(self, tenant_id: str, session_id: str) -> None:
        async with self._db.tenant_uow(tenant_id) as uow:
            await uow.turns.delete_for_session(session_id)
            await uow.decisions.delete_for_session(session_id)
            file_ids = [str(f["id"]) for f in await uow.files.list_for_session(session_id)]
        for file_id in file_ids:
            await self._files.purge(tenant_id, file_id)
        async with self._db.tenant_uow(tenant_id) as uow:
            await uow.sessions.delete(session_id)

    def _audit(self, tenant_id: str, counts: Counter[str], failed: int) -> None:
        details: dict[str, int] = {**counts, "failed": failed}
        self._outbox.put(
            OutboxRecord(
                kind="audit_event",
                id=self._ids.new("aud"),
                tenant_id=tenant_id,
                payload={
                    "actor_type": "system",
                    "actor_id": ACTOR,
                    "action": "retention.sweep",
                    "resource_kind": "tenant",
                    "resource_id": tenant_id,
                    "details": dict(details),
                    "at": rfc3339(self._clock.now()),
                },
            )
        )


_ROW_DELETES: Final[dict[str, Callable[[UnitOfWork, str], Awaitable[None]]]] = {
    "turn": lambda uow, row_id: uow.turns.delete(row_id),
    "decision": lambda uow, row_id: uow.decisions.delete(row_id),
}
