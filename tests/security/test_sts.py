"""SE4 (06 §9 "Cross-account access"; ADR-001 SEC-STS-05, -07, -08, -10, -12, -14, -15, the qa-security rows):
the REAL `StsBroker` (in-process moto + the `trust_enforcer`, since moto evaluates no trust policy) feeding the REAL
Converse and Mantle adapters, which talk to the in-process wire mock. A wrong ExternalId, a non-allowlisted role
ARN, swapped tenant credentials and a confused-deputy caller are all denied before any wire byte (the mock's request
log stays empty); ambient AWS_* variables of a third account never sign a tenant call; the captured logs and spans
hold no STS key id, session token or Bedrock token. AWS-side enforcement and CloudTrail are Phase 0 (not run)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.config import FakeConfigRegistry
from tests.fakes.sts_trust import trust_enforcer
from tests.fakes.telemetry import FakeTelemetry
from tests.integration.identity.moto_support import (
    API_ROLE,
    CREDS,
    EXTERNAL_IDS,
    REGION,
    TENANTS,
    moto_url,
    put_secret,
    secret_name,
    seed_external_ids,
    tenants_config,
)
from tests.security.secret_patterns import scan

from noema.adapters.converse import ConverseAdapter
from noema.adapters.mantle import MantleAdapter
from noema.adapters.registry import AdapterRegistryImpl
from noema.adapters.resilience.retry_budget import SlidingRetryBudget
from noema.composition import build
from noema.config.registry import ConfigSnapshotImpl
from noema.core.clock import Deadline, SystemClock
from noema.core.errors import ErrorKind, ForbiddenInEnvironment
from noema.core.protocols.adapters import AdapterError, ModelRequest
from noema.core.runtime import TaskGroupRuntime
from noema.core.values import Effort, Message, TextBlock
from noema.identity.secrets import SecretsManagerProvider
from noema.identity.security import SecurityEvents
from noema.identity.sts import StsBroker
from noema.identity.sts_aws import AioStsClient

mockbedrock = pytest.importorskip("tools.mockbedrock")
pytestmark = [pytest.mark.integration]
AQ = '<app_question>{"instructions": "Intent?", "options": [{"letter": "A", "value": "x", "description": null}]}'
SCENARIO = {"scenario": "sec-sts", "seed": 1, "mantle_auth": "bearer_short_term",
            "defaults": {"latency": {"fixed_ms": 0}},
            "rules": [{"match": {"op": "converse"}, "answer": {"dist": {"x": 1.0}}},
                      {"match": {"op": "chat"}, "answer": {"dist": {"x": 1.0}}}]}  # fmt: skip


@dataclass
class Rig:
    mock: Any
    registry: AdapterRegistryImpl
    broker: StsBroker
    config: FakeConfigRegistry
    telemetry: FakeTelemetry

    async def call(self, alias: str, tenant: str) -> Any:
        clock = SystemClock()
        effort: Effort | None = "low" if alias == "haiku-5.5" else None
        messages = (Message("user", (TextBlock(AQ),)),)
        request = ModelRequest(alias, ("You classify.",), messages, 32, "decision_member", "none", effort=effort)
        ref = self.config.current().tenant_ref(tenant)
        deadline = Deadline(clock.monotonic() + 15.0, clock)
        return await self.registry.for_model(alias).complete(request, tenant=ref, deadline=deadline)


class SwappedBroker:
    """SEC-STS-08: hands out `t_b`'s real credentials for every tenant (a broker bug or a poisoned cache)."""

    def __init__(self, real: StsBroker) -> None:
        self.real = real

    async def get(self, tenant_id: str) -> Any:
        return await self.real.get("t_b")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


@asynccontextmanager
async def rig(
    request: pytest.FixtureRequest, *, config: ConfigSnapshotImpl | None = None, swap: bool = False
) -> AsyncIterator[Rig]:
    url = moto_url(request.config)
    await seed_external_ids(url)
    clock = SystemClock()
    telemetry = FakeTelemetry(strict=True)
    secrets = SecretsManagerProvider(clock, region=REGION, endpoint_url=url, credentials=CREDS)
    sts = AioStsClient(region=REGION, endpoint_url=url, credentials=CREDS)
    trust_enforcer(await sts.client.get(), caller_arn=API_ROLE)
    runtime = TaskGroupRuntime(clock)
    registry_cfg = FakeConfigRegistry(config or tenants_config())
    security = SecurityEvents(telemetry=telemetry)
    broker = StsBroker(config=registry_cfg, secrets=secrets, sts=sts, clock=clock, runtime=runtime, deployment="sec",
                       pod_id="pod-1", telemetry=telemetry, security=security)  # fmt: skip
    await runtime.open()
    async with mockbedrock.MockBedrock(scenario=SCENARIO) as mock:
        common: dict[str, Any] = {
            "config": registry_cfg,
            "broker": SwappedBroker(broker) if swap else broker,
            "clock": clock,
            "retry_budget": SlidingRetryBudget(clock),
            "telemetry": telemetry,
            "security": security,
            "endpoint_overrides": {"bedrock-runtime": mock.endpoint_url, "mantle": mock.mantle_url},
        }
        converse, mantle = ConverseAdapter(**common), MantleAdapter(**common)
        adapters: dict[str, Any] = {"bedrock-converse": converse, "bedrock-mantle-chat": mantle}
        try:
            yield Rig(mock, AdapterRegistryImpl(registry_cfg, adapters), broker, registry_cfg, telemetry)
        finally:
            await converse.aclose()
            await mantle.aclose()
            await broker.aclose()
            await runtime.close()
            await sts.aclose()
            await secrets.aclose()


async def denied(r: Rig, alias: str, tenant: str) -> str:
    with pytest.raises(AdapterError) as err:
        await r.call(alias, tenant)
    assert err.value.error.kind is ErrorKind.TENANT_ACCESS, err.value.error
    return str(err.value.error.cause)


@pytest.mark.parametrize("alias", ["haiku-5.5", "deepseek-3.2"])
async def test_sec_sts_05_wrong_external_id_never_reaches_the_wire(request: pytest.FixtureRequest, alias: str) -> None:
    url = moto_url(request.config)
    await put_secret(url, secret_name("t_b"), "aghoo-t_b-" + "X" * 32)  # right prefix, wrong random part
    try:
        async with rig(request) as r:
            causes = {await denied(r, alias, "t_b") for _ in range(50)}
            assert causes == {"access_denied"}
            assert r.broker.assume_role_calls == 1  # negative cache: one STS call for 50 requests
            assert r.mock.requests() == []
    finally:
        await put_secret(url, secret_name("t_b"), EXTERNAL_IDS["t_b"])


async def test_sec_sts_07_non_allowlisted_role_arn(request: pytest.FixtureRequest) -> None:
    config = tenants_config(t_a={"invoke_role_arn": f"arn:aws:iam::{TENANTS['t_a']}:role/Admin"})
    async with rig(request, config=config) as r:
        assert await denied(r, "haiku-5.5", "t_a") == "role_not_allowlisted"
        assert r.broker.assume_role_calls == 0 and r.mock.requests() == []


async def test_sec_sts_07_role_in_another_account_is_refused_by_the_config_checker() -> None:
    with pytest.raises(Exception, match=r"(?i)invoke_role_arn|account"):
        tenants_config(t_a={"invoke_role_arn": f"arn:aws:iam::{TENANTS['t_b']}:role/AghooBedrockInvoke"})


@pytest.mark.parametrize("alias", ["haiku-5.5", "deepseek-3.2"])
async def test_sec_sts_08_swapped_credentials_fail_before_any_wire_byte(
    request: pytest.FixtureRequest, alias: str
) -> None:
    async with rig(request, swap=True) as r:
        assert await denied(r, alias, "t_a") == "account_mismatch"
        assert r.mock.requests() == []
        assert r.telemetry.counter("aghoo_security_violations_total", kind="account_mismatch") >= 1


async def test_sec_sts_10_confused_deputy(request: pytest.FixtureRequest) -> None:
    async with rig(request) as r:  # t_c trusts only aghoo-jobs; this broker runs as noema-api
        assert await denied(r, "haiku-5.5", "t_c") == "access_denied"
        assert r.mock.requests() == []


async def test_sec_sts_15_ambient_aws_variables_never_sign_a_tenant_call(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Also SEC-STS-12: the run's logs (DEBUG, botocore included) and spans hold no key id, token or secret."""
    ambient = "AKIAZZZZZZZZZZZZZZZZ"
    config = tmp_path / "config"
    config.write_text(f"[profile third-account]\naws_access_key_id = {ambient}\naws_secret_access_key = {'y' * 40}\n")
    caplog.set_level(logging.DEBUG)
    async with rig(request) as r:  # seeded first; the adapters build their clients at the first call
        for name, value in (
            ("AWS_ACCESS_KEY_ID", ambient),
            ("AWS_SECRET_ACCESS_KEY", "z" * 40),
            ("AWS_PROFILE", "third-account"),
            ("AWS_CONFIG_FILE", str(config)),
        ):
            monkeypatch.setenv(name, value)  # fmt: skip
        await r.call("haiku-5.5", "t_a")
        await r.call("deepseek-3.2", "t_a")
        creds = await r.broker.get("t_a")
        records = r.mock.requests()
        spans = repr([(s.name, s.attributes) for s in r.telemetry.spans])
    assert [rec["akid"] for rec in records] == [creds.aws.access_key_id] * 2, records
    assert ambient not in repr(records)
    secret = creds.aws.secret_access_key.get_secret_value()
    token = creds.aws.session_token.get_secret_value() if creds.aws.session_token else ""
    bearer = creds.mantle.bearer_token.get_secret_value() if creds.mantle.bearer_token else ""
    values = {"secret": secret, "token": token, "bearer": bearer, "external_id": EXTERNAL_IDS["t_a"]}
    logs = "\n".join(rec.getMessage() for rec in caplog.records if rec.name.startswith("noema."))
    kinds = ("aws_secret", "aws_session_token", "sigv4_param", "bedrock_api_key", "external_id")
    assert scan("noema-logs", logs, values, kinds=kinds) == []
    assert scan("spans", spans, values) == []


def test_sec_sts_14_endpoint_overrides_refuse_a_prod_build() -> None:
    environ = {"NOEMA_AWS_ENDPOINT_OVERRIDES": '{"sts": "http://127.0.0.1:1"}', "NOEMA_REDIS_URL": "redis://r:6379/0"}
    with pytest.raises(ForbiddenInEnvironment):
        build("prod", environ=environ)
