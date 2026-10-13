"""`StsBroker` / `AmbientBroker` against in-process moto with the `trust_enforcer` (ADR-001 tests): SEC-STS-01,
-05, -06, -07, -09, -10, -11, -12 (identity's part), -14."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest
from moto.sts.models import sts_backends
from tests.fakes.config import FakeConfigRegistry
from tests.integration.identity.moto_support import (
    CREDS,
    EXTERNAL_IDS,
    JOBS_ROLE,
    PLATFORM_ACCOUNT,
    REGION,
    TENANTS,
    put_secret,
    role_arn,
    secret_name,
    tenants_config,
)

from noema.composition import process_settings
from noema.core.clock import SystemClock
from noema.core.errors import ForbiddenInEnvironment, TenantAccessMisconfigured
from noema.core.settings import Env
from noema.identity.ambient import AmbientBroker
from noema.telemetry import redact

pytestmark = pytest.mark.integration


async def cause(mb: Any, tenant_id: str) -> str:
    with pytest.raises(TenantAccessMisconfigured) as err:
        await mb.broker.get(tenant_id)
    assert err.value.spec.status == 503 and not err.value.spec.retryable
    assert err.value.details == {"cause": err.value.cause}
    return err.value.cause


async def test_happy_path_on_moto(moto_broker: Callable[..., Any]) -> None:
    """SEC-STS-01: the exact ExternalId reaches STS (moto records it), 3600 s, the session-name pattern, account."""
    async with moto_broker() as mb:
        creds = await mb.broker.get("t_a")
        again = await mb.broker.get("t_a")
    assumed = sts_backends[TENANTS["t_a"]]["aws"].assumed_roles[-1]
    assert assumed.external_id == EXTERNAL_IDS["t_a"]
    assert assumed.role_arn == role_arn("t_a") and assumed.session_name == "noema-it-pod-1"
    assert creds.expires_at is not None
    assert abs((creds.expires_at - assumed.expiration.replace(tzinfo=creds.expires_at.tzinfo)).total_seconds()) < 1
    assert 3590 <= (creds.expires_at - SystemClock().now()).total_seconds() <= 3600
    assert creds.account_id == TENANTS["t_a"] and creds.region == REGION and creds.source == "sts"
    assert creds.aws.access_key_id.startswith("ASIA") and again.aws.access_key_id == creds.aws.access_key_id
    assert mb.broker.assume_role_calls == 1
    assert creds.mantle.mode == "bearer" and creds.mantle.bearer_token is not None


async def test_wrong_external_id_is_denied(moto_broker: Callable[..., Any], moto: str) -> None:
    """SEC-STS-05: right prefix, wrong random part; the trust policy denies -> access_denied; exactly 1 STS call
    across 50 requests within 10 s (negative cache)."""
    await put_secret(moto, secret_name("t_b"), "aghoo-t_b-" + "X" * 32)
    try:
        async with moto_broker() as mb:
            for _ in range(50):
                assert await cause(mb, "t_b") == "access_denied"
            assert mb.broker.assume_role_calls == 1
            assert mb.telemetry.counter("aghoo_tenant_access_failures_total", tenant="t_b", cause="access_denied") == 1
    finally:
        await put_secret(moto, secret_name("t_b"), EXTERNAL_IDS["t_b"])


async def test_external_id_ref_of_another_tenant(moto_broker: Callable[..., Any]) -> None:
    """SEC-STS-06: t_b's ref points at t_a's secret -> external_id_invalid; 0 STS calls."""
    config = tenants_config(t_b={"external_id_ref": f"aws-sm://{secret_name('t_a')}"})
    async with moto_broker(config=config) as mb:
        assert await cause(mb, "t_b") == "external_id_invalid"
        assert mb.broker.assume_role_calls == 0


async def test_non_allowlisted_role(moto_broker: Callable[..., Any]) -> None:
    """SEC-STS-07: role Admin in the right account -> role_not_allowlisted; 0 STS calls (AghooBedrockInvoke in
    another account is already rejected by the config checker; the unit test covers the broker's own check)."""
    config = tenants_config(t_a={"invoke_role_arn": f"arn:aws:iam::{TENANTS['t_a']}:role/Admin"})
    async with moto_broker(config=config) as mb:
        assert await cause(mb, "t_a") == "role_not_allowlisted"
        assert mb.broker.assume_role_calls == 0


async def test_secrets_manager_down(moto_broker: Callable[..., Any]) -> None:
    """SEC-STS-09: Secrets Manager unreachable -> external_id_unavailable; no fallback credentials."""
    async with moto_broker(secrets_url="http://127.0.0.1:1") as mb:
        assert await cause(mb, "t_a") == "external_id_unavailable"
        assert mb.broker.assume_role_calls == 0


async def test_confused_deputy(moto_broker: Callable[..., Any]) -> None:
    """SEC-STS-10: t_c's trust policy lists only aghoo-jobs; the API's data-plane principal is denied."""
    async with moto_broker() as mb:
        assert await cause(mb, "t_c") == "access_denied"
    async with moto_broker(caller_arn=JOBS_ROLE) as jobs:
        assert (await jobs.broker.get("t_c")).account_id == TENANTS["t_c"]


async def test_ambient_broker(moto: str) -> None:
    """SEC-STS-11: the constructor refuses prod/staging; on moto GetCallerIdentity = 123456789012 and a tenant in
    111111111111 -> ambient_account_mismatch; a tenant mapped to the caller account gets ambient credentials."""
    refused: tuple[Env, ...] = ("prod", "staging")
    for env in refused:
        with pytest.raises(ForbiddenInEnvironment):
            AmbientBroker(config=FakeConfigRegistry(tenants_config()), clock=SystemClock(), env=env, region=REGION)
    config = tenants_config(
        t_c={
            "aws_account_id": PLATFORM_ACCOUNT,
            "invoke_role_arn": f"arn:aws:iam::{PLATFORM_ACCOUNT}:role/AghooBedrockInvoke",
        }
    )
    broker = AmbientBroker(
        config=FakeConfigRegistry(config),
        clock=SystemClock(),
        env="dev",
        region=REGION,
        endpoint_url=moto,
        credentials=CREDS,
        environ={},
    )
    await broker.start()
    assert broker.caller_account == PLATFORM_ACCOUNT
    with pytest.raises(TenantAccessMisconfigured) as err:
        await broker.get("t_a")
    assert err.value.cause == "ambient_account_mismatch"
    creds = await broker.get("t_c")
    assert creds.source == "ambient" and creds.account_id == PLATFORM_ACCOUNT and creds.expires_at is None
    assert creds.mantle.mode == "bearer" and creds.mantle.bearer_token is not None  # short-term from ambient creds
    with_key = AmbientBroker(
        config=FakeConfigRegistry(config),
        clock=SystemClock(),
        env="test",
        region=REGION,
        endpoint_url=moto,
        credentials=CREDS,
        environ={"AWS_BEARER_TOKEN_BEDROCK": "ABSK" + "k" * 40},
    )
    token = (await with_key.get("t_c")).mantle.bearer_token
    assert token is not None and token.get_secret_value() == "ABSK" + "k" * 40


async def test_no_credential_material_in_logs_or_spans(
    moto_broker: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    """SEC-STS-12 (identity's part): with log and span capture, no AWS secret, session token, bearer token or
    ExternalId appears: never in identity's own records, and nowhere at the production level (INFO) once the
    redactor ran (third-party DEBUG logging, e.g. botocore wire dumps, is off in production)."""
    caplog.set_level(logging.DEBUG)
    async with moto_broker() as mb:
        creds = await mb.broker.get("t_a")
        await cause(mb, "t_c")
    values = [
        creds.aws.secret_access_key.get_secret_value(),
        creds.aws.session_token.get_secret_value() if creds.aws.session_token else "",
        creds.mantle.bearer_token.get_secret_value() if creds.mantle.bearer_token else "",
        *EXTERNAL_IDS.values(),
    ]
    own = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("noema."))
    own += repr([s.attributes for s in mb.telemetry.spans])
    emitted = redact("\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.INFO))
    for value in values:
        assert value and value not in own and value not in emitted


def test_endpoint_overrides_refused_outside_dev_test() -> None:
    """SEC-STS-14: NOEMA_AWS_ENDPOINT_OVERRIDES with NOEMA_ENV=prod refuses startup (composition.process_settings)."""
    environ = {"NOEMA_AWS_ENDPOINT_OVERRIDES": '{"sts": "http://127.0.0.1:1"}', "NOEMA_REDIS_URL": "redis://r:6379/0"}
    with pytest.raises(ForbiddenInEnvironment):
        process_settings("prod", environ)
    assert process_settings("test", environ).endpoint_overrides == {"sts": "http://127.0.0.1:1"}
