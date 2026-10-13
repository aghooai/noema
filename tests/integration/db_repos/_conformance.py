"""Harness pieces of the real `Database` registration (conformance_impls.py): a dedicated per-process database
emptied before every kit, past-RLS seeding, a Postgres-down switch and a FakeClock that ages the database.

Postgres keeps its own clock (`now()`, spec-04 §2): the repositories and the definer functions compare leases, run
times and expiries with it. `AgingClock.advance(s)` therefore moves every such column s seconds into the past after
moving virtual time, which is what s seconds of real time would do to those comparisons.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Final
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
from tests.fakes.clock import FakeClock

from noema.core.principal import Principal
from noema.db.repos import PgUnitOfWork
from noema.db.system import PgSystemCalls
from noema.db.uow import PgDatabase

type Conn = asyncpg.Connection[asyncpg.Record]

SCHEMAS: Final = ("noema", "auth", "jobs", "ops", "platform")
TABLES: Final = {  # suite table names (spec-03 bare names) -> qualified names; the rest live in schema noema
    "job": "jobs.job",
    "api_key": "auth.api_key",
    "config_version": "platform.config_version",
    "price_catalog": "platform.price_catalog",
    "operator": "platform.operator",
    "outbox_dead_letter": "ops.outbox_dead_letter",
}
AGING: Final = {  # the columns Postgres compares with now() (repository SQL and the spec-03 §4 functions)
    "noema.session": ("lease_expires_at", "expires_at"),
    "noema.turn": ("expires_at",),
    "noema.decision": ("expires_at",),
    "noema.file": ("expires_at",),
    "noema.idempotency_record": ("lease_expires_at", "expires_at"),
    "jobs.job": ("run_after", "locked_until", "updated_at"),
    "auth.api_key": ("last_used_at",),
}
_IDENT: Final = re.compile(r"^[a-z_][a-z0-9_]*$")
_PRIMARY_KEY: Final = (
    "SELECT a.attname FROM pg_catalog.pg_index i JOIN pg_catalog.pg_attribute a"
    " ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) WHERE i.indrelid = $1::regclass AND i.indisprimary"
)
SEED_OPERATORS: Final = ["op_seed_1", "op_seed_2"]


def dsn(base: str, *, dbname: str, user: str | None = None, password: str | None = None) -> str:
    """`base` with another database (and optionally another login)."""
    parts = urlsplit(base)
    user = user if user is not None else (parts.username or "")
    password = password if password is not None else (parts.password or "")
    host = parts.hostname or "127.0.0.1"
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, f"/{dbname}", parts.query, ""))


async def truncate_all(admin: Conn) -> None:
    """Empties every table of the Noema schemas (a dedicated database: nothing else uses it)."""
    tables = await admin.fetch(
        "SELECT pg_catalog.format('%I.%I', schemaname, tablename) AS t FROM pg_catalog.pg_tables"
        " WHERE schemaname = ANY($1::text[])",
        list(SCHEMAS),
    )
    if tables:
        await admin.execute(f"TRUNCATE {', '.join(r['t'] for r in tables)} RESTART IDENTITY CASCADE")


async def seed(admin: Conn, table: str, row: Mapping[str, Any]) -> None:
    """Insert or replace one row past RLS. An `active` calibrator row without its two-person activation fields gets
    two seed operators, so the spec-03 CHECK (which the fake store does not model) accepts it."""
    qualified = TABLES.get(table, f"noema.{table}")
    values = dict(row)
    if not all(_IDENT.match(name) for name in (table, *values)):
        raise ValueError(f"unsafe identifier in seed({table!r}, ...)")
    if qualified == "noema.calibrator" and values.get("status") == "active" and not values.get("activated_by"):
        values.setdefault("approved_by", SEED_OPERATORS[0])
        values.update(activated_by=SEED_OPERATORS, activated_at=values.get("activated_at") or values.get("fitted_at"))
    key = [r["attname"] for r in await admin.fetch(_PRIMARY_KEY, qualified)]
    if key and all(k in values for k in key):
        where = " AND ".join(f'"{k}" = ${i}' for i, k in enumerate(key, 1))
        await admin.execute(f"DELETE FROM {qualified} WHERE {where}", *(values[k] for k in key))
    columns = ", ".join(f'"{c}"' for c in values)
    placeholders = ", ".join(f"${i}" for i in range(1, len(values) + 1))
    await admin.execute(f"INSERT INTO {qualified} ({columns}) VALUES ({placeholders})", *values.values())


async def age(admin: Conn, seconds: float) -> None:
    """Moves every `AGING` column `seconds` into the past."""
    for table, columns in AGING.items():
        assignments = ", ".join(f"{c} = {c} - $1::float8 * interval '1 second'" for c in columns)
        await admin.execute(f"UPDATE {table} SET {assignments}", seconds)


class AgingClock(FakeClock):
    """A `FakeClock` whose time moves also age the database by the same amount (see the module doc)."""

    def __init__(self, on_advance: Callable[[float], Awaitable[None]]) -> None:
        super().__init__()
        self._on_advance = on_advance

    async def advance_to(self, target: float) -> None:
        before = self.monotonic()
        await super().advance_to(target)
        if (moved := self.monotonic() - before) > 0:
            await self._on_advance(moved)


class SwitchableDatabase:
    """`Database` over the real `PgDatabase`, or (after `set_available(False)`) over one whose engine points at a
    closed port, so the three factories fail exactly as with Postgres down (`NoemaError(internal_error)`)."""

    def __init__(self, up: PgDatabase[PgUnitOfWork], down: PgDatabase[PgUnitOfWork]) -> None:
        self._up, self._down = up, down
        self._available = True

    def set_available(self, up: bool) -> None:
        self._available = up

    @property
    def _db(self) -> PgDatabase[PgUnitOfWork]:
        return self._up if self._available else self._down

    def uow(self, principal: Principal) -> AbstractAsyncContextManager[PgUnitOfWork]:
        return self._db.uow(principal)

    def tenant_uow(self, tenant_id: str, *, project_id: str | None = None) -> AbstractAsyncContextManager[PgUnitOfWork]:
        return self._db.tenant_uow(tenant_id, project_id=project_id)

    def system_uow(self) -> AbstractAsyncContextManager[PgSystemCalls]:
        return self._db.system_uow()
