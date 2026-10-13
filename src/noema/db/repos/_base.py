"""Building blocks of the repositories: table metadata, statement builders, row mapping and keyset cursors.

Every statement of `noema.db.repos` is built from static SQL fragments; only values travel as bind parameters. Column
names come from the `Table` whitelists below (a row key outside the whitelist is a `ValueError`, i.e. a bug), so no
caller-supplied text ever becomes SQL.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

import asyncpg
import orjson

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Page, Row
from noema.db.uow import Tx

MAX_LIMIT: Final = 200  # openapi `Limit`: 1..200 (spec-02 §7)


def unstorable(value: str) -> bool:
    """True when `value` holds U+0000, which Postgres text cannot hold: such an id cannot exist (Wave E2, R7)."""
    return "\x00" in value


@dataclass(frozen=True, slots=True)
class Table:
    """A table the repositories write. `columns` are the writable columns (generated ones excluded);
    `sql_defaults` are SQL expressions used for omitted NOT NULL columns that have no column default."""

    name: str
    columns: frozenset[str]
    project_scoped: bool = False
    sql_defaults: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


class Query:
    """Collects bind parameters: `q.param(v)` appends `v` and returns its placeholder (`$n`)."""

    __slots__ = ("args",)

    def __init__(self, *args: object) -> None:
        self.args: list[object] = list(args)

    def param(self, value: object) -> str:
        self.args.append(value)
        return f"${len(self.args)}"


def _quoted(column: str) -> str:
    return f'"{column}"'


def check_columns(table: Table, names: Sequence[str] | frozenset[str], allowed: frozenset[str] | None = None) -> None:
    """ValueError if a name is not a writable column of `table` (or not in `allowed`)."""
    permitted = table.columns if allowed is None else allowed & table.columns
    unknown = sorted(set(names) - permitted)
    if unknown:
        raise ValueError(f"{table.name}: columns not writable here: {unknown}")


def insert_sql(table: Table, tx: Tx, row: Row, *, suffix: str = "RETURNING *") -> tuple[str, list[object]]:
    """`INSERT INTO <table> (<row columns>) VALUES (...) <suffix>` and its arguments. tenant_id defaults to the
    uow's tenant and, for project-scoped tables in a project uow, project_id to its project (a different value
    is rejected by RLS). Omitted columns take the column default or `table.sql_defaults`."""
    values = dict(row)
    values.setdefault("tenant_id", tx.tenant_id)
    if table.project_scoped and tx.project_id:
        values.setdefault("project_id", tx.project_id)
    check_columns(table, list(values))
    q = Query()
    columns = [_quoted(c) for c in values]
    placeholders = [q.param(v) for v in values.values()]
    for column, expression in table.sql_defaults.items():
        if column not in values:
            columns.append(_quoted(column))
            placeholders.append(expression)
    sql = f"INSERT INTO {table.name} ({', '.join(columns)}) VALUES ({', '.join(placeholders)}) {suffix}"  # noqa: S608
    return sql, q.args


def set_clause(table: Table, q: Query, changes: Mapping[str, object], allowed: frozenset[str]) -> str:
    """`"a" = $n, "b" = $m` for an UPDATE; ValueError for a column outside `allowed`."""
    check_columns(table, list(changes), allowed)
    return ", ".join(f"{_quoted(c)} = {q.param(v)}" for c, v in changes.items())


def select_sql(table: str, where: Sequence[str], *, order: str, limit: str | None = None) -> str:
    """`SELECT * FROM <table> WHERE <where AND ...> ORDER BY <order> [LIMIT <limit>]` from static fragments (values
    are bind parameters placed by `Query.param`)."""
    sql = f"SELECT * FROM {table} WHERE {' AND '.join(where)} ORDER BY {order}"  # noqa: S608 (static fragments)
    return sql if limit is None else f"{sql} LIMIT {limit}"


def update_sql(table: Table, assignments: str, where: str) -> str:
    """`UPDATE <table> SET <assignments> WHERE <where> RETURNING *` (assignments from `set_clause`)."""
    return f"UPDATE {table.name} SET {assignments} WHERE {where} RETURNING *"  # noqa: S608 (whitelisted columns)


def to_row(record: asyncpg.Record) -> Row:
    """A record as a plain dict keyed by column name (jsonb decoded, numeric as Decimal, timestamptz aware)."""
    return dict(record.items())


def to_rows(records: Sequence[asyncpg.Record]) -> tuple[Row, ...]:
    return tuple(dict(r.items()) for r in records)


def affected(status: str) -> int:
    """The row count of an asyncpg command status such as 'DELETE 3' or 'UPDATE 1'."""
    tail = status.rpartition(" ")[2]
    return int(tail) if tail.isdigit() else 0


# --- keyset pagination (spec-02 §7) ---------------------------------------------------------------------------------
def _malformed() -> NoemaError:
    return NoemaError(ErrorCode.SCHEMA_VIOLATION, "The pagination cursor is malformed.", param="after")


def check_limit(limit: int) -> int:
    """`limit` if it is 1..200, else `NoemaError(schema_violation, param="limit")`."""
    if isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "limit must be between 1 and 200.", param="limit")
    return limit


def encode_cursor(key: Sequence[str | int | None]) -> str:
    """base64url (unpadded) of the JSON array of the last row's sort key."""
    return base64.urlsafe_b64encode(orjson.dumps(list(key))).decode().rstrip("=")


def decode_cursor(after: str, shape: Sequence[tuple[type, ...]]) -> list[str | int | None]:
    """The sort key inside `after`; element i must be an instance of one of `shape[i]` (`type(None)` allows
    null). Anything else raises `NoemaError(schema_violation, param="after")`."""
    try:
        raw = base64.urlsafe_b64decode(after + "=" * (-len(after) % 4))
        value = orjson.loads(raw)
    except (ValueError, binascii.Error, orjson.JSONDecodeError):
        raise _malformed() from None
    if not isinstance(value, list) or len(value) != len(shape):
        raise _malformed()
    for item, types in zip(value, shape, strict=True):
        if isinstance(item, bool) or not isinstance(item, types) or (isinstance(item, str) and unstorable(item)):
            raise _malformed()
    return value


def page_of(
    records: Sequence[asyncpg.Record], limit: int, key: Callable[[Row], Sequence[str | int | None]]
) -> Page[Row]:
    """A page from up to `limit + 1` records (the extra one only decides `has_more`)."""
    rows = to_rows(records[:limit])
    has_more = len(records) > limit
    return Page(data=rows, has_more=has_more, next_cursor=encode_cursor(key(rows[-1])) if has_more and rows else None)


def id_after(after: str | None) -> str | None:
    """The id inside an `id DESC` cursor (None on the first page)."""
    if after is None:
        return None
    (last_id,) = decode_cursor(after, ((str,),))
    assert isinstance(last_id, str)
    return last_id


def id_key(row: Row) -> tuple[str]:
    return (str(row["id"]),)


def eq_filters(q: Query, **eq: object) -> list[str]:
    """`<column> = $n` for every keyword whose value is not None (the keywords are static column names)."""
    return [f"{column} = {q.param(value)}" for column, value in eq.items() if value is not None]


async def page_by_id_desc(
    tx: Tx, table: str, q: Query, where: Sequence[str], after: str | None, limit: int
) -> Page[Row]:
    """One `id DESC` keyset page of `table` rows matching `where` (newest first: ids are ULIDs)."""
    check_limit(limit)
    conditions = list(where)
    last_id = id_after(after)
    if last_id is not None:
        conditions.append(f"id < {q.param(last_id)}")
    sql = select_sql(table, conditions, order="id DESC", limit=q.param(limit + 1))
    return page_of(await tx.conn.fetch(sql, *q.args), limit, id_key)
