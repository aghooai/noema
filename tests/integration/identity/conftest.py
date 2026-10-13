"""Fixtures of the identity integration tests: in-process moto (STS, IAM, Secrets Manager) and a broker on it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from tests.fakes.config import FakeConfigRegistry
from tests.fakes.sts_trust import trust_enforcer
from tests.fakes.telemetry import FakeTelemetry
from tests.integration.identity.cli_rig import CliRig, open_cli_rig
from tests.integration.identity.moto_support import API_ROLE, CREDS, REGION, moto_url, seed_external_ids, tenants_config

from noema.config.registry import ConfigSnapshotImpl
from noema.core.clock import SystemClock
from noema.core.runtime import TaskGroupRuntime
from noema.identity.secrets import SecretsManagerProvider
from noema.identity.security import SecurityEvents
from noema.identity.sts import StsBroker
from noema.identity.sts_aws import AioStsClient


@pytest.fixture
def moto(request: pytest.FixtureRequest) -> str:
    return moto_url(request.config)


@dataclass
class MotoBroker:
    broker: StsBroker
    sts: AioStsClient
    secrets: SecretsManagerProvider
    telemetry: FakeTelemetry
    url: str


@pytest.fixture
def moto_broker(moto: str) -> Callable[..., Any]:
    """`async with moto_broker(caller_arn=..., config=..., secrets_url=...) as mb:` a real StsBroker on moto, with the
    `trust_enforcer` on its STS client (moto itself evaluates no trust policy)."""

    @asynccontextmanager
    async def make(
        *,
        caller_arn: str = API_ROLE,
        config: ConfigSnapshotImpl | None = None,
        secrets_url: str | None = None,
    ) -> AsyncIterator[MotoBroker]:
        await seed_external_ids(moto)
        clock = SystemClock()
        telemetry = FakeTelemetry(strict=True)
        secrets = SecretsManagerProvider(clock, region=REGION, endpoint_url=secrets_url or moto, credentials=CREDS)
        sts = AioStsClient(region=REGION, endpoint_url=moto, credentials=CREDS)
        trust_enforcer(await sts.client.get(), caller_arn=caller_arn)
        runtime = TaskGroupRuntime(clock)
        broker = StsBroker(
            config=FakeConfigRegistry(config or tenants_config()),
            secrets=secrets,
            sts=sts,
            clock=clock,
            runtime=runtime,
            deployment="it",
            pod_id="pod-1",
            telemetry=telemetry,
            security=SecurityEvents(telemetry=telemetry),
        )
        await runtime.open()
        try:
            yield MotoBroker(broker, sts, secrets, telemetry, moto)
        finally:
            await broker.aclose()
            await runtime.close()
            await sts.aclose()
            await secrets.aclose()

    return make


@pytest.fixture
async def cli(
    noema_services: Any, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[CliRig]:
    """A `noema-admin` rig on a fresh database (see cli_rig.py)."""
    async with open_cli_rig(noema_services, request.config, monkeypatch) as rig:
        yield rig
