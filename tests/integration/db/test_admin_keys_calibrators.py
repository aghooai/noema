"""admin.* projects, principals, keys and the calibrator lifecycle (spec-03 §4; IMPL-008, IMPL-044, IMPL-072)."""

from __future__ import annotations

import uuid
from datetime import datetime
from ipaddress import ip_network

import asyncpg
import pytest
from tests.fixtures.db import seed_tenant
from tests.integration.db._admin import Acting, key_hash, key_id, listening, operator
from tests.unit.labeling.test_admin_routes import calibrator

from noema.contracts.validate import ContractValidator
from noema.core.values import JsonValue
from noema.db.system import AdminError
from noema.db.uow import PgDatabase, Tx
from noema.labeling.views import calibrator_info

pytestmark = pytest.mark.integration

type Conn = asyncpg.Connection[asyncpg.Record]


async def test_projects_principals_and_keys(pg_admin: Conn, pg_cli: Conn, pg_db: PgDatabase[Tx]) -> None:
    calls = Acting(pg_cli)
    a = await operator(pg_admin)
    tenant = (await seed_tenant(pg_admin, projects=())).tenant_id
    project = await calls.create_project(a, tenant, "billing", default_data_class="confidential")
    other_project = await calls.create_project(a, tenant, "other")
    with pytest.raises(AdminError) as same_name:
        await calls.create_project(a, tenant, "billing")
    assert same_name.value.reason == "conflict"
    with pytest.raises(AdminError) as no_tenant:
        await calls.create_project(a, "t_missing", "x")
    assert no_tenant.value.reason == "not_found"
    principal = await calls.create_principal(a, tenant, project, "user@example.com", ["developer", "reviewer"])
    with pytest.raises(AdminError) as no_roles:
        await calls.create_principal(a, tenant, project, "nobody", [])
    assert no_roles.value.reason == "invalid_argument"
    kid = key_id()
    common = {"tenant_id": tenant, "principal_id": principal, "env": "live", "scopes": ["turns", "decisions"]}
    for bad in (
        {"project_id": other_project, "key_id": key_id(), "key_hash": key_hash(), "pepper_version": 1},
        {"project_id": project, "key_id": key_id(), "key_hash": key_hash(2), "pepper_version": 1},
        {"project_id": project, "key_id": "short", "key_hash": key_hash(), "pepper_version": 1},
    ):
        with pytest.raises(AdminError) as refused:
            await calls.create_api_key(a, **common, **bad)
        assert refused.value.reason in ("not_found", "invalid_argument")
    await calls.create_api_key(
        a, **common, project_id=project, key_id=kid, key_hash=key_hash(), pepper_version=1,
        ip_allowlist=[ip_network("10.0.0.0/8")],
    )  # fmt: skip
    async with pg_db.system_uow() as system:
        row = await system.lookup_api_key(kid)
    assert (
        row is not None
        and row["roles"] == ["developer", "reviewer"]
        and row["ip_allowlist"] == [ip_network("10.0.0.0/8")]
    )
    listed = await calls.list_api_keys(a, tenant)
    assert [k["key_id"] for k in listed] == [kid] and "key_hash" not in listed[0]
    assert {p["name"] for p in await calls.list_projects(a, tenant)} == {"billing", "other"}
    async with listening(pg_admin) as notes:
        await calls.revoke_api_key(a, kid)
        assert await notes.wait_for("keys_revoked", lambda p: p["key_id"] == kid) == {"tenant_id": tenant,
                                                                                       "key_id": kid}  # fmt: skip
    assert await pg_admin.fetchval("SELECT revoked_at FROM auth.api_key WHERE key_id = $1", kid) is not None
    with pytest.raises(AdminError) as unknown_key:
        await calls.revoke_api_key(a, key_id())
    assert unknown_key.value.reason == "not_found"


_CAL = (
    "INSERT INTO noema.calibrator (id, scope, tenant_id, profile_id, profile_version, question_name,"
    " question_signature, member_subset, input_view_signature, method, member_t, pooled, holdout, acceptance,"
    " reliability, status, fitted_at) VALUES ($1, 'tenant', $2, 'router-default', '1', 'q', $3, '{m1,m2}', 'vs',"
    " 'temperature', '{}', '{}', '{}', '{}', '{}', 'fitted', now())"
)


async def _fitted(admin: Conn, tenant: str, signature: str) -> str:
    cal = f"cal_{uuid.uuid4().hex[:20]}"
    await admin.execute(_CAL, cal, tenant, signature)
    return cal


async def _epoch(pg_db: PgDatabase[Tx], scope: str) -> int:
    async with pg_db.system_uow() as system:
        return (await system.calibrator_epochs())[scope]


async def test_two_person_activation_replaces_and_bumps_the_epoch(
    pg_admin: Conn, pg_cli: Conn, pg_db: PgDatabase[Tx]
) -> None:
    calls = Acting(pg_cli)
    a, b = await operator(pg_admin), await operator(pg_admin)
    tenant = (await seed_tenant(pg_admin)).tenant_id
    signature = f"qs_{uuid.uuid4().hex[:8]}"
    first = await _fitted(pg_admin, tenant, signature)
    assert await _epoch(pg_db, tenant) == 0
    with pytest.raises(AdminError) as not_accepted:
        await calls.calibrator_approve(a, first)
    assert not_accepted.value.reason == "invalid_state"
    await calls.set_calibrator_status(a, first, "accepted")
    await calls.calibrator_approve(a, first)
    with pytest.raises(AdminError) as self_activation:
        await calls.calibrator_activate(a, first)
    assert self_activation.value.reason == "two_person"
    async with listening(pg_admin) as notes:
        await calls.calibrator_activate(b, first)
        assert await notes.wait_for("calibrators_changed", lambda p: p["scope_key"] == tenant) == {
            "scope_key": tenant, "n": 1
        }  # fmt: skip
    row = await pg_admin.fetchrow("SELECT status, activated_by FROM noema.calibrator WHERE id = $1", first)
    assert row is not None
    assert (row["status"], row["activated_by"]) == ("active", [a, b])
    second = await _fitted(pg_admin, tenant, signature)
    await calls.set_calibrator_status(a, second, "accepted")
    await calls.calibrator_approve(b, second)
    await calls.calibrator_activate(a, second)
    statuses = {r["id"]: (r["status"], r["retired_reason"]) for r in await calls.list_calibrators(a, scope_key=tenant)}
    assert statuses == {first: ("retired", "replaced"), second: ("active", None)}
    assert await _epoch(pg_db, tenant) == 2
    async with pg_db.system_uow() as system:
        (active,) = await system.active_calibrators([tenant])
    assert active["id"] == second and "acceptance" not in active and "reliability" not in active
    with pytest.raises(AdminError) as never_active:
        await calls.set_calibrator_status(a, first, "active")
    assert never_active.value.reason == "invalid_state"


async def test_noema_app_may_only_downgrade_an_active_artifact(
    pg_admin: Conn, pg_cli: Conn, pg_db: PgDatabase[Tx]
) -> None:
    calls = Acting(pg_cli)
    a, b = await operator(pg_admin), await operator(pg_admin)
    tenant = (await seed_tenant(pg_admin)).tenant_id
    active = await _fitted(pg_admin, tenant, f"qs_{uuid.uuid4().hex[:8]}")
    fitted = await _fitted(pg_admin, tenant, f"qs_{uuid.uuid4().hex[:8]}")
    await calls.set_calibrator_status(a, active, "accepted")
    await calls.calibrator_approve(a, active)
    await calls.calibrator_activate(b, active)
    async with pg_db.system_uow() as system:
        for target, status, reason, actor, expected in (
            (fitted, "accepted", None, "system:labeling.drift", "forbidden"),
            (active, "retired", "manual", "system:labeling.drift", "forbidden"),
            (active, "downgraded", "drift_ece", a, "invalid_argument"),  # the app is never an operator
            (active, "downgraded", "because", "system:labeling.drift", "invalid_argument"),
        ):
            with pytest.raises(AdminError) as refused:
                await system.set_calibrator_status(actor, target, status, reason=reason)
            assert refused.value.reason == expected, (status, actor)
        await system.set_calibrator_status("system:labeling.drift", active, "downgraded", reason="drift_ece")
        assert await system.bump_epoch("system:labeling.drift", tenant) == 3
        with pytest.raises(AdminError) as unknown_scope:
            await system.bump_epoch("system:labeling.drift", "t_unknown")
        assert unknown_scope.value.reason == "not_found"
    row = await pg_admin.fetchrow("SELECT status, downgrade_reason FROM noema.calibrator WHERE id = $1", active)
    assert row is not None
    assert (row["status"], row["downgrade_reason"]) == ("downgraded", "drift_ece")
    audit = await pg_admin.fetchrow(
        "SELECT actor_type, actor_id FROM noema.audit_event WHERE resource_id = $1 AND action = 'calibrator.status'"
        " AND details->>'to' = 'downgraded'",
        active,
    )
    assert audit is not None
    assert (audit["actor_type"], audit["actor_id"]) == ("system", "system:labeling.drift")


async def test_platform_artifacts_go_through_admin_only(pg_admin: Conn, pg_cli: Conn, pg_db: PgDatabase[Tx]) -> None:
    calls = Acting(pg_cli)
    a, b = await operator(pg_admin), await operator(pg_admin)
    cal = f"cal_{uuid.uuid4().hex[:20]}"
    # a real fitted artifact (the labeling fit + gate), so what listCalibrators serves from it is a valid
    # CalibratorInfo; the function itself checks only scope, id and status (docs/modules/db.md)
    fitted = calibrator(cal, scope="platform", status="accepted")
    fitted["question_signature"] = f"sha256:{uuid.uuid4().hex}{uuid.uuid4().hex}"  # a binding of this test only
    row: dict[str, JsonValue] = {k: v.isoformat() if isinstance(v, datetime) else v for k, v in fitted.items()}
    bads: list[dict[str, JsonValue]] = [{**row, "tenant_id": "t_x"}, {**row, "status": "active"}, {**row, "id": "nope"}]
    for bad in bads:
        with pytest.raises(AdminError) as refused:
            await calls.upsert_platform_calibrator(a, bad)
        assert refused.value.reason == "invalid_argument"
    assert await calls.upsert_platform_calibrator(a, row) == cal
    assert await calls.upsert_platform_calibrator(a, {**row, "pooled": {"temperature": 1.3}}) == cal
    platform_before = await _epoch(pg_db, "platform")
    await calls.calibrator_approve(a, cal)
    await calls.calibrator_activate(b, cal)
    assert await _epoch(pg_db, "platform") == platform_before + 1
    with pytest.raises(AdminError) as replace_active:
        await calls.upsert_platform_calibrator(a, row)
    assert replace_active.value.reason == "invalid_state"
    stored = await pg_admin.fetchrow("SELECT * FROM noema.calibrator WHERE id = $1", cal)
    assert stored is not None
    assert (stored["scope"], stored["tenant_id"], stored["pooled"]) == ("platform", None, {"temperature": 1.3})
    served = {"data": [calibrator_info(dict(stored))], "has_more": False, "next_cursor": None}
    ContractValidator().validate_schema("listCalibrators", 200, "application/json", served)
    await calls.calibrator_retire(a, cal, reason="profile_version_retired")
    assert await _epoch(pg_db, "platform") == platform_before + 2
    with pytest.raises(AdminError) as retired:
        await calls.calibrator_retire(a, cal)
    assert retired.value.reason == "invalid_state"


async def test_operators_bump_epochs_too(pg_admin: Conn, pg_cli: Conn, pg_db: PgDatabase[Tx]) -> None:
    calls = Acting(pg_cli)
    a, revoked = await operator(pg_admin), await operator(pg_admin, revoked=True)
    before = await _epoch(pg_db, "platform")
    assert await calls.bump_epoch(a, "platform") == before + 1
    with pytest.raises(AdminError) as not_live:
        await calls.bump_epoch(revoked, "platform")
    assert not_live.value.reason == "operator_invalid"
    with pytest.raises(AdminError) as system_actor:  # 'system:*' actors exist only for noema_app sessions
        await calls.bump_epoch("system:labeling.drift", "platform")
    assert system_actor.value.reason == "operator_invalid"
