"""T-OBX-1 / T-OBX-2 (spec-11 §8) on the compose Postgres and Valkey: Postgres down -> Redis -> drained exactly
once (replays are no-ops); both down -> memory, then delivered; a dead pod's entries are claimed; delivery count
10 -> dead letter; turn_finalize keeps the session busy (session_pending: the next turn's acquire is Busy) until
the drain, then the next turn gets the next seq; replays are no-ops. Fault cases: test_outbox_faults.py."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from tests.fakes.clock import FakeClock
from tests.fakes.telemetry import FakeTelemetry
from tests.fixtures.db import SeededTenant
from tests.integration.db_repos._rows import new_id

from noema.core.protocols.ops import UsageLine
from noema.core.protocols.storage import Busy, Database, OutboxRecord, SessionLease
from noema.core.redis_keys import OUTBOX_SHARDS, RedisKeys
from noema.core.values import Cost, JsonValue, UsageRecord
from noema.ops.cost import usage_line_record
from noema.ops.outbox import OutboxDrainer, WriteBehindOutbox

from .conftest import SwitchDb, SwitchRedis

pytestmark = pytest.mark.integration
AT = datetime(2026, 10, 10, 12, tzinfo=UTC)


def line(tenant: SeededTenant, i: int, project: str = "prj_a") -> OutboxRecord:
    usage = UsageLine(
        id=f"{i:026d}", tenant_id=tenant.tenant_id, project_id=project, line="decision", model_id="deepseek.v3.2",
        region="us-east-1", usage=UsageRecord(10, 2), cost=Cost(Decimal("0.000010"), False), decision_id="dec_1",
    )  # fmt: skip
    return usage_line_record(usage, AT)


def pieces(db: SwitchDb, redis: SwitchRedis, keys: RedisKeys, **drain: Any) -> tuple[Any, ...]:
    clock, telemetry = FakeClock(), FakeTelemetry()
    outbox = WriteBehindOutbox(cast(Database, db), cast(Any, redis), keys, clock, telemetry.instruments)
    drainer = OutboxDrainer(
        cast(Database, db), cast(Any, redis), keys, clock, telemetry.instruments, consumer="pod-a", **drain
    )
    return outbox, drainer, clock, telemetry


async def usage_ids(pg_admin: Any, tenant: SeededTenant) -> list[str]:
    rows = await pg_admin.fetch("SELECT id FROM noema.usage_line WHERE tenant_id = $1 ORDER BY id", tenant.tenant_id)
    return [r["id"] for r in rows]


async def stream_len(redis: SwitchRedis, keys: RedisKeys) -> int:
    return sum([int(await redis.real.xlen(keys.outbox(s))) for s in range(OUTBOX_SHARDS)])


async def test_postgres_down_goes_through_redis_exactly_once(
    ledger_db: SwitchDb, valkey: SwitchRedis, keys: RedisKeys, tenant: SeededTenant, pg_admin: Any
) -> None:
    outbox, drainer, _, telemetry = pieces(ledger_db, valkey, keys)
    ledger_db.up = False
    for i in range(3):
        outbox.put(line(tenant, i))
    await outbox.flush(5)
    assert await stream_len(valkey, keys) == 3 and await usage_ids(pg_admin, tenant) == []
    ledger_db.up = True
    assert await drainer.drain_once() == 3
    assert await usage_ids(pg_admin, tenant) == [f"{i:026d}" for i in range(3)]
    assert await stream_len(valkey, keys) == 0  # acknowledged and deleted
    assert telemetry.metrics[-1].name == "aghoo_outbox_backlog" and telemetry.metrics[-1].labels == {"sink": "redis"}
    outbox.put(line(tenant, 1))  # a replay of an applied line
    await outbox.flush(5)
    assert await usage_ids(pg_admin, tenant) == [f"{i:026d}" for i in range(3)]


async def test_both_down_stays_in_memory_until_a_sink_returns(
    ledger_db: SwitchDb, valkey: SwitchRedis, keys: RedisKeys, tenant: SeededTenant, pg_admin: Any
) -> None:
    outbox, drainer, _, _ = pieces(ledger_db, valkey, keys)
    ledger_db.up = valkey.up = False
    outbox.put(line(tenant, 7))
    assert await outbox.write_once() == 0 and outbox.backlog == 1
    valkey.up = True  # Redis back first: the record moves there, then the drainer applies it
    assert await outbox.write_once() == 1 and outbox.backlog == 0
    ledger_db.up = True
    await drainer.drain_once()
    assert await usage_ids(pg_admin, tenant) == [f"{7:026d}"]


async def test_entries_of_a_dead_consumer_are_claimed(
    ledger_db: SwitchDb, valkey: SwitchRedis, keys: RedisKeys, tenant: SeededTenant, pg_admin: Any
) -> None:
    outbox, dead_pod, _, _ = pieces(ledger_db, valkey, keys)
    ledger_db.up = False
    outbox.put(line(tenant, 1))
    await outbox.flush(5)
    assert await dead_pod.drain_once() == 0  # read, Postgres down: left pending on pod-a, which then dies
    ledger_db.up = True
    _, other_pod, _, _ = pieces(ledger_db, valkey, keys, min_idle_ms=0)
    other_pod._consumer = "pod-b"
    assert await other_pod.drain_once() == 1
    assert await usage_ids(pg_admin, tenant) == [f"{1:026d}"]


async def test_tenth_failed_delivery_dead_letters(
    ledger_db: SwitchDb, valkey: SwitchRedis, keys: RedisKeys, tenant: SeededTenant, pg_admin: Any
) -> None:
    outbox, drainer, _, _ = pieces(ledger_db, valkey, keys, min_idle_ms=0)
    bad = line(tenant, 5)
    poison = OutboxRecord("usage_line", bad.id, bad.tenant_id, {**bad.payload, "tenant_id": "t_other"})
    ledger_db.up = False
    outbox.put(poison)
    await outbox.flush(5)
    ledger_db.up = True
    for _ in range(9):
        assert await drainer.drain_once() == 0
    assert await pg_admin.fetchval("SELECT count(*) FROM ops.outbox_dead_letter WHERE id = $1", bad.id) == 0
    assert await drainer.drain_once() == 1  # the 10th delivery
    dead = await pg_admin.fetchrow("SELECT * FROM ops.outbox_dead_letter WHERE kind = 'usage_line' AND id = $1", bad.id)
    assert dead["attempts"] == 10 and dead["error"] == "tenant_mismatch" and dead["tenant_id"] == tenant.tenant_id
    assert await stream_len(valkey, keys) == 0


async def test_postgres_down_never_dead_letters(
    ledger_db: SwitchDb, valkey: SwitchRedis, keys: RedisKeys, tenant: SeededTenant, pg_admin: Any
) -> None:
    outbox, drainer, _, _ = pieces(ledger_db, valkey, keys, min_idle_ms=0)
    ledger_db.up = False
    outbox.put(line(tenant, 3))
    await outbox.flush(5)
    for _ in range(12):
        assert await drainer.drain_once() == 0
    ledger_db.up = True
    assert await drainer.drain_once() == 1
    assert await usage_ids(pg_admin, tenant) == [f"{3:026d}"]


def finalize(tenant: SeededTenant, session_id: str, turn_id: str, last_seq: int) -> OutboxRecord:
    turn: dict[str, JsonValue] = {
        "tenant_id": tenant.tenant_id, "project_id": "prj_a", "id": turn_id, "session_id": session_id,
        "seq": last_seq + 1, "principal_id": tenant.principal_ids["prj_a"], "status": "completed",
        "content_policy": "full", "input_parts": [{"type": "text", "text": "hi"}], "output_text": "hello",
        "routing": {"profile": "fast"}, "usage": {"input_tokens": 3, "output_tokens": 2}, "timings": {"e2e_ms": 10},
        "created_at": "2026-10-10T12:00:00Z", "completed_at": "2026-10-10T12:00:01Z",
    }  # fmt: skip
    payload: dict[str, JsonValue] = {
        "lease": {"session_id": session_id, "turn_id": turn_id, "last_seq": last_seq},
        "turn": turn, "attempts": [], "decisions": [], "turn_files": [], "sticky_spec": {"profile": "fast"},
    }  # fmt: skip
    return OutboxRecord("turn_finalize", turn_id, tenant.tenant_id, payload, session_id)


async def next_turn(db: SwitchDb, redis: SwitchRedis, keys: RedisKeys, tenant: SeededTenant, sid: str) -> Any:
    """The next turn's session acquire as spec-04 §2 orders it: `EXISTS session_pending` first (busy), then the
    `SessionRepo.acquire` UPDATE."""
    if await redis.real.exists(keys.session_pending(sid)):
        return Busy()
    async with db.tenant_uow(tenant.tenant_id, project_id="prj_a") as uow:
        return await uow.sessions.acquire(sid, new_id("turn"), expected_seq=None, lease_s=30)


async def test_turn_finalize_keeps_the_session_busy_until_drained(
    ledger_db: SwitchDb, valkey: SwitchRedis, keys: RedisKeys, tenant: SeededTenant, pg_admin: Any
) -> None:
    sid, tid = new_id("ses"), new_id("turn")
    await pg_admin.execute(
        "INSERT INTO noema.session (tenant_id, project_id, id, owner_principal_id, data_class, active_turn_id,"
        " lease_expires_at) VALUES ($1, 'prj_a', $2, $3, 'internal', $4, now() - interval '1 minute')",
        tenant.tenant_id, sid, tenant.principal_ids["prj_a"], tid,
    )  # fmt: skip
    pending = keys.session_pending(sid)
    await valkey.real.set(pending, tid.encode(), ex=86_400)  # set by the orchestrator before the put
    outbox, drainer, _, _ = pieces(ledger_db, valkey, keys)
    ledger_db.up = False
    outbox.put(finalize(tenant, sid, tid, 0))
    await outbox.flush(5)
    assert await valkey.real.exists(pending) == 1  # busy: the lease expired, but the finalize is still pending
    assert isinstance(await next_turn(ledger_db, valkey, keys, tenant, sid), Busy)  # no turn can take seq 1
    ledger_db.up = True
    assert await drainer.drain_once() == 1
    assert await valkey.real.exists(pending) == 0
    session = await pg_admin.fetchrow(
        "SELECT * FROM noema.session WHERE tenant_id = $1 AND id = $2", tenant.tenant_id, sid
    )
    assert session["last_seq"] == 1 and session["active_turn_id"] is None
    assert session["sticky_spec"] == {"profile": "fast"}
    outbox.put(finalize(tenant, sid, tid, 0))  # a replay: the turn row exists -> applied, nothing changes
    await outbox.flush(5)
    count = await pg_admin.fetchval("SELECT count(*) FROM noema.turn WHERE tenant_id = $1", tenant.tenant_id)
    assert count == 1
    last_seq = await pg_admin.fetchval("SELECT last_seq FROM noema.session WHERE tenant_id = $1", tenant.tenant_id)
    assert last_seq == 1
    lease = await next_turn(ledger_db, valkey, keys, tenant, sid)
    assert isinstance(lease, SessionLease) and lease.last_seq == 1  # no longer busy: the next turn gets seq 2
