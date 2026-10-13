"""admin.* operators, config activation and prices (spec-03 §4, spec-05 §2-§3; IMPL-044): bootstrap once, operator
checks, two-person approval and rollback, tenant sync, P1-P1 and the config_changed notification."""

from __future__ import annotations

import hashlib
from typing import Any

import asyncpg
import pytest
from tests.fixtures.db import new_tenant_id
from tests.integration.db._admin import Acting, key_hash, key_id, listening, login, operator

from noema.core.values import JsonValue
from noema.db.system import AdminError, PgAdminCalls

pytestmark = pytest.mark.integration

type Conn = asyncpg.Connection[asyncpg.Record]


def _digest(body: object) -> str:
    return "sha256:" + hashlib.sha256(repr(body).encode()).hexdigest()


async def _next_version(admin: Conn) -> int:
    return int(await admin.fetchval("SELECT coalesce(max(version), 0) + 1 FROM platform.config_version"))


async def test_bootstrap_is_one_time_and_owner_only(pg_admin: Conn, pg_owner: Conn, pg_cli: Conn) -> None:
    await pg_admin.execute("DELETE FROM platform.operator")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await PgAdminCalls(pg_cli).bootstrap_operator("first", key_id(), key_hash())
    first_key = key_id()
    op_id = await PgAdminCalls(pg_owner).bootstrap_operator("first", first_key, key_hash())
    assert op_id.startswith("op_")
    with pytest.raises(AdminError) as again:
        await PgAdminCalls(pg_owner).bootstrap_operator("second", key_id(), key_hash())
    assert again.value.reason == "conflict"
    found = await PgAdminCalls(pg_cli).lookup_operator(first_key)
    assert found is not None and (found["id"], found["name"], found["revoked_at"]) == (op_id, "first", None)
    assert await PgAdminCalls(pg_cli).lookup_operator(key_id()) is None
    audit = await pg_admin.fetchrow("SELECT tenant_id, actor_type FROM noema.audit_event WHERE resource_id = $1", op_id)
    assert audit is not None
    assert (audit["tenant_id"], audit["actor_type"]) == ("__platform__", "system")


async def test_operators_are_checked_and_the_last_one_stays(pg_admin: Conn, pg_cli: Conn) -> None:
    calls = Acting(pg_cli)
    await pg_admin.execute("DELETE FROM platform.operator")
    a = await operator(pg_admin)
    with pytest.raises(AdminError) as plaintext:
        await calls.create_operator(a, "b", key_id(), "not-a-hash")
    assert plaintext.value.reason == "invalid_argument"
    b = await calls.create_operator(a, "b", key_id(), key_hash())
    with pytest.raises(AdminError) as duplicate:
        await calls.create_operator(a, "b", key_id(), key_hash())
    assert duplicate.value.reason == "conflict"
    await calls.revoke_operator(a, b)
    with pytest.raises(AdminError) as revoked:
        await calls.list_config_versions(b)
    assert revoked.value.reason == "operator_invalid"
    with pytest.raises(AdminError) as last:
        await calls.revoke_operator(a, a)
    assert last.value.reason == "invalid_state"
    with pytest.raises(AdminError) as unknown:
        await calls.list_projects("op_nobody", "t_x")
    assert unknown.value.reason == "operator_invalid"


async def test_config_needs_two_people_and_syncs_tenants(pg_admin: Conn, pg_cli: Conn) -> None:
    calls = Acting(pg_cli)
    a, b = await operator(pg_admin), await operator(pg_admin)
    gone, kept = new_tenant_id(), new_tenant_id()
    await pg_admin.execute("INSERT INTO noema.tenant (id, aws_account_id) VALUES ($1, '1'), ($2, '1')", gone, kept)
    version = await _next_version(pg_admin)
    body: dict[str, JsonValue] = {"config_version": version, "tenants": {kept: {"aws_account_id": "222233334444"}}}
    with pytest.raises(AdminError) as bad_digest:
        await calls.config_propose(a, "md5:nope", body)
    assert bad_digest.value.reason == "invalid_argument"
    assert await calls.config_propose(a, _digest(body), body) == version
    with pytest.raises(AdminError) as stale:
        await calls.config_propose(a, _digest(body), {**body, "config_version": version})
    assert stale.value.reason == "conflict"
    with pytest.raises(AdminError) as self_approval:
        await calls.config_approve(a, version)
    assert self_approval.value.reason == "two_person"
    async with listening(pg_admin) as notes:
        await calls.config_approve(b, version)
        assert await notes.wait_for("config_changed", lambda p: p["version"] == version) == {"version": version}
    row = await pg_admin.fetchrow("SELECT status, approved_by FROM platform.config_version WHERE version = $1", version)
    assert row is not None
    assert (row["status"], row["approved_by"]) == ("active", b)
    tenants = {r["id"]: r for r in await pg_admin.fetch("SELECT * FROM noema.tenant WHERE id = ANY($1)", [gone, kept])}
    assert tenants[kept]["deleted_at"] is None and tenants[kept]["aws_account_id"] == "222233334444"
    assert tenants[gone]["deleted_at"] is not None
    with pytest.raises(AdminError) as twice:
        await calls.config_approve(b, version)
    assert twice.value.reason == "invalid_state"
    assert await calls.sync_tenants(a) == 1


async def test_rollback_reactivates_a_superseded_version_by_two_people(pg_admin: Conn, pg_cli: Conn) -> None:
    calls = Acting(pg_cli)
    a, b = await operator(pg_admin), await operator(pg_admin)
    first = await _next_version(pg_admin)
    for version in (first, first + 1):
        body: dict[str, JsonValue] = {"config_version": version, "tenants": {}}
        await calls.config_propose(a, _digest(body), body)
        await calls.config_approve(b, version)
    with pytest.raises(AdminError) as not_superseded:
        await calls.config_rollback(a, first + 1)
    assert not_superseded.value.reason == "invalid_state"
    await calls.config_rollback(a, first)
    with pytest.raises(AdminError) as same_person:
        await calls.config_approve(a, first)
    assert same_person.value.reason == "two_person"
    await calls.config_approve(b, first)
    statuses = {r["version"]: r["status"] for r in await calls.list_config_versions(a)}
    assert (statuses[first], statuses[first + 1]) == ("active", "superseded")
    assert "body" not in (await calls.list_config_versions(a))[0]


async def test_prices_are_versioned_and_a_db_prices_ref_must_exist(pg_admin: Conn, pg_cli: Conn) -> None:
    calls = Acting(pg_cli)
    a, b = await operator(pg_admin), await operator(pg_admin)
    prices: dict[str, JsonValue] = {"version": 1, "prices": []}
    v1 = await calls.import_prices(a, _digest(prices), prices)
    assert await calls.import_prices(a, _digest(prices), prices) == v1 + 1
    missing = await _next_version(pg_admin)
    body: dict[str, JsonValue] = {
        "config_version": missing,
        "tenants": {},
        "platform": {"cost": {"prices_ref": "db:999999"}},
    }
    await calls.config_propose(a, _digest(body), body)
    with pytest.raises(AdminError) as p1:
        await calls.config_approve(b, missing)
    assert p1.value.reason == "invalid_state" and "P1-P1" in p1.value.message
    ok = missing + 1
    body = {"config_version": ok, "tenants": {}, "platform": {"cost": {"prices_ref": f"db:{v1}"}}}
    await calls.config_propose(a, _digest(body), body)
    await calls.config_approve(b, ok)
    assert await pg_admin.fetchval("SELECT status FROM platform.config_version WHERE version = $1", ok) == "active"


async def test_record_audit_writes_a_platform_or_tenant_row(pg_admin: Conn, pg_cli: Conn) -> None:
    calls = Acting(pg_cli)
    a = await operator(pg_admin)
    tenant = new_tenant_id()
    audit_id = await calls.record_audit(
        a, tenant_id=tenant, action="external_id.rotate", resource_kind="tenant", resource_id=tenant,
        details={"secret_version": "v2"},
    )  # fmt: skip
    row = await pg_admin.fetchrow("SELECT * FROM noema.audit_event WHERE tenant_id = $1 AND id = $2", tenant, audit_id)
    assert row is not None
    assert (row["actor_type"], row["actor_id"], row["action"], row["details"]) == (
        "operator", a, "external_id.rotate", {"secret_version": "v2"}
    )  # fmt: skip
    with pytest.raises(AdminError) as empty:
        await calls.record_audit(a, tenant_id=None, action="", resource_kind="x", resource_id=None)
    assert empty.value.reason == "invalid_argument"


async def test_config_body_reads_one_stored_version(pg_admin: Conn, pg_cli: Conn) -> None:
    """identity request 1: the body of a stored version for an operator; None for an unknown version."""
    calls = Acting(pg_cli)
    a, gone = await operator(pg_admin), await operator(pg_admin, revoked=True)
    version = await _next_version(pg_admin)
    body: dict[str, JsonValue] = {"config_version": version, "tenants": {}, "platform": {"cost": {"x": [1, 2]}}}
    await calls.config_propose(a, _digest(body), body)
    assert await calls.config_body(a, version) == body
    assert await calls.config_body(a, version + 1000) is None
    with pytest.raises(AdminError) as revoked:
        await calls.config_body(gone, version)
    assert revoked.value.reason == "operator_invalid"


async def test_rehash_operator_moves_only_the_caller_forward(pg_admin: Conn, pg_cli: Conn) -> None:
    """identity request 5: the calling operator's key hash, only for a newer pepper version (compared as numbers),
    audited; a malformed or inconsistent hash is invalid_argument. Since 0003 every call proves the key as stored
    (the CLI proves it again after a re-hash)."""
    from tests.integration.db._admin import _KEYS

    from noema.core.secret import SecretStr
    from noema.identity.apikeys import Pepper, hash_key

    calls = PgAdminCalls(pg_cli)
    me, other, gone = await operator(pg_admin), await operator(pg_admin), await operator(pg_admin, revoked=True)
    kid, secret = _KEYS[me]

    def pepper(version: int) -> Pepper:
        return Pepper(version, SecretStr(f"pepper-v{version}".ljust(32, "x")))

    def hashed(version: int) -> str:
        return hash_key(pepper(version), kid, secret)

    async def prove(version: int) -> None:
        await calls.prove_operator(kid, secret, version, pepper(version).key.get_secret_value())

    async def stored(op_id: str) -> str:
        return str(await pg_admin.fetchval("SELECT key_hash FROM platform.operator WHERE id = $1", op_id))

    await login(pg_cli, me)
    other_before = await stored(other)
    assert await calls.rehash_operator(me, hashed(2), 2) is True
    assert await stored(me) == hashed(2) and await stored(other) == other_before
    await prove(2)
    assert await calls.rehash_operator(me, hashed(2), 2) is False  # same version
    assert await calls.rehash_operator(me, key_hash(1), 1) is False  # never a downgrade
    assert await stored(me) == hashed(2)
    await pg_admin.execute("UPDATE platform.operator SET key_hash = $2 WHERE id = $1", me, hashed(9))
    await prove(9)
    assert await calls.rehash_operator(me, hashed(10), 10) is True and await stored(me) == hashed(10)  # 10 > 9
    audits = await pg_admin.fetch(
        "SELECT details FROM noema.audit_event WHERE action = 'operator.rehash' AND resource_id = $1", me
    )
    assert sorted(r["details"]["pepper_version"] for r in audits) == [2, 10]
    await prove(10)
    for bad_hash, version in ((key_hash(11), 12), ("hmac-sha256:v12:short", 12), ("plain", 12)):
        with pytest.raises(AdminError) as invalid:
            await calls.rehash_operator(me, bad_hash, version)
        assert invalid.value.reason == "invalid_argument"
    with pytest.raises(AdminError) as revoked:
        await calls.rehash_operator(gone, key_hash(5), 5)
    assert revoked.value.reason == "operator_invalid"


async def test_a_connection_acts_only_as_the_operator_whose_key_it_proved(pg_admin: Conn, pg_cli: Conn) -> None:
    """Wave E edge fix 8 (IMPL-044 two-person rule, SEC-CFG-01): operator ids are readable (audit rows,
    list_config_versions), so a plain id is not enough. Without a proof, with another operator's proof, or with a
    wrong secret, pepper or version, every admin call is operator_invalid."""
    from tests.integration.db._admin import _KEYS, PEPPER

    raw = PgAdminCalls(pg_cli)  # no automatic proof
    a, b = await operator(pg_admin), await operator(pg_admin)
    version = await _next_version(pg_admin)
    body: dict[str, JsonValue] = {"config_version": version, "tenants": {}}

    async def refused(call: Any) -> None:
        with pytest.raises(AdminError) as error:
            await call
        assert error.value.reason == "operator_invalid"

    await refused(raw.config_propose(a, _digest(body), body))
    await refused(raw.create_operator(a, "x", key_id(), key_hash()))
    await login(pg_cli, a)
    assert await raw.config_propose(a, _digest(body), body) == version
    await refused(raw.config_approve(b, version))  # a's proof, approving as b
    kid, secret = _KEYS[b]
    pepper = PEPPER.key.get_secret_value()
    for wrong in ((kid, secret + "x", 1, pepper), (kid, secret, 1, "q" * 32), (kid, secret, 2, pepper)):
        await raw.prove_operator(*wrong)
        await refused(raw.config_approve(b, version))
    await pg_cli.execute("SELECT set_config('noema.operator_key', 'not json', false)")
    await refused(raw.config_approve(b, version))
    await login(pg_cli, b)  # b's own key: the second person
    await raw.config_approve(b, version)
    assert await pg_admin.fetchval("SELECT approved_by FROM platform.config_version WHERE version = $1", version) == b
