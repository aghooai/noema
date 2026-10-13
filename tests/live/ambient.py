"""A live-smoke `CredentialBroker` over the developer's ambient IAM credentials (ADR-001 §7, dev + NOEMA_LIVE only).

The root conftest scrubs `AWS_*` and points the shared files at /dev/null, so the live suite reads the credentials
file explicitly (`NOEMA_LIVE_AWS_CREDENTIALS_FILE`, default `~/.aws/credentials`; profile `NOEMA_LIVE_AWS_PROFILE`,
default `default`) exactly once, calls `sts:GetCallerIdentity` once in an explicit region (not a model call), and
serves the one configured tenant whose `aws_account_id` is the caller account (`ambient_account_mismatch`
otherwise). Mantle auth is either a short-term bearer token presigned offline from those credentials
(`aws-bedrock-token-generator`, ADR-001 §6) or SigV4. Identity's `AmbientBroker` replaces this once it lands.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from aiobotocore.session import AioSession
from aws_bedrock_token_generator import BedrockTokenGenerator
from botocore.configloader import raw_config_parse
from botocore.credentials import Credentials

from noema.core.clock import Clock
from noema.core.errors import TenantAccessMisconfigured
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.credentials import AwsCredentials, MantleAuth, TenantCredentials
from noema.core.secret import SecretStr

REGION: Final = "us-east-1"  # explicit: the host's ~/.aws/config default region is invalid (03-env)
BEARER_TTL: Final = timedelta(hours=12)


class LiveAmbientBroker:
    """See the module docstring. `start()` before use; `mantle_mode` may be switched between calls (L3)."""

    def __init__(
        self,
        clock: Clock,
        config: ConfigRegistry,
        *,
        mantle_mode: Literal["bearer", "sigv4"] = "bearer",
        credentials_file: str | None = None,
        profile: str | None = None,
        sts_endpoint: str | None = None,
    ) -> None:
        self._clock = clock
        self._config = config
        self.mantle_mode: Literal["bearer", "sigv4"] = mantle_mode
        self._file = Path(credentials_file or os.environ.get("NOEMA_LIVE_AWS_CREDENTIALS_FILE", "~/.aws/credentials"))
        self._profile = profile or os.environ.get("NOEMA_LIVE_AWS_PROFILE", "default")
        self._sts_endpoint = sts_endpoint  # tests: moto
        self._aws: AwsCredentials | None = None
        self.caller_account: str | None = None

    async def start(self) -> None:
        section = raw_config_parse(str(self._file.expanduser())).get(self._profile) or {}
        akid, secret = section.get("aws_access_key_id"), section.get("aws_secret_access_key")
        if not akid or not secret:
            raise RuntimeError(f"no credentials for profile {self._profile!r} in {self._file}")
        token = section.get("aws_session_token")
        self._aws = AwsCredentials(
            access_key_id=akid,
            secret_access_key=SecretStr(secret, register=True),
            session_token=SecretStr(token, register=True) if token else None,
            expires_at=None,
        )
        session = AioSession()
        async with session.create_client(
            "sts",
            region_name=REGION,
            endpoint_url=self._sts_endpoint,
            aws_access_key_id=akid,
            aws_secret_access_key=secret,
            aws_session_token=token,
        ) as sts:
            self.caller_account = (await sts.get_caller_identity())["Account"]

    def _mantle(self, aws: AwsCredentials) -> MantleAuth:
        if self.mantle_mode == "sigv4":
            return MantleAuth(mode="sigv4", bearer_token=None, expires_at=None)
        token = aws.session_token.get_secret_value() if aws.session_token is not None else None
        creds = Credentials(aws.access_key_id, aws.secret_access_key.get_secret_value(), token)
        bearer = BedrockTokenGenerator().get_token(creds, REGION)
        expires: datetime = self._clock.now() + BEARER_TTL
        return MantleAuth(mode="bearer", bearer_token=SecretStr(bearer, register=True), expires_at=expires)

    async def get(self, tenant_id: str) -> TenantCredentials:
        if self._aws is None or self.caller_account is None:
            raise TenantAccessMisconfigured("sts_unavailable", "LiveAmbientBroker.start() was not awaited")
        ref = self._config.current().tenant_ref(tenant_id)
        if ref.aws_account_id != self.caller_account:
            raise TenantAccessMisconfigured("ambient_account_mismatch")
        return TenantCredentials(
            tenant_id=tenant_id,
            account_id=self.caller_account,
            region=ref.home_region,
            aws=self._aws,
            mantle=self._mantle(self._aws),
            expires_at=None,
            source="ambient",
        )

    def invalidate(self, tenant_id: str) -> None:
        """Static credentials: nothing to drop."""
