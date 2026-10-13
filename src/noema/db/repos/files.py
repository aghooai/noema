"""`FileRepo` and `DerivativeRepo` (spec-02 §7.1, spec-03 §2, spec-07)."""

from __future__ import annotations

from typing import Final

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Row
from noema.db.repos import _tables as t
from noema.db.repos._base import Query, insert_sql, set_clause, to_row, to_rows, unstorable, update_sql
from noema.db.uow import Tx

FILE_UPDATABLE: Final = frozenset({"status", "media_type", "rejection", "failure", "pages", "deleted_at"})
_DERIVATIVE_KEY: Final = frozenset({"tenant_id", "file_id", "kind"})

_GET: Final = "SELECT * FROM noema.file WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL"
_FOR_SESSION: Final = (
    "SELECT * FROM noema.file WHERE tenant_id = $1 AND session_id = $2 AND deleted_at IS NULL ORDER BY id"
)
_DELETE: Final = "DELETE FROM noema.file WHERE tenant_id = $1 AND id = $2"
_DERIVATIVES: Final = (  # through the file row, so its project scope (RLS) applies to its derivatives too
    "SELECT d.* FROM noema.derivative d JOIN noema.file f ON f.tenant_id = d.tenant_id AND f.id = d.file_id"
    " WHERE d.tenant_id = $1 AND d.file_id = $2 ORDER BY d.kind"
)


class PgFileRepo:
    """`FileRepo` over one unit of work. `get`/`list_for_session` hide deleted files; `update` and `delete` reach
    them too (the media pipeline may finish after a delete)."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def create(self, row: Row) -> Row:
        """INSERT the file row; the stored row (created_at from the column default)."""
        sql, args = insert_sql(t.FILE, self._tx, row)
        record = await self._tx.conn.fetchrow(sql, *args)
        assert record is not None
        return to_row(record)

    async def get(self, file_id: str) -> Row | None:
        """The file, or None (missing, deleted or outside the uow's scope)."""
        if unstorable(file_id):
            return None
        record = await self._tx.conn.fetchrow(_GET, self._tx.tenant_id, file_id)
        return None if record is None else to_row(record)

    async def update(self, file_id: str, **changes: object) -> Row:
        """Sets status, media_type, rejection, failure, pages and/or deleted_at (ValueError for other columns); the
        updated row. Raises `NoemaError(not_found)` for a missing or invisible file."""
        q = Query(self._tx.tenant_id, file_id)
        if changes:
            assignments = set_clause(t.FILE, q, changes, FILE_UPDATABLE)
            sql = update_sql(t.FILE, assignments, "tenant_id = $1 AND id = $2")
        else:
            sql = "SELECT * FROM noema.file WHERE tenant_id = $1 AND id = $2"
        record = await self._tx.conn.fetchrow(sql, *q.args)
        if record is None:
            raise NoemaError(ErrorCode.NOT_FOUND, "The file does not exist.")
        return to_row(record)

    async def list_for_session(self, session_id: str) -> tuple[Row, ...]:
        """The session's live files, id ASC."""
        return to_rows(await self._tx.conn.fetch(_FOR_SESSION, self._tx.tenant_id, session_id))

    async def delete(self, file_id: str) -> None:
        """Deletes the file row; its derivative rows go with it (ON DELETE CASCADE). S3 objects are the caller's."""
        await self._tx.conn.execute(_DELETE, self._tx.tenant_id, file_id)


class PgDerivativeRepo:
    """`DerivativeRepo` over one unit of work."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def upsert(self, row: Row) -> None:
        """Keyed by (file_id, kind): overwrite the given columns of the existing derivative (the columns the row
        omits keep their values, e.g. id and created_at), else INSERT it (then the row must be complete: id,
        status, extractor_version). ValueError without file_id and kind."""
        if "file_id" not in row or "kind" not in row:
            raise ValueError("a derivative upsert needs file_id and kind")
        changes = {c: v for c, v in row.items() if c not in _DERIVATIVE_KEY}
        if changes:
            q = Query(self._tx.tenant_id, row["file_id"], row["kind"])
            assignments = set_clause(t.DERIVATIVE, q, changes, t.DERIVATIVE.columns)
            where = "tenant_id = $1 AND file_id = $2 AND kind = $3"
            if await self._tx.conn.fetchrow(update_sql(t.DERIVATIVE, assignments, where), *q.args) is not None:
                return
        sql, args = insert_sql(t.DERIVATIVE, self._tx, row, suffix="")
        if changes:  # a concurrent insert of the same key wins the race: apply our columns on top
            sql += " ON CONFLICT (tenant_id, file_id, kind) DO UPDATE SET "
            sql += ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in changes)
        else:
            sql += " ON CONFLICT (tenant_id, file_id, kind) DO NOTHING"
        await self._tx.conn.execute(sql, *args)

    async def list_for_file(self, file_id: str) -> tuple[Row, ...]:
        """The derivatives of a visible file (deleted files included), kind ASC."""
        return to_rows(await self._tx.conn.fetch(_DERIVATIVES, self._tx.tenant_id, file_id))
