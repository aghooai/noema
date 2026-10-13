"""What both adapters share: the per-tenant client pool, credential fetch and assertion, the in-adapter connect
retry, failure side effects and metrics (spec-02 §4, ADR-001 §2 and §4, IMPL-016, IMPL-076).

Every failure of `complete()` and `stream().__aenter__` is an `AdapterError`: unexpected broker exceptions and client
creation failures are classified (`classify`), and a call racing `aclose()` gets `AdapterError(retryable,
"adapter_closed")`. An account mismatch is a spec-09 §1 security event: given a `SecurityEventSink` (identity's
`SecurityEvents`: log record, counter and audit row), the adapters emit through it; without one they log the
structured record and count `aghoo_security_violations_total` themselves (no audit row: the adapters hold no outbox,
request id or job id)."""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

import anyio

from noema.adapters.capabilities import capabilities_of
from noema.adapters.classify import INVALIDATING_CODES, classify, is_connect_error, possibly_delivered, timeout_error
from noema.adapters.wire import AdapterKind, check_request
from noema.config.models import ModelCfg
from noema.core.clock import Clock
from noema.core.errors import ClassifiedError, ErrorKind, NoemaError, TenantAccessMisconfigured
from noema.core.principal import TenantRef
from noema.core.protocols.adapters import AdapterError, ModelCapabilities, ModelRequest
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.credentials import CredentialBroker, TenantCredentials, assert_creds_for
from noema.core.protocols.ops import RetryBudget
from noema.core.protocols.telemetry import Telemetry
from noema.core.values import JsonValue, UsageRecord

_LOG: Final = logging.getLogger("noema.adapters")
_SECURITY_LOG: Final = logging.getLogger("noema.adapters.security")
CLOSED: Final = ClassifiedError(ErrorKind.RETRYABLE, "the adapter is closed", "adapter_closed")
RETRY_JITTER_S: Final = 0.2  # full jitter before the one in-adapter connect retry (design 04 §2)
PoolKey = tuple[str, str, str]  # (tenant_id, account_id, region)


class SecurityEventSink(Protocol):
    """Where the adapters report a spec-09 §1 security event (`noema.identity.security.SecurityEvents` fits)."""

    def emit(
        self,
        name: Literal["account_mismatch"],
        *,
        tenant_id: str | None,
        details: dict[str, JsonValue] | None = None,
    ) -> None: ...


@dataclass(eq=False)
class PoolEntry[C]:
    """One pooled client. `inflight` calls hold it; a `stale` entry is closed when the last call releases it."""

    key: PoolKey
    client: C
    close: Callable[[], Awaitable[None]]
    inflight: int = 0
    stale: bool = False


@dataclass
class ClientPool[C]:
    """Clients keyed by (tenant id, account id, region) (08:L488, ADR-001 §4), created once per key (single
    flight). `evict(tenant_id)` retires a tenant's clients after its credentials were rejected: busy ones close
    when their last call releases them, idle ones at the next `acquire`/`release`."""

    factory: Callable[[TenantCredentials], Awaitable[tuple[C, Callable[[], Awaitable[None]]]]]
    _entries: dict[PoolKey, PoolEntry[C]] = field(default_factory=dict)
    _locks: dict[PoolKey, anyio.Lock] = field(default_factory=dict)
    _graveyard: list[PoolEntry[C]] = field(default_factory=list)
    _closed: bool = False

    async def acquire(self, creds: TenantCredentials) -> PoolEntry[C]:
        """The client of `creds`' key (created on first use). Raises `AdapterError` only: `adapter_closed` after
        `aclose()`, the classified error when the client cannot be created."""
        try:
            return await self._acquire(creds)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(classify(exc)) from None

    async def _acquire(self, creds: TenantCredentials) -> PoolEntry[C]:
        if self._closed:
            raise AdapterError(CLOSED)
        if self._graveyard:
            await self._bury()
        key = (creds.tenant_id, creds.account_id, creds.region)
        entry = self._entries.get(key)
        if entry is None:
            lock = self._locks.setdefault(key, anyio.Lock())
            async with lock:
                entry = self._entries.get(key)
                if entry is None:
                    if self._closed:
                        raise AdapterError(CLOSED)
                    client, close = await self.factory(creds)
                    entry = self._entries[key] = PoolEntry(key, client, close)
        entry.inflight += 1
        return entry

    async def release(self, entry: PoolEntry[C]) -> None:
        entry.inflight -= 1
        if entry.stale and entry.inflight == 0:
            with anyio.CancelScope(shield=True):
                await entry.close()
        if self._graveyard:
            await self._bury()

    def evict(self, tenant_id: str) -> None:
        """Sync: retire every client of `tenant_id`."""
        for key in [k for k in self._entries if k[0] == tenant_id]:
            entry = self._entries.pop(key)
            entry.stale = True
            if entry.inflight == 0:
                self._graveyard.append(entry)

    async def _bury(self) -> None:
        dead, self._graveyard = self._graveyard, []
        with anyio.CancelScope(shield=True):
            for entry in dead:
                await entry.close()

    async def aclose(self) -> None:
        """Close every client (lifespan shutdown)."""
        self._closed = True
        entries, self._entries = list(self._entries.values()), {}
        self._graveyard.extend(entries)
        await self._bury()

    def __len__(self) -> int:
        return len(self._entries)


class AdapterBase:
    """The shared half of `ConverseAdapter` and `MantleAdapter` (see the module docstring)."""

    kind: AdapterKind

    def __init__(
        self,
        *,
        config: ConfigRegistry,
        broker: CredentialBroker,
        clock: Clock,
        retry_budget: RetryBudget,
        telemetry: Telemetry | None = None,
        security: SecurityEventSink | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._config = config
        self._broker = broker
        self._clock = clock
        self._budget = retry_budget
        self._telemetry = telemetry
        self._security = security
        self._rng = rng or random.Random()  # noqa: S311 - retry jitter, not cryptography
        self._cache_sums: dict[str, list[int]] = {}
        self._open_streams = 0

    @property
    def open_streams(self) -> int:
        """Provider streams currently open (between `stream().__aenter__` and `__aexit__`)."""
        return self._open_streams

    def capabilities(self, alias: str) -> ModelCapabilities:
        """Sync: the current snapshot's `models.<alias>` (raises `NoemaError(not_found)` for an unknown alias)."""
        return capabilities_of(self._config.current().model(alias))

    def classify_error(self, exc: BaseException) -> ClassifiedError:
        """Sync: spec-02 §4.3."""
        return classify(exc)

    def _resolve(self, req: ModelRequest) -> ModelCfg:
        """`models.<alias>` from the request's pinned snapshot (else the current one), after the pre-wire rules."""
        try:
            model = (req.snapshot or self._config.current()).model(req.model_alias)
        except NoemaError:
            raise AdapterError(ClassifiedError(ErrorKind.INVALID_REQUEST, "unknown model alias")) from None
        check_request(req, model, self.kind)
        return model

    async def _credentials(self, tenant: TenantRef) -> TenantCredentials:
        """`broker.get` + `assert_creds_for` before any byte (IMPL-076, ADR-001 §4). Any broker failure becomes an
        `AdapterError` (a `TenantAccessMisconfigured` keeps its cause; anything else is classified)."""
        try:
            creds = await self._broker.get(tenant.tenant_id)
            assert_creds_for(creds, tenant.tenant_id, tenant.aws_account_id)
        except TenantAccessMisconfigured as exc:
            if exc.cause == "account_mismatch":
                self._account_mismatch(tenant.tenant_id)
            raise AdapterError(classify(exc)) from None
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(classify(exc)) from None
        return creds

    def _account_mismatch(self, tenant_id: str) -> None:
        """The spec-09 §1 `account_mismatch` security event (see the module docstring)."""
        self._count("aghoo_tenant_access_failures_total", tenant=tenant_id, cause="account_mismatch")
        if self._security is not None:
            self._security.emit("account_mismatch", tenant_id=tenant_id, details={"source": "adapter"})
            return
        _SECURITY_LOG.warning(
            "security name=account_mismatch tenant_id=%s request_id= kind=account_mismatch",
            tenant_id,
            extra={"event": "security", "code": "account_mismatch", "tenant": tenant_id},
        )
        self._count("aghoo_security_violations_total", kind="account_mismatch")

    def _fail(self, exc: BaseException, tenant: TenantRef, model_id: str) -> ClassifiedError:
        """Classify a provider failure and apply its side effects: rejected credentials invalidate the broker
        session and retire the tenant's clients (ADR-001 §2); throttles and access failures are counted."""
        error = classify(exc)
        if error.kind is ErrorKind.TENANT_ACCESS and error.provider_code is not None:
            if error.provider_code in INVALIDATING_CODES:
                self._broker.invalidate(tenant.tenant_id)
                self._evict(tenant.tenant_id)
            self._count("aghoo_tenant_access_failures_total", tenant=tenant.tenant_id, cause=error.cause or "")
        elif error.kind is ErrorKind.THROTTLED or error.provider_code in ("ThrottlingException", "429"):
            self._count("aghoo_bedrock_throttles_total", tenant=tenant.tenant_id, model=model_id)
        _LOG.warning(
            "model call failed",
            extra={
                "adapter": self.kind,
                "model_id": model_id,
                "tenant_id": tenant.tenant_id,
                "kind": error.kind.value,
                "provider_code": error.provider_code,
            },
        )
        return error

    def _evict(self, tenant_id: str) -> None:
        """Retire the tenant's pooled clients (overridden by the adapters)."""

    async def _send[T](
        self,
        req: ModelRequest,
        tenant: TenantRef,
        model_id: str,
        send: Callable[[], Awaitable[T]],
        *,
        deadline_at: float,
    ) -> T:
        """Send with the one in-adapter retry on a connect/DNS/TLS error before any response byte: only for
        `retry="default"` and only if the RetryBudget grants it (IMPL-016, IMPL-050). Every wire request is counted
        in the budget. A retry after a reset or disconnect that followed the request write (`possibly_delivered`) is
        logged at WARNING: the provider may have billed that first dispatch, which no usage reports. No request is
        sent once the clock has reached `deadline_at`: that raises
        `AdapterError(timeout:deadline)` (the HTTP clients can write a request before their first checkpoint, so a
        cancel scope alone would not stop it)."""
        retried = False
        while True:
            if self._clock.monotonic() >= deadline_at:
                raise AdapterError(timeout_error("deadline"))
            self._budget.note_request(tenant.aws_account_id, model_id)
            try:
                return await send()
            except Exception as exc:
                if retried or req.retry != "default" or not is_connect_error(exc):
                    raise
                if not self._budget.try_spend(tenant.aws_account_id, model_id):
                    raise
                retried = True
                if possibly_delivered(exc):  # IMPL-016 assumes no model call; this first dispatch may be billed
                    _LOG.warning(
                        "connect retry after a possible delivery",
                        extra={"model_id": model_id, "error": type(exc).__name__, "possibly_delivered": True},
                    )
                else:
                    _LOG.info("connect retry", extra={"model_id": model_id, "error": type(exc).__name__})
            await self._clock.sleep(self._rng.uniform(0.0, RETRY_JITTER_S))

    def _observe_usage(self, model_id: str, usage: UsageRecord) -> None:
        """`aghoo_bedrock_cached_input_ratio{model}`: cached / input tokens since process start."""
        if self._telemetry is None or usage.input_tokens <= 0:
            return
        sums = self._cache_sums.setdefault(model_id, [0, 0])
        sums[0] += usage.cached_input_tokens
        sums[1] += usage.input_tokens
        self._telemetry.instruments.set("aghoo_bedrock_cached_input_ratio", sums[0] / sums[1], model=model_id)

    def _count(self, name: str, **labels: str) -> None:
        if self._telemetry is not None:
            self._telemetry.instruments.add(name, 1, **labels)


def endpoint(overrides: Mapping[str, str], key: str) -> str | None:
    """A dev/test endpoint override (`NOEMA_AWS_ENDPOINT_OVERRIDES`, validated by composition, ADR-001 §8)."""
    value = overrides.get(key)
    return value.rstrip("/") if value else None
