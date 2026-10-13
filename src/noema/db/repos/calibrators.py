"""`CalibratorRepo` (spec-02 §7.1, spec-03 §2-§3; ADR-003, IMPL-072).

Reads see the tenant's own rows and every platform row (`cal_read`). Writes reach tenant rows only (`cal_write`),
and the lifecycle guard lets the app create `fitted` rows and move a `fitted` row to `accepted`/`rejected` only;
approval, activation, downgrade and retirement go through the admin.* definer functions.
"""

from __future__ import annotations

from typing import Final

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Page, Row
from noema.db.repos import _tables as t
from noema.db.repos._base import Query, eq_filters, insert_sql, page_by_id_desc, set_clause, to_row, update_sql
from noema.db.uow import Tx

CALIBRATOR_UPDATABLE: Final = frozenset({"status", "acceptance", "holdout", "accepted_at"})

_VISIBLE: Final = "(tenant_id = $1 OR scope = 'platform')"
_GET: Final = "SELECT * FROM noema.calibrator WHERE id = $2 AND (tenant_id = $1 OR scope = 'platform')"


class PgCalibratorRepo:
    """`CalibratorRepo` over one unit of work."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def insert(self, row: Row) -> None:
        """INSERT a tenant artifact (scope 'tenant', status 'fitted' and fitted_at = now() unless given). A
        platform row or any other status is refused by RLS (asyncpg.InsufficientPrivilegeError)."""
        values = {"scope": "tenant", "status": "fitted", **row}
        sql, args = insert_sql(t.CALIBRATOR, self._tx, values, suffix="")
        await self._tx.conn.execute(sql, *args)

    async def get(self, calibrator_id: str) -> Row | None:
        """The artifact (own tenant or platform), or None."""
        record = await self._tx.conn.fetchrow(_GET, self._tx.tenant_id, calibrator_id)
        return None if record is None else to_row(record)

    async def update(self, calibrator_id: str, **changes: object) -> Row:
        """Sets status (accepted|rejected), acceptance, holdout and/or accepted_at on a `fitted` tenant row
        (ValueError for other columns); the updated row. Raises `NoemaError(not_found)` when no such row is
        writable: missing, another tenant's, a platform row, or no longer `fitted`."""
        if not changes:
            current = await self.get(calibrator_id)
            if current is None:
                raise NoemaError(ErrorCode.NOT_FOUND, "The calibrator does not exist.")
            return current
        q = Query(self._tx.tenant_id, calibrator_id)
        assignments = set_clause(t.CALIBRATOR, q, changes, CALIBRATOR_UPDATABLE)
        sql = update_sql(t.CALIBRATOR, assignments, "id = $2 AND tenant_id = $1 AND scope = 'tenant'")
        record = await self._tx.conn.fetchrow(sql, *q.args)
        if record is None:
            raise NoemaError(ErrorCode.NOT_FOUND, "The calibrator does not exist or cannot change.")
        return to_row(record)

    async def list(
        self, *, profile_id: str | None, question_name: str | None, status: str | None, after: str | None, limit: int
    ) -> Page[Row]:
        """Tenant and platform artifacts, id DESC, filtered by the given (non-None) fields."""
        q = Query(self._tx.tenant_id)
        filters = eq_filters(q, profile_id=profile_id, question_name=question_name, status=status)
        return await page_by_id_desc(self._tx, t.CALIBRATOR.name, q, [_VISIBLE, *filters], after, limit)
