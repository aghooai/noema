"""Harness pieces of the ops-ledger conformance kits on real Postgres and Valkey.

- `dedicated_database(h)`: one database per process (cloned from the migrated template, dropped at session end,
  re-created if it vanished), because the suites use fixed ids (t_acme, k1, ...); `truncate_all` empties it
  before a kit.
- `LedgerClock`: the suites drive a `FakeClock`, while Postgres compares leases, run times and expiries with its
  own `now()`. Every virtual time move (explicit `advance` and `autojump`) first ages the database columns by the
  same amount (`tests.integration.db_repos._conformance.age`), and `autojump` only jumps once no database or Redis
  call is in flight (the `busy` counter of `CountingDatabase`), so real I/O never races virtual time.
- `DbTimeClock`: the clock the implementation under test gets. Its `now()` is the database's timeline (real UTC,
  which the aging keeps consistent with the aged columns); `monotonic`/`sleep` are the suite's virtual clock.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from datetime import UTC, datetime
from functools import partial
from typing import Any

import anyio
from tests.contract_internal.registry import Harness
from tests.fakes.clock import FakeClock, single_errors
from tests.fixtures.db import DEV_PASSWORDS, create_scratch_database, ensure_template
from tests.integration.db_repos._conformance import age, dsn, truncate_all

from noema.core.principal import Principal
from noema.core.secret import SecretStr
from noema.db.engine import ROLE_APP, Engine, PoolSettings, connect, drop_database
from noema.db.repos import PgUnitOfWork, make_unit_of_work
from noema.db.system import PgSystemCalls
from noema.db.uow import PgDatabase

_DEDICATED: dict[int, tuple[str, SecretStr, SecretStr]] = {}  # pid -> (name, admin dsn, app dsn)


def _drop(base: str, name: str) -> None:
    async def run() -> None:
        admin = await connect(SecretStr(base), expected_role=None)
        try:
            await drop_database(admin, name)
        finally:
            await admin.close()

    asyncio.run(run())


async def dedicated_database(h: Harness) -> tuple[SecretStr, SecretStr]:
    """(admin dsn, noema_app dsn) of this process's dedicated ops-ledger database. `create_scratch_database`
    creates and comments it under the fixture's advisory lock (a concurrent reaper never sees it without its
    provenance comment); it is created again if it vanished anyway (ops-ledger requests item 7)."""
    base = dsn(str(h.fixture("noema_services").pg_admin_dsn), dbname="postgres")
    admin = await connect(SecretStr(base), expected_role=None)
    try:
        found = _DEDICATED.get(os.getpid())
        if found is not None:
            name, admin_dsn, app_dsn = found
            if await admin.fetchval("SELECT true FROM pg_catalog.pg_database WHERE datname = $1", name):
                return admin_dsn, app_dsn
        template = await ensure_template(base)
        name = await create_scratch_database(admin, template=template)
    finally:
        await admin.close()
    h.request.config.add_cleanup(partial(_drop, base, name))
    password = DEV_PASSWORDS[ROLE_APP].get_secret_value()
    admin_dsn, app_dsn = (
        SecretStr(dsn(base, dbname=name)),
        SecretStr(dsn(base, dbname=name, user=ROLE_APP, password=password)),
    )
    _DEDICATED[os.getpid()] = (name, admin_dsn, app_dsn)
    return admin_dsn, app_dsn


class CountingDatabase:
    """The real `Database`; `busy` counts the units of work currently open (for `LedgerClock`)."""

    def __init__(self, db: PgDatabase[PgUnitOfWork]) -> None:
        self._db = db
        self.busy = 0

    @contextmanager
    def io(self) -> Iterator[None]:
        """Count a block of real I/O (also used around Redis and S3 calls of a kit)."""
        self.busy += 1
        try:
            yield
        finally:
            self.busy -= 1

    @asynccontextmanager
    async def _count(self, cm: AbstractAsyncContextManager[Any]) -> AsyncIterator[Any]:
        with self.io():
            async with cm as value:
                yield value

    def uow(self, principal: Principal) -> AbstractAsyncContextManager[PgUnitOfWork]:
        return self._count(self._db.uow(principal))

    def tenant_uow(self, tenant_id: str, *, project_id: str | None = None) -> AbstractAsyncContextManager[PgUnitOfWork]:
        return self._count(self._db.tenant_uow(tenant_id, project_id=project_id))

    def system_uow(self) -> AbstractAsyncContextManager[PgSystemCalls]:
        return self._count(self._db.system_uow())


class LedgerClock(FakeClock):
    """See the module docstring. `on_advance(seconds)` ages the database; `busy()` > 0 while I/O is in flight."""

    def __init__(self, on_advance: Callable[[float], Awaitable[None]], busy: Callable[[], int]) -> None:
        super().__init__()
        self._on_advance = on_advance
        self._busy = busy
        self._jumping = False

    async def advance_to(self, target: float) -> None:
        await self.quiesce()
        moved = target - self.monotonic()
        if moved > 0:
            await self._on_advance(moved)
        await super().advance_to(target)

    async def quiesce(self) -> None:
        """Until every other task is blocked and no I/O is in flight."""
        while True:
            await self.settle()
            if self._busy() == 0:
                await self.settle()
                if self._busy() == 0:
                    return
            await asyncio.sleep(0.002)

    @asynccontextmanager
    async def autojump(self) -> AsyncIterator[FakeClock]:
        if self._jumping:
            raise RuntimeError("autojump() is already active")
        self._jumping = True
        try:
            with single_errors():
                async with anyio.create_task_group() as tg:
                    tg.start_soon(self._drive)
                    try:
                        yield self
                    finally:
                        tg.cancel_scope.cancel()
        finally:
            self._jumping = False

    async def _drive(self) -> None:
        while True:
            await self.quiesce()
            due = self.next_deadline()
            if due is None:
                await asyncio.sleep(0.002)
                continue
            await self.advance_to(due)


class DbTimeClock:
    """`now()` = real UTC (the database's timeline); `monotonic()`/`sleep()` = the suite's virtual clock."""

    def __init__(self, virtual: FakeClock) -> None:
        self._virtual = virtual

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return self._virtual.monotonic()

    async def sleep(self, seconds: float) -> None:
        await self._virtual.sleep(seconds)


@asynccontextmanager
async def emptied_database(h: Harness) -> AsyncIterator[tuple[Any, CountingDatabase, LedgerClock]]:
    """(admin connection, counting noema_app Database, aging clock) on the emptied dedicated database."""
    admin_dsn, app_dsn = await dedicated_database(h)
    admin = await connect(admin_dsn, expected_role=None)
    engine = await Engine.create(PoolSettings(dsn=app_dsn, max_size=4))
    try:
        await truncate_all(admin)
        db = CountingDatabase(PgDatabase(engine, make_unit_of_work))
        yield admin, db, LedgerClock(partial(age, admin), lambda: db.busy)
    finally:
        await engine.close()
        await admin.close()
