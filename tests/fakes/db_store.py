"""The in-memory store behind `FakeDatabase` (spec-02 §7.1, spec-03 §2-§3).

Tables are dicts of rows keyed by their spec-03 primary key. Semantics, and where they differ from Postgres:
- RLS: every repo read and write goes through a `Scope` (tenant, project or "" = tenant-wide); project-scoped tables
  (`session`, `turn`, `decision`, `file`, `usage_line`, `idempotency_record`) also filter on the project; a write
  that would put a row in another scope raises `RlsViolation` (a bug, like a Postgres RLS error).
- Transactions apply writes immediately and keep an undo log; a rollback restores the previous rows. Concurrent
  transactions therefore see each other's uncommitted writes (no isolation); conditional updates are atomic
  because a repo method never awaits between its check and its write.
- Keyset pagination: `after` is base64url(orjson(sort key)); a malformed cursor raises
  `NoemaError(schema_violation, param="after")`.
"""

from __future__ import annotations

import base64
import copy
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import orjson

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Page, Row

PROJECT_TABLES: Final = frozenset({"session", "turn", "decision", "file", "usage_line", "idempotency_record"})
TENANT_TABLES: Final = (
    frozenset(
        {
            "turn_file",
            "attempt",
            "derivative",
            "audit_event",
            "job",
            "labeling_run",
            "label_item",
            "project",
            "principal",
            "api_key",
        }
    )
    | PROJECT_TABLES
)
PLATFORM_TENANT: Final = "__platform__"
MAX_LIMIT: Final = 200


class RlsViolation(Exception):
    """A write outside the transaction's tenant/project scope (Postgres would raise an RLS error)."""


def pk_of(table: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    """The spec-03 primary key of a row."""
    if table in ("job", "calibrator", "tenant"):
        return (row["id"],)
    if table == "api_key":
        return (row["key_id"],)
    if table == "turn_file":
        return (row["tenant_id"], row["turn_id"], row["part_index"])
    if table == "outbox_dead_letter":
        return (row["kind"], row["id"])
    if table in ("config_version", "price_catalog"):
        return (row["version"],)
    if table == "calibrator_epoch":
        return (row["scope_key"],)
    return (row["tenant_id"], row["id"])


@dataclass(frozen=True, slots=True)
class Scope:
    """An RLS scope: `project_id` "" = every project of the tenant."""

    tenant_id: str
    project_id: str

    def sees(self, table: str, row: Mapping[str, Any]) -> bool:
        if table == "calibrator":
            return row.get("scope") == "platform" or row.get("tenant_id") == self.tenant_id
        if table == "calibrator_epoch":
            return row["scope_key"] in ("platform", self.tenant_id)
        if table == "tenant":
            return bool(row["id"] == self.tenant_id)
        if row.get("tenant_id") != self.tenant_id:
            return False
        return table not in PROJECT_TABLES or self.project_id == "" or row.get("project_id") == self.project_id

    def check_write(self, table: str, row: Mapping[str, Any]) -> None:
        if table == "calibrator":
            ok = row.get("scope") == "tenant" and row.get("tenant_id") == self.tenant_id
        else:
            ok = self.sees(table, row)
        if not ok:
            raise RlsViolation(f"row outside the scope {self} in {table}")


class FakeStore:
    """All tables. `now` is the clock's wall time; `available = False` makes every transaction factory raise
    `NoemaError(internal_error)` (Postgres down)."""

    def __init__(self, now: Callable[[], datetime]) -> None:
        self.now = now
        self.tables: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
        self.available = True
        self.commits = 0
        self.rollbacks = 0

    def table(self, name: str) -> dict[tuple[Any, ...], dict[str, Any]]:
        return self.tables.setdefault(name, {})

    def rows(self, name: str) -> list[dict[str, Any]]:
        """Every row of a table, unscoped (test inspection)."""
        return [copy.deepcopy(r) for r in self.table(name).values()]

    def seed(self, name: str, row: Mapping[str, Any]) -> dict[str, Any]:
        """Insert or replace a row without scope checks (fixtures)."""
        stored = dict(row)
        self.table(name)[pk_of(name, stored)] = stored
        return dict(stored)


class Tx:
    """One transaction's undo log over a `FakeStore`."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self._undo: list[tuple[str, tuple[Any, ...], dict[str, Any] | None]] = []

    def put(self, table: str, row: Mapping[str, Any]) -> dict[str, Any]:
        key = pk_of(table, row)
        tbl = self.store.table(table)
        self._undo.append((table, key, copy.deepcopy(tbl.get(key))))
        tbl[key] = dict(row)
        return dict(row)

    def delete(self, table: str, key: tuple[Any, ...]) -> None:
        tbl = self.store.table(table)
        if key in tbl:
            self._undo.append((table, key, tbl.pop(key)))

    def rollback(self) -> None:
        for table, key, before in reversed(self._undo):
            tbl = self.store.table(table)
            if before is None:
                tbl.pop(key, None)
            else:
                tbl[key] = before
        self._undo.clear()
        self.store.rollbacks += 1

    def commit(self) -> None:
        self._undo.clear()
        self.store.commits += 1


def encode_cursor(key: Iterable[Any]) -> str:
    return base64.urlsafe_b64encode(orjson.dumps(list(key), default=str)).decode().rstrip("=")


def decode_cursor(after: str) -> list[Any]:
    try:
        value = orjson.loads(base64.urlsafe_b64decode(after + "=" * (-len(after) % 4)))
    except (ValueError, orjson.JSONDecodeError):
        raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "Malformed pagination cursor.", param="after") from None
    if not isinstance(value, list):
        raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "Malformed pagination cursor.", param="after")
    return value


def check_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_LIMIT:
        raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "limit must be between 1 and 200.", param="limit")
    return limit


def paginate(
    rows: list[dict[str, Any]],
    key: Callable[[Mapping[str, Any]], tuple[Any, ...]],
    *,
    descending: bool,
    after: str | None,
    limit: int,
) -> Page[Row]:
    """Keyset page over `rows` ordered by `key` (`descending` for id DESC lists)."""
    check_limit(limit)
    ordered = sorted(rows, key=key, reverse=descending)
    if after is not None:
        mark = tuple(decode_cursor(after))
        ordered = [r for r in ordered if (key(r) < mark if descending else key(r) > mark)]
    page = ordered[:limit]
    has_more = len(ordered) > limit
    return Page(
        data=tuple(copy.deepcopy(r) for r in page),
        has_more=has_more,
        next_cursor=encode_cursor(key(page[-1])) if has_more and page else None,
    )
