"""`AmbientBroker` (ADR-001 §7): dev and live-smoke credentials from the ambient chain. Never staging or prod.

- The constructor raises `ForbiddenInEnvironment` unless NOEMA_ENV is dev or test (composition also checks the
  `broker.ambient` component kind).
- `start()` resolves the default credential chain exactly once (the only component that does) and calls
  `sts:GetCallerIdentity` once with an explicit region, recording the caller account.
- `get(tenant_id)` raises `ambient_account_mismatch` unless the tenant's `aws_account_id` is the caller account.
- Mantle: `bearer` with `AWS_BEARER_TOKEN_BEDROCK` when set (read once, never logged); otherwise the config's
  `platform.credentials.mantle_auth` (a short-term token presigned from the ambient credentials, or SigV4).
- `source="ambient"` on every credential (spans show `aghoo.credentials.source`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final

import anyio
from aiobotocore.session import get_session
from botocore.exceptions import BotoCoreError, ClientError

from noema.core.clock import Clock
from noema.core.errors import ForbiddenInEnvironment, NoemaError, TenantAccessMisconfigured
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.credentials import AwsCredentials, MantleAuth, TenantCredentials
from noema.core.secret import SecretStr
from noema.core.settings import Env
from noema.identity.aws import LazyClient, StaticCredentials, client_config
from noema.identity.mantle import SIGV4, short_term_bearer
from noema.identity.security import SecurityEvents

BEARER_ENV: Final = "AWS_BEARER_TOKEN_BEDROCK"
ALLOWED_ENVS: Final = frozenset({"dev", "test"})
SHORT_TERM_RENEW: Final = timedelta(minutes=5)


class AmbientBroker:
    """Implements `CredentialBroker` from ambient credentials (see the module docstring). `credentials` replaces
    the default chain (tests against moto); `environ` defaults to `os.environ` (read once, at `start()`)."""

    def __init__(
        self,
        *,
        config: ConfigRegistry,
        clock: Clock,
        env: Env,
        region: str,
        endpoint_url: str | None = None,
        credentials: StaticCredentials | None = None,
        environ: Mapping[str, str] | None = None,
        security: SecurityEvents | None = None,
    ) -> None:
        if env not in ALLOWED_ENVS:
            raise ForbiddenInEnvironment(f"AmbientBroker is not allowed with NOEMA_ENV={env} (ADR-001 §7)")
        self._config = config
        self._clock = clock
        self._region = region
        self._endpoint_url = endpoint_url
        self._static = credentials
        self._environ = environ
        self._security = security or SecurityEvents()
        self._lock = anyio.Lock()
        self._chain: Any = None  # botocore credentials of the ambient chain (refreshable or static)
        self._account: str | None = None
        self._bearer: SecretStr | None = None
        self._short: dict[str, MantleAuth] = {}  # presigned short-term tokens per region

    @property
    def caller_account(self) -> str | None:
        """The account of GetCallerIdentity (None before `start()`)."""
        return self._account

    async def start(self) -> None:
        """Resolve the chain once and record the caller account. Raises `TenantAccessMisconfigured`
        (`sts_unavailable` / `access_denied`) when either step fails."""
        async with self._lock:
            if self._account is not None:
                return
            environ = os.environ if self._environ is None else self._environ
            token = environ.get(BEARER_ENV)
            self._bearer = SecretStr(token, register=True) if token else None
            if self._static is None:
                self._chain = await get_session().get_credentials()
                if self._chain is None:
                    raise TenantAccessMisconfigured("access_denied", "No ambient AWS credentials were found.")
            sts = LazyClient(
                "sts",
                region=self._region,
                endpoint_url=self._endpoint_url,
                credentials=self._static or await self._frozen_static(),
                config=client_config(connect_timeout_s=1.0, read_timeout_s=2.0, max_attempts=1),
            )
            try:
                identity = await (await sts.get()).get_caller_identity()
            except ClientError:
                raise TenantAccessMisconfigured("access_denied") from None
            except (BotoCoreError, OSError):
                raise TenantAccessMisconfigured("sts_unavailable") from None
            finally:
                await sts.aclose()
            self._account = str(identity["Account"])

    async def get(self, tenant_id: str) -> TenantCredentials:
        """Ambient credentials for a tenant mapped to the caller account (`start()` runs first if needed)."""
        if self._account is None:
            await self.start()
        snapshot = self._config.current()
        try:
            tenant = snapshot.tenant(tenant_id)
        except NoemaError:
            raise TenantAccessMisconfigured("role_not_allowlisted") from None
        account = self._account
        if account is None or tenant.aws_account_id != account:
            self._security.emit("ambient_account_mismatch", tenant_id=tenant_id)
            raise TenantAccessMisconfigured("ambient_account_mismatch")
        static = self._static or await self._frozen_static()
        aws = AwsCredentials(
            access_key_id=static.access_key_id,
            secret_access_key=static.secret_access_key,
            session_token=static.session_token,
            expires_at=None,
        )
        if self._bearer is not None:
            mantle = MantleAuth(mode="bearer", bearer_token=self._bearer, expires_at=None)
        elif snapshot.platform.credentials.mantle_auth == "bearer_short_term":
            mantle = self._short_term(aws, tenant.home_region)
        else:
            mantle = SIGV4
        return TenantCredentials(
            tenant_id=tenant_id,
            account_id=account,
            region=tenant.home_region,
            aws=aws,
            mantle=mantle,
            expires_at=None,
            source="ambient",
        )

    def invalidate(self, tenant_id: str) -> None:
        """No-op: ambient credentials are not cached per tenant."""

    async def aclose(self) -> None:
        """Lifespan hook (nothing to release)."""

    def _short_term(self, aws: AwsCredentials, region: str) -> MantleAuth:
        now = self._clock.now()
        cached = self._short.get(region)
        if cached is not None and cached.expires_at is not None and now < cached.expires_at - SHORT_TERM_RENEW:
            return cached
        mantle = self._short[region] = short_term_bearer(aws, region, now=now, expires_at=None)
        return mantle

    async def _frozen_static(self) -> StaticCredentials:
        frozen = await self._chain.get_frozen_credentials()
        token = frozen.token
        return StaticCredentials(
            frozen.access_key,
            SecretStr(frozen.secret_key, register=True),
            None if token is None else SecretStr(token, register=True),
        )
