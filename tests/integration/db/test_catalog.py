"""Catalog checks (spec-03 §6): RLS flags, tenant_id conventions, policies, roles and the exact noema_app grants."""

from __future__ import annotations

import asyncpg
import pytest

pytestmark = pytest.mark.integration

type Conn = asyncpg.Connection[asyncpg.Record]

RLS_SCHEMAS = ["noema", "auth", "jobs"]
TENANT_ISOLATION_TABLES = {
    "noema.project", "noema.principal", "auth.api_key", "noema.session", "noema.turn", "noema.turn_file",
    "noema.attempt", "noema.decision", "noema.file", "noema.derivative", "noema.usage_line",
    "noema.idempotency_record", "noema.audit_event", "jobs.job", "noema.labeling_run", "noema.label_item",
}  # fmt: skip
PROJECT_TABLES = {
    "noema.session", "noema.turn", "noema.decision", "noema.file", "noema.usage_line", "noema.idempotency_record",
}  # fmt: skip
# spec-03 §1: the noema_app grant table, exactly
_CRUD = {"SELECT", "INSERT", "UPDATE", "DELETE"}
APP_GRANTS: dict[str, set[str]] = {
    "noema.tenant": {"SELECT"}, "noema.project": {"SELECT"}, "noema.principal": {"SELECT"},
    "noema.calibrator_epoch": {"SELECT"},
    **{f"noema.{t}": _CRUD for t in ("session", "turn", "turn_file", "attempt", "decision", "file", "derivative",
                                      "idempotency_record", "labeling_run", "label_item")},
    "noema.calibrator": {"SELECT", "INSERT", "UPDATE"}, "noema.usage_line": {"SELECT"},
    "noema.audit_event": {"SELECT", "INSERT"}, "jobs.job": {"SELECT", "INSERT"},
    "platform.config_version": {"SELECT"}, "platform.price_catalog": {"SELECT"},
}  # fmt: skip
ISOLATION_EXPR = "(tenant_id = current_setting('app.tenant_id'::text, true))"


async def _tables(conn: Conn, schemas: list[str]) -> dict[str, asyncpg.Record]:
    rows = await conn.fetch(
        "SELECT n.nspname || '.' || c.relname AS t, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND n.nspname = ANY($1)",
        schemas,
    )
    return {r["t"]: r for r in rows}


async def test_every_noema_auth_jobs_table_has_enable_and_force_rls(pg_admin: Conn) -> None:
    tables = await _tables(pg_admin, RLS_SCHEMAS)
    assert len(tables) == 19
    assert {t for t, r in tables.items() if not (r["relrowsecurity"] and r["relforcerowsecurity"])} == set()


async def test_platform_and_dead_letter_tables_have_no_rls(pg_admin: Conn) -> None:
    tables = await _tables(pg_admin, ["platform", "ops"])
    assert set(tables) == {"platform.config_version", "platform.price_catalog", "platform.operator",
                           "ops.outbox_dead_letter"}  # fmt: skip
    assert not any(r["relrowsecurity"] for r in tables.values())


async def test_tenant_id_is_not_null_with_the_empty_check_everywhere(pg_admin: Conn) -> None:
    rows = await pg_admin.fetch(
        "SELECT table_schema || '.' || table_name AS t, is_nullable, is_generated FROM information_schema.columns"
        " WHERE column_name = 'tenant_id' AND table_schema = ANY($1)",
        ["noema", "auth", "jobs", "ops"],
    )
    by_table = {r["t"]: r for r in rows}
    exceptions = {"noema.calibrator", "noema.calibrator_epoch"}  # NULL tenant_id = platform rows (spec-03 §1)
    assert {t for t, r in by_table.items() if r["is_nullable"] != "NO"} == exceptions
    assert by_table["noema.calibrator_epoch"]["is_generated"] == "ALWAYS"
    checks = await pg_admin.fetch(
        "SELECT n.nspname || '.' || c.relname AS t FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE k.contype = 'c' AND pg_get_constraintdef(k.oid) LIKE '%tenant_id <> ''''::text%'"
    )
    assert set(by_table) - {"noema.calibrator_epoch"} <= {r["t"] for r in checks}
    epoch_check = await pg_admin.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'noema.calibrator_epoch'::regclass"
        " AND pg_get_constraintdef(oid) LIKE '%scope_key <> ''''::text%'"
    )
    assert epoch_check == 1


async def test_policies_match_spec_03_section_3(pg_admin: Conn) -> None:
    rows = await pg_admin.fetch(
        "SELECT schemaname || '.' || tablename AS t, policyname, permissive, cmd, qual, with_check FROM pg_policies"
    )
    by_table: dict[str, dict[str, asyncpg.Record]] = {}
    for r in rows:
        by_table.setdefault(r["t"], {})[r["policyname"]] = r
    for table in TENANT_ISOLATION_TABLES:
        iso = by_table[table]["tenant_isolation"]
        assert (iso["permissive"], iso["cmd"], iso["qual"], iso["with_check"]) == (
            "PERMISSIVE",
            "ALL",
            ISOLATION_EXPR,
            ISOLATION_EXPR,
        ), table
        expected = {"tenant_isolation", "project_scope"} if table in PROJECT_TABLES else {"tenant_isolation"}
        assert set(by_table[table]) == expected, table
    for table in PROJECT_TABLES:
        scope = by_table[table]["project_scope"]
        assert (scope["permissive"], scope["cmd"]) == ("RESTRICTIVE", "ALL")
        assert "app.project_id" in scope["qual"] and "project_id =" in scope["qual"]
    assert set(by_table["noema.tenant"]) == {"tenant_read"}
    assert by_table["noema.tenant"]["tenant_read"]["cmd"] == "SELECT"
    assert set(by_table["noema.calibrator_epoch"]) == {"epoch_read"}
    assert by_table["noema.calibrator_epoch"]["epoch_read"]["cmd"] == "SELECT"
    cal = by_table["noema.calibrator"]
    assert set(cal) == {"cal_read", "cal_write", "cal_lifecycle_insert", "cal_lifecycle_update"}
    assert (cal["cal_read"]["cmd"], cal["cal_write"]["cmd"]) == ("SELECT", "ALL")
    assert {cal["cal_lifecycle_insert"]["permissive"], cal["cal_lifecycle_update"]["permissive"]} == {"RESTRICTIVE"}


async def test_noema_app_has_exactly_the_spec_03_grants(pg_admin: Conn) -> None:
    rows = await pg_admin.fetch(
        "SELECT table_schema || '.' || table_name AS t, privilege_type FROM information_schema.role_table_grants"
        " WHERE grantee = 'noema_app'"
    )
    granted: dict[str, set[str]] = {}
    for r in rows:
        granted.setdefault(r["t"], set()).add(r["privilege_type"])
    assert granted == APP_GRANTS
    assert not await pg_admin.fetchval(
        "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee IN ('noema_cli', 'PUBLIC')"
        " AND table_schema = ANY($1)",
        ["noema", "auth", "jobs", "ops", "admin", "platform", "public"],
    )


async def test_roles_have_the_spec_03_attributes(pg_admin: Conn) -> None:
    roles = {
        r["rolname"]: r
        for r in await pg_admin.fetch(
            "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin, rolcreatedb, rolcreaterole FROM pg_roles"
            " WHERE rolname LIKE 'noema\\_%'"
        )
    }
    for name in ("noema_owner", "noema_app", "noema_cli"):
        r = roles[name]
        assert (r["rolsuper"], r["rolbypassrls"], r["rolcanlogin"], r["rolcreatedb"], r["rolcreaterole"]) == (
            False,
            False,
            True,
            False,
            False,
        ), name
    d = roles["noema_definer"]
    assert (d["rolsuper"], d["rolbypassrls"], d["rolcanlogin"]) == (False, True, False)
    membership = await pg_admin.fetch(
        "SELECT r.rolname AS role, m.rolname AS member, a.inherit_option, a.set_option FROM pg_auth_members a"
        " JOIN pg_roles r ON r.oid = a.roleid JOIN pg_roles m ON m.oid = a.member"
        " WHERE r.rolname LIKE 'noema\\_%' OR m.rolname LIKE 'noema\\_%'"
    )
    assert [(x["role"], x["member"], x["inherit_option"], x["set_option"]) for x in membership] == [
        ("noema_definer", "noema_owner", False, True)
    ]


async def test_no_create_anywhere_and_no_public_database_privileges(pg_admin: Conn, pg_database: object) -> None:
    creators = await pg_admin.fetch(
        "SELECT r.rolname, n.nspname FROM pg_namespace n CROSS JOIN pg_roles r"
        " WHERE r.rolname IN ('noema_app', 'noema_cli', 'noema_definer')"
        " AND has_schema_privilege(r.oid, n.oid, 'CREATE')"
    )
    assert [(r["rolname"], r["nspname"]) for r in creators] == []
    acl = await pg_admin.fetchval("SELECT datacl::text[] FROM pg_database WHERE datname = current_database()")
    assert not [entry for entry in acl if entry.startswith("=")], acl  # PUBLIC: no CONNECT, no TEMP
    for role in ("noema_app", "noema_cli"):
        assert await pg_admin.fetchval("SELECT has_database_privilege($1, current_database(), 'CONNECT')", role)
        assert not await pg_admin.fetchval("SELECT has_database_privilege($1, current_database(), 'TEMP')", role)


async def test_functions_created_later_are_not_public_by_default(pg_admin: Conn) -> None:
    rows = await pg_admin.fetch(
        "SELECT r.rolname, d.defaclacl::text[] AS acl FROM pg_default_acl d JOIN pg_roles r ON r.oid = d.defaclrole"
        " WHERE d.defaclobjtype = 'f' AND d.defaclnamespace = 0"
    )
    acls = {r["rolname"]: r["acl"] for r in rows}
    assert set(acls) == {"noema_owner", "noema_definer"}
    for role, acl in acls.items():
        assert acl == [f"{role}=X/{role}"], acl  # only the owner itself; nothing for PUBLIC
