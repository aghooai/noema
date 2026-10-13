"""`TurnRepo` and `AttemptRepo` (spec-02 §7.1, spec-03 §2, spec-04 §10), plus the turn insert shared with
`SessionRepo.finalize_turn`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Page, Row
from noema.db.repos import _tables as t
from noema.db.repos._base import affected, check_limit, decode_cursor, insert_sql, page_of, to_row, to_rows, unstorable
from noema.db.uow import Tx


async def insert_turn_rows(
    tx: Tx,
    turn: Row,
    attempts: Sequence[Row],
    decisions: Sequence[Row],
    turn_files: Sequence[Row],
    *,
    session_id: str | None,
    seq: int | None,
    project_id: str | None = None,
) -> None:
    """INSERT the turn (with `session_id`/`seq` forced), its attempts and turn files (turn_id forced to the turn's
    id) and its decision rows. `project_id` is the default project of the turn and its decisions when the rows
    carry none (finalize passes the session's project). Statement errors (a duplicate id, a constraint) propagate
    as asyncpg errors."""
    values = {**turn, "session_id": session_id, "seq": seq}
    if project_id is not None:
        values.setdefault("project_id", project_id)
    sql, args = insert_sql(t.TURN, tx, values, suffix="RETURNING id, project_id")
    record = await tx.conn.fetchrow(sql, *args)
    assert record is not None
    turn_id, turn_project = record["id"], record["project_id"]
    for attempt in attempts:
        sql, args = insert_sql(t.ATTEMPT, tx, {**attempt, "turn_id": turn_id}, suffix="")
        await tx.conn.execute(sql, *args)
    for decision in decisions:
        sql, args = insert_sql(t.DECISION, tx, {"project_id": turn_project, **decision}, suffix="")
        await tx.conn.execute(sql, *args)
    for turn_file in turn_files:
        sql, args = insert_sql(t.TURN_FILE, tx, {**turn_file, "turn_id": turn_id}, suffix="")
        await tx.conn.execute(sql, *args)


_MAX_SEQ: Final = 2**31 - 1  # turn.seq is int4
_GET: Final = "SELECT * FROM noema.turn WHERE tenant_id = $1 AND id = $2"
_PAGE: Final = (
    "SELECT * FROM noema.turn WHERE tenant_id = $1 AND session_id = $2 AND seq > $3 ORDER BY seq ASC LIMIT $4"
)
_RECENT: Final = (
    "SELECT * FROM noema.turn WHERE tenant_id = $1 AND session_id = $2 AND seq <= $3 ORDER BY seq DESC LIMIT $4"
)
_DELETE: Final = "DELETE FROM noema.turn WHERE tenant_id = $1 AND id = $2"
_DELETE_FOR_SESSION: Final = "DELETE FROM noema.turn WHERE tenant_id = $1 AND session_id = $2"
_ATTEMPTS: Final = (  # through the turn row, so its project scope (RLS) applies to its attempts too
    "SELECT a.* FROM noema.attempt a JOIN noema.turn t ON t.tenant_id = a.tenant_id AND t.id = a.turn_id"
    " WHERE a.tenant_id = $1 AND a.turn_id = $2 ORDER BY a.started_at, a.id"
)


def _seq_key(row: Row) -> tuple[int]:
    seq = row["seq"]
    assert isinstance(seq, int)
    return (seq,)


class PgTurnRepo:
    """`TurnRepo` over one unit of work."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def get(self, turn_id: str) -> Row | None:
        """The turn, or None (missing or outside the uow's scope)."""
        if unstorable(turn_id):
            return None
        record = await self._tx.conn.fetchrow(_GET, self._tx.tenant_id, turn_id)
        return None if record is None else to_row(record)

    async def insert(
        self, turn: Row, attempts: Sequence[Row], decisions: Sequence[Row], turn_files: Sequence[Row]
    ) -> None:
        """A `store: true` turn without a session (session_id and seq NULL), with its child rows."""
        await insert_turn_rows(self._tx, turn, attempts, decisions, turn_files, session_id=None, seq=None)

    async def list_for_session(self, session_id: str, *, after: str | None, limit: int) -> Page[Row]:
        """The session's turns, seq ASC (keyset on seq; the turn_tenant_id_session_id_seq_key index)."""
        check_limit(limit)
        last_seq = 0
        if after is not None:
            (cursor_seq,) = decode_cursor(after, ((int,),))
            if not isinstance(cursor_seq, int) or not 0 <= cursor_seq <= _MAX_SEQ:
                raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "The pagination cursor is malformed.", param="after")
            last_seq = cursor_seq
        records = await self._tx.conn.fetch(_PAGE, self._tx.tenant_id, session_id, last_seq, limit + 1)
        return page_of(records, limit, _seq_key)

    async def recent(self, session_id: str, *, max_seq: int, limit: int) -> tuple[Row, ...]:
        """Up to `limit` turns with seq <= max_seq, seq DESC (history selection)."""
        if limit <= 0:
            return ()
        return to_rows(await self._tx.conn.fetch(_RECENT, self._tx.tenant_id, session_id, max_seq, limit))

    async def delete(self, turn_id: str) -> None:
        """Deletes the turn; its attempt and turn_file rows go with it (ON DELETE CASCADE). Absent: no-op."""
        await self._tx.conn.execute(_DELETE, self._tx.tenant_id, turn_id)

    async def delete_for_session(self, session_id: str) -> int:
        """Deletes every turn of the session (cascading); the number deleted."""
        return affected(await self._tx.conn.execute(_DELETE_FOR_SESSION, self._tx.tenant_id, session_id))


class PgAttemptRepo:
    """`AttemptRepo` over one unit of work."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def list_for_turn(self, turn_id: str) -> tuple[Row, ...]:
        """The attempts of a visible turn, started_at ASC (id breaks ties); () for an invisible turn."""
        return to_rows(await self._tx.conn.fetch(_ATTEMPTS, self._tx.tenant_id, turn_id))
