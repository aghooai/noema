"""The usage report behind `GET /v1/usage` (spec-11 §2, IMPL-040, spec-09 §3).

`parse_query` turns the coerced query parameters into a `UsageQuery`: `group_by` (default `line`), the inclusive
UTC day range (`to` defaults to today, `from` to `to` minus 29 days; `from` after `to` or a range longer than 366
days is 400 `schema_violation`), `limit` (1..200, default 50) and the opaque `after` cursor.

`UsageReporter.report` reads `UsageRepo.report` under `Database.uow(principal)`: RLS confines a developer to its
project and gives tenant_admin/auditor the whole tenant. `render_report` serializes the `UsageReport` body by hand
so `cost_usd` is the exact decimal sum as a JSON number with 6 decimals (a float would lose digits on large sums).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Literal, cast, get_args

import orjson

from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.storage import Database, Page, Row
from noema.core.values import JsonValue

UsageGroupBy = Literal["project", "session", "model", "day", "line"]
GROUP_BY_VALUES: Final[frozenset[str]] = frozenset(get_args(UsageGroupBy))
DEFAULT_DAYS: Final = 30  # from = to - 29 days: the last 30 days, inclusive
MAX_DAYS: Final = 366
DEFAULT_LIMIT: Final = 50
MICRO: Final = Decimal("0.000001")
_COUNTS: Final = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "reasoning_tokens",
)


@dataclass(frozen=True, slots=True)
class UsageQuery:
    """One validated usage request."""

    group_by: UsageGroupBy
    day_from: date
    day_to: date
    after: str | None
    limit: int


def _violation(param: str, message: str) -> NoemaError:
    return NoemaError(ErrorCode.SCHEMA_VIOLATION, message, param=param)


def _day(params: Mapping[str, JsonValue], name: str) -> date | None:
    raw = params.get(name)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _violation(name, f"`{name}` must be a date (YYYY-MM-DD).")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise _violation(name, f"`{name}` must be a date (YYYY-MM-DD).") from None


def parse_query(params: Mapping[str, JsonValue], today: date) -> UsageQuery:
    """The `UsageQuery` of getUsage's coerced parameters; `today` is the current UTC date. Raises
    `NoemaError(schema_violation)` with `param` for an unknown group_by, a bad date or range, or a bad limit."""
    group_by = params.get("group_by", "line")
    if not isinstance(group_by, str) or group_by not in GROUP_BY_VALUES:
        raise _violation("group_by", "`group_by` must be one of project, session, model, day, line.")
    day_to = _day(params, "to") or today
    day_from = _day(params, "from") or day_to - timedelta(days=DEFAULT_DAYS - 1)
    if day_from > day_to:
        raise _violation("from", "`from` must not be after `to`.")
    if (day_to - day_from).days + 1 > MAX_DAYS:
        raise _violation("from", f"The date range may span at most {MAX_DAYS} days.")
    limit = params.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise _violation("limit", "`limit` must be an integer from 1 to 200.")
    after = params.get("after")
    if after is not None and not isinstance(after, str):
        raise _violation("after", "Malformed pagination cursor.")
    return UsageQuery(cast(UsageGroupBy, group_by), day_from, day_to, after, limit)


def _cost(value: object) -> bytes:
    usd = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(usd.quantize(MICRO), "f").encode()


def _flag(value: object) -> bytes:
    return b"true" if value else b"false"


def _row(row: Row) -> bytes:
    counts = b",".join(b'"%s":%d' % (name.encode(), int(cast(int, row[name]))) for name in _COUNTS)
    return b"".join(
        (
            b'{"key":',
            orjson.dumps(row["key"]),
            b',"line":',
            orjson.dumps(row["line"]),
            b',"usage":{',
            counts,
            b',"cost_usd":',
            _cost(row["cost_usd"]),
            b',"cost_estimated":',
            _flag(row["cost_estimated"]),
            b',"tokens_estimated":',
            _flag(row["tokens_estimated"]),
            b"}}",
        )
    )


def render_report(query: UsageQuery, page: Page[Row]) -> bytes:
    """The `UsageReport` JSON body of one page."""
    return b"".join(
        (
            b'{"group_by":',
            orjson.dumps(query.group_by),
            b',"from":',
            orjson.dumps(query.day_from.isoformat()),
            b',"to":',
            orjson.dumps(query.day_to.isoformat()),
            b',"data":[',
            b",".join(_row(r) for r in page.data),
            b'],"has_more":',
            _flag(page.has_more),
            b',"next_cursor":',
            orjson.dumps(page.next_cursor),
            b"}",
        )
    )


class UsageReporter:
    """Reads one report page under the caller's RLS scope."""

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    async def page(self, principal: Principal, query: UsageQuery) -> Page[Row]:
        """`UsageRepo.report` under `uow(principal)`; a bad cursor raises `NoemaError(schema_violation, after)`."""
        async with self._db.uow(principal) as uow:
            return await uow.usage.report(
                group_by=query.group_by,
                day_from=query.day_from,
                day_to=query.day_to,
                after=query.after,
                limit=query.limit,
            )

    async def report(self, principal: Principal, query: UsageQuery) -> bytes:
        """The rendered `UsageReport` body."""
        return render_report(query, await self.page(principal, query))
