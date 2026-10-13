"""An `StsBroker` on the fakes: FakeStsClient (trust table), FakeSecretsProvider (ExternalIds), FakeTelemetry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from tests.fakes.clock import FakeClock
from tests.fakes.config import FakeConfigRegistry, patched_example, snapshot_from
from tests.fakes.credentials import FakeStsClient
from tests.fakes.secrets import FakeSecretsProvider
from tests.fakes.telemetry import FakeTelemetry

from noema.config.models import PlatformCfg, TenantCfg
from noema.core.protocols.config import ConfigRegistry, ConfigSnapshot
from noema.core.runtime import TaskGroupRuntime
from noema.identity.security import SecurityEvents
from noema.identity.sts import StsBroker

ACME: Final = "t_acme"
ACME_ACCOUNT: Final = "111122223333"
ACME_ROLE: Final = f"arn:aws:iam::{ACME_ACCOUNT}:role/AghooBedrockInvoke"
ACME_REF: Final = "aws-sm://platform/tenants/t_acme/external-id"
ACME_EXTERNAL_ID: Final = "aghoo-t_acme-0123456789abcdefABCDEF0123456789"
BETA: Final = "t_beta"
BETA_ACCOUNT: Final = "222222222222"
CALLER: Final = "arn:aws:iam::123456789012:role/noema-api"


def beta_tenant(**overrides: Any) -> dict[str, Any]:
    return {
        "aws_account_id": BETA_ACCOUNT,
        "invoke_role_arn": f"arn:aws:iam::{BETA_ACCOUNT}:role/AghooBedrockInvoke",
        "external_id_ref": "aws-sm://platform/tenants/t_beta/external-id",
        "home_region": "us-east-1",
        "defaults": {"decision_profile": "lightweight-model-decide", "generation_profile": "sol-fast"},
        **overrides,
    }


class OverrideTenants:
    """A ConfigRegistry whose snapshot answers `tenant()` with replaced fields (configs the loader would reject)."""

    def __init__(self, inner: ConfigRegistry, tenants: Mapping[str, Mapping[str, Any]]) -> None:
        self._inner = inner
        self._tenants = tenants

    def current(self) -> Any:
        return _Snapshot(self._inner.current(), self._tenants)


@dataclass
class _Snapshot:
    inner: ConfigSnapshot
    tenants: Mapping[str, Mapping[str, Any]]

    @property
    def platform(self) -> PlatformCfg:
        return self.inner.platform

    def tenant(self, tenant_id: str) -> TenantCfg:
        cfg = self.inner.tenant(tenant_id)
        return replace(cfg, **self.tenants.get(tenant_id, {}))


@dataclass
class StsRig:
    clock: FakeClock
    sts: FakeStsClient
    secrets: FakeSecretsProvider
    telemetry: FakeTelemetry
    runtime: TaskGroupRuntime
    broker: StsBroker
    config: Any


def sts_rig(*, patch: Mapping[str, Any] | None = None, mantle: str = "bearer_short_term", config: Any = None) -> StsRig:
    clock = FakeClock()
    body = patched_example({"platform": {"credentials": {"mantle_auth": mantle}}, **(patch or {})})
    registry = config if config is not None else FakeConfigRegistry(snapshot_from(body))
    sts = FakeStsClient(clock, caller_arn=CALLER)
    sts.allow(ACME_ROLE, ACME_EXTERNAL_ID)
    secrets = FakeSecretsProvider(clock, {ACME_REF: ACME_EXTERNAL_ID})
    telemetry = FakeTelemetry(strict=True)
    runtime = TaskGroupRuntime(clock)
    broker = StsBroker(
        config=registry,
        secrets=secrets,
        sts=sts,
        clock=clock,
        runtime=runtime,
        deployment="test",
        pod_id="pod-1",
        telemetry=telemetry,
        security=SecurityEvents(telemetry=telemetry),
    )
    return StsRig(clock, sts, secrets, telemetry, runtime, broker, registry)
