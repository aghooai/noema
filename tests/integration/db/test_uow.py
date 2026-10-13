"""Units of work and the engine (spec-02 §7, spec-03 §3): scope per principal, transaction-local settings, commit and
rollback, system calls without tenant scope, role checks, JSON codecs and Postgres-unreachable mapping."""

from __future__ import annotations

import socket
import uuid
from decimal import Decimal

import anyio
import asyncpg
import pytest
from tests.fixtures.db import PgTestDatabase, seed_tenant

from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import ALL_SCOPES, Principal, Role, system_principal
from noema.core.secret import SecretStr
from noema.db.engine import ROLE_APP, ROLE_OWNER, Engine, PoolSettings, WrongDatabaseRole, connect
from noema.db.uow import PgDatabase, Scope, Tx, scope_for

pytestmark = pytest.mark.integration

type Conn = asyncpg.Connection[asyncpg.Record]
_SETTINGS = "SELECT current_setting('app.tenant_id', true) AS t, current_setting('app.project_id', true) AS p"


def _principal(tenant: str, project: str, *roles: Role) -> Principal:
    return Principal(tenant_id=tenant, project_id=project, principal_id="prn_x", key_id="K", roles=frozenset(roles),
                     scopes=ALL_SCOPES, request_id="req_test")  # fmt: skip


@pytest.mark.parametrize(
    ("roles", "expected_project"),
    [(("developer",), "prj_a"), (("end_user",), "prj_a"), (("reviewer",), "prj_a"), (("tenant_admin",), ""),
     (("auditor",), ""), (("developer", "auditor"), "")],
)  # fmt: skip
async def test_uow_scope_follows_the_principal(
    pg_db: PgDatabase[Tx], roles: tuple[Role, ...], expected_project: str
) -> None:
    async with pg_db.uow(_principal("t_scope", "prj_a", *roles)) as tx:
        row = await tx.conn.fetchrow(_SETTINGS)
        assert row is not None
        assert (row["t"], row["p"]) == ("t_scope", expected_project)
        assert (tx.tenant_id, tx.project_id) == ("t_scope", expected_project)


async def test_tenant_uow_and_system_principal_are_tenant_wide_unless_a_project_is_given(
    pg_db: PgDatabase[Tx],
) -> None:
    async with pg_db.tenant_uow("t_scope") as tx:
        assert tuple(await tx.conn.fetchrow(_SETTINGS) or ()) == ("t_scope", "")
    async with pg_db.tenant_uow("t_scope", project_id="prj_b") as tx:
        assert tuple(await tx.conn.fetchrow(_SETTINGS) or ()) == ("t_scope", "prj_b")
    async with pg_db.uow(system_principal("t_scope", purpose="sweep", request_id="req_1")) as tx:
        assert tuple(await tx.conn.fetchrow(_SETTINGS) or ()) == ("t_scope", "")


def test_scope_needs_a_tenant_and_a_project_for_project_roles() -> None:
    with pytest.raises(ValueError, match="project_id"):
        scope_for(_principal("t_a", "", "developer"))
    with pytest.raises(ValueError, match="tenant_id"):
        Scope("", "")


async def test_settings_are_transaction_local(pg_app_engine: Engine) -> None:
    db = PgDatabase(pg_app_engine, lambda tx: tx)
    async with db.tenant_uow("t_local", project_id="prj_a"):
        pass
    async with pg_app_engine.acquire() as a, pg_app_engine.acquire() as b:
        for conn in (a, b):  # whichever pooled connection ran the unit of work
            row = await conn.fetchrow(_SETTINGS)
            assert row is not None
            assert row["t"] in (None, "") and row["p"] in (None, "")


async def test_clean_exit_commits_and_an_exception_rolls_back(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    seeded = await seed_tenant(pg_admin)
    principal = _principal(seeded.tenant_id, "prj_a", "developer")
    insert = (
        "INSERT INTO noema.session (tenant_id, project_id, id, owner_principal_id, data_class)"
        " VALUES ($1, $2, $3, 'prn_x', 'internal')"
    )
    kept, dropped = f"ses_{uuid.uuid4().hex[:12]}", f"ses_{uuid.uuid4().hex[:12]}"
    async with pg_db.uow(principal) as tx:
        await tx.conn.execute(insert, tx.tenant_id, tx.project_id, kept)

    async def insert_then_fail() -> None:
        async with pg_db.uow(principal) as tx:
            await tx.conn.execute(insert, tx.tenant_id, tx.project_id, dropped)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await insert_then_fail()
    async with pg_db.uow(principal) as tx:
        ids = {r["id"] for r in await tx.conn.fetch("SELECT id FROM noema.session")}
    assert kept in ids and dropped not in ids


async def test_system_uow_reads_platform_rows_and_sees_no_tenant_rows(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    version = int(await pg_admin.fetchval("SELECT coalesce(max(version), 0) + 1 FROM platform.price_catalog"))
    await pg_admin.execute(
        "INSERT INTO platform.price_catalog (version, digest, body, imported_by) VALUES ($1, 'sha256:x', $2, 'op')",
        version,
        {"version": 1, "prices": []},
    )
    async with pg_db.system_uow() as calls:
        row = await calls.price_catalog(version)
        assert row is not None and row["body"] == {"version": 1, "prices": []}
        assert await calls.price_catalog(10**9) is None
        active = await calls.active_config()
        assert active is None or active["status"] == "active"


async def test_jsonb_values_are_python_json_both_ways(pg_db: PgDatabase[Tx]) -> None:
    async with pg_db.tenant_uow("t_json") as tx:
        value = await tx.conn.fetchval("SELECT $1::jsonb", {"a": [1, Decimal("1.50"), None], "b": "é"})
    assert value == {"a": [1, "1.50", None], "b": "é"}


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_unreachable_postgres_is_a_retryable_internal_error() -> None:
    dsn = SecretStr(f"postgresql://noema_app:x@127.0.0.1:{_closed_port()}/noema")
    engine = await Engine.create(PoolSettings(dsn=dsn))
    db = PgDatabase(engine, lambda tx: tx)
    try:
        for entry in (db.tenant_uow("t_a"), db.uow(_principal("t_a", "p", "developer")), db.system_uow()):
            with pytest.raises(NoemaError) as error:
                async with entry:
                    pass
            assert error.value.code is ErrorCode.INTERNAL_ERROR and error.value.spec.retryable
        with pytest.raises(NoemaError):
            await connect(dsn, expected_role=ROLE_APP)
    finally:
        await engine.close()


async def test_an_exhausted_pool_times_out_as_internal_error(pg_database: PgTestDatabase) -> None:
    engine = await Engine.create(PoolSettings(dsn=pg_database.app_dsn, max_size=1, acquire_timeout_s=0.2))
    try:
        async with engine.acquire():
            with pytest.raises(NoemaError) as error:
                async with engine.acquire():
                    pass
        assert error.value.code is ErrorCode.INTERNAL_ERROR
    finally:
        await engine.close()


async def test_the_engine_refuses_any_other_role(pg_database: PgTestDatabase) -> None:
    for dsn in (pg_database.admin_dsn, pg_database.owner_dsn):  # a superuser; the right attributes but wrong role
        engine = await Engine.create(PoolSettings(dsn=dsn, expected_role=ROLE_APP))
        try:
            with pytest.raises(WrongDatabaseRole):
                async with engine.acquire():
                    pass
        finally:
            await engine.close()
    with pytest.raises(WrongDatabaseRole):
        await connect(pg_database.admin_dsn, expected_role=ROLE_OWNER)
    conn = await connect(pg_database.owner_dsn, expected_role=ROLE_OWNER)
    await conn.close()


async def test_a_lost_connection_inside_a_unit_of_work_is_an_internal_error(
    pg_admin: Conn, pg_db: PgDatabase[Tx]
) -> None:
    async def killed_mid_transaction() -> None:
        async with pg_db.tenant_uow("t_lost") as tx:
            pid = await tx.conn.fetchval("SELECT pg_backend_pid()")
            await pg_admin.execute("SELECT pg_terminate_backend($1)", pid)
            await tx.conn.fetchval("SELECT 1")

    with pytest.raises(NoemaError) as error:
        await killed_mid_transaction()
    assert error.value.code is ErrorCode.INTERNAL_ERROR
    async with pg_db.tenant_uow("t_lost") as tx:  # the pool recovers with a fresh connection
        assert await tx.conn.fetchval("SELECT 1") == 1


async def test_caller_errors_inside_a_unit_of_work_propagate_unchanged(pg_db: PgDatabase[Tx]) -> None:
    async def caller_timeout() -> None:
        async with pg_db.tenant_uow("t_timeout"):
            raise TimeoutError("the caller's own deadline")

    with pytest.raises(TimeoutError, match="own deadline"):
        await caller_timeout()


async def test_a_hung_postgres_fails_fast_as_internal_error(pg_database: PgTestDatabase) -> None:
    """Wave E edge fix 16: a statement on a connection that stopped answering raises the retryable 500 at the app
    pool's command timeout (here lowered to 0.5 s), and a slow statement at the server's statement_timeout."""
    import dataclasses
    from urllib.parse import urlsplit, urlunsplit

    from noema.db.engine import app_pool_settings

    from .blackhole import blackhole_proxy

    parts = urlsplit(pg_database.app_dsn.get_secret_value())
    async with blackhole_proxy(int(parts.port or 5432)) as proxy:
        netloc = f"{parts.netloc.rsplit('@', 1)[0]}@127.0.0.1:{proxy.port}"
        proxied = SecretStr(urlunsplit(parts._replace(netloc=netloc)))
        settings = dataclasses.replace(app_pool_settings(proxied, max_size=1), command_timeout_s=0.5)
        engine = await Engine.create(settings)
        try:
            async with engine.acquire() as conn:
                assert await conn.fetchval("SELECT 1") == 1
            proxy.freeze()
            with anyio.fail_after(5), pytest.raises(NoemaError) as error:  # before: ~15 min of TCP retries
                async with engine.acquire() as conn:
                    await conn.fetchval("SELECT 1")
            assert error.value.code is ErrorCode.INTERNAL_ERROR and error.value.spec.retryable
        finally:
            with anyio.move_on_after(3):
                await engine.close()
    direct = dataclasses.replace(
        app_pool_settings(pg_database.app_dsn, max_size=1), server_settings={"statement_timeout": "200ms"}
    )
    engine = await Engine.create(direct)
    try:
        with anyio.fail_after(5), pytest.raises(NoemaError):
            async with engine.acquire() as conn:
                await conn.fetchval("SELECT pg_sleep(3)")
    finally:
        await engine.close()
