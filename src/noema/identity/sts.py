"""Tenant credentials through STS (ADR-001, IMPL-007): `StsBroker` implements `CredentialBroker`.

One cached STS session per (pod, tenant), memory only: `DurationSeconds` 3600; a soft refresh in the background from
`expires_at - 600 s` (callers keep the cached credentials; a failure backs off 10 s and never fails callers); a hard
refresh from `expires_at - 300 s` (credentials in that window are never handed out; callers wait on one single-flight
refresh per tenant). A cached session whose account, invoke role or home region no longer matches the active
config is a miss (an account move takes effect on the next call). Every failure is fail-closed
`TenantAccessMisconfigured(cause)` (503, not retryable), negative-cached 10 s, counted
(`aghoo_tenant_access_failures_total{tenant,cause}`) and emitted as a security event.

Refresh steps: the local role-ARN allowlist (`role_not_allowlisted`), the ExternalId from the `SecretsProvider`
(`external_id_unavailable`) and its format (`external_id_invalid`), one AssumeRole without retries (`access_denied`
or `sts_unavailable`), the account of `AssumedRoleUser.Arn` (`account_mismatch`), then the Mantle auth of the
snapshot's `platform.credentials.mantle_auth`.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

import anyio
from botocore.exceptions import ClientError

from noema.core.clock import Clock, deadline_scope
from noema.core.errors import NoemaError, TenantAccessCause, TenantAccessMisconfigured
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.credentials import AwsCredentials, SecretsProvider, TenantCredentials
from noema.core.protocols.runtime import ProcessRuntime
from noema.core.protocols.telemetry import Telemetry
from noema.core.secret import SecretStr
from noema.identity.mantle import SIGV4, short_term_bearer
from noema.identity.security import SecurityEvents

ROLE_ARN: Final = re.compile(r"arn:aws:iam::(\d{12}):role/AghooBedrockInvoke")
ASSUMED_ROLE_ARN: Final = re.compile(r"arn:aws:sts::(\d{12}):assumed-role/[\w+=,.@-]+/[\w+=,.@-]+")
_SESSION_CHARS: Final = re.compile(r"[^\w+=,.@-]")
ACCESS_DENIED_CODES: Final = frozenset(
    {"AccessDenied", "InvalidClientTokenId", "ExpiredToken", "RegionDisabledException", "MalformedPolicyDocument"}
)
STS_TIMEOUT_S: Final = 3.5  # connect 1 s + read 2 s (the client config) plus slack, on the broker's clock
INVALIDATE_MIN_AGE_S: Final = 60.0

log = logging.getLogger("noema.identity.sts")


@dataclass(frozen=True, slots=True)
class AssumeRoleResult:
    """One AssumeRole answer: the session credentials and `AssumedRoleUser.Arn`."""

    creds: AwsCredentials
    assumed_role_arn: str


class AssumedRole(Protocol):
    """What `StsClient.assume_role` returns: `AssumeRoleResult` or any value with the same read-only fields (e.g.
    `tests.fakes.credentials.FakeAssumeRoleResult`)."""

    @property
    def creds(self) -> AwsCredentials: ...
    @property
    def assumed_role_arn(self) -> str: ...


class StsClient(Protocol):
    """A thin AssumeRole client (aiobotocore in production, `tests.fakes.credentials.FakeStsClient` in unit tests).
    Raises `botocore.exceptions.ClientError` for STS errors."""

    async def assume_role(
        self, *, role_arn: str, external_id: str, session_name: str, duration_s: int
    ) -> AssumedRole: ...


def external_id_pattern(tenant_id: str) -> re.Pattern[str]:
    """`aghoo-<tenant_id>-<32 base62>` (ADR-001 §3): a ref pointing at another tenant's secret fails it."""
    return re.compile(rf"aghoo-{re.escape(tenant_id)}-[A-Za-z0-9]{{32}}")


def role_session_name(deployment: str, pod_id: str) -> str:
    """`noema-{deployment}-{pod_id}`, charset `[\\w+=,.@-]`, at most 64 characters (CloudTrail per pod)."""
    return _SESSION_CHARS.sub("-", f"noema-{deployment}-{pod_id}")[:64]


def allowlisted_account(role_arn: str) -> str | None:
    """The account of an allowlisted invoke role ARN, else None."""
    match = ROLE_ARN.fullmatch(role_arn)
    return None if match is None else match.group(1)


def assumed_account(assumed_role_arn: str) -> str | None:
    """The account parsed from `AssumedRoleUser.Arn`, else None."""
    match = ASSUMED_ROLE_ARN.fullmatch(assumed_role_arn)
    return None if match is None else match.group(1)


def classify_sts_error(exc: BaseException) -> TenantAccessCause:
    """`access_denied` for the ADR-001 §2 denial codes, `sts_unavailable` for everything else (throttling, 5xx,
    timeouts, connection errors)."""
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ACCESS_DENIED_CODES:
            return "access_denied"
    return "sts_unavailable"


@dataclass(slots=True)
class _Entry:
    creds: TenantCredentials
    role_arn: str
    external_id: SecretStr  # held next to the session (ADR-001 §3)
    stored_mono: float
    soft_at: datetime
    hard_at: datetime
    next_refresh_mono: float = -math.inf
    refreshing: bool = False


@dataclass(frozen=True, slots=True)
class _Negative:
    cause: TenantAccessCause
    until_mono: float


class StsBroker:
    """Implements `CredentialBroker` (see the module docstring). Background soft refreshes run through
    `runtime.spawn`; `aclose()` cancels them and forgets every session (lifespan)."""

    def __init__(
        self,
        *,
        config: ConfigRegistry,
        secrets: SecretsProvider,
        sts: StsClient,
        clock: Clock,
        runtime: ProcessRuntime,
        deployment: str,
        pod_id: str,
        telemetry: Telemetry | None = None,
        security: SecurityEvents | None = None,
        duration_s: int = 3600,
        soft_refresh_s: int = 600,
        hard_refresh_s: int = 300,
        negative_ttl_s: float = 10.0,
    ) -> None:
        if not 0 < hard_refresh_s < soft_refresh_s < duration_s:
            raise ValueError("0 < hard_refresh_s < soft_refresh_s < duration_s")
        self._config = config
        self._secrets = secrets
        self._sts = sts
        self._clock = clock
        self._runtime = runtime
        self._session_name = role_session_name(deployment, pod_id)
        self._telemetry = telemetry
        self._security = security or SecurityEvents(telemetry=telemetry)
        self._duration_s = duration_s
        self._soft = timedelta(seconds=soft_refresh_s)
        self._hard = timedelta(seconds=hard_refresh_s)
        self._negative_ttl_s = negative_ttl_s
        self._entries: dict[str, _Entry] = {}
        self._negative: dict[str, _Negative] = {}
        self._locks: dict[str, anyio.Lock] = {}
        self._scopes: set[anyio.CancelScope] = set()
        self.assume_role_calls = 0

    async def start(self) -> None:
        """Lifespan hook: no STS call (sessions are created lazily)."""

    async def aclose(self) -> None:
        """Cancel background refreshes and drop every cached session."""
        for scope in list(self._scopes):
            scope.cancel()
        self._entries.clear()
        self._negative.clear()

    async def get(self, tenant_id: str) -> TenantCredentials:
        """The tenant's credentials; a memory hit while warm. Raises `TenantAccessMisconfigured`."""
        entry = self._entries.get(tenant_id)
        if entry is not None and self._matches_config(tenant_id, entry):
            now = self._clock.now()
            if now < entry.hard_at:
                if now >= entry.soft_at and not entry.refreshing and self._clock.monotonic() >= entry.next_refresh_mono:
                    self._soft_refresh(tenant_id, entry)
                return entry.creds
        self._raise_if_negative(tenant_id)
        async with self._lock(tenant_id):
            entry = self._entries.get(tenant_id)
            if entry is not None and self._clock.now() < entry.hard_at and self._matches_config(tenant_id, entry):
                return entry.creds
            self._raise_if_negative(tenant_id)
            try:
                fresh = await self._refresh(tenant_id)
            except TenantAccessMisconfigured as exc:
                self._entries.pop(tenant_id, None)
                self._negative[tenant_id] = _Negative(exc.cause, self._clock.monotonic() + self._negative_ttl_s)
                raise
            self._entries[tenant_id] = fresh
            self._negative.pop(tenant_id, None)
            return fresh.creds

    def invalidate(self, tenant_id: str) -> None:
        """Drop the cached session (e.g. after Bedrock `ExpiredTokenException`), only if it is >= 60 s old, so a
        rejection loop costs at most one AssumeRole per minute per tenant per pod."""
        entry = self._entries.get(tenant_id)
        if entry is not None and self._clock.monotonic() - entry.stored_mono >= INVALIDATE_MIN_AGE_S:
            del self._entries[tenant_id]

    def _matches_config(self, tenant_id: str, entry: _Entry) -> bool:
        """The cached session still belongs to the tenant's active config (account, invoke role, home region); a
        session from before an account move or a region change is a miss, never handed out."""
        try:
            tenant = self._config.current().tenant(tenant_id)
        except NoemaError:
            return False
        creds = entry.creds
        return (
            tenant.aws_account_id == creds.account_id
            and tenant.invoke_role_arn == entry.role_arn
            and tenant.home_region == creds.region
        )

    def _lock(self, tenant_id: str) -> anyio.Lock:
        lock = self._locks.get(tenant_id)
        if lock is None:
            lock = self._locks[tenant_id] = anyio.Lock()
        return lock

    def _raise_if_negative(self, tenant_id: str) -> None:
        negative = self._negative.get(tenant_id)
        if negative is not None and self._clock.monotonic() < negative.until_mono:
            raise TenantAccessMisconfigured(negative.cause)

    def _soft_refresh(self, tenant_id: str, entry: _Entry) -> None:
        entry.refreshing = True

        async def run() -> None:
            scope = anyio.CancelScope()
            self._scopes.add(scope)
            try:
                with scope:
                    async with self._lock(tenant_id):
                        if self._entries.get(tenant_id) is not entry:
                            return  # a hard refresh replaced it meanwhile
                        try:
                            self._entries[tenant_id] = await self._refresh(tenant_id)
                        except TenantAccessMisconfigured:
                            entry.next_refresh_mono = self._clock.monotonic() + self._negative_ttl_s
            finally:
                entry.refreshing = False
                self._scopes.discard(scope)

        try:
            self._runtime.spawn("identity.sts_soft_refresh", run)
        except RuntimeError:  # no open runtime: the hard refresh will renew the session
            entry.refreshing = False
            entry.next_refresh_mono = self._clock.monotonic() + self._negative_ttl_s

    async def _refresh(self, tenant_id: str) -> _Entry:
        try:
            return await self._assume(tenant_id)
        except TenantAccessMisconfigured as exc:
            self._failed(tenant_id, exc.cause)
            raise

    async def _assume(self, tenant_id: str) -> _Entry:
        snapshot = self._config.current()
        try:
            tenant = snapshot.tenant(tenant_id)
        except NoemaError:
            raise TenantAccessMisconfigured("role_not_allowlisted") from None
        if allowlisted_account(tenant.invoke_role_arn) != tenant.aws_account_id:
            raise TenantAccessMisconfigured("role_not_allowlisted")
        try:
            raw = await self._secrets.get(tenant.external_id_ref)
        except NoemaError:
            raise TenantAccessMisconfigured("external_id_unavailable") from None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise TenantAccessMisconfigured("external_id_invalid") from None
        if not external_id_pattern(tenant_id).fullmatch(text):
            raise TenantAccessMisconfigured("external_id_invalid")
        external_id = SecretStr(text, register=True)
        result = await self._call_sts(tenant_id, tenant.invoke_role_arn, external_id)
        if assumed_account(result.assumed_role_arn) != tenant.aws_account_id:
            self._security.emit("account_mismatch", tenant_id=tenant_id, details={"source": "sts"})
            raise TenantAccessMisconfigured("account_mismatch")
        now = self._clock.now()
        expires_at = result.creds.expires_at or now + timedelta(seconds=self._duration_s)
        if snapshot.platform.credentials.mantle_auth == "bearer_short_term":
            mantle = short_term_bearer(result.creds, tenant.home_region, now=now, expires_at=expires_at)
        else:
            mantle = SIGV4
        creds = TenantCredentials(
            tenant_id=tenant_id,
            account_id=tenant.aws_account_id,
            region=tenant.home_region,
            aws=result.creds,
            mantle=mantle,
            expires_at=expires_at,
            source="sts",
        )
        return _Entry(
            creds=creds,
            role_arn=tenant.invoke_role_arn,
            external_id=external_id,
            stored_mono=self._clock.monotonic(),
            soft_at=expires_at - self._soft,
            hard_at=expires_at - self._hard,
        )

    async def _call_sts(self, tenant_id: str, role_arn: str, external_id: SecretStr) -> AssumedRole:
        self.assume_role_calls += 1
        result: AssumedRole | None = None
        cause: TenantAccessCause | None = None
        with contextlib.ExitStack() as stack:
            span = None
            if self._telemetry is not None:
                attrs = {"aghoo.tenant": tenant_id, "aghoo.credentials.source": "sts"}
                span = stack.enter_context(self._telemetry.span("sts.assume_role", **attrs))
            async with deadline_scope(self._clock, self._clock.monotonic() + STS_TIMEOUT_S) as scope:
                try:
                    result = await self._sts.assume_role(
                        role_arn=role_arn,
                        external_id=external_id.get_secret_value(),
                        session_name=self._session_name,
                        duration_s=self._duration_s,
                    )
                except Exception as exc:  # fail closed on any STS error, classified (never retried here)
                    cause = classify_sts_error(exc)
                    log.warning("sts.assume_role failed tenant=%s error=%s", tenant_id, type(exc).__name__)
            if scope.cancelled_caught or result is None:
                cause = cause or "sts_unavailable"
            if cause is not None and span is not None:
                span.error("tenant_access_misconfigured")
        if cause is not None or result is None:
            raise TenantAccessMisconfigured(cause or "sts_unavailable")
        return result

    def _failed(self, tenant_id: str, cause: TenantAccessCause) -> None:
        if self._telemetry is not None:
            self._telemetry.instruments.add("aghoo_tenant_access_failures_total", tenant=tenant_id, cause=cause)
        if cause != "account_mismatch":  # account_mismatch is its own security event (emitted where detected)
            self._security.emit("tenant_access_misconfigured", tenant_id=tenant_id, details={"cause": cause})
