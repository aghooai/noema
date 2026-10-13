"""SECURITY DEFINER functions (spec-03 §4, §6; IMPL-009): ownership, search_path, EXECUTE only for the §4 callers,
hijack attempts through temporary tables and user-writable schemas, and the key lookup filters."""

from __future__ import annotations

import uuid

import asyncpg
import pytest
from tests.fixtures.db import SeededTenant, seed_tenant

from noema.db.engine import Engine
from noema.db.uow import PgDatabase, Tx

pytestmark = pytest.mark.integration

type Conn = asyncpg.Connection[asyncpg.Record]

_APP = {
    "auth.lookup_api_key", "auth.touch_api_key", "auth.rehash_api_key", "jobs.claim", "jobs.extend", "jobs.finish",
    "jobs.ensure_periodic", "ops.outbox_apply", "ops.outbox_dead", "ops.reap_stuck", "ops.retention_candidates",
    "ops.retention_page", "ops.calibrator_epochs", "ops.active_calibrators", "admin.set_calibrator_status",
    "admin.bump_epoch",
}  # fmt: skip
_CLI = {
    "auth.lookup_operator", "admin.create_operator", "admin.revoke_operator", "admin.config_propose",
    "admin.config_approve", "admin.config_rollback", "admin.sync_tenants", "admin.list_config_versions",
    "admin.import_prices", "admin.create_project", "admin.create_principal", "admin.create_api_key",
    "admin.revoke_api_key", "admin.list_projects", "admin.list_api_keys", "admin.record_audit",
    "admin.calibrator_approve", "admin.calibrator_activate", "admin.calibrator_retire",
    "admin.upsert_platform_calibrator", "admin.set_calibrator_status", "admin.bump_epoch", "admin.list_calibrators",
    "admin.config_body", "admin.rehash_operator",
}  # fmt: skip
_OWNER = {"admin.bootstrap_operator"}
SECURITY_DEFINER = _APP | _CLI | _OWNER


def expected_callers(fn: str) -> set[str]:
    return {r for r, fns in (("noema_app", _APP), ("noema_cli", _CLI), ("noema_owner", _OWNER)) if fn in fns}


async def _functions(conn: Conn) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT p.oid, n.nspname || '.' || p.proname AS fn, p.proowner::regrole::text AS owner, p.prosecdef,"
        " p.proconfig, coalesce(p.proacl::text[], '{}') AS acl FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname IN ('auth', 'jobs', 'ops', 'admin')"
    )


async def test_every_function_is_owned_by_the_definer_with_a_safe_search_path(pg_admin: Conn) -> None:
    functions = await _functions(pg_admin)
    assert {f["fn"] for f in functions if f["prosecdef"]} == SECURITY_DEFINER
    for f in functions:
        assert f["owner"] == "noema_definer", f["fn"]
        assert f["proconfig"] == ["search_path=pg_catalog, pg_temp"], f["fn"]


async def test_execute_is_granted_to_the_spec_callers_only(pg_admin: Conn) -> None:
    for f in await _functions(pg_admin):
        grantees = {entry.split("=", 1)[0] for entry in f["acl"]}
        assert "" not in grantees, f"{f['fn']} is executable by PUBLIC"
        assert grantees - {"noema_definer"} == expected_callers(f["fn"]), f["fn"]
        for role in ("noema_app", "noema_cli", "noema_owner"):
            allowed = await pg_admin.fetchval("SELECT has_function_privilege($1, $2::oid, 'EXECUTE')", role, f["oid"])
            assert allowed == (role in expected_callers(f["fn"])), (role, f["fn"])


async def _seed_key(admin: Conn, *, tenant: SeededTenant | None = None) -> tuple[SeededTenant, str]:
    seeded = tenant or await seed_tenant(admin)
    key_id = uuid.uuid4().hex[:26].upper()
    project = seeded.project_ids[0]
    await admin.execute(
        "INSERT INTO auth.api_key (key_id, tenant_id, project_id, principal_id, key_hash, pepper_version, env, scopes)"
        " VALUES ($1, $2, $3, $4, 'hmac-sha256:v1:' || repeat('0', 64), 1, 'live', '{turns}')",
        key_id,
        seeded.tenant_id,
        project,
        seeded.principal_ids[project],
    )
    return seeded, key_id


async def test_lookup_api_key_filters_disabled_principals_and_deleted_projects_and_tenants(
    pg_admin: Conn, pg_db: PgDatabase[Tx]
) -> None:
    seeded, key_id = await _seed_key(pg_admin)
    project = seeded.project_ids[0]
    async with pg_db.system_uow() as calls:
        row = await calls.lookup_api_key(key_id)
        assert row is not None
        assert (row["tenant_id"], row["project_id"], row["roles"], row["scopes"]) == (
            seeded.tenant_id, project, ["developer"], ["turns"]
        )  # fmt: skip
        assert await calls.lookup_api_key("0" * 26) is None
        for disable, enable in (
            ("UPDATE noema.principal SET disabled_at = now() WHERE tenant_id = $1", "disabled_at = NULL"),
            ("UPDATE noema.project SET deleted_at = now() WHERE tenant_id = $1", "deleted_at = NULL"),
            ("UPDATE noema.tenant SET deleted_at = now() WHERE id = $1", "deleted_at = NULL"),
        ):
            await pg_admin.execute(disable, seeded.tenant_id)
            assert await calls.lookup_api_key(key_id) is None
            await pg_admin.execute(disable.replace("disabled_at = now()", enable).replace("deleted_at = now()", enable),
                                   seeded.tenant_id)  # fmt: skip
            assert await calls.lookup_api_key(key_id) is not None


async def test_touch_and_rehash_api_key(pg_admin: Conn, pg_db: PgDatabase[Tx]) -> None:
    _, key_id = await _seed_key(pg_admin)
    async with pg_db.system_uow() as calls:
        await calls.touch_api_key(key_id)
        first = await pg_admin.fetchval("SELECT last_used_at FROM auth.api_key WHERE key_id = $1", key_id)
        assert first is not None
        await calls.touch_api_key(key_id)  # within a minute: unchanged
        assert await pg_admin.fetchval("SELECT last_used_at FROM auth.api_key WHERE key_id = $1", key_id) == first
        newer = "hmac-sha256:v2:" + "a" * 64
        assert await calls.rehash_api_key(key_id, newer, 2) is True
        assert await calls.rehash_api_key(key_id, "hmac-sha256:v1:" + "b" * 64, 1) is False  # never a downgrade
        assert await calls.rehash_api_key(key_id, "plaintext", 3) is False  # only a well-formed hash
    row = await pg_admin.fetchrow("SELECT key_hash, pepper_version FROM auth.api_key WHERE key_id = $1", key_id)
    assert row is not None
    assert (row["key_hash"], row["pepper_version"]) == (newer, 2)


async def test_noema_app_cannot_create_temporary_tables(pg_app_engine: Engine) -> None:
    async with pg_app_engine.acquire() as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
            await conn.execute("CREATE TEMP TABLE api_key (key_id text)")


async def test_temporary_tables_cannot_shadow_referenced_relations(pg_admin: Conn) -> None:
    """Defense in depth: even a session that has TEMP (here the superuser, then SET ROLE noema_app) cannot make a
    definer function read its temporary api_key/principal/project/tenant tables."""
    forged = "F" * 26
    for ddl in (
        "CREATE TEMP TABLE api_key (key_id text, tenant_id text, project_id text, principal_id text, key_hash text,"
        " pepper_version int, env text, scopes text[], ip_allowlist cidr[], expires_at timestamptz,"
        " revoked_at timestamptz)",
        "CREATE TEMP TABLE principal (tenant_id text, id text, roles text[], disabled_at timestamptz)",
        "CREATE TEMP TABLE project (tenant_id text, id text, deleted_at timestamptz)",
        "CREATE TEMP TABLE tenant (id text, deleted_at timestamptz)",
        "CREATE TEMP TABLE job (id text, tenant_id text, kind text, payload jsonb, attempts int, state text,"
        " run_after timestamptz, priority int)",
    ):
        await pg_admin.execute(ddl)
    await pg_admin.execute(
        f"INSERT INTO pg_temp.api_key VALUES ('{forged}', 't_evil', 'p', 'u', 'h', 1, 'live', '{{turns}}', NULL, NULL,"
        " NULL)"
    )
    await pg_admin.execute("INSERT INTO pg_temp.principal VALUES ('t_evil', 'u', '{tenant_admin}', NULL)")
    await pg_admin.execute("INSERT INTO pg_temp.project VALUES ('t_evil', 'p', NULL)")
    await pg_admin.execute("INSERT INTO pg_temp.tenant VALUES ('t_evil', NULL)")
    await pg_admin.execute("INSERT INTO pg_temp.job VALUES ('job_evil', 't', 'k', '{}', 0, 'queued', now(), 1)")
    try:
        await pg_admin.execute("SET ROLE noema_app")
        await pg_admin.execute("SET search_path = pg_temp, public")
        assert await pg_admin.fetch("SELECT * FROM auth.lookup_api_key($1)", forged) == []
        assert await pg_admin.fetch("SELECT * FROM jobs.claim('{k}', 10, 'w', 60)") == []
    finally:
        await pg_admin.execute("RESET ROLE")
        await pg_admin.execute("RESET search_path")


async def test_user_writable_schema_operators_and_functions_have_no_effect(pg_admin: Conn) -> None:
    """A schema placed before pg_catalog in the caller's search_path hijacks `=` and `now()` for an unprotected
    definer function (the control), but not for the noema functions (search_path = pg_catalog, pg_temp)."""
    _, key_id = await _seed_key(pg_admin)
    schema = f"evil_{uuid.uuid4().hex[:8]}"
    job_id = f"job_{uuid.uuid4().hex[:20]}"
    kind = f"kind_{uuid.uuid4().hex[:8]}"
    await pg_admin.execute(f"CREATE SCHEMA {schema}")
    try:
        await pg_admin.execute(f"GRANT USAGE ON SCHEMA {schema} TO noema_app")
        await pg_admin.execute(
            f"CREATE FUNCTION {schema}.always(text, text) RETURNS boolean LANGUAGE sql IMMUTABLE AS 'SELECT true'"
        )
        await pg_admin.execute(
            f"CREATE OPERATOR {schema}.= (LEFTARG = text, RIGHTARG = text, FUNCTION = {schema}.always)"
        )
        await pg_admin.execute(
            f"CREATE FUNCTION {schema}.now() RETURNS timestamptz LANGUAGE sql AS $$ SELECT timestamptz '2100-01-01' $$"
        )
        await pg_admin.execute(
            f"CREATE FUNCTION {schema}.unsafe_lookup(p text) RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER"
            " AS $$ SELECT count(*) FROM auth.api_key k WHERE k.key_id = p $$"
        )
        await pg_admin.execute(
            "INSERT INTO jobs.job (id, tenant_id, kind, payload, state, run_after)"
            " VALUES ($1, 't_x', $2, '{}', 'queued', now() + interval '1 day')",
            job_id,
            kind,
        )
        await pg_admin.execute("SET ROLE noema_app")
        await pg_admin.execute(f"SET search_path = {schema}, pg_catalog")
        assert await pg_admin.fetchval("SELECT now()") > await pg_admin.fetchval("SELECT pg_catalog.now()")
        assert await pg_admin.fetchval(f"SELECT {schema}.unsafe_lookup('no-such-key')") >= 1  # the control is hijacked
        assert await pg_admin.fetch("SELECT * FROM auth.lookup_api_key('no-such-key')") == []
        assert len(await pg_admin.fetch("SELECT * FROM auth.lookup_api_key($1)", key_id)) == 1
        assert await pg_admin.fetch("SELECT * FROM jobs.claim(ARRAY[$1], 10, 'w', 60)", kind) == []  # not due yet
    finally:
        await pg_admin.execute("RESET ROLE")
        await pg_admin.execute("RESET search_path")
        await pg_admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await pg_admin.execute("DELETE FROM jobs.job WHERE id = $1", job_id)
