"""The retry budget: the real `RetryBudget` (spec-02 §8, IMPL-016, IMPL-078; design 04 §2 "10 % of requests per
(tenant account, model) per minute"; owner ruling R8, 2026-10-12).

Per (tenant account, Bedrock model id) a sliding 60 s window of 1-second buckets counts wire requests (the adapters
call `note_request` for every request they send, in-adapter retries included), granted retries and unsent sends.
R8: the budget counts only re-sends that the upstream may have received (generation's throttle re-send and its
re-send to a model the turn already called, the adapters' connect retry after a read or protocol error that followed
the request write), and it is 10 % of first sends with NO floor: `try_spend` grants a retry while
`retries + 1 <= ratio * first_sends`, first sends = requests - retries - unsent, so a model's requests stay within
1.1x its first sends at every traffic level (F12, measured per failing upstream). A quiet key has no budget: its
first retry needs 10 first sends in the window. A fallback to a different model is not a retry and spends no budget
of either model (owner ruling R6).

The adapters' connect retry of a send that provably never reached the upstream (a failed connect, DNS, TLS, proxy or
request write: `is_connect_error` and not `possibly_delivered`) is exempt (R8, IMPL-016): `try_spend` grants it
without spending and counts the failed send as unsent, so the retry's own `note_request` is the call's one first
send. The adapter allows that retry once per call (`_base._send`). `try_spend` tells it apart from a budgeted retry
by the exception the adapter is handling when it asks (`sys.exception()`; the frozen protocol passes no cause);
every other caller asks outside an `except` block of a transport error and is budgeted.

Hot-path calls are sync and in memory; `run(redis)` coarse-syncs every second: each pod writes its window totals
(requests net of unsent, retries) into the hash `RedisKeys.retry_budget(account, model)` (field = pod id) and reads
the other pods' fresh totals, which `try_spend` adds to its own. With Redis down the budget is local only.
"""

from __future__ import annotations

import logging
import math
import sys
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from noema.adapters.classify import is_connect_error, possibly_delivered
from noema.core.clock import Clock
from noema.core.redis_keys import RedisKeys

if TYPE_CHECKING:
    from redis.asyncio import Redis

_LOG: Final = logging.getLogger("noema.adapters.retry_budget")


@dataclass(frozen=True, slots=True)
class RetryBudgetPolicy:
    """`ratio` of first sends per `window_s`, at least `min_retries` per window (R8: 0, no floor; a test knob);
    `sync_s` between Redis syncs; other pods' totals older than `remote_stale_s` are ignored."""

    ratio: float = 0.1
    window_s: int = 60
    min_retries: int = 0
    sync_s: float = 1.0
    remote_stale_s: float = 5.0


class _Bucket:
    __slots__ = ("requests", "retries", "second", "unsent")

    def __init__(self, second: int) -> None:
        self.second = second
        self.requests = 0
        self.retries = 0
        self.unsent = 0


class _Window:
    __slots__ = (
        "buckets", "last_used", "remote_at", "remote_requests", "remote_retries", "requests", "retries", "unsent"
    )  # fmt: skip

    def __init__(self) -> None:
        self.buckets: deque[_Bucket] = deque()
        self.requests = 0
        self.retries = 0
        self.unsent = 0
        self.remote_requests = 0
        self.remote_retries = 0
        self.remote_at = -math.inf
        self.last_used = -math.inf

    def bucket(self, now: float, window_s: int) -> _Bucket:
        second = math.floor(now)
        oldest = second - window_s + 1
        while self.buckets and self.buckets[0].second < oldest:
            gone = self.buckets.popleft()
            self.requests -= gone.requests
            self.retries -= gone.retries
            self.unsent -= gone.unsent
        if not self.buckets or self.buckets[-1].second != second:
            self.buckets.append(_Bucket(second))
        self.last_used = now
        return self.buckets[-1]


class SlidingRetryBudget:
    """Implements `RetryBudget` (see the module docstring). `keys` None: no Redis sync."""

    def __init__(
        self,
        clock: Clock,
        *,
        policy: RetryBudgetPolicy | None = None,
        keys: RedisKeys | None = None,
        pod_id: str = "local",
    ) -> None:
        self._clock = clock
        self._p = policy or RetryBudgetPolicy()
        self._keys = keys
        self._pod = pod_id
        self._windows: dict[tuple[str, str], _Window] = {}

    def _window(self, tenant_account: str, model_id: str) -> _Window:
        w = self._windows.get((tenant_account, model_id))
        if w is None:
            w = self._windows[(tenant_account, model_id)] = _Window()
        return w

    def note_request(self, tenant_account: str, model_id: str) -> None:
        w = self._window(tenant_account, model_id)
        w.bucket(self._clock.monotonic(), self._p.window_s).requests += 1
        w.requests += 1

    def try_spend(self, tenant_account: str, model_id: str) -> bool:
        now = self._clock.monotonic()
        w = self._window(tenant_account, model_id)
        bucket = w.bucket(now, self._p.window_s)
        if _unsent_connect_failure():  # R8: exempt, and the failed send was no first send
            bucket.unsent += 1
            w.unsent += 1
            return True
        fresh = now - w.remote_at <= self._p.remote_stale_s
        requests = max(0, w.requests - w.unsent) + (w.remote_requests if fresh else 0)
        retries = w.retries + (w.remote_retries if fresh else 0)
        allowed = max(float(self._p.min_retries), self._p.ratio * max(0, requests - retries))
        if retries + 1 > allowed:
            return False
        bucket.retries += 1
        w.retries += 1
        return True

    async def run(self, redis: Redis) -> None:
        """The 1 s sync task (composition.start, api and jobs); runs until cancelled and never raises for Redis
        failures (logged; the budget stays local)."""
        if self._keys is None:
            return
        while True:
            await self._clock.sleep(self._p.sync_s)
            try:
                await self.sync_once(redis)
            except Exception as exc:  # Redis down: local only until it is back
                _LOG.warning("retry budget sync failed", extra={"error": type(exc).__name__})

    async def sync_once(self, redis: Redis) -> None:
        """One sync round: publish this pod's totals of every key used in the window, read the other pods'."""
        assert self._keys is not None
        now = self._clock.monotonic()
        wall_ms = round(self._clock.now().timestamp() * 1000)
        active: list[tuple[tuple[str, str], _Window]] = []
        for key, w in list(self._windows.items()):
            if now - w.last_used > 2 * self._p.window_s:
                del self._windows[key]
                continue
            w.bucket(now, self._p.window_s)
            active.append((key, w))
        if not active:
            return
        pipe = redis.pipeline(transaction=False)
        for (account, model), w in active:
            rkey = self._keys.retry_budget(account, model)
            pipe.hset(rkey, self._pod, f"{max(0, w.requests - w.unsent)},{w.retries},{wall_ms}")
            pipe.expire(rkey, 2 * self._p.window_s)
            pipe.hgetall(rkey)
        results = await pipe.execute()
        stale_ms = self._p.remote_stale_s * 1000
        for i, (_, w) in enumerate(active):
            fields = results[3 * i + 2] or {}
            requests = retries = 0
            for pod, value in fields.items():
                if _text(pod) == self._pod:
                    continue
                parsed = _parse_totals(_text(value))
                if parsed is not None and wall_ms - parsed[2] <= stale_ms:
                    requests += parsed[0]
                    retries += parsed[1]
            w.remote_requests, w.remote_retries, w.remote_at = requests, retries, now


def _unsent_connect_failure() -> bool:
    """True while the caller handles a transport error raised before the request could reach the upstream (the
    adapters' connect retry, `_base._send`)."""
    exc = sys.exception()
    return exc is not None and is_connect_error(exc) and not possibly_delivered(exc)


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _parse_totals(value: str) -> tuple[int, int, int] | None:
    parts = value.split(",")
    if len(parts) != 3:
        return None
    try:
        requests, retries, at = (int(p) for p in parts)
    except ValueError:
        return None
    return (requests, retries, at) if requests >= 0 and retries >= 0 else None
