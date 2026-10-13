"""The real `Database` (`PgDatabase(engine, make_unit_of_work)`, noema_app, RLS) for the storage conformance suites
(spec-02 §14: test_repos.py, test_session_repo.py, test_uow_rls.py).

The suites use fixed ids (t_acme, ses_1, ...), so the kit runs on a dedicated database per process (cloned from
the migrated template, dropped at session end) that is emptied before every kit. Hooks: `seed` inserts past RLS,
`available` switches to a closed port, and `clock` is an `AgingClock` (Postgres keeps its own clock; see
_conformance.py).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial

import pytest
from tests.contract_internal.kits import DatabaseKit
from tests.contract_internal.registry import Harness, register
from tests.fixtures.db import DEV_PASSWORDS, create_scratch_database, ensure_template
from tests.integration.db_repos._conformance import AgingClock, SwitchableDatabase, age, dsn, seed, truncate_all

from noema.core.secret import SecretStr
from noema.db.engine import ROLE_APP, Engine, PoolSettings, connect, drop_database
from noema.db.repos import make_unit_of_work
from noema.db.uow import PgDatabase

_UNREACHABLE = SecretStr("postgresql://noema_app:unused@127.0.0.1:1/noema_down")  # nothing listens on port 1


@dataclass(frozen=True)
class _Dedicated:
    admin_dsn: SecretStr
    app_dsn: SecretStr


_DEDICATED: dict[int, _Dedicated] = {}  # one database per process (xdist worker)


def _drop(base: str, name: str) -> None:
    async def run() -> None:
        admin = await connect(SecretStr(base), expected_role=None)
        try:
            await drop_database(admin, name)
        finally:
            await admin.close()

    asyncio.run(run())


async def _dedicated(h: Harness) -> _Dedicated:
    found = _DEDICATED.get(os.getpid())
    if found is not None:
        return found
    base = dsn(str(h.fixture("noema_services").pg_admin_dsn), dbname="postgres")
    template = await ensure_template(base)  # the pg_template fixture is sync (asyncio.run): unusable from here
    admin = await connect(SecretStr(base), expected_role=None)
    try:
        name = await create_scratch_database(admin, template=template)
    finally:
        await admin.close()
    h.request.config.add_cleanup(partial(_drop, base, name))
    password = DEV_PASSWORDS[ROLE_APP].get_secret_value()
    found = _Dedicated(
        SecretStr(dsn(base, dbname=name)), SecretStr(dsn(base, dbname=name, user=ROLE_APP, password=password))
    )
    _DEDICATED[os.getpid()] = found
    return found


@register("database", "postgres", marks=(pytest.mark.integration,))
@asynccontextmanager
async def postgres_database(h: Harness) -> AsyncIterator[DatabaseKit]:
    target = await _dedicated(h)
    admin = await connect(target.admin_dsn, expected_role=None)
    engine = await Engine.create(PoolSettings(dsn=target.app_dsn, max_size=4))
    dead = await Engine.create(PoolSettings(dsn=_UNREACHABLE, max_size=1))
    try:
        await truncate_all(admin)
        db = SwitchableDatabase(PgDatabase(engine, make_unit_of_work), PgDatabase(dead, make_unit_of_work))
        yield DatabaseKit(db, AgingClock(partial(age, admin)), partial(seed, admin), db.set_available)
    finally:
        await dead.close()
        await engine.close()
        await admin.close()
