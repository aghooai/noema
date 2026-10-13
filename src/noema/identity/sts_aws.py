"""aiobotocore pieces of ADR-001: the production `StsClient` and broker-backed refreshable credentials.

`AioStsClient`: the platform region's STS endpoint, no retries (`max_attempts` 1), connect 1 s / read 2 s.
`broker_credentials(...)`: an `AioRefreshableCredentials` whose refresh callback is bound to one tenant: it calls
`broker.get(tenant_id)` and `assert_creds_for`, and its refresh windows equal the broker's (advisory 600 s, mandatory
300 s), so botocore never forces a refresh inside the broker's soft window (ADR-001 §4, SEC-STS-17). Clients built
on it get an explicit region and never resolve the default chain.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from aiobotocore.credentials import AioDeferredRefreshableCredentials

from noema.core.protocols.credentials import AwsCredentials, CredentialBroker, assert_creds_for
from noema.core.secret import SecretStr
from noema.identity.aws import PLATFORM_REGION, LazyClient, StaticCredentials, client_config
from noema.identity.sts import AssumeRoleResult

SOFT_REFRESH_S: Final = 600
HARD_REFRESH_S: Final = 300
STATIC_REFRESH: Final = timedelta(hours=1)  # static (ambient) credentials are re-read from the broker hourly


class AioStsClient:
    """Implements `StsClient` over one lazily created STS client (`client` exposes it, e.g. for the test
    `trust_enforcer`). `aclose()` closes it (lifespan)."""

    def __init__(
        self,
        *,
        region: str = PLATFORM_REGION,
        endpoint_url: str | None = None,
        credentials: StaticCredentials | None = None,
    ) -> None:
        self.client = LazyClient(
            "sts",
            region=region,
            endpoint_url=endpoint_url,
            credentials=credentials,
            config=client_config(connect_timeout_s=1.0, read_timeout_s=2.0, max_attempts=1),
        )

    async def start(self) -> None:
        """Lifespan hook: create the client now (its botocore data loads in a worker thread), not on first use."""
        await self.client.get()

    async def assume_role(
        self, *, role_arn: str, external_id: str, session_name: str, duration_s: int
    ) -> AssumeRoleResult:
        """One AssumeRole; `ClientError` / botocore errors propagate to the broker's classification."""
        client = await self.client.get()
        response = await client.assume_role(
            RoleArn=role_arn, RoleSessionName=session_name, ExternalId=external_id, DurationSeconds=duration_s
        )
        c = response["Credentials"]
        expiration = c["Expiration"]
        if isinstance(expiration, datetime) and expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=UTC)
        creds = AwsCredentials(
            access_key_id=c["AccessKeyId"],
            secret_access_key=SecretStr(c["SecretAccessKey"], register=True),
            session_token=SecretStr(c["SessionToken"], register=True),
            expires_at=expiration if isinstance(expiration, datetime) else None,
        )
        return AssumeRoleResult(creds=creds, assumed_role_arn=response["AssumedRoleUser"]["Arn"])

    async def aclose(self) -> None:
        await self.client.aclose()


class BrokerCredentials(AioDeferredRefreshableCredentials):
    """Deferred refreshable credentials with the broker's refresh windows (fetched on first use)."""

    def __init__(
        self,
        refresh_using: Callable[[], Awaitable[dict[str, Any]]],
        *,
        time_fetcher: Callable[[], datetime],
        advisory_s: int,
        mandatory_s: int,
    ) -> None:
        super().__init__(refresh_using=refresh_using, method="noema-broker", time_fetcher=time_fetcher)
        self._advisory_refresh_timeout = advisory_s
        self._mandatory_refresh_timeout = mandatory_s


def broker_credentials(
    broker: CredentialBroker,
    tenant_id: str,
    aws_account_id: str,
    *,
    time_fetcher: Callable[[], datetime],
    soft_refresh_s: int = SOFT_REFRESH_S,
    hard_refresh_s: int = HARD_REFRESH_S,
) -> BrokerCredentials:
    """Refreshable credentials for one tenant's aiobotocore clients (see the module docstring). `time_fetcher` is
    the process clock's `now` (aware UTC). The first fetch happens on first use."""

    async def refresh() -> dict[str, Any]:
        creds = await broker.get(tenant_id)
        assert_creds_for(creds, tenant_id, aws_account_id)
        expires = creds.aws.expires_at or creds.expires_at
        token = creds.aws.session_token
        return {
            "access_key": creds.aws.access_key_id,
            "secret_key": creds.aws.secret_access_key.get_secret_value(),
            "token": None if token is None else token.get_secret_value(),
            "expiry_time": (expires or time_fetcher() + STATIC_REFRESH).isoformat(),
        }

    return BrokerCredentials(refresh, time_fetcher=time_fetcher, advisory_s=soft_refresh_s, mandatory_s=hard_refresh_s)
