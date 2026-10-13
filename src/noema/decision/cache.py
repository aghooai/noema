"""The L2 decision cache: `DecisionCacheImpl` over Redis and the 8-part key (spec-06 §14, §19.9; IMPL-004).

Fail-open: every GET/PUT has a 20 ms timeout on the injected clock; an error or timeout is a miss (counted as
`aghoo_cache_misses_total{layer="l2",reason="error"}`) and never raised. Keys are tenant-prefixed and the digest
is a per-tenant HMAC, so tenants never share an entry.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Final

import orjson

from noema.core.clock import Clock, deadline_scope
from noema.core.protocols.decision import DecisionContext, DecisionRequest
from noema.core.protocols.telemetry import Instruments
from noema.core.redis_keys import RedisKeys

from .escalation import chain

if TYPE_CHECKING:
    from redis.asyncio import Redis

_log: Final = logging.getLogger("noema.decision.cache")
L2_TIMEOUT_S: Final = 0.02
_SCAN_COUNT: Final = 500


def cacheable(ctx: DecisionContext, epoch: str) -> bool:
    """Get and put are allowed only for stored, public/internal (with `l2_cache`), non-verification decisions of a
    tenant whose calibrator epoch is known."""
    if not ctx.store or ctx.purpose == "verification" or not epoch:
        return False
    if ctx.data_class not in ("public", "internal"):
        return False
    return ctx.snapshot.platform.data_classes.for_class(ctx.data_class).l2_cache


def profile_part(ctx: DecisionContext, req: DecisionRequest) -> str:
    """`{id}@{ver};q={min_quorum};d={deadline_ms};e={off | chain};m={f|h per question}`."""
    profile = ctx.snapshot.decision_profile(req.profile.id)
    refs = "off" if req.escalation == "off" else ">".join(f"{p.id}@{p.version}" for p in chain(ctx.snapshot, profile))
    methods = "".join("h" if q.method == "hierarchical" else "f" for q in req.questions.questions)
    return f"{req.profile.id}@{req.profile.version};q={req.min_quorum};d={req.deadline_ms};e={refs};m={methods}"


def l2_key(
    keys: RedisKeys, ctx: DecisionContext, req: DecisionRequest, *, digest: str, view_signature: str, epoch: str
) -> str:
    """`{ns}dec:l2:{tenant}:` + sha256(orjson(parts 2-8))."""
    parts = [
        ctx.principal.project_id,
        req.questions.signature,
        digest,
        view_signature,
        profile_part(ctx, req),
        f"cal:{epoch}",
        f"policy:{ctx.snapshot.routing.policy_version}",
    ]
    return keys.l2(ctx.tenant.tenant_id, hashlib.sha256(orjson.dumps(parts)).hexdigest())


class DecisionCacheImpl:
    """Implements `DecisionCache` over a bytes Redis client (`decode_responses=False`). `purge_tenant` SCANs
    `RedisKeys.l2_pattern(tenant)` and UNLINKs."""

    def __init__(self, redis: Redis, keys: RedisKeys, clock: Clock, instruments: Instruments) -> None:
        self._redis = redis
        self._keys = keys
        self._clock = clock
        self._instruments = instruments

    def _error(self, what: str, exc: BaseException | None) -> None:
        self._instruments.add("aghoo_cache_misses_total", layer="l2", reason="error")
        if exc is not None:
            _log.warning("L2 %s failed: %s", what, type(exc).__name__)

    async def get(self, key: str, *, timeout_s: float = L2_TIMEOUT_S) -> bytes | None:
        value: bytes | None = None
        try:
            async with deadline_scope(self._clock, self._clock.monotonic() + timeout_s) as scope:
                raw = await self._redis.get(key)
                value = raw.encode() if isinstance(raw, str) else raw
        except Exception as exc:
            self._error("get", exc)
            return None
        if scope.cancelled_caught:
            self._error("get", None)
            return None
        if value is None:
            self._instruments.add("aghoo_cache_misses_total", layer="l2", reason="miss")
        return value

    async def put(self, key: str, artifact: bytes, ttl_s: int, *, timeout_s: float = L2_TIMEOUT_S) -> None:
        try:
            async with deadline_scope(self._clock, self._clock.monotonic() + timeout_s) as scope:
                await self._redis.set(key, artifact, ex=ttl_s)
        except Exception as exc:
            self._error("put", exc)
            return
        if scope.cancelled_caught:
            self._error("put", None)

    async def purge_tenant(self, tenant_id: str) -> int:
        pattern = self._keys.l2_pattern(tenant_id)
        removed = 0
        async for batch in _batches(self._redis, pattern):
            removed += int(await self._redis.unlink(*batch))
        return removed


async def _batches(redis: Redis, pattern: str) -> AsyncIterator[list[bytes | str]]:
    cursor: int = 0
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=_SCAN_COUNT)
        if keys:
            yield list(keys)
        if cursor == 0:
            return
