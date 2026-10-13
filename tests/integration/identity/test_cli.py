"""`noema-admin` flows on a fresh database and moto (IMPL-044, IMPL-072, ADR-001 §3, ADR-003 §5)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from moto.secretsmanager.models import secretsmanager_backends
from tests.fakes.clock import FakeClock
from tests.fakes.ops_admission import FakeRateLimiter
from tests.fakes.secrets import FakeSecretsProvider
from tests.integration.identity.cli_rig import CliRig
from tests.integration.identity.moto_support import CREDS, PLATFORM_ACCOUNT, REGION, put_secret
from tests.unit.identity.helpers import PEPPER_V2, PEPPER_V3, pepper_secrets

from noema.config.loader import validate_config
from noema.contracts.bundle import VENDOR_DIR
from noema.core.clock import SystemClock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.runtime import TaskGroupRuntime
from noema.db.engine import Engine, PoolSettings
from noema.db.repos import make_unit_of_work
from noema.db.uow import PgDatabase
from noema.identity.apikeys import PEPPER_REF, pepper_value
from noema.identity.authn import ApiKeyAuthenticator
from noema.identity.secrets import SecretsManagerProvider

pytestmark = pytest.mark.integration
EXAMPLE = VENDOR_DIR / "examples" / "platform.example.yaml"
EXAMPLE_TEXT = EXAMPLE.read_text()


def config_file(tmp_path: Path, version: int, *, name: str = "", **patch: object) -> str:
    body = yaml.safe_load(EXAMPLE_TEXT)
    body["config_version"] = version
    body.update(patch)
    path = tmp_path / f"config-v{version}{name}.yaml"
    path.write_text(yaml.safe_dump(body))
    return str(path)


async def test_bootstrap_is_one_time_and_operator_keys_authenticate(cli: CliRig) -> None:
    await cli.operators()
    assert re.fullmatch(r"op_[0-9A-HJKMNP-TV-Z]{26}\.[A-Za-z0-9_-]{43}", cli.keys["alice"])
    code, _, err = await cli.run("bootstrap", "--operator", "mallory")
    assert code == 1 and err["error"] == "conflict"
    stored = await cli.admin.fetch("SELECT name, key_hash FROM platform.operator ORDER BY name")
    assert [r["name"] for r in stored] == ["alice", "bob"]
    assert all(r["key_hash"].startswith("hmac-sha256:v1:") for r in stored)
    assert all(cli.keys["alice"].split(".")[1] not in r["key_hash"] for r in stored)
    cli.keys["forged"] = cli.keys["alice"][:-1] + ("A" if cli.keys["alice"][-1] != "A" else "B")
    code, _, err = await cli.run("config", "list", operator="forged")
    assert code == 3 and err == {"error": "operator_invalid", "message": "Operator authentication failed."}
    code, _, err = await cli.run("config", "list")  # no NOEMA_OPERATOR_KEY at all
    assert code == 3
    bob_id = await cli.admin.fetchval("SELECT id FROM platform.operator WHERE name = 'bob'")
    await cli.ok("operators", "revoke", bob_id)
    code, _, _ = await cli.run("config", "list", operator="bob")
    assert code == 3


async def test_operator_keys_are_rehashed_across_pepper_rotations(cli: CliRig) -> None:
    """requests item 5: an operator verified with the previous pepper is re-hashed under the current one
    (`admin.rehash_operator`, audited `operator.rehash`), so two rotations lock out only an operator who ran nothing
    in between."""
    await cli.operators()

    async def stored(name: str) -> str:
        return str(await cli.admin.fetchval("SELECT key_hash FROM platform.operator WHERE name = $1", name))

    await put_secret(cli.moto, PEPPER_REF, pepper_value(2, PEPPER_V2).decode())  # v1 becomes AWSPREVIOUS
    await cli.ok("config", "list")
    assert (await stored("alice")).startswith("hmac-sha256:v2:") and (await stored("bob")).startswith("hmac-sha256:v1:")
    await cli.ok("config", "list")  # a current-pepper key is not re-hashed again
    audits = await cli.admin.fetch("SELECT actor_id, details FROM noema.audit_event WHERE action = 'operator.rehash'")
    alice = await cli.admin.fetchval("SELECT id FROM platform.operator WHERE name = 'alice'")
    assert [(a["actor_id"], a["details"]) for a in audits] == [(alice, {"pepper_version": 2})]
    await put_secret(cli.moto, PEPPER_REF, pepper_value(3, PEPPER_V3).decode())  # v1 leaves the ring
    await cli.ok("config", "list")
    assert (await stored("alice")).startswith("hmac-sha256:v3:")
    code, _, err = await cli.run("config", "list", operator="bob")
    assert code == 3 and err["error"] == "operator_invalid"


async def test_config_two_person_activation(cli: CliRig, tmp_path: Path) -> None:
    await cli.operators()
    v1 = config_file(tmp_path, 1)
    valid = await cli.ok("config", "validate", v1, operator=None)
    filled, errors = validate_config(yaml.safe_dump(yaml.safe_load(EXAMPLE_TEXT)), env="test")
    assert errors == [] and valid["valid"] is True and valid["digest"].startswith("sha256:")
    bad = config_file(tmp_path, 9, name="-bad", routing=None)
    code, _, err = await cli.run("config", "validate", bad, operator=None)
    assert code == 1 and err["error"] == "invalid_config"
    proposed = await cli.ok("config", "propose", v1)
    assert proposed == {"version": 1, "digest": valid["digest"], "status": "proposed"}
    code, _, err = await cli.run("config", "approve", "1", "--file", v1, operator="alice")
    assert code == 1 and err["error"] == "two_person"
    sample = {**filled["labeling"]["sample"], "uniform_rate": 0.5}
    other = config_file(tmp_path, 1, name="-other", labeling={**filled["labeling"], "sample": sample})
    code, _, err = await cli.run("config", "approve", "1", "--file", other, operator="bob")
    assert code == 1 and err["error"] == "digest_mismatch"
    approved = await cli.ok("config", "approve", "1", "--file", v1, operator="bob")
    assert approved["status"] == "active" and approved["retired_calibrators"] == []
    rows = await cli.admin.fetch("SELECT version, status, proposed_by, approved_by FROM platform.config_version")
    assert [(r["version"], r["status"]) for r in rows] == [(1, "active")]
    assert rows[0]["proposed_by"] != rows[0]["approved_by"]
    assert await cli.admin.fetchval("SELECT count(*) FROM noema.tenant WHERE id = 't_acme'") == 1
    listed = await cli.ok("config", "list")
    assert [v["version"] for v in listed["data"]] == [1]
    v2 = config_file(tmp_path, 2)
    await cli.ok("config", "propose", v2, operator="bob")
    await cli.ok("config", "approve", "2", "--file", v2, operator="alice")
    await cli.ok("config", "rollback", "1", operator="alice")
    code, _, err = await cli.run("config", "approve", "1", "--file", v1, operator="alice")
    assert code == 1 and err["error"] == "two_person"  # the rollback proposer cannot approve it
    await cli.ok("config", "approve", "1", "--file", v1, operator="bob")
    statuses = {
        r["version"]: r["status"] for r in await cli.admin.fetch("SELECT version, status FROM platform.config_version")
    }
    assert statuses == {1: "active", 2: "superseded"}


async def test_keys_projects_principals(cli: CliRig, tmp_path: Path) -> None:
    await cli.operators()
    v1 = config_file(tmp_path, 1)
    await cli.ok("config", "propose", v1)
    await cli.ok("config", "approve", "1", "--file", v1, operator="bob")
    project = await cli.ok("projects", "create", "--tenant", "t_acme", "--name", "support")
    pid = project["project_id"]
    assert [p["id"] for p in (await cli.ok("projects", "list", "--tenant", "t_acme"))["data"]] == [pid]
    principal = await cli.ok(
        "principals",
        "create",
        "--tenant",
        "t_acme",
        "--project",
        pid,
        "--subject",
        "svc-1",
        "--roles",
        "developer,tenant_admin",
    )
    code, _, err = await cli.run(
        "principals",
        "create",
        "--tenant",
        "t_acme",
        "--project",
        pid,
        "--subject",
        "x",
        "--roles",
        "root",
        operator="alice",
    )
    assert code == 1 and err["error"] == "invalid_argument"
    key = await cli.ok(
        "keys",
        "create",
        "--tenant",
        "t_acme",
        "--project",
        pid,
        "--principal",
        principal["principal_id"],
        "--scopes",
        "turns,sessions",
        "--env",
        "test",
        "--ip-allow",
        "10.0.0.0/8",
    )
    assert re.fullmatch(r"ak_test_[0-9A-HJKMNP-TV-Z]{26}\.[A-Za-z0-9_-]{43}", key["key"])
    listed = (await cli.ok("keys", "list", "--tenant", "t_acme"))["data"]
    assert [k["key_id"] for k in listed] == [key["key_id"]] and all("key_hash" not in k for k in listed)
    assert key["key"].split(".")[1] not in str(listed)
    clock = FakeClock()
    engine = await Engine.create(PoolSettings(dsn=cli.app_dsn, max_size=2))
    try:
        authn = ApiKeyAuthenticator(
            db=PgDatabase(engine, make_unit_of_work),
            secrets=_moto_pepper(clock),
            limiter=FakeRateLimiter(clock),
            clock=clock,
            runtime=TaskGroupRuntime(clock),
            env="test",
        )
        who = await authn.authenticate({"authorization": f"Bearer {key['key']}"}, "10.1.2.3")
        assert who.principal_id == principal["principal_id"] and who.roles == {"developer", "tenant_admin"}
        assert who.scopes == {"turns", "sessions"} and who.project_id == pid
        await cli.ok("keys", "revoke", key["key_id"])
        authn.evict(key["key_id"])
        with pytest.raises(NoemaError) as err401:
            await authn.authenticate({"authorization": f"Bearer {key['key']}"}, "10.1.2.3")
        assert err401.value.code is ErrorCode.INVALID_API_KEY
    finally:
        await engine.close()
    code, _, err = await cli.run(
        "keys",
        "create",
        "--tenant",
        "t_acme",
        "--project",
        pid,
        "--principal",
        principal["principal_id"],
        "--scopes",
        "nope",
        operator="alice",
    )
    assert code == 1 and err["error"] == "invalid_argument"


def _moto_pepper(clock: FakeClock) -> FakeSecretsProvider:
    """The same pepper the CLI read from moto (version 1 of tests.unit.identity.helpers)."""
    return pepper_secrets(clock, current=1)


async def test_tenant_external_ids(cli: CliRig) -> None:
    """ADR-001 §3: create writes the secret and prints it once; create again refuses; rotate stages a new version
    (AWSPENDING: brokers keep the current value); promote makes it current; every step is audited without the
    value."""
    await cli.operators()
    tenant = "t_ext"
    name = f"platform/tenants/{tenant}/external-id"
    backend = secretsmanager_backends[PLATFORM_ACCOUNT][REGION]
    if backend.secrets.get(name) is not None:
        backend.delete_secret(name, None, True)
    created = await cli.ok("tenants", "external-id", "create", "--tenant", tenant)
    assert re.fullmatch(rf"aghoo-{tenant}-[A-Za-z0-9]{{32}}", created["external_id"])
    assert created["external_id_ref"] == f"aws-sm://{name}"
    code, _, err = await cli.run("tenants", "external-id", "create", "--tenant", tenant, operator="alice")
    assert code == 1 and err["error"] == "conflict"
    rotated = await cli.ok("tenants", "external-id", "rotate", "--tenant", tenant, operator="bob")
    assert rotated["external_id"] != created["external_id"]

    async def read(version: str | None = None) -> str:
        reader = SecretsManagerProvider(SystemClock(), region=REGION, endpoint_url=cli.moto, credentials=CREDS)
        try:
            return (await reader.get(f"aws-sm://{name}", version=version)).decode()
        finally:
            await reader.aclose()

    assert await read() == created["external_id"] and await read("AWSPENDING") == rotated["external_id"]
    promoted = await cli.ok("tenants", "external-id", "promote", "--tenant", tenant)
    assert promoted["version_id"] == rotated["version_id"] and "external_id" not in promoted
    assert await read() == rotated["external_id"] and await read("AWSPREVIOUS") == created["external_id"]
    code, _, err = await cli.run("tenants", "external-id", "promote", "--tenant", tenant, operator="alice")
    assert code == 1 and err["error"] == "invalid_state"
    code, _, err = await cli.run("tenants", "external-id", "create", "--tenant", "t_a/../t_b", operator="alice")
    assert code == 1 and err["error"] == "invalid_argument"
    audits = await cli.admin.fetch("SELECT action, details FROM noema.audit_event WHERE tenant_id = $1", tenant)
    assert sorted(a["action"] for a in audits) == [
        "tenant.external_id.create",
        "tenant.external_id.promote",
        "tenant.external_id.rotate",
    ]
    values = (created["external_id"], rotated["external_id"])
    assert all(v not in str(a["details"]) for a in audits for v in values)


async def test_database_login_failures_are_json_refusals(cli: CliRig) -> None:
    """A wrong password or a DSN of another role is one JSON refusal on stderr, never a traceback or the DSN."""
    good = cli.environ["NOEMA_CLI_DSN"]
    password = good.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    cases = {
        "wrong password": good.replace(f":{password}@", ":wrong-password@"),
        "another role": cli.app_dsn.get_secret_value(),
    }
    for case, dsn in cases.items():
        cli.environ["NOEMA_CLI_DSN"] = dsn
        code, out, err = await cli.run("keys", "list", "--tenant", "t_x")
        assert code == 1 and out is None and err["error"] == "invalid_argument", case
        assert "wrong-password" not in err["message"] and password not in err["message"]


_CAL = (
    "INSERT INTO noema.calibrator (id, scope, tenant_id, profile_id, profile_version, question_name,"
    " question_signature, member_subset, input_view_signature, method, member_t, pooled, holdout, acceptance,"
    " reliability, status, fitted_at, accepted_at, approved_by, activated_by, activated_at)"
    " VALUES ($1, 'platform', NULL, $2, $3, 'q', $4, '{m1,m2}', 'vs', 'temperature', '{}', '{}', '{}', '{}', '{}',"
    " $5, now(), now(), $6, $7, CASE WHEN $5 = 'active' THEN now() END)"
)


async def test_calibrators_two_person_and_retirement_on_config_approve(cli: CliRig, tmp_path: Path) -> None:
    """IMPL-072: approve (A) then activate (B != A); ADR-003 §5: `config approve` retires the artifacts whose
    profile version left the active config."""
    await cli.operators()
    alice, bob = [r["id"] for r in await cli.admin.fetch("SELECT id FROM platform.operator ORDER BY name")]
    version = str(yaml.safe_load(EXAMPLE_TEXT)["decision_profiles"]["lightweight-model-decide"]["version"])
    live, stale, stale_active = "cal_live000000000000000001", "cal_stale00000000000000001", "cal_stale00000000000000002"
    await cli.admin.execute(_CAL, live, "lightweight-model-decide", version, "qs1", "accepted", None, None)
    await cli.admin.execute(_CAL, stale, "lightweight-model-decide", "999", "qs2", "accepted", None, None)
    await cli.admin.execute(_CAL, stale_active, "router-default", "998", "qs3", "active", alice, [alice, bob])
    await cli.ok("calibrators", "approve", live)
    code, _, err = await cli.run("calibrators", "activate", live, operator="alice")
    assert code == 1 and err["error"] == "two_person"
    await cli.ok("calibrators", "activate", live, operator="bob")
    row = await cli.admin.fetchrow("SELECT status, activated_by FROM noema.calibrator WHERE id = $1", live)
    assert row is not None and row["status"] == "active" and len(row["activated_by"]) == 2
    listed = await cli.ok("calibrators", "list", "--scope", "platform", "--status", "active")
    assert {c["id"] for c in listed["data"]} == {live, stale_active}
    v1 = config_file(tmp_path, 1)
    await cli.ok("config", "propose", v1)
    approved = await cli.ok("config", "approve", "1", "--file", v1, operator="bob")
    assert sorted(approved["retired_calibrators"]) == sorted([stale, stale_active])
    rows = {
        r["id"]: r["retired_reason"] for r in await cli.admin.fetch("SELECT id, retired_reason FROM noema.calibrator")
    }
    assert rows == {live: None, stale: "profile_version_retired", stale_active: "profile_version_retired"}
    await cli.ok("calibrators", "retire", live, "--reason", "manual")
    assert await cli.admin.fetchval("SELECT retired_reason FROM noema.calibrator WHERE id = $1", live) == "manual"


async def test_config_approve_without_a_file_uses_the_stored_body(cli: CliRig, tmp_path: Path) -> None:
    """requests item 1: without `--file`, approve reads the stored body (`admin.config_body`) for the retirement."""
    await cli.operators()
    stale = "cal_stale00000000000000003"
    await cli.admin.execute(_CAL, stale, "lightweight-model-decide", "999", "qs2", "accepted", None, None)
    await cli.ok("config", "propose", config_file(tmp_path, 1))
    code, _, err = await cli.run("config", "approve", "7", operator="bob")
    assert code == 1 and err["error"] == "not_found"
    approved = await cli.ok("config", "approve", "1", operator="bob")
    assert approved == {"version": 1, "status": "active", "retired_calibrators": [stale]}
    assert await cli.admin.fetchval("SELECT status FROM platform.config_version WHERE version = 1") == "active"


async def test_prices_import(cli: CliRig, tmp_path: Path) -> None:
    await cli.operators()
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "entries": [
                    {
                        "model": "us.openai.gpt-6-luna",
                        "region": "us-east-1",
                        "effective_from": "2026-10-01",
                        "usd_per_mtok": {"input": "0.10", "output": "0.40"},
                    }
                ],
            }
        )
    )
    imported = await cli.ok("prices", "import", str(prices))
    assert imported["prices_ref"] == f"db:{imported['version']}"
    bad = tmp_path / "bad.yaml"
    bad.write_text("rows: 3\n")
    code, _, err = await cli.run("prices", "import", str(bad), operator="alice")
    assert code == 1 and err["error"] == "invalid_prices"
