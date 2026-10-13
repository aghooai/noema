"""API-key authentication (spec-09 §2, IMPL-008): `ApiKeyAuthenticator` implements `Authenticator`.

Per request: the per-IP auth-failure bucket (429 `rpm_exceeded` while empty, before any verification), the header
format, the row (LRU `KeyCache`, else `auth.lookup_api_key`), the peppered HMAC (constant-time), the key
environment, revocation and expiry, the IP allowlist, then the `Principal`. Every failure is the same 401
`invalid_api_key` ("Invalid API key.") and one `auth.failure` log line with the reason, never the key. A successful
lookup touches `last_used_at` at most once a minute per key and re-hashes a previous-pepper key, both after the
response (`ProcessRuntime.spawn`, best effort). While Postgres is unavailable a key whose cache entry expired is
checked against its last good row (`KeyCache.stale`, at most 10 min old; Wave E), so authenticated requests that
need no database (`store: false` turns, `/v1/decisions`) continue through an outage (06 §7).

Hot-path loads: concurrent cache misses of one key share one `auth.lookup_api_key` (single flight). The pepper ring
is loaded by `start()` (lifespan) or the first request (one shared load; a failure is negative-cached 5 s, so
callers fail fast); once its 300 s TTL passes it is served stale while one background task reloads it. A reload that
fails, or that could not read the previous version (`PepperRing.partial`), keeps the loaded versions and retries
after 10 s.
"""

from __future__ import annotations

import ipaddress
import logging
import math
from collections.abc import Mapping
from typing import Final, Literal

import anyio

from noema.core.clock import Clock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.credentials import SecretsProvider
from noema.core.protocols.ops import RateLimiter
from noema.core.protocols.runtime import ProcessRuntime
from noema.core.protocols.storage import Database
from noema.core.settings import Env
from noema.identity.apikeys import PepperRing, hash_key, load_peppers, parse_bearer, verify_key
from noema.identity.keycache import CacheEntry, KeyCache, KeyRow

AUTHFAIL_RATE_PER_S: Final = 10.0
AUTHFAIL_BURST: Final = 50
PEPPER_TTL_S: Final = 300.0
PEPPER_RETRY_S: Final = 10.0  # a failed or partial pepper reload is retried after this
PEPPER_NEGATIVE_S: Final = 5.0  # a failed first load fails callers fast for this long
PEPPER_UNAVAILABLE: Final = "The API key pepper is unavailable."
LOOKUP_UNAVAILABLE: Final = "The API key store is unavailable."
TOUCH_INTERVAL_S: Final = 60.0
STALE_RETRY_S: Final = 5.0  # after a failed lookup, a key with a last good row skips Postgres this long
INVALID_KEY_MESSAGE: Final = "Invalid API key."
FailureReason = Literal["format", "unknown_key", "bad_secret", "env_mismatch", "revoked", "expired", "ip_denied"]

log = logging.getLogger("noema.identity.authn")


def authfail_bucket(client_ip: str) -> str:
    """The per-IP auth-failure GCRA bucket name (`RedisKeys.gcra(bucket)` in the limiter)."""
    return f"authfail:{client_ip}"


def _ip_allowed(client_ip: str, allowlist: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]) -> bool:
    if not allowlist:
        return True
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped  # a dual-stack socket reports IPv4 peers as ::ffff:a.b.c.d
    return any(addr.version == net.version and addr in net for net in allowlist)


class _Lookup:
    """One in-flight `lookup_api_key` shared by the concurrent misses of a key."""

    __slots__ = ("done", "entry", "failed")

    def __init__(self) -> None:
        self.done = anyio.Event()
        self.entry: CacheEntry | None = None
        self.failed = False


class ApiKeyAuthenticator:
    """Implements `Authenticator` (see the module docstring). `env` is NOEMA_ENV (`ak_test_` keys are refused in
    prod). One instance per process; `evict` is called by the `keys_revoked` listener."""

    def __init__(
        self,
        *,
        db: Database,
        secrets: SecretsProvider,
        limiter: RateLimiter,
        clock: Clock,
        runtime: ProcessRuntime,
        env: Env,
        cache: KeyCache | None = None,
        pepper_ttl_s: float = PEPPER_TTL_S,
    ) -> None:
        self._db = db
        self._secrets = secrets
        self._limiter = limiter
        self._clock = clock
        self._runtime = runtime
        self._env = env
        self.cache = cache or KeyCache()
        self._pepper_ttl_s = pepper_ttl_s
        self._ring: PepperRing | None = None
        self._ring_due = -math.inf  # monotonic time of the next reload
        self._ring_failed_until = -math.inf
        self._ring_refreshing = False
        self._ring_lock = anyio.Lock()
        self._lookups: dict[str, _Lookup] = {}
        self._evictions = 0
        self._lookup_down_until = -math.inf  # monotonic: Postgres failed a lookup moments ago

    async def start(self) -> None:
        """Lifespan hook (api process): load the pepper ring, which also creates the Secrets Manager client, so the
        first request pays for neither. A failure is logged, not raised: requests retry the load."""
        try:
            await self._load_ring()
        except NoemaError:
            log.warning("api-key pepper preload failed; requests will retry")

    async def authenticate(self, headers: Mapping[str, str], client_ip: str) -> Principal:
        """The caller's Principal. Raises 401 `invalid_api_key`, 429 `rpm_exceeded` (the IP's failure bucket is
        empty), 500 `internal_error` (Postgres or the pepper secret unavailable; nothing is cached)."""
        bucket = authfail_bucket(client_ip)
        retry = await self._limiter.check(bucket, rate_per_s=AUTHFAIL_RATE_PER_S, burst=AUTHFAIL_BURST)
        if retry is not None:
            raise NoemaError(
                ErrorCode.RPM_EXCEEDED,
                "Too many failed authentication attempts from this address.",
                retry_after_s=max(1, math.ceil(retry)),
            )
        header = headers.get("authorization")
        if header is None:
            header = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        parsed = parse_bearer(header)
        if parsed is None:
            raise await self._fail("format", None, client_ip)
        now = self._clock.monotonic()
        entry = self.cache.get(parsed.key_id, now)
        if entry is None:
            entry = await self._lookup(parsed.key_id, now)
        row = entry.row
        if row is None:
            raise await self._fail("unknown_key", parsed.key_id, client_ip)
        ring = await self._peppers()
        if ring.partial and ring.previous is None and 1 <= row.pepper_version < ring.current.version:
            raise NoemaError(ErrorCode.INTERNAL_ERROR, PEPPER_UNAVAILABLE)  # cannot tell bad_secret from a blip
        pepper = None if row.pepper_version < 1 else verify_key(ring, row.key_hash, parsed.key_id, parsed.secret)
        if pepper is None or pepper.version != row.pepper_version:
            raise await self._fail("bad_secret", parsed.key_id, client_ip)
        if parsed.env != row.env or (parsed.env == "test" and self._env == "prod"):
            raise await self._fail("env_mismatch", parsed.key_id, client_ip)
        if row.revoked_at is not None:
            raise await self._fail("revoked", parsed.key_id, client_ip)
        if row.expires_at is not None and row.expires_at <= self._clock.now():
            raise await self._fail("expired", parsed.key_id, client_ip)
        if not _ip_allowed(client_ip, row.ip_allowlist):
            raise await self._fail("ip_denied", parsed.key_id, client_ip)
        self._after_success(parsed.key_id, entry, now, ring, pepper.version, parsed.secret.get_secret_value())
        return Principal(
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            principal_id=row.principal_id,
            key_id=parsed.key_id,
            roles=row.roles,
            scopes=row.scopes,
            request_id=headers.get("x-request-id", ""),
        )

    def evict(self, key_id: str) -> bool:
        """Drop a cached key row (revocation); True if it was cached. A lookup in flight meanwhile is not cached."""
        self._evictions += 1
        return self.cache.evict(key_id)

    async def _fail(self, reason: FailureReason, key_id: str | None, client_ip: str) -> NoemaError:
        log.info(
            "auth.failure reason=%s key_id=%s client_ip=%s",
            reason,
            key_id or "-",
            client_ip,
            extra={"event": "auth.failure", "code": reason},
        )
        await self._limiter.debit(authfail_bucket(client_ip), rate_per_s=AUTHFAIL_RATE_PER_S, burst=AUTHFAIL_BURST)
        return NoemaError(ErrorCode.INVALID_API_KEY, INVALID_KEY_MESSAGE)

    async def _lookup(self, key_id: str, now: float) -> CacheEntry:
        """The cache entry of a missed key; concurrent misses share one `lookup_api_key`."""
        if now < self._lookup_down_until and (stale := self.cache.stale(key_id, now)) is not None:
            return stale  # Postgres failed moments ago: no request waits for it again for STALE_RETRY_S
        while (shared := self._lookups.get(key_id)) is not None:
            await shared.done.wait()
            if shared.entry is not None:
                return shared.entry
            if shared.failed:
                raise NoemaError(ErrorCode.INTERNAL_ERROR, LOOKUP_UNAVAILABLE)
            # the leading request was cancelled: one of the waiters leads a new lookup
        lookup = self._lookups[key_id] = _Lookup()
        evictions = self._evictions
        try:
            async with self._db.system_uow() as calls:
                found = await calls.lookup_api_key(key_id)
            entry = self.cache.put(key_id, None if found is None else KeyRow.from_row(found), now)
            if evictions != self._evictions:  # a revocation arrived during the lookup: never cache that read
                self.cache.evict(key_id)
            lookup.entry = entry
            return entry
        except NoemaError as exc:
            stale = self.cache.stale(key_id, now) if exc.code is ErrorCode.INTERNAL_ERROR else None
            if stale is None or evictions != self._evictions:
                lookup.failed = True
                raise
            if now >= self._lookup_down_until:
                log.warning("api-key lookups failing (Postgres unavailable); using the last good rows")
            self._lookup_down_until = now + STALE_RETRY_S  # the entry itself is not refreshed
            lookup.entry = stale
            return stale
        except Exception:
            lookup.failed = True
            raise
        finally:
            del self._lookups[key_id]
            lookup.done.set()

    async def _peppers(self) -> PepperRing:
        ring = self._ring
        if ring is None:
            return await self._load_ring()
        if not self._ring_refreshing and self._clock.monotonic() >= self._ring_due:
            self._ring_refreshing = True
            try:
                self._runtime.spawn("identity.pepper_reload", self._reload_ring)
            except RuntimeError:  # no open runtime (a test without a lifespan): reload inline
                await self._reload_ring()
                return self._ring or ring
        return ring

    async def _load_ring(self) -> PepperRing:
        """The first load: one at a time (waiters reuse its result); a failure fails callers fast for 5 s."""
        async with self._ring_lock:
            if self._ring is not None:
                return self._ring
            if self._clock.monotonic() < self._ring_failed_until:
                raise NoemaError(ErrorCode.INTERNAL_ERROR, PEPPER_UNAVAILABLE)
            try:
                ring = await load_peppers(self._secrets)
            except NoemaError:
                self._ring_failed_until = self._clock.monotonic() + PEPPER_NEGATIVE_S
                raise
            self._install(ring)
            return ring

    async def _reload_ring(self) -> None:
        """The background reload of a loaded ring (single flight); failures keep the loaded versions."""
        try:
            ring = await load_peppers(self._secrets, fallback=self._ring)
        except NoemaError:
            log.warning("api-key pepper reload failed; keeping the loaded pepper versions")
            self._ring_due = self._clock.monotonic() + PEPPER_RETRY_S
        else:
            if ring.partial:
                log.warning("api-key pepper reload could not read the previous version; keeping the loaded one")
            self._install(ring)
        finally:
            self._ring_refreshing = False

    def _install(self, ring: PepperRing) -> None:
        self._ring = ring
        self._ring_due = self._clock.monotonic() + (PEPPER_RETRY_S if ring.partial else self._pepper_ttl_s)

    def _after_success(
        self, key_id: str, entry: CacheEntry, now: float, ring: PepperRing, version: int, secret: str
    ) -> None:
        if entry.last_touch_mono is None or now - entry.last_touch_mono >= TOUCH_INTERVAL_S:
            entry.last_touch_mono = now
            self._spawn("identity.touch_api_key", key_id, None)
        if version != ring.current.version and not entry.rehash_started:
            entry.rehash_started = True
            rehash = (hash_key(ring.current, key_id, secret), ring.current.version)
            self._spawn("identity.rehash_api_key", key_id, rehash)

    def _spawn(self, name: str, key_id: str, rehash: tuple[str, int] | None) -> None:
        async def run() -> None:
            async with self._db.system_uow() as calls:
                if rehash is None:
                    await calls.touch_api_key(key_id)
                else:
                    await calls.rehash_api_key(key_id, rehash[0], rehash[1])

        try:
            self._runtime.spawn(name, run)
        except RuntimeError:  # the runtime is not open (CLI, a test without a lifespan): skip the housekeeping
            log.debug("%s skipped: the process runtime is not open", name)
