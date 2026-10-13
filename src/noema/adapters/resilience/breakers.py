"""Circuit breakers: the real `HealthRegistry` (spec-02 §8, design 04 §2, IMPL-078).

Two breaker families, both on a rolling window of 1-second buckets:
- **service** breakers per (Bedrock model id, region), shared by every tenant: `error` and `timeout` outcomes are
  failures, `ok` a success; `throttled` is a per-account quota signal and never trips them. They also open when the
  window's p95 TTFT exceeds `ttft_factor` x the model's baseline (an EWMA of earlier windows' p95), the latest sample
  does too (no re-open on stale samples after a recovery), and samples above that threshold came from at least
  `ttft_min_accounts` distinct tenant accounts (io #10: one tenant's slow calls
  alone never open the breaker every tenant shares; a breaching window never feeds the baseline). The quorum spans
  processes: each keeps its own window and baseline and may see few tenants (keep-alive connections), so a process
  whose window breaches without the quorum publishes its breaching accounts and its limit (`ttft_breach`, valid for
  one window); every process counts the fresh remote accounts and answers at once, and again on later samples, with
  its own accounts above that limit that nobody reported yet (a process without a baseline of its own included); a
  breach still pending (`_BREACH_PENDING_S`) opens as soon as an answer completes the quorum (its clients may have
  moved to another process, so no new sample of its own would re-check it);
- **quota** breakers per (tenant account, model id): `throttled` is the failure, `ok` the success.

A breaker opens when the window holds at least `min_requests` outcomes and the failure share exceeds
`failure_ratio`; it stays open `open_s`, then turns half-open and admits one call in `probe_every` (5 %). A
successful probe closes it, a failed one re-opens it. `admit` is False while either breaker of the call is open.
In half-open only as many outcomes count as probes were admitted since the transition (Wave E): the late outcome of
a call admitted before the open (a long stream) neither closes the breaker during the burst nor re-opens it.

The service breaker is shared by every tenant, so callers record only provider-health signals: `error` for a
`retryable` or `fatal` failure, `timeout` for a provider `timeout:*` one (connect, first event, read, idle; never
`timeout:deadline` or any call cut by the caller's own deadline), `throttled` for `throttled`. A `tenant_access` or
`invalid_request` failure is the tenant's or the request's fault and must not be recorded at all, or one
misconfigured tenant could open the breaker for everyone (generation filters this way; the registry cannot, since
an `Outcome` carries no error kind).

Every hot-path method is sync and in memory. Local open/close transitions are published on
`RedisKeys.breaker_events()` (`{model, region, tenant_account?, state, until_ms?, origin}`; plus
`{model, region, state: "ttft_breach", accounts, limit_ms, until_ms, origin}` for the TTFT quorum) by `run(redis)`,
which
also applies other pods' events; `origin` is a per-instance nonce, so a pod ignores its own events (it subscribes to
the channel it publishes on). With Redis down each pod keeps its local view (spec-04 §12). The subscription is
checked at the application level (`noema.core.pubsub.heartbeat_messages`: a PING after 5 s quiet, resubscribe after
15 s without a reply).
"""

from __future__ import annotations

import contextlib
import logging
import math
import secrets
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

import anyio
import orjson

from noema.core.clock import Clock
from noema.core.protocols.telemetry import Telemetry
from noema.core.pubsub import heartbeat_messages
from noema.core.redis_keys import RedisKeys

if TYPE_CHECKING:
    from redis.asyncio import Redis

BreakerState = Literal["closed", "open", "half_open"]
Outcome = Literal["ok", "error", "throttled", "timeout"]
STATE_GAUGE: Final[Mapping[BreakerState, int]] = {"closed": 0, "half_open": 1, "open": 2}
_LOG: Final = logging.getLogger("noema.adapters.breakers")
_OUTBOX_MAX: Final = 1024
_RESUBSCRIBE_S: Final = 1.0
_EVENT_STATES: Final = ("open", "closed", "half_open", "ttft_breach")
_BREACH_PENDING_S: Final = 5.0  # a TTFT breach waits this long for answers that complete its account quorum


@dataclass(frozen=True, slots=True)
class BreakerPolicy:
    """Breaker parameters (design 04 §2: 30 s window, >= 20 requests, > 50 % errors or p95 TTFT > 3x baseline,
    half-open after 15 s with 5 % probes). `baseline_windows`: full windows of TTFT history needed before the TTFT
    rule is armed; `baseline_alpha`: the EWMA weight of a new window's p95; `ttft_min_accounts`: distinct tenant
    accounts whose samples must exceed the TTFT threshold before the rule opens the breaker."""

    window_s: int = 30
    min_requests: int = 20
    failure_ratio: float = 0.5
    ttft_factor: float = 3.0
    open_s: float = 15.0
    probe_every: int = 20
    baseline_alpha: float = 0.2
    baseline_windows: int = 3
    ttft_min_accounts: int = 2


class _Bucket:
    __slots__ = ("fails", "n", "second", "ttfts")

    def __init__(self, second: int) -> None:
        self.second = second
        self.n = 0
        self.fails = 0
        self.ttfts: list[tuple[int, str]] = []  # (ttft_ms, tenant account)


class _Breaker:
    """One breaker's state. `remote`: the current open period came from another pod's event."""

    __slots__ = (
        "asked_limit",
        "asked_until",
        "baseline",
        "baseline_at",
        "baseline_n",
        "breach_slow",
        "breach_until",
        "buckets",
        "fails",
        "granted",
        "n",
        "opened_until",
        "probes",
        "remote",
        "slow_remote",
        "state",
        "told",
        "ttft_n",
    )

    def __init__(self) -> None:
        self.state: BreakerState = "closed"
        self.opened_until = 0.0
        self.buckets: deque[_Bucket] = deque()
        self.n = 0
        self.fails = 0
        self.ttft_n = 0
        self.probes = 0
        self.granted = 0  # probes admitted (and not yet answered) in the current half-open period
        self.remote = False
        self.slow_remote: dict[str, float] = {}  # account -> monotonic expiry of another process's ttft_breach
        self.asked_limit = math.inf  # the limit of the latest remote ttft_breach, answered until `asked_until`
        self.asked_until = -math.inf
        self.told: dict[str, float] = {}  # account -> expiry of this process's own ttft_breach report of it
        self.breach_slow: set[str] = set()  # local breaching accounts of a breach pending the quorum, until ...
        self.breach_until = -math.inf
        self.baseline: float | None = None
        self.baseline_n = 0
        self.baseline_at = -math.inf

    def clear_window(self) -> None:
        self.buckets.clear()
        self.n = self.fails = self.ttft_n = 0
        self.breach_until = -math.inf


def _p95(values: list[int]) -> float:
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)])


class BreakerRegistry:
    """Implements `HealthRegistry` (see the module docstring). `keys` None: no Redis sync (`run` returns)."""

    def __init__(
        self,
        clock: Clock,
        *,
        policy: BreakerPolicy | None = None,
        telemetry: Telemetry | None = None,
        keys: RedisKeys | None = None,
    ) -> None:
        self._clock = clock
        self._p = policy or BreakerPolicy()
        self._telemetry = telemetry
        self._keys = keys
        self._service: dict[tuple[str, str], _Breaker] = {}
        self._quota: dict[tuple[str, str], _Breaker] = {}
        self._outbox: deque[bytes] = deque(maxlen=_OUTBOX_MAX)
        self._wake: anyio.Event | None = None
        self.origin = secrets.token_hex(8)  # this instance's `origin` in published events

    # --- HealthRegistry ------------------------------------------------------------------------------------------
    def admit(self, model_id: str, region: str, tenant_account: str) -> bool:
        service = self._service.get((model_id, region))
        quota = self._quota.get((tenant_account, model_id))
        ok = True
        probing: list[_Breaker] = []
        for key, b in (((model_id, region, None), service), ((model_id, region, tenant_account), quota)):
            if b is None:
                continue
            state = self._state(b, key)
            if state == "open":
                ok = False
            elif state == "half_open":
                b.probes += 1
                if (b.probes - 1) % self._p.probe_every == 0:
                    probing.append(b)
                else:
                    ok = False
        if ok:
            for b in probing:
                b.granted += 1  # a probe only when the call is actually admitted
        return ok

    def record(
        self,
        model_id: str,
        region: str,
        tenant_account: str,
        outcome: Outcome,
        ttft_ms: int | None,
    ) -> None:
        """One call's outcome; provider-health signals only (see the module docstring: never `tenant_access` or
        `invalid_request` failures)."""
        now = self._clock.monotonic()
        if outcome != "throttled":
            b = self._service.get((model_id, region))
            if b is None:
                b = self._service[(model_id, region)] = _Breaker()
            failed = outcome != "ok"
            ttft = None if failed or ttft_ms is None else (ttft_ms, tenant_account)
            self._observe(b, (model_id, region, None), now, failed, ttft)
        if outcome in ("ok", "throttled"):
            q = self._quota.get((tenant_account, model_id))
            if q is None:
                if outcome == "ok":
                    return  # nothing to learn for an account that was never throttled
                q = self._quota[(tenant_account, model_id)] = _Breaker()
            self._observe(q, (model_id, region, tenant_account), now, outcome == "throttled", None)

    def state(self, model_id: str, region: str) -> BreakerState:
        b = self._service.get((model_id, region))
        return "closed" if b is None else self._state(b, (model_id, region, None))

    # --- state machine -------------------------------------------------------------------------------------------
    def _state(self, b: _Breaker, key: tuple[str, str, str | None]) -> BreakerState:
        if b.state == "open" and self._clock.monotonic() >= b.opened_until:
            b.state, b.probes, b.granted = "half_open", 0, 0
            self._gauge(key, "half_open")
        return b.state

    def _observe(
        self, b: _Breaker, key: tuple[str, str, str | None], now: float, failed: bool, ttft: tuple[int, str] | None
    ) -> None:
        state = self._state(b, key)
        if state == "open":
            return
        if state == "half_open":
            if b.granted == 0:
                return  # no probe outstanding: the outcome of a call admitted before the open
            b.granted -= 1
            if failed:
                self._open(b, key, now, self._p.open_s)
            else:
                b.state, b.remote = "closed", False
                b.clear_window()
                self._gauge(key, "closed")
                self._publish(key, "closed", None)
            return
        second = math.floor(now)
        new_bucket = not b.buckets or b.buckets[-1].second != second
        if new_bucket:
            self._expire(b, second)
            if key[2] is None and b.ttft_n:
                self._check_ttft(b, key, now, ttft)
                if b.state != "closed":
                    return
            b.buckets.append(_Bucket(second))
        bucket = b.buckets[-1]
        bucket.n += 1
        b.n += 1
        if failed:
            bucket.fails += 1
            b.fails += 1
        elif ttft is not None:
            bucket.ttfts.append(ttft)
            b.ttft_n += 1
        if b.n >= self._p.min_requests and b.fails / b.n > self._p.failure_ratio:
            self._open(b, key, now, self._p.open_s)

    def _expire(self, b: _Breaker, second: int) -> None:
        oldest = second - self._p.window_s + 1
        while b.buckets and b.buckets[0].second < oldest:
            gone = b.buckets.popleft()
            b.n -= gone.n
            b.fails -= gone.fails
            b.ttft_n -= len(gone.ttfts)

    def _check_ttft(
        self, b: _Breaker, key: tuple[str, str, str | None], now: float, incoming: tuple[int, str] | None
    ) -> None:
        """Once per second at most (on a new bucket, over the window plus the `incoming` sample): the p95 TTFT rule
        with its account quorum, which also needs the latest sample above the limit (a window refilled with slow
        samples after a half-open close must not re-open the breaker once the latency is back), then the baseline
        update; and, while another process's breach is fresh, the answer with this window's accounts above its
        limit that it has not reported."""
        samples = [s for bucket in b.buckets for s in bucket.ttfts] + ([incoming] if incoming else [])
        latest = samples[-1]  # the incoming sample when it has one: the slowdown must still be current
        b.slow_remote = {a: until for a, until in b.slow_remote.items() if until > now}
        b.breach_until = -math.inf
        if b.ttft_n >= self._p.min_requests:
            p95 = _p95([t for t, _ in samples])
            armed = b.baseline is not None and b.baseline_n >= self._p.baseline_windows
            limit = math.inf if b.baseline is None else self._p.ttft_factor * b.baseline
            if armed and p95 > limit and latest[0] > limit:
                slow = {account for t, account in samples if t > limit}
                if len(slow | b.slow_remote.keys()) >= self._p.ttft_min_accounts:
                    self._open(b, key, now, self._p.open_s)
                else:
                    b.breach_slow, b.breach_until = slow, now + _BREACH_PENDING_S
                    self._publish_breach(b, key, slow, limit, now)
                return  # a breaching window never feeds the baseline, whether or not it opened the breaker
            if now - b.baseline_at >= self._p.window_s:
                a = self._p.baseline_alpha
                b.baseline = p95 if b.baseline is None else (1 - a) * b.baseline + a * p95
                b.baseline_n += 1
                b.baseline_at = now
        if now < b.asked_until:
            self._publish_breach(b, key, {a for t, a in samples if t > b.asked_limit}, b.asked_limit, now)

    def _open(
        self, b: _Breaker, key: tuple[str, str, str | None], now: float, open_s: float, *, remote: bool = False
    ) -> None:
        b.state, b.opened_until, b.probes, b.remote = "open", now + open_s, 0, remote
        b.clear_window()
        self._gauge(key, "open")
        if not remote:
            self._publish(key, "open", open_s)
            # model id and region in the text: the JSON log formatter keeps only the 08 §7 keys of the extras
            kind = "quota circuit" if key[2] is not None else "circuit"
            extra = {"model": key[0], "region": key[1], "quota": key[2] is not None}
            _LOG.warning("%s open: %s %s", kind, key[0], key[1], extra=extra)

    def _gauge(self, key: tuple[str, str, str | None], state: BreakerState) -> None:
        if self._telemetry is not None and key[2] is None:
            self._telemetry.instruments.set("aghoo_circuit_state", STATE_GAUGE[state], model=key[0], region=key[1])

    # --- cross-pod sync ------------------------------------------------------------------------------------------
    def _publish(self, key: tuple[str, str, str | None], state: BreakerState, open_s: float | None) -> None:
        if self._keys is None:
            return
        payload: dict[str, str | int] = {"model": key[0], "region": key[1], "state": state, "origin": self.origin}
        if key[2] is not None:
            payload["tenant_account"] = key[2]
        if open_s is not None:
            payload["until_ms"] = round(self._clock.now().timestamp() * 1000 + open_s * 1000)
        self._outbox.append(orjson.dumps(payload))
        if self._wake is not None:
            self._wake.set()

    def _publish_breach(
        self, b: _Breaker, key: tuple[str, str, str | None], accounts: set[str], limit: float, now: float
    ) -> None:
        """TTFT evidence for the account quorum (see the module docstring): accounts with samples above `limit` in
        this window that neither another process nor this one reported in the last window (no echo loop)."""
        b.told = {a: until for a, until in b.told.items() if until > now}
        accounts = accounts - b.slow_remote.keys() - b.told.keys()
        if self._keys is None or not accounts:
            return
        for a in accounts:
            b.told[a] = now + self._p.window_s
        until_ms = round(self._clock.now().timestamp() * 1000 + self._p.window_s * 1000)
        payload = {"model": key[0], "region": key[1], "state": "ttft_breach", "accounts": sorted(accounts)}
        self._outbox.append(orjson.dumps({**payload, "limit_ms": limit, "until_ms": until_ms, "origin": self.origin}))
        if self._wake is not None:
            self._wake.set()

    def apply_event(self, raw: bytes | str) -> bool:
        """Apply another pod's `breaker_events` payload; False (ignored) when it is malformed, True (a no-op) for
        this instance's own events. `open` opens a closed or half-open breaker until `until_ms` (as remote) and
        extends an open one to `until_ms` when that is later, keeping it local if local evidence opened it; `closed`
        closes a half-open breaker or one opened remotely (local evidence is never overridden); `ttft_breach` adds
        its accounts to the service breaker's remote TTFT evidence until `until_ms`."""
        try:
            event = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return False
        if not isinstance(event, dict):
            return False
        model, region, state = event.get("model"), event.get("region"), event.get("state")
        account = event.get("tenant_account")
        if not (isinstance(model, str) and isinstance(region, str) and state in _EVENT_STATES):
            return False
        if account is not None and not isinstance(account, str):
            return False
        if event.get("origin") == self.origin:
            return True  # our own transition, echoed by the channel
        if state == "ttft_breach":
            return account is None and self._apply_breach(model, region, event)
        table, bkey = (self._service, (model, region)) if account is None else (self._quota, (account, model))
        key = (model, region, account)
        b = table.get(bkey)
        if state == "open":
            until = event.get("until_ms")
            if not isinstance(until, int | float) or isinstance(until, bool):
                return False
            remaining = (until - self._clock.now().timestamp() * 1000) / 1000
            if remaining <= 0:
                return True
            if b is None:
                b = table[bkey] = _Breaker()
            now = self._clock.monotonic()
            if self._state(b, key) == "open":
                b.opened_until = max(b.opened_until, now + remaining)  # `remote` unchanged: local evidence stays
            else:
                self._open(b, key, now, remaining, remote=True)
            return True
        if state == "closed" and b is not None:
            current = self._state(b, key)
            if current == "half_open" or (current == "open" and b.remote):
                b.state, b.remote = "closed", False
                b.clear_window()
                self._gauge(key, "closed")
        return True

    def _apply_breach(self, model: str, region: str, event: dict[str, object]) -> bool:
        accounts, limit, until = event.get("accounts"), event.get("limit_ms"), event.get("until_ms")
        if not isinstance(accounts, list) or not all(isinstance(a, str) for a in accounts):
            return False
        if not isinstance(limit, int | float) or isinstance(limit, bool):
            return False
        if not isinstance(until, int | float) or isinstance(until, bool):
            return False
        remaining = (until - self._clock.now().timestamp() * 1000) / 1000
        if remaining <= 0:
            return True
        b = self._service.get((model, region))
        if b is None:
            b = self._service[(model, region)] = _Breaker()
        expiry = self._clock.monotonic() + remaining
        for a in accounts:
            b.slow_remote[a] = max(b.slow_remote.get(a, expiry), expiry)
        b.asked_limit, b.asked_until = float(limit), expiry
        now, key = self._clock.monotonic(), (model, region, None)
        if self._state(b, key) != "closed":
            return True
        if now < b.breach_until and len(b.breach_slow | b.slow_remote.keys()) >= self._p.ttft_min_accounts:
            self._open(b, key, now, self._p.open_s)  # the answer completes this process's pending breach
            return True
        self._expire(b, math.floor(now))
        window = {a for bucket in b.buckets for t, a in bucket.ttfts if t > limit}
        self._publish_breach(b, key, window, float(limit), now)  # the answer, at once
        return True

    async def run(self, redis: Redis) -> None:
        """The pub/sub sync task (composition.start, api and jobs): publishes local transitions and applies remote
        ones until cancelled. Redis failures are logged and retried every second; the breakers stay local
        meanwhile."""
        if self._keys is None:
            return
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._publisher, redis)
            await self._subscriber(redis)

    async def _publisher(self, redis: Redis) -> None:
        assert self._keys is not None
        channel = self._keys.breaker_events()
        while True:
            if not self._outbox:
                self._wake = anyio.Event()
                await self._wake.wait()
                self._wake = None
            while self._outbox:
                payload = self._outbox[0]
                try:
                    await redis.publish(channel, payload)
                except Exception as exc:  # Redis down: keep the event, retry later
                    _LOG.warning("breaker event publish failed", extra={"error": type(exc).__name__})
                    await self._clock.sleep(_RESUBSCRIBE_S)
                    continue
                self._outbox.popleft()

    async def _subscriber(self, redis: Redis) -> None:
        assert self._keys is not None
        channel = self._keys.breaker_events()
        while True:
            pubsub = redis.pubsub()
            try:
                await pubsub.subscribe(channel)
                async for message in heartbeat_messages(pubsub, self._clock):
                    if message.get("type") == "message":
                        data = message.get("data")
                        if isinstance(data, bytes | str) and not self.apply_event(data):
                            _LOG.warning("malformed breaker event dropped")
            except Exception as exc:  # Redis down or connection lost: local-only until it is back
                _LOG.warning("breaker event subscription failed", extra={"error": type(exc).__name__})
            finally:
                with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
                    await pubsub.aclose()  # type: ignore[no-untyped-call]  # best-effort close
            await self._clock.sleep(_RESUBSCRIBE_S)
