"""`UsageRepo` (spec-02 §7.1, spec-11 §2; IMPL-019, IMPL-020, IMPL-040): the GET /v1/usage report.

Rows are written only by `ops.outbox_apply`; this repo reads them under the uow's RLS scope (a developer sees its
project, tenant_admin/auditor the whole tenant). One report row per (key, line): the token sums, the cost sum
(Decimal, numeric(18,6)) and whether any line was estimated. Keys order `COLLATE "C"`, NULL keys last, then line; the
cursor is base64url(JSON [key, line]) of the last row.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from types import MappingProxyType
from typing import Final, Literal

from noema.core.protocols.storage import Page, Row
from noema.db.repos._base import Query, check_limit, decode_cursor, page_of
from noema.db.uow import Tx

type UsageGroupBy = Literal["project", "session", "model", "day", "line"]

GROUP_KEYS: Final[Mapping[str, str]] = MappingProxyType(  # group_by -> the SQL key expression (a whitelist)
    {
        "project": "project_id",
        "session": "session_id",
        "model": "model_id",
        "day": "to_char(day, 'YYYY-MM-DD')",
        "line": "line",
    }
)
# after the cursor (k, l): every non-null key above (k, l) plus every null key when k is set; only null keys with a
# line above l when k is null (spec-11 §2)
_AFTER: Final = (
    '(({k}::text IS NOT NULL AND (key IS NULL OR (key, line) > ({k}::text COLLATE "C", {l}::text COLLATE "C")))'
    ' OR ({k}::text IS NULL AND key IS NULL AND line > {l}::text COLLATE "C"))'
)
_REPORT: Final = (  # spec-11 §2; {key}: a GROUP_KEYS expression, {where}/{after}: static conditions over $n
    'WITH r AS (SELECT ({key}) COLLATE "C" AS key, line COLLATE "C" AS line, sum(input_tokens) AS input_tokens,'
    " sum(output_tokens) AS output_tokens, sum(cached_input_tokens) AS cached_input_tokens,"
    " sum(cache_write_input_tokens) AS cache_write_input_tokens, sum(reasoning_tokens) AS reasoning_tokens,"
    " sum(cost_usd) AS cost_usd, bool_or(cost_estimated) AS cost_estimated,"
    " bool_or(tokens_estimated) AS tokens_estimated"
    " FROM noema.usage_line WHERE {where} GROUP BY 1, 2)"
    " SELECT * FROM r WHERE {after} ORDER BY key NULLS LAST, line LIMIT {limit}"
)


def _usage_key(row: Row) -> tuple[str | None, str]:
    key, line = row["key"], row["line"]
    assert key is None or isinstance(key, str)
    assert isinstance(line, str)
    return (key, line)


class PgUsageRepo:
    """`UsageRepo` over one unit of work."""

    __slots__ = ("_tx",)

    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    async def report(
        self,
        *,
        group_by: UsageGroupBy,
        day_from: date,
        day_to: date,
        after: str | None,
        limit: int,
    ) -> Page[Row]:
        """Usage lines with day_from <= day <= day_to (UTC days) grouped by (key, line); rows {key, line,
        input_tokens, output_tokens, cached_input_tokens, cache_write_input_tokens, reasoning_tokens, cost_usd,
        cost_estimated, tokens_estimated}. Raises `NoemaError(schema_violation)` for a bad limit or cursor;
        ValueError for an unknown group_by."""
        check_limit(limit)
        key_sql = GROUP_KEYS.get(group_by)
        if key_sql is None:
            raise ValueError(f"unknown group_by {group_by!r}")
        q = Query(self._tx.tenant_id, day_from, day_to)
        where = ["tenant_id = $1", "day BETWEEN $2 AND $3"]
        if self._tx.project_id:  # explicit, so the planner can use the usage_report index (RLS also applies)
            where.append(f"project_id = {q.param(self._tx.project_id)}")
        outer = "true"
        if after is not None:
            last_key, last_line = decode_cursor(after, ((str, type(None)), (str,)))
            outer = _AFTER.format(k=q.param(last_key), l=q.param(last_line))
        sql = _REPORT.format(key=key_sql, where=" AND ".join(where), after=outer, limit=q.param(limit + 1))
        return page_of(await self._tx.conn.fetch(sql, *q.args), limit, _usage_key)
