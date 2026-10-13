"""Credential fakes (spec-02 §3, ADR-001 §4-§6): `FakeBroker` and `FakeStsClient`.

`FakeBroker` mints deterministic fake STS credentials per tenant of the current config (account and home region
from `tenants.<id>`), caches them for `duration_s` with the ADR-001 hard-refresh window, counts mints per tenant,
and lets tests inject a fail-closed cause (`fail`) or hand out another tenant's credentials (`swap`, SEC-STS-08).
Fake key material matches the real shapes (`ASIA…` key ids, `bedrock-api-key-…` bearer tokens) so redaction tests
see realistic values; every secret is wrapped in `SecretStr`.
"""

from __future__ import annotations

import base64
import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

import anyio
import anyio.lowlevel
from botocore.exceptions import ClientError

from noema.core.clock import Clock
from noema.core.errors import NoemaError, TenantAccessCause, TenantAccessMisconfigured
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.credentials import AwsCredentials, MantleAuth, TenantCredentials
from noema.core.secret import SecretStr

HARD_REFRESH_S: Final = 300
BEARER_MAX_S: Final = 12 * 3600
_B32: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _material(seed: str, n: int, alphabet: str = _B32) -> str:
    digest = hashlib.sha256(seed.encode()).digest() * 8
    return "".join(alphabet[b % len(alphabet)] for b in digest[:n])


def fake_aws_credentials(seed: str, *, expires_at: datetime | None) -> AwsCredentials:
    """Deterministic fake STS credentials (`ASIA` + 16 chars; 40-char secret; 120-char session token)."""
    token = base64.b64encode(hashlib.sha512(f"tok:{seed}".encode()).digest() * 2).decode()[:120]
    return AwsCredentials(
        access_key_id="ASIA" + _material(f"akid:{seed}", 16),
        secret_access_key=SecretStr(
            base64.b64encode(hashlib.sha256(f"sk:{seed}".encode()).digest()).decode()[:40], register=True
        ),
        session_token=SecretStr(token, register=True),
        expires_at=expires_at,
    )


class FakeBroker:
    """Implements `CredentialBroker` for the tenants of `config.current()`."""

    def __init__(
        self,
        clock: Clock,
        config: ConfigRegistry,
        *,
        mantle_mode: Literal["sigv4", "bearer_short_term"] = "bearer_short_term",
        source: Literal["sts", "ambient"] = "sts",
        duration_s: int = 3600,
    ) -> None:
        self._clock = clock
        self._config = config
        self._mantle_mode = mantle_mode
        self._source: Literal["sts", "ambient"] = source
        self._duration_s = duration_s
        self._cache: dict[str, TenantCredentials] = {}
        self._failures: dict[str, TenantAccessCause] = {}
        self._swaps: dict[str, str] = {}
        self.mints: Counter[str] = Counter()
        self.gets: Counter[str] = Counter()
        self.invalidations: Counter[str] = Counter()
        self.delay_s = 0.0

    def fail(self, tenant_id: str, cause: TenantAccessCause | None) -> None:
        """Make `get(tenant_id)` raise `TenantAccessMisconfigured(cause)` (None clears it)."""
        if cause is None:
            self._failures.pop(tenant_id, None)
        else:
            self._failures[tenant_id] = cause

    def swap(self, tenant_id: str, other_tenant_id: str | None) -> None:
        """Hand out `other_tenant_id`'s credentials for `tenant_id` (a broken broker, SEC-STS-08)."""
        if other_tenant_id is None:
            self._swaps.pop(tenant_id, None)
        else:
            self._swaps[tenant_id] = other_tenant_id

    async def get(self, tenant_id: str) -> TenantCredentials:
        self.gets[tenant_id] += 1
        if self.delay_s:
            await self._clock.sleep(self.delay_s)
        else:
            await anyio.lowlevel.checkpoint()
        if tenant_id in self._failures:
            raise TenantAccessMisconfigured(self._failures[tenant_id])
        effective = self._swaps.get(tenant_id, tenant_id)
        cached = self._cache.get(effective)
        fresh_until = None if cached is None or cached.expires_at is None else cached.expires_at
        if (
            cached is not None
            and fresh_until is not None
            and self._clock.now() < fresh_until - timedelta(seconds=HARD_REFRESH_S)
        ):
            return cached
        creds = self._mint(effective)
        self._cache[effective] = creds
        return creds

    def invalidate(self, tenant_id: str) -> None:
        self.invalidations[tenant_id] += 1
        self._cache.pop(tenant_id, None)

    def _mint(self, tenant_id: str) -> TenantCredentials:
        try:
            ref = self._config.current().tenant_ref(tenant_id)
        except NoemaError:
            raise TenantAccessMisconfigured("role_not_allowlisted") from None
        self.mints[tenant_id] += 1
        now = self._clock.now()
        expires = now + timedelta(seconds=self._duration_s)
        seed = f"{tenant_id}:{self.mints[tenant_id]}"
        mantle = MantleAuth(mode="sigv4", bearer_token=None, expires_at=None)
        if self._mantle_mode == "bearer_short_term":
            bearer_exp = min(expires, now + timedelta(seconds=BEARER_MAX_S))
            token = "bedrock-api-key-" + base64.b64encode(hashlib.sha256(f"brk:{seed}".encode()).digest()).decode()
            mantle = MantleAuth(mode="bearer", bearer_token=SecretStr(token, register=True), expires_at=bearer_exp)
        return TenantCredentials(
            tenant_id=tenant_id,
            account_id=ref.aws_account_id,
            region=ref.home_region,
            aws=fake_aws_credentials(seed, expires_at=expires),
            mantle=mantle,
            expires_at=expires,
            source=self._source,
        )


@dataclass(frozen=True, slots=True)
class FakeAssumeRoleResult:
    """Structurally the identity `AssumeRoleResult(creds, assumed_role_arn)` (ADR-001 §5)."""

    creds: AwsCredentials
    assumed_role_arn: str


@dataclass(frozen=True, slots=True)
class TrustEntry:
    """One role's trust: the ExternalId it requires and the caller principals it trusts."""

    external_id: str
    trusted_principals: frozenset[str]


class FakeStsClient:
    """A unit-level `StsClient` (ADR-001 §5): `assume_role` checks a trust table keyed by role ARN (unknown role,
    wrong ExternalId or an untrusted caller -> `ClientError(AccessDenied)`), counts calls, and supports latency
    (`delay_s`, on the given clock) and injected failures (`fail_next(code)`)."""

    def __init__(self, clock: Clock, *, caller_arn: str = "arn:aws:iam::123456789012:role/noema-api") -> None:
        self._clock = clock
        self.caller_arn = caller_arn
        self.trust: dict[str, TrustEntry] = {}
        self.calls: list[dict[str, object]] = []
        self.delay_s = 0.0
        self._fail: list[str] = []

    def allow(self, role_arn: str, external_id: str, *principals: str) -> None:
        """Add a role to the trust table (default principal: `caller_arn`)."""
        self.trust[role_arn] = TrustEntry(external_id, frozenset(principals or (self.caller_arn,)))

    def fail_next(self, code: str = "Throttling", times: int = 1) -> None:
        """The next `times` calls raise `ClientError(code)` before any trust check."""
        self._fail.extend([code] * times)

    async def assume_role(
        self, *, role_arn: str, external_id: str, session_name: str, duration_s: int
    ) -> FakeAssumeRoleResult:
        self.calls.append(
            {"role_arn": role_arn, "external_id": external_id, "session_name": session_name, "duration_s": duration_s}
        )
        if self.delay_s:
            await self._clock.sleep(self.delay_s)
        else:
            await anyio.lowlevel.checkpoint()
        if self._fail:
            raise _client_error(self._fail.pop(0))
        entry = self.trust.get(role_arn)
        if entry is None or entry.external_id != external_id or self.caller_arn not in entry.trusted_principals:
            raise _client_error("AccessDenied")
        account = role_arn.split(":")[4]
        role_name = role_arn.rsplit("/", 1)[-1]
        expires = self._clock.now() + timedelta(seconds=duration_s)
        seed = f"{role_arn}:{len(self.calls)}"
        return FakeAssumeRoleResult(
            creds=fake_aws_credentials(seed, expires_at=expires),
            assumed_role_arn=f"arn:aws:sts::{account}:assumed-role/{role_name}/{session_name}",
        )


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"fake {code}"}}, "AssumeRole")
