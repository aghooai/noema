"""Repository tests (spec-02 §7.1, §14 test_repos / test_session_repo) against the compose Postgres, as noema_app."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from tests.fixtures.db import PgTestDatabase, SeededTenant, seed_tenant
from tests.integration.db_repos._rows import Conn

from noema.db.engine import Engine, PoolSettings
from noema.db.repos import PgUnitOfWork, make_unit_of_work
from noema.db.uow import PgDatabase


@pytest.fixture
async def repo_db(pg_app_engine: Engine) -> PgDatabase[PgUnitOfWork]:
    """The production `Database`: `PgDatabase(engine, make_unit_of_work)` over a noema_app pool."""
    return PgDatabase(pg_app_engine, make_unit_of_work)


@pytest.fixture
async def other_db(pg_database: PgTestDatabase) -> AsyncIterator[PgDatabase[PgUnitOfWork]]:
    """A second, independent noema_app pool (a second 'process' for the race tests)."""
    engine = await Engine.create(PoolSettings(dsn=pg_database.app_dsn, max_size=4))
    try:
        yield PgDatabase(engine, make_unit_of_work)
    finally:
        await engine.close()


@pytest.fixture
async def tenant(pg_admin: Conn) -> SeededTenant:
    """A fresh tenant with projects prj_a and prj_b and one developer principal per project."""
    return await seed_tenant(pg_admin, projects=("prj_a", "prj_b"))
