"""`LabelRepo` (spec-02 §7.1, spec-03 §2; ADR-003, IMPL-072): labeling runs and their items."""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Page, Row
from noema.db.repos import _tables as t
from noema.db.repos._base import (
    Query,
    eq_filters,
    insert_sql,
    page_by_id_desc,
    select_sql,
    set_clause,
    to_row,
    to_rows,
    update_sql,
)
from noema.db.uow import Tx

RUN_UPDATABLE: Final = t.LABELING_RUN.columns - {"tenant_id", "id"}

_OPEN_RUN: Final = (
    "SELECT * FROM noema.labeling_run WHERE tenant_id = $1 AND profile_id = $2 AND profile_version = $3"
    " AND question_name = $4 AND ($5::text IS NULL OR question_signature = $5) AND status = 'running'"
    " ORDER BY id DESC LIMIT 1"
)
_FOR_DECISION: Final = "SELECT * FROM noema.label_item WHERE tenant_id = $1 AND decision_id = $2 ORDER BY id"
_KEPT: Final = (  # labeling_run_by_definition, then label_item_kept per run
    "SELECT i.* FROM noema.label_item i JOIN noema.labeling_run r ON r.tenant_id = i.tenant_id AND r.id = i.run_id"
    " WHERE r.tenant_id = $1 AND r.profile_id = $2 AND r.profile_version = $3 AND r.question_signature = $4"
    " AND i.tenant_id = $1 AND i.kept AND i.created_at >= $5 AND ($6::text IS NULL OR i.sample_reason = $6)"
    " ORDER BY i.created_at, i.id"
)


class PgLabelRepo:
    """`LabelRepo` over one unit of work (tenant-wide tables: labeling_run, label_item)."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def create_run(self, row: Row) -> Row:
        """INSERT the run; the stored row. A second running run of the same (profile, version, question name and
        signature) raises asyncpg.UniqueViolationError (the labeling_run_open index)."""
        sql, args = insert_sql(t.LABELING_RUN, self._tx, row)
        record = await self._tx.conn.fetchrow(sql, *args)
        assert record is not None
        return to_row(record)

    async def update_run(self, run_id: str, **changes: object) -> Row:
        """Sets any non-key columns (status, counts, consensus rates, usage, completed_at, ...); the updated row.
        Raises `NoemaError(not_found)` for a missing run."""
        q = Query(self._tx.tenant_id, run_id)
        if changes:
            assignments = set_clause(t.LABELING_RUN, q, changes, RUN_UPDATABLE)
            sql = update_sql(t.LABELING_RUN, assignments, "tenant_id = $1 AND id = $2")
        else:
            sql = "SELECT * FROM noema.labeling_run WHERE tenant_id = $1 AND id = $2"
        record = await self._tx.conn.fetchrow(sql, *q.args)
        if record is None:
            raise NoemaError(ErrorCode.NOT_FOUND, "The labeling run does not exist.")
        return to_row(record)

    async def open_run(
        self, profile_id: str, profile_version: str, question_name: str, question_signature: str | None = None
    ) -> Row | None:
        """The running run of that question definition, if any (at most one per name and signature, by the
        labeling_run_open index); `question_signature` None: the newest running run of the name."""
        tenant = self._tx.tenant_id
        args = (tenant, profile_id, profile_version, question_name, question_signature)
        record = await self._tx.conn.fetchrow(_OPEN_RUN, *args)
        return None if record is None else to_row(record)

    async def insert_item(self, row: Row) -> None:
        """INSERT the item; a retried job's duplicate (same run, decision and question) is a no-op."""
        sql, args = insert_sql(
            t.LABEL_ITEM, self._tx, row, suffix="ON CONFLICT (tenant_id, run_id, decision_id, question_name) DO NOTHING"
        )
        await self._tx.conn.execute(sql, *args)

    async def items(self, run_id: str, *, kept: bool | None = None) -> tuple[Row, ...]:
        """The run's items, id ASC; `kept` filters kept (True) or dropped (False) items."""
        q = Query(self._tx.tenant_id, run_id)
        where = ["tenant_id = $1", "run_id = $2", *eq_filters(q, kept=kept)]
        sql = select_sql(t.LABEL_ITEM.name, where, order="id")
        return to_rows(await self._tx.conn.fetch(sql, *q.args))

    async def items_for_decision(self, decision_id: str) -> tuple[Row, ...]:
        """The decision's items in every run, id ASC (the label_item_by_decision index)."""
        return to_rows(await self._tx.conn.fetch(_FOR_DECISION, self._tx.tenant_id, decision_id))

    async def kept_items(
        self,
        profile_id: str,
        profile_version: str,
        question_signature: str,
        since: datetime,
        sample_reason: Literal["uniform", "uncertainty"] | None = "uniform",
    ) -> tuple[Row, ...]:
        """Kept items with created_at >= since of every run of that definition, created_at then id ASC;
        `sample_reason` None = both."""
        args = (self._tx.tenant_id, profile_id, profile_version, question_signature, since, sample_reason)
        return to_rows(await self._tx.conn.fetch(_KEPT, *args))

    async def list_runs(
        self,
        *,
        profile_id: str | None,
        question_name: str | None,
        status: str | None,
        after: str | None,
        limit: int,
    ) -> Page[Row]:
        """Runs, id DESC, filtered by the given (non-None) fields."""
        q = Query(self._tx.tenant_id)
        filters = eq_filters(q, profile_id=profile_id, question_name=question_name, status=status)
        return await page_by_id_desc(self._tx, t.LABELING_RUN.name, q, ["tenant_id = $1", *filters], after, limit)

    async def list_dropped(
        self, *, run_id: str | None, question_name: str | None, reason: str | None, after: str | None, limit: int
    ) -> Page[Row]:
        """Dropped items (kept = false), id DESC (the label_item_dropped index), filtered by the given fields;
        `reason` matches drop_reason."""
        q = Query(self._tx.tenant_id)
        filters = eq_filters(q, run_id=run_id, question_name=question_name, drop_reason=reason)
        where = ["tenant_id = $1", "NOT kept", *filters]
        return await page_by_id_desc(self._tx, t.LABEL_ITEM.name, q, where, after, limit)
