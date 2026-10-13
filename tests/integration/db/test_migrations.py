"""Migrations (spec-03 §6-§7): linear history, apply from empty, downgrade base (CI only), role guard, naming."""

from __future__ import annotations

import itertools
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

import anyio
import asyncpg
import pytest
from alembic.script import ScriptDirectory
from tests.fixtures.db import DEV_PASSWORDS, PgTestDatabase, _with, clone_name, create_scratch_database, template_name

from noema.core.errors import ForbiddenInEnvironment
from noema.core.secret import SecretStr
from noema.db.alembic.runner import alembic_config, downgrade, migration_hash, upgrade
from noema.db.alembic.runner import main as runner_main
from noema.db.engine import ROLE_OWNER, connect, drop_database

pytestmark = pytest.mark.integration

HEAD = "0003_operator_proof"
NOEMA_SCHEMAS = {"noema", "auth", "jobs", "ops", "admin", "platform"}
# IMPL-010: the Phase 1 schema is exactly spec-03
SPEC_03_TABLES = {
    "platform.config_version", "platform.price_catalog", "platform.operator",
    "noema.tenant", "noema.project", "noema.principal", "auth.api_key",
    "noema.session", "noema.turn", "noema.turn_file", "noema.attempt", "noema.decision",
    "noema.file", "noema.derivative",
    "noema.usage_line", "noema.idempotency_record", "noema.audit_event", "jobs.job", "ops.outbox_dead_letter",
    "noema.labeling_run", "noema.label_item", "noema.calibrator", "noema.calibrator_epoch",
}  # fmt: skip


async def _schemas(conn: asyncpg.Connection[asyncpg.Record]) -> set[str]:
    rows = await conn.fetch("SELECT nspname FROM pg_namespace WHERE nspname = ANY($1)", list(NOEMA_SCHEMAS))
    return {r["nspname"] for r in rows}


def test_history_is_one_linear_chain_of_numbered_revisions() -> None:
    script = ScriptDirectory.from_config(alembic_config(SecretStr("postgresql://unused@127.0.0.1/unused")))
    assert script.get_heads() == [HEAD]
    revisions = list(script.walk_revisions())
    assert all(re.fullmatch(r"\d{4}_[a-z0-9_]+", r.revision) for r in revisions)
    assert revisions[-1].down_revision is None
    for newer, older in itertools.pairwise(revisions):
        assert newer.down_revision == older.revision


def test_migration_hash_is_stable_and_names_the_template() -> None:
    first = migration_hash()
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert migration_hash() == first
    assert template_name() == f"noema_tpl_{first[:16]}"


async def test_session_database_is_a_clone_at_head(pg_admin: asyncpg.Connection[asyncpg.Record]) -> None:
    assert await pg_admin.fetchval("SELECT version_num FROM public.alembic_version") == HEAD
    assert await _schemas(pg_admin) == NOEMA_SCHEMAS


@asynccontextmanager
async def _scratch(pg_database: PgTestDatabase) -> AsyncIterator[tuple[str, str]]:
    """An empty database (reaper-safe), its name and the superuser DSN of the cluster; dropped afterwards."""
    base_admin = _with(pg_database.admin_dsn.get_secret_value(), dbname="postgres")
    admin = await connect(SecretStr(base_admin), expected_role=None)
    try:
        name = await create_scratch_database(admin)
        try:
            yield name, base_admin
        finally:
            await drop_database(admin, name)
    finally:
        await admin.close()


async def test_apply_from_empty_then_downgrade_base_then_reapply(pg_database: PgTestDatabase) -> None:
    async with _scratch(pg_database) as (name, base_admin):
        password = DEV_PASSWORDS[ROLE_OWNER].get_secret_value()
        owner_dsn = SecretStr(_with(base_admin, user=ROLE_OWNER, password=password, dbname=name))
        await anyio.to_thread.run_sync(upgrade, owner_dsn)
        db_admin = await connect(SecretStr(_with(base_admin, dbname=name)), expected_role=None)
        try:
            assert await _schemas(db_admin) == NOEMA_SCHEMAS
            rows = await db_admin.fetch(
                "SELECT n.nspname || '.' || c.relname AS t FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND n.nspname = ANY($1)",
                list(NOEMA_SCHEMAS),
            )
            assert {r["t"] for r in rows} == SPEC_03_TABLES
            await anyio.to_thread.run_sync(downgrade, owner_dsn)
            assert await _schemas(db_admin) == set()
            assert await db_admin.fetchval("SELECT count(*) FROM public.alembic_version") == 0
            await anyio.to_thread.run_sync(upgrade, owner_dsn)
            assert await _schemas(db_admin) == NOEMA_SCHEMAS
        finally:
            await db_admin.close()


async def test_downgrade_one_revision_restores_0001_then_reapplies(pg_database: PgTestDatabase) -> None:
    """0002's down scripts undo exactly 0002: back at 0001, the 0001 jobs.finish (boolean) and index are back."""
    async with _scratch(pg_database) as (name, base_admin):
        password = DEV_PASSWORDS[ROLE_OWNER].get_secret_value()
        owner_dsn = SecretStr(_with(base_admin, user=ROLE_OWNER, password=password, dbname=name))
        await anyio.to_thread.run_sync(upgrade, owner_dsn)
        db_admin = await connect(SecretStr(_with(base_admin, dbname=name)), expected_role=None)
        finish_type = (
            "SELECT p.prorettype::regtype::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'jobs' AND p.proname = 'finish'"
        )
        open_index = "SELECT pg_get_indexdef('noema.labeling_run_open'::regclass)"
        try:
            assert await db_admin.fetchval(finish_type) == "text"
            assert "question_signature" in await db_admin.fetchval(open_index)
            await anyio.to_thread.run_sync(partial(downgrade, owner_dsn, "0001_initial"))
            assert await db_admin.fetchval("SELECT version_num FROM public.alembic_version") == "0001_initial"
            assert await db_admin.fetchval(finish_type) == "boolean"
            assert "question_signature" not in await db_admin.fetchval(open_index)
            assert not await db_admin.fetchval(
                "SELECT count(*) FROM information_schema.columns WHERE table_name = 'derivative'"
                " AND column_name = 'facts'"
            )
            await anyio.to_thread.run_sync(upgrade, owner_dsn)
            assert await db_admin.fetchval("SELECT version_num FROM public.alembic_version") == HEAD
        finally:
            await db_admin.close()


async def test_migrations_refuse_any_role_but_noema_owner(pg_database: PgTestDatabase) -> None:
    async with _scratch(pg_database) as (name, base_admin):
        with pytest.raises(asyncpg.RaiseError, match="run as noema_owner"):
            await anyio.to_thread.run_sync(upgrade, SecretStr(_with(base_admin, dbname=name)))


def test_downgrade_is_refused_outside_dev_and_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOEMA_ENV", "prod")
    with pytest.raises(ForbiddenInEnvironment):
        downgrade(SecretStr("postgresql://unused@127.0.0.1:1/unused"))
    assert os.environ["NOEMA_ENV"] == "prod"


def test_clone_names_are_safe_unique_and_bounded() -> None:
    long = clone_name(agent="Agent-With.Weird/Chars" * 5, worker="gw3", pid=123456, token="ab12cd")
    assert len(long) <= 63 and long.startswith("noema_t_agent_with_weird_chars") and long.endswith("_gw3_123456_ab12cd")
    assert clone_name(agent="a", worker="w0", pid=1, token="x") != clone_name(agent="a", worker="w0", pid=1, token="y")


def test_runner_command_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert runner_main(["hash"]) == 0
    assert capsys.readouterr().out == migration_hash() + "\n"
    assert runner_main(["nonsense"]) == 2
    assert "usage" in capsys.readouterr().err
