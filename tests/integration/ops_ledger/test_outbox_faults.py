"""Outbox fault cases on the compose Postgres and Valkey (spec-11 §3, IMPL-022): a dead-lettered turn_finalize
releases its session_pending key; a release is a compare-and-delete (a late release never frees a later turn's
key); two drainers on the same entries apply each once; a Postgres outage adds at most one delivery; claimed
delivery counts are exact; through toxiproxy, a blackholed Redis never slows the batch writer and a slow Redis
still drains exactly once."""

from __future__ import annotations

from typing import Any, cast

import anyio
import pytest
from redis.asyncio import Redis
from tests.fakes.clock import FakeClock
from tests.fakes.telemetry import FakeTelemetry
from tests.fixtures.db import SeededTenant
from tests.integration.db_repos._rows import new_id

from noema.core.protocols.storage import Database, OutboxRecord
from noema.core.redis_keys import RedisKeys
from noema.ops.outbox import FIELD, GROUP, OutboxDrainer, PendingReleases, WriteBehindOutbox, encode

from .conftest import SwitchDb, SwitchRedis, ValkeyProxy
from .test_outbox import finalize, line, pieces, stream_len, usage_ids

pytestmark = pytest.mark.integration


async def dead_letters(pg_admin: Any, record_id: str) -> list[Any]:
    return list(await pg_admin.fetch("SELECT * FROM ops.outbox_dead_letter WHERE id = $1", record_id))


def poison(tenant: SeededTenant, i: int) -> OutboxRecord:
    """A usage line the database rejects every time (`tenant_mismatch`)."""
    good = line(tenant, i)
    return OutboxRecord("usage_line", good.id, good.tenant_id, {**good.payload, "tenant_id": "t_other"})


async def session(pg_admin: Any, tenant: SeededTenant) -> tuple[str, str]:
    """(session id, active turn id) of a session whose lease expired while its turn's finalize is pending."""
    sid, tid = new_id("ses"), new_id("turn")
    await pg_admin.execute(
        "INSERT INTO noema.session (tenant_id, project_id, id, owner_principal_id, data_class, active_turn_id,"
        " lease_expires_at) VALUES ($1, 'prj_a', $2, $3, 'internal', $4, now() - interval '1 minute')",
        tenant.tenant_id, sid, tenant.principal_ids["prj_a"], tid,
    )  # fmt: skip
    return sid, tid


async def test_a_dead_lettered_turn_finalize_releases_session_pending(
    ledger_db: SwitchDb, valkey: SwitchRedis, keys: RedisKeys, tenant: SeededTenant, pg_admin: Any
) -> None:
    sid, tid = new_id("ses"), new_id("turn")  # no such session: every apply is a seq_conflict
    pending = keys.session_pending(sid)
    await valkey.real.set(pending, tid.encode(), ex=86_400)
    outbox, drainer, _, _ = pieces(ledger_db, valkey, keys, min_idle_ms=0)
    ledger_db.up = False
    outbox.put(finalize(tenant, sid, tid, 0))
    await outbox.flush(5)
    ledger_db.up = True
    for _ in range(9):
        assert await drainer.drain_once() == 0
        assert await valkey.real.exists(pending) == 1  # still busy while it is retried
    assert await drainer.drain_once() == 1  # the 10th delivery: dead-lettered
    [dead] = await dead_letters(pg_admin, tid)
    assert (dead["kind"], dead["error"], dead["attempts"]) == ("turn_finalize", "seq_conflict", 10)
    assert await valkey.real.exists(pending) == 0  # released after outbox_dead too (spec-11 §3)


async def test_a_late_release_never_frees_a_later_turns_key(valkey: SwitchRedis, keys: RedisKeys) -> None:
    releases = PendingReleases(cast(Any, valkey), keys, FakeClock())
    await valkey.real.set(keys.session_pending("ses_a"), b"turn_T2")  # T1 released long ago; T2 now pending
    await valkey.real.set(keys.session_pending("ses_b"), b"turn_B1")
    releases.add(
        [
            OutboxRecord("turn_finalize", "turn_T1", "t_x", {}, "ses_a"),  # a retried or re-applied release of T1
            OutboxRecord("turn_finalize", "turn_B1", "t_x", {}, "ses_b"),
            OutboxRecord("usage_line", "line_1", "t_x", {}, "ses_c"),  # not a finalize: nothing to release
        ]
    )
    assert await releases.retry() and not releases.outstanding
    assert await valkey.real.get(keys.session_pending("ses_a")) == b"turn_T2"
    assert await valkey.real.exists(keys.session_pending("ses_b")) == 0


async def test_two_drainers_on_the_same_entries_apply_each_once(
    ledger_db: SwitchDb, valkey: SwitchRedis, keys: RedisKeys, tenant: SeededTenant, pg_admin: Any
) -> None:
    outbox, pod_a, _, _ = pieces(ledger_db, valkey, keys, min_idle_ms=0)  # min idle 0: each claims the other's
    _, pod_b, _, _ = pieces(ledger_db, valkey, keys, min_idle_ms=0)
    pod_b._consumer = "pod-b"
    ledger_db.up = False
    for i in range(40):
        outbox.put(line(tenant, i))
    await outbox.flush(5)
    ledger_db.up = True
    with anyio.fail_after(20):
        while await stream_len(valkey, keys):
            async with anyio.create_task_group() as tg:
                tg.start_soon(pod_a.drain_once)
                tg.start_soon(pod_b.drain_once)
    assert await usage_ids(pg_admin, tenant) == [f"{i:026d}" for i in range(40)]  # each exactly once
    assert (
        await pg_admin.fetchval("SELECT count(*) FROM ops.outbox_dead_letter WHERE tenant_id = $1", tenant.tenant_id)
        == 0
    )


async def pending_count(redis: SwitchRedis, stream: str, entry_id: bytes) -> int:
    [row] = await redis.real.xpending_range(stream, GROUP, entry_id, entry_id, 1)
    return int(row["times_delivered"])


async def test_a_postgres_outage_adds_at_most_one_delivery(
    ledger_db: SwitchDb, valkey: SwitchRedis, keys: RedisKeys, tenant: SeededTenant, pg_admin: Any
) -> None:
    outbox, drainer, _, _ = pieces(ledger_db, valkey, keys, min_idle_ms=0)  # claims every cycle, as after 30 s
    bad = poison(tenant, 4)
    stream = keys.outbox(RedisKeys.outbox_shard(tenant.tenant_id))
    ledger_db.up = False
    outbox.put(bad)
    await outbox.flush(5)
    for _ in range(12):  # a long outage: the first cycle reads the entry (delivery 1) and finds Postgres down
        assert await drainer.drain_once() == 0
    [(entry_id, _)] = cast(list[tuple[bytes, Any]], await valkey.real.xrange(stream))
    assert await pending_count(valkey, stream, entry_id) == 1  # the later cycles only probed Postgres
    ledger_db.up = True
    assert await drainer.drain_once() == 0  # the first real apply after recovery fails: retried, not dead
    assert await pending_count(valkey, stream, entry_id) == 2 and await dead_letters(pg_admin, bad.id) == []
    for _ in range(7):
        assert await drainer.drain_once() == 0
    assert await drainer.drain_once() == 1  # dead at the 10th delivery, as without the outage
    assert [d["attempts"] for d in await dead_letters(pg_admin, bad.id)] == [10]


async def test_claimed_delivery_counts_are_exact(
    ledger_db: SwitchDb, valkey: SwitchRedis, keys: RedisKeys, tenant: SeededTenant, pg_admin: Any
) -> None:
    _, drainer, _, _ = pieces(ledger_db, valkey, keys)  # consumer pod-a, min idle 30 s
    stream = keys.outbox(RedisKeys.outbox_shard(tenant.tenant_id))
    await valkey.real.xgroup_create(stream, GROUP, id="0", mkstream=True)
    a, b, c = (poison(tenant, i) for i in (1, 2, 3))
    ids = [cast(bytes, await valkey.real.xadd(stream, {FIELD: encode(r)})) for r in (a, b, c)]
    await valkey.real.xreadgroup(GROUP, "pod-a", {stream: ">"})  # all three pending on pod-a: delivery 1
    for _ in range(7):
        await valkey.real.xclaim(stream, GROUP, "pod-a", 0, [ids[0], ids[2]])  # A and C: 8 deliveries
    await valkey.real.xclaim(stream, GROUP, "pod-a", 0, [ids[0], ids[2]], idle=60_000)  # 9, idle for 60 s
    await valkey.real.xclaim(stream, GROUP, "pod-a", 0, [ids[1]], idle=0)  # B, between them: retried just now
    assert await drainer.drain_once() == 2  # A and C claimed at delivery 10 (not 1): both dead-lettered
    assert [d["attempts"] for r in (a, c) for d in await dead_letters(pg_admin, r.id)] == [10, 10]
    assert await dead_letters(pg_admin, b.id) == [] and await pending_count(valkey, stream, ids[1]) == 2


async def test_a_blackholed_redis_never_slows_the_writer(
    ledger_db: SwitchDb, valkey: SwitchRedis, valkey_proxy: ValkeyProxy, keys: RedisKeys, tenant: SeededTenant,
    pg_admin: Any,
) -> None:  # fmt: skip
    sid, tid = await session(pg_admin, tenant)
    pending = keys.session_pending(sid)
    await valkey.real.set(pending, tid.encode(), ex=86_400)
    through_proxy = Redis.from_url(valkey_proxy.url, decode_responses=False, socket_connect_timeout=0.5)
    clock = FakeClock()  # virtual: only the test moves it
    outbox = WriteBehindOutbox(cast(Database, ledger_db), through_proxy, keys, clock, FakeTelemetry().instruments)
    releases = outbox.pending_releases
    valkey_proxy.blackhole()  # Redis replies are lost; nothing is refused
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(releases.run)
            outbox.put(finalize(tenant, sid, tid, 0))
            with anyio.fail_after(5):
                assert await outbox.write_once() == 1  # applied; its release now waits on Redis in the background
            for cycle in range(3):
                for i in range(500):
                    outbox.put(line(tenant, 1000 * (cycle + 1) + i))
                with anyio.fail_after(5):  # Postgres time only (the unbounded DEL used to add ~10 s per cycle)
                    assert await outbox.write_once() == 500
            assert clock.monotonic() == FakeClock().monotonic()  # no Redis timeout was waited for
            assert len(await usage_ids(pg_admin, tenant)) == 1500
            assert releases.outstanding == {(sid, tid)} and await valkey.real.exists(pending) == 1
            valkey_proxy.heal()
            with anyio.fail_after(20):
                while releases.outstanding:  # each step fires the 2 s bound of a stuck attempt or a backoff
                    await clock.advance(1.0)
                    await anyio.sleep(0.02)
            assert await valkey.real.exists(pending) == 0
            tg.cancel_scope.cancel()
    finally:
        valkey_proxy.heal()
        await through_proxy.aclose()


async def test_a_slow_redis_still_drains_exactly_once(
    ledger_db: SwitchDb, valkey: SwitchRedis, valkey_proxy: ValkeyProxy, keys: RedisKeys, tenant: SeededTenant,
    pg_admin: Any,
) -> None:  # fmt: skip
    sid, tid = await session(pg_admin, tenant)
    pending = keys.session_pending(sid)
    await valkey.real.set(pending, tid.encode(), ex=86_400)
    outbox, _, _, telemetry = pieces(ledger_db, valkey, keys)
    ledger_db.up = False
    outbox.put(finalize(tenant, sid, tid, 0))
    for i in range(30):
        outbox.put(line(tenant, i))
    await outbox.flush(5)
    ledger_db.up = True
    through_proxy = Redis.from_url(valkey_proxy.url, decode_responses=False, socket_connect_timeout=0.5)
    drainer = OutboxDrainer(
        cast(Database, ledger_db), through_proxy, keys, FakeClock(), telemetry.instruments, consumer="pod-slow"
    )
    valkey_proxy.latency(40)  # every reply 40 ms late
    try:
        with anyio.fail_after(30):
            while await stream_len(valkey, keys):
                await drainer.drain_once()
    finally:
        valkey_proxy.heal()
        await through_proxy.aclose()
    assert await usage_ids(pg_admin, tenant) == [f"{i:026d}" for i in range(30)]
    assert await valkey.real.exists(pending) == 0
    assert await pg_admin.fetchval("SELECT last_seq FROM noema.session WHERE id = $1", sid) == 1
