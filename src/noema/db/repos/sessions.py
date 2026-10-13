"""`SessionRepo` (spec-02 §7.1): session rows and the ordering lease of spec-04 §2 (IMPL-022).

Lease rules (all on the Postgres clock, `now()`):
- `acquire` is one conditional UPDATE: the session must be live and either unleased, its lease expired, or already
  held by the same turn (a retried acquire is idempotent), and `expected_seq` (if given) must equal `last_seq`. Two
  concurrent acquirers serialize on the row lock; exactly one gets the lease, the other re-evaluates and sees `Busy`.
- An expired lease is free, so no reaper releases sessions. A finalize still waiting in the outbox is the caller's
  check (`RedisKeys.session_pending`) before `acquire`: the database cannot see it.
- `finalize_turn` first moves the session (WHERE active_turn_id = lease.turn_id AND last_seq = lease.last_seq), then
  inserts the turn with `seq = last_seq + 1`; a lost lease therefore raises `LeaseLost` before any insert (never a
  unique violation on (session_id, seq)). An expired lease that nobody took over still finalizes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Busy, LeaseLost, Page, Row, SessionLease
from noema.core.values import JsonValue
from noema.db.repos import _tables as t
from noema.db.repos._base import (
    Query,
    eq_filters,
    insert_sql,
    page_by_id_desc,
    set_clause,
    to_row,
    unstorable,
    update_sql,
)
from noema.db.repos.turns import insert_turn_rows
from noema.db.uow import Tx

SESSION_UPDATABLE: Final = frozenset({"metadata", "data_class", "retention", "expires_at"})

_LIVE: Final = "tenant_id = $1 AND id = $2 AND deleted_at IS NULL"  # the WHERE of every live-session statement
_GET: Final = "SELECT * FROM noema.session WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL"
_MARK_DELETED: Final = (
    "UPDATE noema.session SET deleted_at = now() WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL"
)
_DELETE: Final = "DELETE FROM noema.session WHERE tenant_id = $1 AND id = $2"
_ACQUIRE: Final = """
UPDATE noema.session SET active_turn_id = $3, lease_expires_at = now() + $4::int * interval '1 second'
 WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL
   AND (active_turn_id IS NULL OR lease_expires_at < now() OR active_turn_id = $3)
   AND ($5::int IS NULL OR last_seq = $5::int)
RETURNING last_seq, sticky_spec"""
_PROBE: Final = "SELECT last_seq FROM noema.session WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL"
_RENEW: Final = """
UPDATE noema.session SET lease_expires_at = now() + $4::int * interval '1 second'
 WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL AND active_turn_id = $3
RETURNING id"""
_FINALIZE: Final = """
UPDATE noema.session SET last_seq = $4 + 1, sticky_spec = $5, active_turn_id = NULL, lease_expires_at = NULL
 WHERE tenant_id = $1 AND id = $2 AND deleted_at IS NULL AND active_turn_id = $3 AND last_seq = $4
RETURNING last_seq, project_id"""
_RELEASE: Final = """
UPDATE noema.session SET active_turn_id = NULL, lease_expires_at = NULL
 WHERE tenant_id = $1 AND id = $2 AND active_turn_id = $3"""


def _not_found() -> NoemaError:
    return NoemaError(ErrorCode.SESSION_NOT_FOUND, "The session does not exist.")


def _check_lease_s(lease_s: int) -> None:
    if lease_s <= 0:
        raise ValueError("lease_s must be positive")


class PgSessionRepo:
    """`SessionRepo` over one unit of work. `get`/`update`/`list` see live sessions only (deleted_at IS NULL)."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def create(self, row: Row) -> Row:
        """INSERT the session (column defaults fill retention, metadata, last_seq, created_at); the stored row."""
        sql, args = insert_sql(t.SESSION, self._tx, row)
        record = await self._tx.conn.fetchrow(sql, *args)
        assert record is not None
        return to_row(record)

    async def get(self, session_id: str) -> Row | None:
        """The live session, or None (missing, deleted or outside the uow's scope)."""
        if unstorable(session_id):
            return None
        record = await self._tx.conn.fetchrow(_GET, self._tx.tenant_id, session_id)
        return None if record is None else to_row(record)

    async def update(self, session_id: str, **changes: object) -> Row:
        """Sets metadata, data_class, retention and/or expires_at (ValueError for other columns); the updated row.
        Raises `NoemaError(session_not_found)` for a missing, deleted or invisible session."""
        if not changes:
            current = await self.get(session_id)
            if current is None:
                raise _not_found()
            return current
        q = Query(self._tx.tenant_id, session_id)
        assignments = set_clause(t.SESSION, q, changes, SESSION_UPDATABLE)
        record = await self._tx.conn.fetchrow(update_sql(t.SESSION, assignments, _LIVE), *q.args)
        if record is None:
            raise _not_found()
        return to_row(record)

    async def list(self, *, owner_principal_id: str | None, after: str | None, limit: int) -> Page[Row]:
        """Live sessions, id DESC (newest first), optionally of one owner (the session_by_owner index)."""
        q = Query(self._tx.tenant_id)
        where = ["tenant_id = $1", "deleted_at IS NULL", *eq_filters(q, owner_principal_id=owner_principal_id)]
        return await page_by_id_desc(self._tx, t.SESSION.name, q, where, after, limit)

    async def mark_deleted(self, session_id: str) -> None:
        """deleted_at = now() on a live session (no-op otherwise); the session.delete job removes the data."""
        await self._tx.conn.execute(_MARK_DELETED, self._tx.tenant_id, session_id)

    async def delete(self, session_id: str) -> None:
        """Beyond the frozen Protocol (spec-03 §5, the session.delete job and the retention sweeper): deletes the
        session row, deleted or not. Its turns must be deleted first (`TurnRepo.delete_for_session`; the turn
        foreign key raises asyncpg.ForeignKeyViolationError otherwise). Absent: no-op."""
        await self._tx.conn.execute(_DELETE, self._tx.tenant_id, session_id)

    # --- the ordering lease (spec-04 §2) -----------------------------------------------------------------------------
    async def acquire(
        self, session_id: str, turn_id: str, *, expected_seq: int | None, lease_s: int
    ) -> SessionLease | Busy:
        """Takes the lease for `turn_id` for `lease_s` seconds. Raises `NoemaError(session_not_found)` (missing,
        deleted or foreign) or `NoemaError(session_conflict)` (`expected_seq` != last_seq; details.last_seq), else
        returns `Busy` while another live lease holds the session. The caller checks `session_pending` first."""
        _check_lease_s(lease_s)
        tenant = self._tx.tenant_id
        record = await self._tx.conn.fetchrow(_ACQUIRE, tenant, session_id, turn_id, lease_s, expected_seq)
        if record is not None:
            return SessionLease(session_id, turn_id, int(record["last_seq"]), record["sticky_spec"])
        last_seq = await self._tx.conn.fetchval(_PROBE, tenant, session_id)
        if last_seq is None:
            raise _not_found()
        if expected_seq is not None and expected_seq != last_seq:
            raise NoemaError(
                ErrorCode.SESSION_CONFLICT,
                "The session has moved on; reload it and retry with its last_seq.",
                details={"last_seq": int(last_seq)},
            )
        return Busy()

    async def renew(self, lease: SessionLease, lease_s: int) -> bool:
        """Extends the lease by `lease_s` seconds from now; False = it was taken over (or the session deleted)."""
        _check_lease_s(lease_s)
        record = await self._tx.conn.fetchrow(_RENEW, self._tx.tenant_id, lease.session_id, lease.turn_id, lease_s)
        return record is not None

    async def finalize_turn(
        self,
        lease: SessionLease,
        turn: Row,
        attempts: Sequence[Row],
        decisions: Sequence[Row],
        turn_files: Sequence[Row],
        sticky_spec: Mapping[str, JsonValue] | None,
    ) -> int:
        """Moves the session to last_seq + 1 (storing `sticky_spec`, clearing the lease) and inserts the turn with
        that seq plus its attempts, decisions and turn files; returns the new last_seq. Raises `LeaseLost` when the
        session no longer has this lease at lease.last_seq (taken over, finalized, or deleted); the caller's uow
        then rolls back. Publishing session_released after commit is the caller's."""
        record = await self._tx.conn.fetchrow(
            _FINALIZE,
            self._tx.tenant_id,
            lease.session_id,
            lease.turn_id,
            lease.last_seq,
            None if sticky_spec is None else dict(sticky_spec),
        )
        if record is None:
            raise LeaseLost(lease.session_id)
        seq = int(record["last_seq"])
        await insert_turn_rows(
            self._tx,
            turn,
            attempts,
            decisions,
            turn_files,
            session_id=lease.session_id,
            seq=seq,
            project_id=str(record["project_id"]),
        )
        return seq

    async def release(self, lease: SessionLease) -> None:
        """Clears the lease iff this turn still holds it (a 429 after acquire, a lost renewal); else a no-op."""
        await self._tx.conn.execute(_RELEASE, self._tx.tenant_id, lease.session_id, lease.turn_id)
