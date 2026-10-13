"""The test-database fixtures' reaper safety (spec-10 §1.1; ops-ledger request 7): scratch databases are created and
commented under the fixture lock, and a clone's pid counts as dead only in the PID namespace that made it."""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime, timedelta

import anyio
import asyncpg
import pytest
from tests.fixtures.db import _LOCK_KEY, PgTestDatabase, _pid_namespace, _with, create_scratch_database, stale_clone

from noema.core.secret import SecretStr
from noema.db.engine import connect, drop_database

pytestmark = pytest.mark.integration

type Conn = asyncpg.Connection[asyncpg.Record]
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
GONE = 2**31 - 2  # above any pid_max: os.kill reports no such process


def _meta(**changes: object) -> dict[str, object]:
    meta: dict[str, object] = {"created_at": NOW.isoformat(), "pid": GONE, "host": "h1", "pidns": "pid:[1]"}
    return {**meta, **changes}


@pytest.mark.parametrize(
    ("meta", "stale"),
    [
        (_meta(), True),  # this host and PID namespace, the process is gone
        (_meta(pidns="pid:[2]"), False),  # same hostname, another PID namespace (a sandboxed agent): unknown pid
        (_meta(pidns=None), False),  # a comment without a namespace (older fixtures): only the 6 h rule
        (_meta(host="h2"), False),
        (_meta(pid=os.getpid()), False),  # alive
        (_meta(pidns="pid:[2]", created_at=(NOW - timedelta(hours=7)).isoformat()), True),  # older than 6 h
        ({}, True),  # no provenance at all
    ],
)
def test_a_pid_is_dead_only_in_the_namespace_that_recorded_it(meta: dict[str, object], stale: bool) -> None:
    assert stale_clone(meta, now=NOW, host="h1", pidns="pid:[1]") is stale


async def test_scratch_databases_are_created_under_the_fixture_lock(pg_database: PgTestDatabase) -> None:
    base = SecretStr(_with(pg_database.admin_dsn.get_secret_value(), dbname="postgres"))
    holder, creator = await connect(base, expected_role=None), await connect(base, expected_role=None)
    names: list[str] = []
    try:
        await holder.execute("SELECT pg_catalog.pg_advisory_lock($1)", _LOCK_KEY)
        async with anyio.create_task_group() as tg:

            async def create() -> None:
                names.append(await create_scratch_database(creator))

            tg.start_soon(create)
            waiting = (
                "SELECT count(*) FROM pg_catalog.pg_locks WHERE locktype = 'advisory' AND NOT granted AND pid = $1"
            )
            with anyio.fail_after(10):  # polls the server's lock table (no event to wait for)
                while True:
                    if await holder.fetchval(waiting, creator.get_server_pid()):
                        break
                    await anyio.sleep(0.01)
            assert names == []  # waits for the lock before CREATE DATABASE
            await holder.execute("SELECT pg_catalog.pg_advisory_unlock($1)", _LOCK_KEY)
        (name,) = names
        comment = await holder.fetchval(
            "SELECT pg_catalog.shobj_description(oid, 'pg_database') FROM pg_catalog.pg_database WHERE datname = $1",
            name,
        )
        meta = json.loads(comment)
        assert (meta["pid"], meta["host"], meta["pidns"]) == (os.getpid(), socket.gethostname(), _pid_namespace())
        assert not await creator.fetchval("SELECT count(*) FROM pg_catalog.pg_locks WHERE locktype = 'advisory'"
                                          " AND pid = pg_backend_pid()")  # fmt: skip
    finally:
        for name in names:
            await drop_database(holder, name)
        await creator.close()
        await holder.close()
