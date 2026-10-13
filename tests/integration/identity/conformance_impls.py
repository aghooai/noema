"""identity's real implementations in the shared conformance suites (spec-02 §14):

- `secrets_provider` / `secretsmanager`: `SecretsManagerProvider` on in-process moto;
- `credential_broker` / `sts-fakes`: `StsBroker` on FakeStsClient + FakeSecretsProvider;
- `credential_broker` / `sts-moto`: `StsBroker` + `AioStsClient` + `SecretsManagerProvider` on moto;
- `auth` / `apikey-fakedb`: `ApiKeyAuthenticator` + `MatrixAuthorizer` on the fake database;
- `auth` / `apikey-postgres`: the same on the session Postgres clone (`auth.lookup_api_key`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from moto.secretsmanager.models import secretsmanager_backends
from tests.contract_internal.kits import AuthKit, BrokerKit, SecretsKit
from tests.contract_internal.registry import Harness, register
from tests.fakes.config import FakeConfigRegistry
from tests.fakes.credentials import FakeStsClient
from tests.fakes.db import FakeDatabase
from tests.fakes.db_system import seed_identity
from tests.fakes.ops_admission import FakeRateLimiter
from tests.fakes.secrets import FakeSecretsProvider
from tests.integration.identity.moto_support import (
    API_ROLE,
    CREDS,
    EXTERNAL_IDS,
    PLATFORM_ACCOUNT,
    REGION,
    TENANTS,
    moto_url,
    seed_external_ids,
    tenants_config,
)
from tests.integration.identity.pg_support import scratch_db
from tests.unit.identity.helpers import RING_V2, pepper_secrets

from noema.core.principal import Principal
from noema.core.runtime import TaskGroupRuntime
from noema.db.engine import Engine, PoolSettings, connect
from noema.db.repos import make_unit_of_work
from noema.db.uow import PgDatabase
from noema.identity.apikeys import hash_key, parse_bearer
from noema.identity.authn import ApiKeyAuthenticator
from noema.identity.authz import MatrixAuthorizer
from noema.identity.secrets import SecretsManagerProvider, secret_id
from noema.identity.sts import AssumedRole, AssumeRoleResult, StsBroker
from noema.identity.sts_aws import AioStsClient

INTEGRATION = (pytest.mark.integration,)


@register("secrets_provider", "secretsmanager", marks=INTEGRATION)
@asynccontextmanager
async def secrets_manager(h: Harness) -> AsyncIterator[SecretsKit]:
    url = moto_url(h.request.config)
    backend = secretsmanager_backends[PLATFORM_ACCOUNT][REGION]
    backend.reset()  # the suite uses fixed names; this process's tests run one at a time
    provider = SecretsManagerProvider(h.clock, region=REGION, endpoint_url=url, credentials=CREDS)
    labels: dict[str, dict[str, str]] = {}  # ref -> custom staging label -> VersionId

    async def put(ref: str, value: bytes, version: str | None) -> None:
        name = secret_id(ref)
        client = await provider.client.get()
        try:
            version_id = (await client.put_secret_value(SecretId=name, SecretString=value.decode()))["VersionId"]
        except client.exceptions.ResourceNotFoundException:
            version_id = (await client.create_secret(Name=name, SecretString=value.decode()))["VersionId"]
        if version is not None:
            labels.setdefault(ref, {})[version] = version_id
        attached = (await client.describe_secret(SecretId=name)).get("VersionIdsToStages", {})
        for label, vid in labels.get(ref, {}).items():  # moto drops custom labels of older versions; AWS keeps them
            if label not in attached.get(vid, []):
                await client.update_secret_version_stage(SecretId=name, VersionStage=label, MoveToVersionId=vid)

    broken: set[str] = set()  # secret names whose reads fail like a throttled backend (not a missing secret)

    def throttle(params: dict[str, Any], **_: Any) -> tuple[SimpleNamespace, dict[str, Any]] | None:
        if json.loads(params["body"]).get("SecretId") not in broken:
            return None
        return SimpleNamespace(status_code=400), {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}

    (await provider.client.get()).meta.events.register("before-call.secrets-manager.GetSecretValue", throttle)

    def break_backend(ref: str) -> None:
        broken.add(secret_id(ref))

    try:
        yield SecretsKit(provider, h.clock, put, lambda: provider.backend_reads, break_backend)
    finally:
        await provider.aclose()


@register("credential_broker", "sts-fakes")
@asynccontextmanager
async def sts_fakes(h: Harness) -> AsyncIterator[BrokerKit]:
    registry = FakeConfigRegistry(tenants_config())
    sts = FakeStsClient(h.clock, caller_arn=API_ROLE)
    secrets = FakeSecretsProvider(h.clock)
    for tenant_id, external_id in EXTERNAL_IDS.items():
        role = f"arn:aws:iam::{TENANTS[tenant_id]}:role/AghooBedrockInvoke"
        sts.allow(role, external_id)
        secrets.put(f"aws-sm://platform/tenants/{tenant_id}/external-id", external_id)
    runtime = TaskGroupRuntime(h.clock)
    broker = StsBroker(
        config=registry, secrets=secrets, sts=sts, clock=h.clock, runtime=runtime, deployment="ci", pod_id="pod-a"
    )

    def fail(tenant_id: str, cause: str) -> None:
        if cause != "external_id_unavailable":
            raise ValueError(f"the sts-fakes kit injects external_id_unavailable only, not {cause}")
        secrets.fail(f"aws-sm://platform/tenants/{tenant_id}/external-id")

    await runtime.open()
    try:
        yield BrokerKit(
            broker, h.clock, registry.current().tenant_ref("t_a"), "t_unknown", fail, lambda _t: len(sts.calls)
        )
    finally:
        await broker.aclose()
        await runtime.close()


class ClockAlignedSts:
    """moto stamps `Expiration` with the wall clock; the suite drives a virtual clock. This shim re-bases the
    expiry on the broker's clock (same duration), so the broker's refresh windows can be exercised in virtual time."""

    def __init__(self, inner: AioStsClient, h: Harness) -> None:
        self.inner = inner
        self.h = h
        self.calls = 0

    async def assume_role(self, *, role_arn: str, external_id: str, session_name: str, duration_s: int) -> AssumedRole:
        self.calls += 1
        result = await self.inner.assume_role(
            role_arn=role_arn, external_id=external_id, session_name=session_name, duration_s=duration_s
        )
        creds = replace(result.creds, expires_at=self.h.clock.now() + timedelta(seconds=duration_s))
        return AssumeRoleResult(creds, result.assumed_role_arn)


@register("credential_broker", "sts-moto", marks=INTEGRATION)
@asynccontextmanager
async def sts_moto(h: Harness) -> AsyncIterator[BrokerKit]:
    from tests.fakes.sts_trust import trust_enforcer

    url = moto_url(h.request.config)
    await seed_external_ids(url)
    registry = FakeConfigRegistry(tenants_config())
    secrets = SecretsManagerProvider(h.clock, region=REGION, endpoint_url=url, credentials=CREDS)
    real = AioStsClient(region=REGION, endpoint_url=url, credentials=CREDS)
    trust_enforcer(await real.client.get(), caller_arn=API_ROLE)
    sts = ClockAlignedSts(real, h)
    runtime = TaskGroupRuntime(h.clock)
    broker = StsBroker(
        config=registry, secrets=secrets, sts=sts, clock=h.clock, runtime=runtime, deployment="ci", pod_id="pod-a"
    )
    broken: list[str] = []

    def fail(tenant_id: str, cause: str) -> None:
        if cause != "external_id_unavailable":
            raise ValueError(f"the sts-moto kit injects external_id_unavailable only, not {cause}")
        name = f"platform/tenants/{tenant_id}/external-id"
        secretsmanager_backends[PLATFORM_ACCOUNT][REGION].delete_secret(name, None, True)
        broken.append(tenant_id)

    await runtime.open()
    try:
        yield BrokerKit(broker, h.clock, registry.current().tenant_ref("t_a"), "t_unknown", fail, lambda _t: sts.calls)
    finally:
        await broker.aclose()
        await runtime.close()
        await secrets.aclose()
        await real.aclose()
        if broken:
            await seed_external_ids(url)


def _register_on_fake(db: FakeDatabase, token: str, who: Principal) -> None:
    parsed = parse_bearer(f"Bearer {token}")
    assert parsed is not None
    seed_identity(
        db.store,
        tenant_id=who.tenant_id,
        project_id=who.project_id,
        principal_id=who.principal_id,
        roles=sorted(who.roles),
    )
    db.store.seed(
        "api_key",
        {
            "key_id": parsed.key_id,
            "tenant_id": who.tenant_id,
            "project_id": who.project_id,
            "principal_id": who.principal_id,
            "key_hash": hash_key(RING_V2.current, parsed.key_id, parsed.secret.get_secret_value()),
            "pepper_version": RING_V2.current.version,
            "env": parsed.env,
            "scopes": sorted(who.scopes),
            "ip_allowlist": None,
            "expires_at": None,
            "revoked_at": None,
        },
    )


@register("auth", "apikey-fakedb")
@asynccontextmanager
async def apikey_fakedb(h: Harness) -> AsyncIterator[AuthKit]:
    db = FakeDatabase(h.clock)
    runtime = TaskGroupRuntime(h.clock)
    authn = ApiKeyAuthenticator(
        db=db,
        secrets=pepper_secrets(h.clock),
        limiter=FakeRateLimiter(h.clock),
        clock=h.clock,
        runtime=runtime,
        env="test",
    )

    async def register_key(token: str, who: Principal) -> None:
        _register_on_fake(db, token, who)

    await runtime.open()
    try:
        yield AuthKit(authn, MatrixAuthorizer(), register_key)
    finally:
        await runtime.close()


@register("auth", "apikey-postgres", marks=INTEGRATION)
@asynccontextmanager
async def apikey_postgres(h: Harness) -> AsyncIterator[AuthKit]:
    database = await scratch_db(h.request.config, str(h.fixture("noema_services").pg_admin_dsn))
    engine = await Engine.create(PoolSettings(dsn=database.app_dsn, max_size=2))
    admin = await connect(database.admin_dsn, expected_role=None)
    runtime = TaskGroupRuntime(h.clock)
    authn = ApiKeyAuthenticator(
        db=PgDatabase(engine, make_unit_of_work),
        secrets=pepper_secrets(h.clock),
        limiter=FakeRateLimiter(h.clock),
        clock=h.clock,
        runtime=runtime,
        env="test",
    )

    async def register_key(token: str, who: Principal) -> None:
        parsed = parse_bearer(f"Bearer {token}")
        assert parsed is not None
        await admin.execute(
            "INSERT INTO noema.tenant (id, aws_account_id) VALUES ($1, '111122223333') ON CONFLICT DO NOTHING",
            who.tenant_id,
        )
        await admin.execute(
            "INSERT INTO noema.project (tenant_id, id, name) VALUES ($1, $2, $2) ON CONFLICT DO NOTHING",
            who.tenant_id,
            who.project_id,
        )
        await admin.execute(
            "INSERT INTO noema.principal (tenant_id, project_id, id, subject, roles) VALUES ($1, $2, $3, $3, $4)"
            " ON CONFLICT (tenant_id, id) DO UPDATE SET roles = EXCLUDED.roles, disabled_at = NULL",
            who.tenant_id,
            who.project_id,
            who.principal_id,
            sorted(who.roles),
        )
        await admin.execute(
            "INSERT INTO auth.api_key (key_id, tenant_id, project_id, principal_id, key_hash, pepper_version, env,"
            " scopes) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (key_id) DO UPDATE SET"
            " tenant_id = EXCLUDED.tenant_id, project_id = EXCLUDED.project_id, principal_id = EXCLUDED.principal_id,"
            " key_hash = EXCLUDED.key_hash, pepper_version = EXCLUDED.pepper_version, env = EXCLUDED.env,"
            " scopes = EXCLUDED.scopes, revoked_at = NULL, expires_at = NULL",
            parsed.key_id,
            who.tenant_id,
            who.project_id,
            who.principal_id,
            hash_key(RING_V2.current, parsed.key_id, parsed.secret.get_secret_value()),
            RING_V2.current.version,
            parsed.env,
            sorted(who.scopes),
        )

    await runtime.open()
    try:
        yield AuthKit(authn, MatrixAuthorizer(), register_key)
    finally:
        await runtime.close()
        await admin.close()
        await engine.close()
