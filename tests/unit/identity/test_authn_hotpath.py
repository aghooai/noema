"""`ApiKeyAuthenticator` hot-path loads: the pepper ring (stale-while-revalidate, single-flight first load with a
negative cache, a previous-version read failure that keeps the loaded pepper) and single-flight key lookups."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import pytest
from tests.fakes.db_system import FakeSystemCalls
from tests.fakes.secrets import FakeSecretsProvider
from tests.unit.identity.helpers import KEY_ID, PEPPER_V2, PEPPER_V3, AuthnRig, authn_rig, bearer, seed_key

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Row
from noema.identity.apikeys import PEPPER_REF, PREVIOUS_VERSION, pepper_value
from noema.identity.authn import PEPPER_NEGATIVE_S, PEPPER_RETRY_S, PEPPER_TTL_S

IP = "198.51.100.1"


class ScriptedSecrets:
    """Wraps a `FakeSecretsProvider`: `down` versions raise the backend-unavailable error, `gate` holds every read
    until set; `reads` and `peak` count backend calls and the most reads in flight at once."""

    def __init__(self, inner: FakeSecretsProvider) -> None:
        self.inner = inner
        self.down: set[str | None] = set()
        self.gate: anyio.Event | None = None
        self.reads = 0
        self.peak = 0
        self._inflight = 0

    async def get(self, ref: str, *, version: str | None = None) -> bytes:
        self.reads += 1
        self._inflight += 1
        self.peak = max(self.peak, self._inflight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if version in self.down:
                raise NoemaError(ErrorCode.INTERNAL_ERROR, "The secrets backend is unavailable.")
            return await self.inner.get(ref, version=version)
        finally:
            self._inflight -= 1


def scripted(rig: AuthnRig) -> ScriptedSecrets:
    wrapper = ScriptedSecrets(rig.secrets)
    rig.authn._secrets = wrapper
    return wrapper


@asynccontextmanager
async def running(rig: AuthnRig) -> AsyncIterator[AuthnRig]:
    await rig.runtime.open()
    try:
        yield rig
    finally:
        await rig.runtime.close()


def no_rehash(rig: AuthnRig) -> None:
    """Keep previous-pepper keys on their version (as if the re-hash had not run yet)."""
    rig.authn._spawn = lambda *_a, **_k: None  # type: ignore[method-assign]


async def code_of(rig: AuthnRig, token: str) -> ErrorCode | None:
    try:
        await rig.authn.authenticate(bearer(token), IP)
    except NoemaError as exc:
        return exc.code
    return None


async def reloaded(rig: AuthnRig) -> None:
    """Let the background pepper reload finish (it reads both versions in a task group)."""
    for _ in range(10):
        await rig.clock.settle()
        if not rig.authn._ring_refreshing:
            return
    raise AssertionError("the pepper reload did not finish")


def rotate_to_v3(rig: AuthnRig) -> None:
    rig.secrets.put(PEPPER_REF, pepper_value(2, PEPPER_V2), version=PREVIOUS_VERSION)
    rig.secrets.put(PEPPER_REF, pepper_value(3, PEPPER_V3))


async def test_previous_read_failure_keeps_the_loaded_previous_pepper() -> None:
    """A backend error on the AWSPREVIOUS read at a reload keeps the loaded previous pepper (no 401 for keys still
    on it) and is retried after 10 s instead of 300 s."""
    rig = authn_rig(current_pepper=3)  # current v3, AWSPREVIOUS v2
    token_v2 = seed_key(rig.db, pepper_version=2)
    no_rehash(rig)
    secrets = scripted(rig)
    async with running(rig):
        assert await code_of(rig, token_v2) is None
        secrets.down.add(PREVIOUS_VERSION)
        await rig.clock.advance(PEPPER_TTL_S + 1)  # the provider's own cache expired as well
        assert await code_of(rig, token_v2) is None
        await reloaded(rig)
        ring = rig.authn._ring
        assert ring is not None and ring.partial and ring.previous is not None and ring.previous.version == 2
        await rig.clock.advance(PEPPER_RETRY_S)
        assert await code_of(rig, token_v2) is None  # still failing: retried, still kept
        await reloaded(rig)
        secrets.down.clear()
        reads = secrets.reads
        await rig.clock.advance(PEPPER_RETRY_S)
        assert await code_of(rig, token_v2) is None
        await reloaded(rig)
        assert secrets.reads > reads
        ring = rig.authn._ring
        assert ring is not None and not ring.partial and ring.previous is not None and ring.previous.version == 2
    assert await rig.limiter.check("authfail:" + IP, rate_per_s=10.0, burst=50) is None


async def test_previous_unreadable_on_the_first_load_is_500_not_401() -> None:
    """With no previous pepper known, a key on an older version cannot be judged: 500 (no auth-failure debit),
    and the ring is reloaded 10 s later."""
    rig = authn_rig(current_pepper=3)
    token_v2 = seed_key(rig.db, pepper_version=2)
    no_rehash(rig)
    secrets = scripted(rig)
    secrets.down.add(PREVIOUS_VERSION)
    async with running(rig):
        for _ in range(60):
            assert await code_of(rig, token_v2) is ErrorCode.INTERNAL_ERROR
        assert await rig.limiter.check("authfail:" + IP, rate_per_s=10.0, burst=50) is None
        secrets.down.clear()
        await rig.clock.advance(PEPPER_RETRY_S)
        await code_of(rig, token_v2)  # triggers the background reload
        await reloaded(rig)
        assert await code_of(rig, token_v2) is None


async def test_reload_serves_the_stale_ring_while_one_background_load_runs() -> None:
    """Past the TTL, requests keep the loaded ring at once; one reload (current and previous read concurrently)
    runs in the background and its result applies afterwards."""
    rig = authn_rig()
    token = seed_key(rig.db)
    secrets = scripted(rig)
    async with running(rig):
        assert await code_of(rig, token) is None
        rotate_to_v3(rig)
        await rig.clock.advance(PEPPER_TTL_S + 1)
        secrets.gate = anyio.Event()
        reads = secrets.reads
        done: list[ErrorCode | None] = []

        async def one() -> None:
            done.append(await code_of(rig, token))

        async with anyio.create_task_group() as tg:
            for _ in range(50):
                tg.start_soon(one)
        assert done == [None] * 50  # nobody waited for Secrets Manager
        assert secrets.reads - reads == 2 and secrets.peak == 2  # one reload, both versions in parallel
        assert rig.authn._ring_refreshing
        secrets.gate.set()
        await reloaded(rig)
        ring = rig.authn._ring
        assert ring is not None and ring.current.version == 3 and ring.previous is not None
        assert ring.previous.version == 2
        assert await code_of(rig, token) is None  # the v2 key now verifies with the previous pepper


async def test_failed_reload_keeps_the_ring_and_retries_after_10_s() -> None:
    rig = authn_rig()
    token = seed_key(rig.db)
    secrets = scripted(rig)
    async with running(rig):
        assert await code_of(rig, token) is None
        secrets.down.update({None, PREVIOUS_VERSION})
        await rig.clock.advance(PEPPER_TTL_S + 1)
        reads = secrets.reads
        for _ in range(5):
            assert await code_of(rig, token) is None
            await reloaded(rig)
        assert secrets.reads - reads == 2  # one attempt, then the 10 s back-off
        await rig.clock.advance(PEPPER_RETRY_S)
        assert await code_of(rig, token) is None
        await reloaded(rig)
        assert secrets.reads - reads == 4


async def test_cold_load_is_shared_and_its_failure_fails_fast_for_5_s() -> None:
    """Secrets Manager down before any ring is loaded: concurrent requests share one load and all get 500; for the
    next 5 s requests fail without another read (no convoy of timeouts)."""
    rig = authn_rig()
    token = seed_key(rig.db)
    secrets = scripted(rig)
    secrets.down.update({None, PREVIOUS_VERSION})
    secrets.gate = anyio.Event()
    codes: list[ErrorCode | None] = []

    async def one() -> None:
        codes.append(await code_of(rig, token))

    async with running(rig):
        async with anyio.create_task_group() as tg:
            for _ in range(20):
                tg.start_soon(one)
            await rig.clock.settle()
            assert secrets.reads == 2 and codes == []  # one load in flight, everyone waits on it
            secrets.gate.set()
        assert codes == [ErrorCode.INTERNAL_ERROR] * 20 and secrets.reads == 2
        await rig.clock.advance(PEPPER_NEGATIVE_S - 0.1)
        assert await code_of(rig, token) is ErrorCode.INTERNAL_ERROR and secrets.reads == 2
        secrets.down.clear()
        await rig.clock.advance(0.1)
        assert await code_of(rig, token) is None and secrets.reads == 4
    assert await rig.limiter.check("authfail:" + IP, rate_per_s=10.0, burst=50) is None  # 500s never debit


async def test_start_preloads_the_ring_and_tolerates_failure() -> None:
    rig = authn_rig()
    token = seed_key(rig.db)
    secrets = scripted(rig)
    await rig.authn.start()
    assert secrets.reads == 2 and rig.authn._ring is not None
    async with running(rig):
        assert await code_of(rig, token) is None and secrets.reads == 2
    down = authn_rig()
    down_secrets = scripted(down)
    down_secrets.down.add(None)
    await down.authn.start()  # logged, not raised
    assert down.authn._ring is None


def gated_lookups(monkeypatch: pytest.MonkeyPatch, gate: anyio.Event, *, fail: bool = False) -> list[str]:
    calls: list[str] = []
    original = FakeSystemCalls.lookup_api_key

    async def lookup(self: FakeSystemCalls, key_id: str) -> Row | None:
        calls.append(key_id)
        await gate.wait()
        if fail:
            raise NoemaError(ErrorCode.INTERNAL_ERROR, "The database is temporarily unavailable.")
        return await original(self, key_id)

    monkeypatch.setattr(FakeSystemCalls, "lookup_api_key", lookup)
    return calls


async def concurrent(rig: AuthnRig, token: str, n: int, gate: anyio.Event) -> list[ErrorCode | None]:
    codes: list[ErrorCode | None] = []

    async def one() -> None:
        codes.append(await code_of(rig, token))

    async with anyio.create_task_group() as tg:
        for _ in range(n):
            tg.start_soon(one)
        await rig.clock.settle()
        gate.set()
    return codes


async def test_concurrent_misses_of_one_key_share_one_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = authn_rig()
    token = seed_key(rig.db)
    gate = anyio.Event()
    calls = gated_lookups(monkeypatch, gate)
    async with running(rig):
        await rig.authn.start()
        assert await concurrent(rig, token, 200, gate) == [None] * 200
        assert calls == [KEY_ID]
        assert await code_of(rig, token) is None and calls == [KEY_ID]  # cached
        await rig.clock.advance(60.0)  # TTL: the hot key expires, again one lookup for all
        assert await concurrent(rig, token, 50, gate) == [None] * 50  # gate already open
        assert calls == [KEY_ID, KEY_ID]


async def test_a_failed_shared_lookup_fails_every_waiter_and_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = authn_rig()
    token = seed_key(rig.db)
    gate = anyio.Event()
    calls = gated_lookups(monkeypatch, gate, fail=True)
    async with running(rig):
        await rig.authn.start()
        assert await concurrent(rig, token, 30, gate) == [ErrorCode.INTERNAL_ERROR] * 30
        assert calls == [KEY_ID] and rig.authn.cache.peek(KEY_ID) is None


async def test_a_revocation_during_the_shared_lookup_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = authn_rig()
    token = seed_key(rig.db)
    gate = anyio.Event()
    calls = gated_lookups(monkeypatch, gate)
    async with running(rig):
        await rig.authn.start()
        codes: list[ErrorCode | None] = []

        async def one() -> None:
            codes.append(await code_of(rig, token))

        async with anyio.create_task_group() as tg:
            for _ in range(10):
                tg.start_soon(one)
            await rig.clock.settle()
            rig.authn.evict(KEY_ID)  # keys_revoked arrives while the shared lookup is in flight
            gate.set()
        assert codes == [None] * 10 and calls == [KEY_ID]  # the read in flight still answers its callers
        assert rig.authn.cache.peek(KEY_ID) is None  # but it is not cached
        await code_of(rig, token)
        assert calls == [KEY_ID, KEY_ID]


async def test_a_cancelled_leader_hands_the_lookup_to_a_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = authn_rig()
    token = seed_key(rig.db)
    gate = anyio.Event()
    calls = gated_lookups(monkeypatch, gate)
    codes: list[ErrorCode | None] = []
    leader = anyio.CancelScope()

    async def one() -> None:
        codes.append(await code_of(rig, token))

    async def lead() -> None:
        with leader:
            await one()

    async with running(rig):
        await rig.authn.start()
        async with anyio.create_task_group() as tg:
            tg.start_soon(lead)
            await rig.clock.settle()
            assert calls == [KEY_ID]
            for _ in range(5):
                tg.start_soon(one)
            await rig.clock.settle()
            leader.cancel()  # the leading request is cancelled (client gone)
            await rig.clock.settle()
            gate.set()
    assert leader.cancelled_caught and codes == [None] * 5 and calls == [KEY_ID, KEY_ID]
