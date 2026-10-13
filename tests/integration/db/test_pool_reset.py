"""Wave E3: the `noema_app` pool (`app_pool_settings`) releases connections without asyncpg's reset query, and a
released connection still carries no RLS scope: `app.tenant_id` / `app.project_id` are '' (or unset) on the very
same backend after a committed unit of work, a rolled-back one, a cancelled one, a nested savepoint and a system
call (`set_config(..., true)` is transaction-local). Other pools keep the full reset. A source scan guards the
invariant the skip relies on: nothing in the app sets session-level state on a pooled connection."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from tests.fixtures.db import PgTestDatabase

from noema.db.engine import Engine, PoolSettings, app_pool_settings
from noema.db.uow import PgDatabase, Tx

pytestmark = pytest.mark.integration

_STATE = (
    "SELECT current_setting('app.tenant_id', true) AS t, current_setting('app.project_id', true) AS p,"
    " pg_backend_pid() AS pid"
)
RESET_QUERY = "RESET ALL"


@pytest.fixture
async def one_connection(pg_database: PgTestDatabase) -> AsyncIterator[Engine]:
    """The production app pool settings on one connection, so every acquire gets the same backend."""
    engine = await Engine.create(app_pool_settings(pg_database.app_dsn, max_size=1))
    try:
        yield engine
    finally:
        await engine.close()


async def _state(engine: Engine) -> tuple[Any, Any, int]:
    async with engine.acquire() as conn:
        row = await conn.fetchrow(_STATE)
    assert row is not None
    return row["t"], row["p"], int(row["pid"])


async def _logged(engine: Engine) -> list[str]:
    """Register a query logger on the pooled connection; returns the list it appends every query to."""
    queries: list[str] = []
    async with engine.acquire() as conn:
        conn.add_query_logger(lambda record: queries.append(record.query))
    return queries


@pytest.mark.parametrize("reset", [False, True], ids=["app-pool", "full-reset"])
async def test_only_the_app_pool_skips_the_reset_query(pg_database: PgTestDatabase, reset: bool) -> None:
    settings = app_pool_settings(pg_database.app_dsn, max_size=1)
    if reset:
        settings = PoolSettings(dsn=pg_database.app_dsn, max_size=1)
    engine = await Engine.create(settings)
    try:
        queries = await _logged(engine)
        db = PgDatabase(engine, lambda tx: tx)
        async with db.tenant_uow("t_reset", project_id="prj_a") as tx:
            await tx.conn.fetchval("SELECT 1")
        _, _, pid = await _state(engine)
        assert any(RESET_QUERY in q for q in queries) is reset
        assert (await _state(engine))[2] == pid  # the same backend: released, not reconnected
    finally:
        await engine.close()


async def _commit(db: PgDatabase[Tx]) -> None:
    async with db.tenant_uow("t_commit", project_id="prj_a") as tx:
        await tx.conn.fetchval("SELECT current_setting('app.tenant_id')")


async def _rollback(db: PgDatabase[Tx]) -> None:
    with pytest.raises(RuntimeError):
        async with db.tenant_uow("t_rollback", project_id="prj_b"):
            raise RuntimeError("caller error")


async def _cancelled(db: PgDatabase[Tx]) -> None:
    with anyio.move_on_after(0.2):
        async with db.tenant_uow("t_cancel", project_id="prj_c") as tx:
            await tx.conn.execute("SELECT pg_sleep(5)")


async def _savepoint(db: PgDatabase[Tx]) -> None:
    async with db.tenant_uow("t_savepoint") as tx, tx.conn.transaction():
        await tx.conn.execute("SELECT pg_catalog.set_config('app.project_id', 'prj_inner', true)")


async def _system(db: PgDatabase[Tx]) -> None:
    async with db.system_uow() as calls:
        await calls.active_config()


@pytest.mark.parametrize("work", [_commit, _rollback, _cancelled, _savepoint, _system])
async def test_a_released_connection_carries_no_tenant_scope(
    one_connection: Engine, work: Callable[[PgDatabase[Tx]], Awaitable[None]]
) -> None:
    _, _, before = await _state(one_connection)
    await work(PgDatabase(one_connection, lambda tx: tx))
    tenant, project, pid = await _state(one_connection)
    assert tenant in (None, "") and project in (None, ""), (tenant, project)
    if work is not _cancelled:  # a cancelled statement may cost asyncpg the connection; otherwise it is reused
        assert pid == before


SRC = Path(__file__).parents[3] / "src" / "noema"
# Session-level state a reset would undo: a session set_config, a SQL SET without LOCAL, LISTEN (asyncpg's
# add_listener), a session advisory lock, a holdable cursor. Allowed on unpooled connections only: the CLI's
# operator proof (db/system.py, `connect()`) and the LISTEN listener (ops/notify_bridge/listener.py, `connect()`).
SESSION_STATE = re.compile(
    r"set_config\([^)]*,\s*false\)|execute\(\s*f?[\"']\s*SET\s+(?!LOCAL\b)|\.add_listener\("
    r"|pg_(try_)?advisory_lock\(|WITH HOLD"
)
UNPOOLED = ("db/system.py", "ops/notify_bridge/listener.py")


def test_nothing_sets_session_state_on_a_pooled_connection() -> None:
    found = {
        f"{path.relative_to(SRC).as_posix()}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        if not path.relative_to(SRC).as_posix().startswith("db/alembic/")
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if SESSION_STATE.search(line)
    }
    assert {f.split(":")[0] for f in found} == set(UNPOOLED), sorted(found)
