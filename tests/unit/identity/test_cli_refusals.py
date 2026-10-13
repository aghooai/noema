"""noema-admin failure paths without a database: database/AWS errors as JSON refusals, the ExternalId steps when a
later step fails, the tenant id check, staged rotation, and operator keys on the previous pepper."""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from tests.unit.identity.helpers import PEPPER_V1, PEPPER_V2, pepper

from noema.db.engine import WrongDatabaseRole
from noema.identity.apikeys import PepperRing, mint_operator_key, parse_operator_key, verify_key
from noema.identity.cli import noema_admin
from noema.identity.cli.context import AdminContext, CliError

DSN_PASSWORD = "s3cret-dsn-password"


class FakeSm:
    """The Secrets Manager calls of `tenants external-id`, with version stages."""

    def __init__(self) -> None:
        self.versions: dict[str, dict[str, tuple[str, set[str]]]] = {}  # name -> {version_id: (value, stages)}
        self.n = 0

    def _new(self, name: str, value: str, stages: set[str]) -> str:
        self.n += 1
        vid = f"0000000{self.n}-0000-4000-8000-000000000000"
        for _, labels in self.versions.setdefault(name, {}).values():
            labels -= stages
        self.versions[name][vid] = (value, stages)
        return vid

    async def create_secret(self, *, Name: str, SecretString: str) -> dict[str, Any]:  # noqa: N803
        if Name in self.versions:
            raise ClientError({"Error": {"Code": "ResourceExistsException"}}, "CreateSecret")
        return {"VersionId": self._new(Name, SecretString, {"AWSCURRENT"})}

    async def put_secret_value(self, *, SecretId: str, SecretString: str, VersionStages: list[str]) -> dict[str, Any]:  # noqa: N803
        if SecretId not in self.versions:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "PutSecretValue")
        return {"VersionId": self._new(SecretId, SecretString, set(VersionStages))}

    async def describe_secret(self, *, SecretId: str) -> dict[str, Any]:  # noqa: N803
        return {"VersionIdsToStages": {v: sorted(s) for v, (_, s) in self.versions.get(SecretId, {}).items()}}

    async def update_secret_version_stage(self, **kw: str) -> None:
        versions = self.versions[kw["SecretId"]]
        if "RemoveFromVersionId" in kw:
            versions[kw["RemoveFromVersionId"]][1].discard(kw["VersionStage"])
            if kw["VersionStage"] == "AWSCURRENT":
                versions[kw["RemoveFromVersionId"]][1].add("AWSPREVIOUS")
        if "MoveToVersionId" in kw:
            versions[kw["MoveToVersionId"]][1].add(kw["VersionStage"])

    def current(self, name: str) -> str:
        return next(value for value, stages in self.versions[name].values() if "AWSCURRENT" in stages)


@dataclass
class FakeAdmin:
    audit_error: BaseException | None = None
    audits: list[dict[str, Any]] = field(default_factory=list)

    async def record_audit(self, op: str, **kw: Any) -> str:
        if self.audit_error is not None:
            raise self.audit_error
        self.audits.append(kw)
        return "aud_1"


@dataclass
class FakeContext:
    sm: FakeSm = field(default_factory=FakeSm)
    db: FakeAdmin = field(default_factory=FakeAdmin)
    admin_error: BaseException | None = None

    @property
    def secrets(self) -> Any:
        sm = self.sm

        class Lazy:
            async def get(self) -> FakeSm:
                return sm

        class Provider:
            client = Lazy()

        return Provider()

    async def admin(self, *, owner: bool = False) -> FakeAdmin:
        if self.admin_error is not None:
            raise self.admin_error
        return self.db

    async def operator(self, admin: object) -> str:
        return "op_1"

    async def aclose(self) -> None:
        return None


async def run(monkeypatch: pytest.MonkeyPatch, ctx: FakeContext, *argv: str) -> tuple[int, Any, list[Any]]:
    monkeypatch.setattr(AdminContext, "from_environ", classmethod(lambda cls, environ: ctx))
    out, err = io.StringIO(), io.StringIO()
    code = await noema_admin.amain(list(argv), environ={}, stdout=out, stderr=err)
    errors = [json.loads(line) for line in err.getvalue().splitlines()]
    assert DSN_PASSWORD not in out.getvalue() + err.getvalue()
    return code, json.loads(out.getvalue() or "null"), errors


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            asyncpg.InvalidPasswordError(f'password authentication failed for user "noema_cli" {DSN_PASSWORD}'),
            "invalid_argument",
        ),
        (WrongDatabaseRole(f"postgresql://noema_app:{DSN_PASSWORD}@db/noema"), "invalid_argument"),
        (asyncpg.InvalidCatalogNameError("database does not exist"), "invalid_argument"),
        (asyncpg.SerializationError("could not serialize"), "unavailable"),
        (asyncpg.ConnectionDoesNotExistError("connection was closed"), "unavailable"),
        (ConnectionResetError("reset"), "unavailable"),
        (EndpointConnectionError(endpoint_url="https://secretsmanager.us-east-1.amazonaws.com"), "unavailable"),
    ],
)
async def test_database_and_aws_errors_are_json_refusals(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, reason: str
) -> None:
    """Every failure is one `{error, message}` object on stderr and exit 1, never a traceback or the DSN."""
    code, out, errors = await run(monkeypatch, FakeContext(admin_error=error), "keys", "list", "--tenant", "t_x")
    assert code == 1 and out is None and len(errors) == 1
    assert errors[0]["error"] == reason and set(errors[0]) == {"error", "message"}


async def test_external_id_rejects_a_tenant_id_that_is_not_one_path_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = FakeContext()
    for tenant in ("t_a/../t_b", "t a", "", "x" * 65, "t.a"):
        code, _, errors = await run(monkeypatch, ctx, "tenants", "external-id", "create", "--tenant", tenant)
        assert code == 1 and errors[0]["error"] == "invalid_argument", tenant
    assert ctx.sm.versions == {}


async def test_rotation_is_staged_then_promoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-001 §3: rotate stages AWSPENDING (brokers keep reading AWSCURRENT); promote makes it current."""
    ctx = FakeContext()
    name = "platform/tenants/t_a/external-id"
    code, created, _ = await run(monkeypatch, ctx, "tenants", "external-id", "create", "--tenant", "t_a")
    assert code == 0 and ctx.sm.current(name) == created["external_id"]
    code, _, errors = await run(monkeypatch, ctx, "tenants", "external-id", "promote", "--tenant", "t_a")
    assert code == 1 and errors[0]["error"] == "invalid_state"  # nothing staged
    code, rotated, _ = await run(monkeypatch, ctx, "tenants", "external-id", "rotate", "--tenant", "t_a")
    assert code == 0 and rotated["external_id"] != created["external_id"]
    assert ctx.sm.current(name) == created["external_id"]  # still the old value until promote
    assert "promote" in rotated["notice"]
    code, promoted, _ = await run(monkeypatch, ctx, "tenants", "external-id", "promote", "--tenant", "t_a")
    assert code == 0 and "external_id" not in promoted
    assert ctx.sm.current(name) == rotated["external_id"]
    assert promoted["version_id"] == rotated["version_id"]
    assert promoted["previous_version_id"] == created["version_id"]
    assert [a["action"] for a in ctx.db.audits] == [
        "tenant.external_id.create",
        "tenant.external_id.rotate",
        "tenant.external_id.promote",
    ]
    for audit in ctx.db.audits:
        assert created["external_id"] not in str(audit) and rotated["external_id"] not in str(audit)


async def test_a_written_external_id_is_printed_even_when_the_audit_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = FakeContext(db=FakeAdmin(audit_error=asyncpg.ConnectionDoesNotExistError("connection was closed")))
    code, out, errors = await run(monkeypatch, ctx, "tenants", "external-id", "create", "--tenant", "t_a")
    assert code == 1 and errors[0]["error"] == "audit_failed"
    assert out["external_id"] == ctx.sm.current("platform/tenants/t_a/external-id")
    code, out, errors = await run(monkeypatch, ctx, "tenants", "external-id", "rotate", "--tenant", "t_a")
    assert code == 1 and errors[0]["error"] == "audit_failed" and out["external_id"].startswith("aghoo-t_a-")
    assert ctx.sm.current("platform/tenants/t_a/external-id") != out["external_id"]  # staged only


class Operators:
    def __init__(self, key_hash: str) -> None:
        self.key_hash = key_hash
        self.rehashed: list[tuple[str, str, int]] = []
        self.proofs: list[int] = []  # pepper versions of the proofs put on the connection (0003, Wave E)

    async def prove_operator(self, key_id: str, secret: str, pepper_version: int, pepper: str) -> None:
        self.proofs.append(pepper_version)

    async def lookup_operator(self, key_id: str) -> dict[str, Any]:
        return {"id": "op_1", "name": "alice", "key_hash": self.key_hash, "revoked_at": None}

    async def rehash_operator(self, operator_id: str, key_hash: str, pepper_version: int) -> bool:
        self.rehashed.append((operator_id, key_hash, pepper_version))
        return True


async def test_operator_keys_on_the_previous_pepper(caplog: pytest.LogCaptureFixture) -> None:
    """A previous-pepper operator is accepted and re-hashed under the current pepper (`admin.rehash_operator`,
    requests item 5); a current-pepper one is not; with the previous version unreadable (partial ring) it is
    `unavailable`, not an authentication failure."""
    key_id = "01J9ZQ7ABCDEFGHJKMNPQRSTVW"
    token, key_hash = mint_operator_key(PepperRing(current=pepper(1, PEPPER_V1)), key_id)
    environ = {"NOEMA_OPERATOR_KEY": token.get_secret_value()}
    rotated = PepperRing(current=pepper(2, PEPPER_V2), previous=pepper(1, PEPPER_V1))
    ctx = AdminContext(environ=environ, env="test", overrides={}, _ring=rotated)
    operators = Operators(key_hash)
    with caplog.at_level(logging.INFO, logger="noema.identity.cli"):
        assert await ctx.operator(operators) == "op_1"  # type: ignore[arg-type]
    [(op, new_hash, version)] = operators.rehashed
    assert (op, version) == ("op_1", 2) and new_hash.startswith("hmac-sha256:v2:")
    parsed = parse_operator_key(token.get_secret_value())
    assert parsed is not None and verify_key(PepperRing(current=pepper(2, PEPPER_V2)), new_hash, *parsed) is not None
    assert "re-hashed with pepper v2" in caplog.text and token.get_secret_value() not in caplog.text
    assert operators.proofs == [1, 2]  # proved as stored, then again under the current pepper after the re-hash
    current = Operators(new_hash)
    assert await ctx.operator(current) == "op_1"  # type: ignore[arg-type]
    assert current.rehashed == [] and current.proofs == [2]
    partial = AdminContext(
        environ=environ, env="test", overrides={}, _ring=PepperRing(current=pepper(2, PEPPER_V2), partial=True)
    )
    with pytest.raises(CliError) as err:
        await partial.operator(Operators(key_hash))  # type: ignore[arg-type]
    assert err.value.reason == "unavailable" and err.value.exit_code == 1
    gone = AdminContext(environ=environ, env="test", overrides={}, _ring=PepperRing(current=pepper(2, PEPPER_V2)))
    with pytest.raises(CliError) as err:
        await gone.operator(Operators(key_hash))  # type: ignore[arg-type]
    assert err.value.reason == "operator_invalid" and err.value.exit_code == 3
