"""`IdempotencyRepo` (spec-02 §7.1, spec-11 §5; IMPL-077, IMPL-085): the rows behind the IdempotencyStore.

- `insert_in_flight` relies on the unique (tenant_id, project_id, scope_hash, key) index: a clash inserts nothing.
- `take_over` is one conditional UPDATE that re-checks that the record is still free to take: expired
  (`expires_at < now()`), `not_stored`, or `in_flight` with a lapsed lease. Of two racing takers exactly one wins;
  the loser gets False and re-reads.
- `finish` stores a body of at most `IDEM_INLINE_MAX_BYTES` inline (`response_body`); a larger body is stored as the
  object key `idempotency_body_key(tenant_id, record_id)` (`response_body_key`, response_body NULL). The store must
  have put the object under that key before calling `finish` (object first, then the row, IMPL-085).
- Headers are stored as a JSON array of [name, value] pairs. Every write is fenced by `owner`.
"""

from __future__ import annotations

from typing import Final

from noema.core.protocols.storage import Row, StoredResponse
from noema.db.repos import _tables as t
from noema.db.repos._base import affected, insert_sql, to_row
from noema.db.uow import Tx

IDEM_INLINE_MAX_BYTES: Final = 1 << 20  # IMPL-085: bodies above 1 MiB go to object storage


def idempotency_body_key(tenant_id: str, record_id: str) -> str:
    """The object key of a large stored body: `<tenant_id>/idempotency/<record_id>` (spec-03 §2 S3 layout)."""
    return f"{tenant_id}/idempotency/{record_id}"


_INSERT_SUFFIX: Final = "ON CONFLICT (tenant_id, project_id, scope_hash, key) DO NOTHING RETURNING id"
_BY_KEY: Final = "SELECT * FROM noema.idempotency_record WHERE tenant_id = $1 AND scope_hash = $2 AND key = $3"
_BY_PROJECT_KEY: Final = (
    "SELECT * FROM noema.idempotency_record WHERE tenant_id = $1 AND project_id = $4 AND scope_hash = $2 AND key = $3"
)
_TAKE_OVER: Final = """
UPDATE noema.idempotency_record
   SET state = 'in_flight', owner = $3, fingerprint = $4, resource_id = $5,
       lease_expires_at = now() + $6::int * interval '1 second', expires_at = now() + $7::int * interval '1 second',
       created_at = now(), response_status = NULL, response_headers = NULL, response_body = NULL,
       response_body_key = NULL, sse_turn_id = NULL
 WHERE tenant_id = $1 AND id = $2
   AND (expires_at < now() OR state = 'not_stored' OR (state = 'in_flight' AND lease_expires_at < now()))
RETURNING id"""
_RENEW: Final = """
UPDATE noema.idempotency_record SET lease_expires_at = now() + $4::int * interval '1 second'
 WHERE tenant_id = $1 AND id = $2 AND owner = $3 AND state = 'in_flight'
RETURNING id"""
_NOT_STORED: Final = """
UPDATE noema.idempotency_record SET state = 'not_stored'
 WHERE tenant_id = $1 AND id = $2 AND owner = $3 AND state = 'in_flight'"""
_COMPLETED: Final = """
UPDATE noema.idempotency_record
   SET state = 'completed', response_status = $4, response_headers = $5, response_body = $6, response_body_key = $7,
       sse_turn_id = $8
 WHERE tenant_id = $1 AND id = $2 AND owner = $3 AND state = 'in_flight'"""
_DELETE: Final = "DELETE FROM noema.idempotency_record WHERE tenant_id = $1 AND id = $2"
_EXPIRE_FOR_SESSION: Final = """
UPDATE noema.idempotency_record SET expires_at = now(), response_body = NULL
 WHERE tenant_id = $1 AND expires_at >= now() AND resource_id IN (
       SELECT id FROM noema.turn WHERE tenant_id = $1 AND session_id = $2
       UNION ALL SELECT id FROM noema.decision WHERE tenant_id = $1 AND session_id = $2)"""
_DELETE_EXPIRED: Final = (
    "DELETE FROM noema.idempotency_record WHERE tenant_id = $1 AND id = $2 AND expires_at < now() RETURNING id"
)


def _check_seconds(**values: int) -> None:
    for name, value in values.items():
        if value <= 0:
            raise ValueError(f"{name} must be positive")


class PgIdempotencyRepo:
    """`IdempotencyRepo` over one unit of work (used only by the IdempotencyStore implementation). In a project
    uow every lookup names the project, so `get_by_key` uses the unique index; a tenant-wide uow finds the record
    by (scope_hash, key) alone, which is unique because scope_hash covers the project."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def insert_in_flight(self, row: Row) -> bool:
        """INSERT an in_flight record (state defaults to 'in_flight'; the row carries id, scope_hash, key,
        fingerprint, owner, lease_expires_at, expires_at and resource_id, plus project_id in a tenant-wide uow).
        False when a record with the same (project, scope_hash, key) exists."""
        sql, args = insert_sql(t.IDEMPOTENCY_RECORD, self._tx, {"state": "in_flight", **row}, suffix=_INSERT_SUFFIX)
        return await self._tx.conn.fetchval(sql, *args) is not None

    async def get_by_key(self, scope_hash: str, key: str) -> Row | None:
        """The record of (scope_hash, key) in any state (expired ones included; the store classifies), or None."""
        if self._tx.project_id:
            record = await self._tx.conn.fetchrow(
                _BY_PROJECT_KEY, self._tx.tenant_id, scope_hash, key, self._tx.project_id
            )
        else:
            record = await self._tx.conn.fetchrow(_BY_KEY, self._tx.tenant_id, scope_hash, key)
        return None if record is None else to_row(record)

    async def take_over(
        self,
        record_id: str,
        *,
        owner: str,
        fingerprint: str,
        lease_s: int,
        ttl_s: int,
        resource_id: str | None = None,
    ) -> bool:
        """Makes the record a fresh in_flight record of `owner` (new fingerprint, lease, TTL, created_at; the old
        response and its object key cleared) iff it is still expired, not_stored or in_flight with a lapsed lease.
        False = a racing taker won (or the record is gone). `resource_id` (beyond the frozen Protocol, spec-11 §5)
        is the new request's turn/decision id; None clears the old one."""
        _check_seconds(lease_s=lease_s, ttl_s=ttl_s)
        record = await self._tx.conn.fetchrow(
            _TAKE_OVER, self._tx.tenant_id, record_id, owner, fingerprint, resource_id, lease_s, ttl_s
        )
        return record is not None

    async def renew(self, record_id: str, *, owner: str, lease_s: int) -> bool:
        """lease_expires_at = now() + lease_s while `owner` holds the in_flight record; False = lease lost."""
        _check_seconds(lease_s=lease_s)
        record = await self._tx.conn.fetchrow(_RENEW, self._tx.tenant_id, record_id, owner, lease_s)
        return record is not None

    async def finish(self, record_id: str, *, owner: str, response: StoredResponse | None) -> None:
        """`None` -> not_stored; else completed with the response (large bodies by object key, see the module
        doc). A no-op unless `owner` still holds the in_flight record, so a repeated call changes nothing."""
        tenant = self._tx.tenant_id
        if response is None:
            await self._tx.conn.execute(_NOT_STORED, tenant, record_id, owner)
            return
        body, body_key = response.body, None
        if body is not None and len(body) > IDEM_INLINE_MAX_BYTES:
            body, body_key = None, idempotency_body_key(tenant, record_id)
        headers = [[name, value] for name, value in response.headers]
        await self._tx.conn.execute(
            _COMPLETED, tenant, record_id, owner, response.status, headers, body, body_key, response.sse_turn_id
        )

    async def delete(self, record_id: str) -> None:
        """Deletes the record (absent: no-op). Its object, if any, is the caller's (the retention sweeper)."""
        await self._tx.conn.execute(_DELETE, self._tx.tenant_id, record_id)

    async def expire_for_session(self, session_id: str) -> int:
        """Ends the records of the session's turns and decisions (beyond the frozen Protocol; `session.delete`,
        Wave E2): expired now, so they are never replayed again and the retention sweeper deletes the row and any
        large body; the inline body is cleared at once. Run it before the turn and decision rows are deleted."""
        return affected(await self._tx.conn.execute(_EXPIRE_FOR_SESSION, self._tx.tenant_id, session_id))

    async def delete_expired(self, record_id: str) -> bool:
        """Deletes the record iff it is still expired (beyond the frozen Protocol; the retention sweeper's delete,
        Wave E): False when it is gone or a new request took it over since the sweeper read it (same id and
        object key). The row stays locked until the unit of work ends, so a racing take-over waits for it."""
        return await self._tx.conn.fetchval(_DELETE_EXPIRED, self._tx.tenant_id, record_id) is not None
