"""`DecisionRepo` (spec-02 §7.1, spec-03 §2; IMPL-071)."""

from __future__ import annotations

from typing import Final

from noema.core.protocols.storage import Row
from noema.db.repos import _tables as t
from noema.db.repos._base import affected, insert_sql, to_row, unstorable
from noema.db.uow import Tx

_GET: Final = "SELECT * FROM noema.decision WHERE tenant_id = $1 AND id = $2"
_DELETE: Final = "DELETE FROM noema.decision WHERE tenant_id = $1 AND id = $2"
_DELETE_FOR_SESSION: Final = "DELETE FROM noema.decision WHERE tenant_id = $1 AND session_id = $2"


class PgDecisionRepo:
    """`DecisionRepo` over one unit of work. Deleting decisions nulls the content_excerpt of their label items
    (the `decision_clear_label_excerpts` trigger, spec-03 §5); the labels themselves stay."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def insert(self, row: Row) -> None:
        """INSERT the decision row (/v1/decisions and the facade). A duplicate id raises
        asyncpg.UniqueViolationError."""
        sql, args = insert_sql(t.DECISION, self._tx, row, suffix="")
        await self._tx.conn.execute(sql, *args)

    async def get(self, decision_id: str) -> Row | None:
        """The decision, or None (missing or outside the uow's scope)."""
        if unstorable(decision_id):
            return None
        record = await self._tx.conn.fetchrow(_GET, self._tx.tenant_id, decision_id)
        return None if record is None else to_row(record)

    async def delete(self, decision_id: str) -> None:
        """Deletes the decision (absent: no-op)."""
        await self._tx.conn.execute(_DELETE, self._tx.tenant_id, decision_id)

    async def delete_for_session(self, session_id: str) -> int:
        """Deletes every decision of the session (the decision_by_session index); the number deleted."""
        return affected(await self._tx.conn.execute(_DELETE_FOR_SESSION, self._tx.tenant_id, session_id))
