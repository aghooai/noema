"""Tenant credentials and secrets (spec-02 §3; exactly ADR-001 §4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from noema.core.errors import TenantAccessMisconfigured
from noema.core.secret import SecretStr


@dataclass(frozen=True, slots=True)
class AwsCredentials:
    """Assumed-role (or, dev only, ambient) AWS credentials; `expires_at` is None only for AmbientBroker static
    credentials."""

    access_key_id: str
    secret_access_key: SecretStr
    session_token: SecretStr | None
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class MantleAuth:
    """How Mantle calls authenticate; `bearer_token` is set iff `mode == "bearer"`."""

    mode: Literal["sigv4", "bearer"]
    bearer_token: SecretStr | None
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class TenantCredentials:
    """Everything an adapter needs to call Bedrock for one tenant. `account_id` is parsed from
    AssumedRoleUser.Arn (StsBroker) or GetCallerIdentity (AmbientBroker); `region` is the tenant's home_region,
    never taken from ~/.aws/config."""

    tenant_id: str
    account_id: str
    region: str
    aws: AwsCredentials
    mantle: MantleAuth
    expires_at: datetime | None
    source: Literal["sts", "ambient"]


class CredentialBroker(Protocol):
    """One cached STS session per (pod, tenant) with single-flight refresh (ADR-001 §1)."""

    async def get(self, tenant_id: str) -> TenantCredentials:
        """The tenant's current credentials (a memory lookup when warm). Raises `TenantAccessMisconfigured`."""
        ...

    def invalidate(self, tenant_id: str) -> None:
        """Sync: drop the cached session (e.g. after ExpiredTokenException)."""
        ...


def assert_creds_for(creds: TenantCredentials, tenant_id: str, aws_account_id: str) -> None:
    """Called by every adapter immediately before signing or sending any byte. Raises
    `TenantAccessMisconfigured("account_mismatch")` when the credentials belong to another tenant or account."""
    if creds.tenant_id != tenant_id or creds.account_id != aws_account_id:
        raise TenantAccessMisconfigured(cause="account_mismatch")


class SecretsProvider(Protocol):
    """Platform and tenant secrets (Secrets Manager in staging/prod; env or moto in dev/test)."""

    async def get(self, ref: str, *, version: str | None = None) -> bytes:
        """The secret named `ref` ("aws-sm://..." or a "noema/<name>" platform secret); `version` None = current
        (the api-key pepper is read by version, IMPL-008). Cached in memory for 300 s. Raises
        `core.errors.SecretMissing` (a `NoemaError(internal_error)`) when the secret or the version does not exist,
        and a plain `NoemaError(internal_error)` when the backend cannot be read."""
        ...
