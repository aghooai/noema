"""The Redis Streams mirror on the compose Valkey (IMPL-021, spec-02 §13): explicit stream ids, MAXLEN, the orphan
and seal TTLs, live remote follow across two "pods" with byte-identical frames (G12b), the turn_attach heartbeat
of the abandon watchdog, and the fail-safe paths (gap, rejected write, Redis down, a dead writer's follower)."""

from __future__ import annotations

import contextlib
import urllib.parse
from collections.abc import AsyncIterator

import anyio
import anyio.abc
import pytest
import redis.asyncio

from noema.core.clock import SystemClock
from noema.core.protocols.events import SseFrame
from noema.core.redis_keys import RedisKeys
from noema.streaming import InProcessTurnEventLog, RedisEventMirror

from .redis_support import redis_namespace, second_client

pytestmark = pytest.mark.integration
T = "turn_1"


def created() -> dict[str, str]:
    return {"type": "turn.created", "turn_id": T}


def delta(i: int) -> dict[str, str | int]:
    return {"type": "generation.delta", "attempt_id": "att_1", "seq": i, "text": f"<{i}>"}


def completed() -> dict[str, str]:
    return {"type": "turn.completed", "turn_id": T}


async def frames_of(it: AsyncIterator[SseFrame] | None) -> list[SseFrame]:
    assert it is not None
    with anyio.fail_after(10):
        return [f async for f in it]


async def attached(log: InProcessTurnEventLog) -> None:
    """Until the remote reader's first `turn_attach` heartbeat has landed (bounded polling, no fixed sleep)."""
    for _ in range(500):  # 5 s
        if await log.idle_seconds(T) is None:
            return
        await anyio.sleep(0.01)
    raise AssertionError("the remote reader never attached")


async def test_frames_are_written_with_their_ids_and_the_ttls(noema_services: object, noema_run_id: str) -> None:
    url = noema_services.redis_url  # type: ignore[attr-defined]
    async with redis_namespace(url, noema_run_id) as (client, keys):
        mirror = RedisEventMirror(client, keys, SystemClock(), orphan_ttl_s=900)
        log = InProcessTurnEventLog(SystemClock(), mirror=mirror)
        log.append(T, created())
        log.append(T, delta(0))
        await mirror.drain()
        key = keys.turn_events(T)
        entries = await client.xrange(key)
        assert entries is not None and [e[0] for e in entries] == [b"1-0", b"2-0"]
        assert entries[1][1] == {b"t": b"generation.delta", b"p": log._logs[T].frames[1].payload}
        assert 0 < await client.ttl(key) <= 900  # the orphan TTL while the turn runs
        log.append(T, completed())
        log.seal(T, ttl_s=60)
        await mirror.drain()
        assert 0 < await client.ttl(key) <= 60  # the resume window after seal
        assert mirror.pending == 0


async def test_a_remote_pod_follows_live_and_byte_identical(noema_services: object, noema_run_id: str) -> None:
    url = noema_services.redis_url  # type: ignore[attr-defined]
    clock = SystemClock()
    async with redis_namespace(url, noema_run_id) as (client, keys):
        other = second_client(url)
        try:
            mirror_a = RedisEventMirror(client, keys, clock)
            mirror_b = RedisEventMirror(other, keys, clock, block_ms=100, beat_s=0.05)
            pod_a = InProcessTurnEventLog(clock, mirror=mirror_a)
            pod_b = InProcessTurnEventLog(clock, mirror=mirror_b)
            pod_a.append(T, created())
            await mirror_a.drain()
            got: list[SseFrame] = []
            all_in = anyio.Event()
            async with anyio.create_task_group() as tg:
                tg.start_soon(mirror_a.run)

                async def remote() -> None:
                    got.extend(await frames_of(await pod_b.attach_remote(T)))
                    all_in.set()

                tg.start_soon(remote)
                await attached(pod_a)  # a remote reader is attached (turn_attach a:): idle_seconds None
                for i in range(5):
                    pod_a.append(T, delta(i))
                    await anyio.sleep(0.01)
                pod_a.append(T, completed())
                with anyio.fail_after(10):
                    await all_in.wait()  # the remote stream ends by itself after turn.completed
                tg.cancel_scope.cancel()
            local = [f async for f in pod_a.subscribe(T)]
            assert [f.wire for f in got] == [f.wire for f in local]
            idle = await pod_a.idle_seconds(T)
            assert idle is not None and idle < 5  # detached: seconds since the last remote delivery
        finally:
            await other.aclose()


async def test_a_remote_reader_of_a_dead_writer_ends_after_follow_idle_s(
    noema_services: object, noema_run_id: str
) -> None:
    url = noema_services.redis_url  # type: ignore[attr-defined]
    async with redis_namespace(url, noema_run_id) as (client, keys):
        dead = RedisEventMirror(client, keys, SystemClock())
        pod_a = InProcessTurnEventLog(SystemClock(), mirror=dead)
        pod_a.append(T, created())
        pod_a.append(T, delta(0))
        await dead.drain()  # the owner pod dies here: no turn.completed, the key keeps its 3600 s orphan TTL
        follower = RedisEventMirror(client, keys, SystemClock(), block_ms=50, follow_idle_s=0.3)
        pod_b = InProcessTurnEventLog(SystemClock(), mirror=follower)
        started = anyio.current_time()
        frames = await frames_of(await pod_b.attach_remote(T))
        assert [f.id for f in frames] == [1, 2]
        assert 0.3 <= anyio.current_time() - started < 5  # ended by the idle bound, not by the key's expiry
        assert await client.ttl(keys.turn_events(T)) > 3000


async def test_attach_remote_is_none_without_a_mirror_key(noema_services: object, noema_run_id: str) -> None:
    url = noema_services.redis_url  # type: ignore[attr-defined]
    async with redis_namespace(url, noema_run_id) as (client, keys):
        log = InProcessTurnEventLog(SystemClock(), mirror=RedisEventMirror(client, keys, SystemClock()))
        assert await log.attach_remote("turn_missing") is None


async def test_a_gap_ends_the_remote_stream(noema_services: object, noema_run_id: str) -> None:
    url = noema_services.redis_url  # type: ignore[attr-defined]
    async with redis_namespace(url, noema_run_id) as (client, keys):
        mirror = RedisEventMirror(client, keys, SystemClock(), block_ms=50)
        log = InProcessTurnEventLog(SystemClock(), mirror=mirror)
        for event in (created(), delta(0), delta(1), completed()):
            log.append(T, event)
        await mirror.drain()
        await client.xdel(keys.turn_events(T), "2-0")
        frames = await frames_of(await log.attach_remote(T))
        assert [f.id for f in frames] == [1]  # stops at the gap instead of skipping frames


async def test_a_rejected_write_breaks_the_turn_mirror(noema_services: object, noema_run_id: str) -> None:
    url = noema_services.redis_url  # type: ignore[attr-defined]
    async with redis_namespace(url, noema_run_id) as (client, keys):
        await client.set(keys.turn_events(T), b"not a stream")  # XADD -> WRONGTYPE
        mirror = RedisEventMirror(client, keys, SystemClock())
        log = InProcessTurnEventLog(SystemClock(), mirror=mirror)
        log.append(T, created())
        await mirror.drain()
        log.append(T, delta(0))  # skipped: the turn's mirror is broken
        await mirror.drain()  # ... and its key is deleted on the next write
        assert await client.exists(keys.turn_events(T)) == 0
        assert await log.attach_remote(T) is None
        log.append(T, completed())
        assert [f.id async for f in log.subscribe(T)] == [1, 2, 3]  # the local log is unaffected


async def test_redis_down_never_breaks_the_local_log() -> None:
    dead = redis.asyncio.Redis.from_url("redis://127.0.0.1:1/0", decode_responses=False, socket_connect_timeout=0.2)
    keys = RedisKeys("ns:test-down:")
    mirror = RedisEventMirror(dead, keys, SystemClock(), retry_s=0.01)
    log = InProcessTurnEventLog(SystemClock(), mirror=mirror)
    try:
        log.append(T, created())
        assert await mirror.flush() is False
        log.append(T, completed())
        await mirror.drain()
        assert await log.attach_remote(T) is None
        assert await log.idle_seconds(T) is not None  # local accounting only
        assert [f.id async for f in log.subscribe(T)] == [1, 2]
    finally:
        await dead.aclose()


async def test_a_rejected_write_breaks_only_its_turn_in_a_shared_batch(
    noema_services: object, noema_run_id: str
) -> None:
    url = noema_services.redis_url  # type: ignore[attr-defined]
    async with redis_namespace(url, noema_run_id) as (client, keys):
        await client.set(keys.turn_events("turn_bad"), b"not a stream")  # its XADD -> WRONGTYPE inside the script
        mirror = RedisEventMirror(client, keys, SystemClock(), orphan_ttl_s=900)
        log = InProcessTurnEventLog(SystemClock(), mirror=mirror)
        for turn in ("turn_bad", T):  # one batch, one script call for both turns
            log.append(turn, created())
            log.append(turn, delta(0))
        await mirror.drain()
        entries = await client.xrange(keys.turn_events(T))
        assert entries is not None and [e[0] for e in entries] == [b"1-0", b"2-0"]
        assert 0 < await client.ttl(keys.turn_events(T)) <= 900
        assert mirror._broken == {"turn_bad"}
        log.append(T, completed())
        log.seal(T, ttl_s=60)
        log.append("turn_bad", completed())  # skipped; the next batch deletes the broken turn's key
        await mirror.drain()
        assert await client.exists(keys.turn_events("turn_bad")) == 0
        assert [f.id for f in await frames_of(await log.attach_remote(T))] == [1, 2, 3]
        assert 0 < await client.ttl(keys.turn_events(T)) <= 60


class _BlackHole:
    """A TCP proxy to the compose Valkey that, while `drop` is set, swallows the client's bytes (connections stay
    open, nothing is answered: toxiproxy's `timeout` toxic, F10 'hung')."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port, self.drop = host, port, False

    async def start(self, tg: anyio.abc.TaskGroup) -> int:
        listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
        tg.start_soon(listener.serve, self._handle)
        return int(listener.extra(anyio.abc.SocketAttribute.local_port))

    async def _handle(self, client: anyio.abc.SocketStream) -> None:
        async with client, await anyio.connect_tcp(self.host, self.port) as up:

            async def c2s() -> None:
                async for data in client:
                    if not self.drop:
                        await up.send(data)

            async def s2c() -> None:
                async for data in up:
                    await client.send(data)

            with contextlib.suppress(Exception):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(c2s)
                    tg.start_soon(s2c)


async def test_a_hung_redis_wedges_neither_the_writer_nor_the_watchdog(
    noema_services: object, noema_run_id: str
) -> None:
    """F10 'hung' on the mirror (Wave E fix): while Redis swallows every request the batch fails within
    `write_timeout_s` and the watchdog's `idle_seconds` answers within `call_timeout_s`; once it answers again a turn
    started after the outage is mirrored and followed."""
    url = urllib.parse.urlsplit(noema_services.redis_url)  # type: ignore[attr-defined]
    async with redis_namespace(noema_services.redis_url, noema_run_id) as (direct, keys):  # type: ignore[attr-defined]
        proxy = _BlackHole(url.hostname or "127.0.0.1", url.port or 6379)
        async with anyio.create_task_group() as tg:
            port = await proxy.start(tg)
            client = redis.asyncio.Redis.from_url(f"redis://127.0.0.1:{port}/0", decode_responses=False)
            mirror = RedisEventMirror(client, keys, SystemClock(), call_timeout_s=0.2, write_timeout_s=0.5)
            log = InProcessTurnEventLog(SystemClock(), mirror=mirror)
            log.append("turn_a", created())
            await mirror.drain()
            assert await direct.exists(keys.turn_events("turn_a")) == 1
            proxy.drop = True
            log.append("turn_a", delta(0))
            with anyio.fail_after(2):
                assert await mirror.flush() is False  # the batch failed: turn_a is broken, not wedged
                assert await log.idle_seconds("turn_a") is not None
            proxy.drop = False
            log.append("turn_b", {"type": "turn.created", "turn_id": "turn_b"})
            log.append("turn_b", {"type": "turn.completed", "turn_id": "turn_b"})
            with anyio.fail_after(5):
                await mirror.drain()
            assert [f.id for f in await frames_of(await log.attach_remote("turn_b"))] == [1, 2]
            await client.aclose()
            tg.cancel_scope.cancel()
