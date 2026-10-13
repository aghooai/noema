"""SessionRepo conformance (spec-02 §7.1, spec-04 §2): acquire/renew/finalize_turn/release, `Busy`,
`session_conflict`, `session_not_found`, lease expiry and `LeaseLost` with rollback."""

from __future__ import annotations

import pytest

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Busy, LeaseLost, SessionLease
from noema.core.values import JsonValue

from .kits import DatabaseKit, principal
from .registry import kit_fixture
from .rows import attempt, seed_tenancy, session, turn

kit = kit_fixture("database")
STICKY: dict[str, JsonValue] = {
    "generation_profile": "sol-fast",
    "effort": None,
    "max_output_tokens": None,
    "answer_mode": "direct",
    "modalities": ["text"],
}


async def setup(kit: DatabaseKit) -> None:
    await seed_tenancy(kit)
    async with kit.db.uow(principal()) as uow:
        await uow.sessions.create(session("ses_1"))


async def acquire(kit: DatabaseKit, turn_id: str, expected: int | None = None) -> SessionLease | Busy:
    async with kit.db.uow(principal()) as uow:
        return await uow.sessions.acquire("ses_1", turn_id, expected_seq=expected, lease_s=30)


async def test_acquire_busy_and_release(kit: DatabaseKit) -> None:
    await setup(kit)
    lease = await acquire(kit, "turn_1")
    assert isinstance(lease, SessionLease) and (lease.last_seq, lease.sticky_spec) == (0, None)
    assert isinstance(await acquire(kit, "turn_2"), Busy)
    async with kit.db.uow(principal()) as uow:
        await uow.sessions.release(SessionLease("ses_1", "turn_2", 0, None))  # not the holder: no-op
    assert isinstance(await acquire(kit, "turn_2"), Busy)
    async with kit.db.uow(principal()) as uow:
        await uow.sessions.release(lease)
    assert isinstance(await acquire(kit, "turn_2"), SessionLease)


async def test_conflict_and_not_found(kit: DatabaseKit) -> None:
    await setup(kit)
    with pytest.raises(NoemaError) as err:
        await acquire(kit, "turn_1", expected=3)
    assert err.value.code is ErrorCode.SESSION_CONFLICT
    with pytest.raises(NoemaError) as err:
        async with kit.db.uow(principal()) as uow:
            await uow.sessions.acquire("ses_missing", "turn_1", expected_seq=None, lease_s=30)
    assert err.value.code is ErrorCode.SESSION_NOT_FOUND
    with pytest.raises(NoemaError) as err:
        async with kit.db.uow(principal(project_id="p2")) as uow:
            await uow.sessions.acquire("ses_1", "turn_1", expected_seq=None, lease_s=30)
    assert err.value.code is ErrorCode.SESSION_NOT_FOUND  # foreign = not found


async def test_finalize_turn_advances_the_session(kit: DatabaseKit) -> None:
    await setup(kit)
    lease = await acquire(kit, "turn_1", expected=0)
    assert isinstance(lease, SessionLease)
    async with kit.db.uow(principal()) as uow:
        assert await uow.sessions.renew(lease, 30)
        seq = await uow.sessions.finalize_turn(lease, turn("turn_1"), [attempt("att_1")], [], [], STICKY)
    assert seq == 1
    async with kit.db.uow(principal()) as uow:
        row = await uow.sessions.get("ses_1")
        assert row is not None and row["last_seq"] == 1 and row["active_turn_id"] is None
        assert row["sticky_spec"] == STICKY
        stored = await uow.turns.get("turn_1")
        assert stored is not None and (stored["session_id"], stored["seq"]) == ("ses_1", 1)
        assert [a["id"] for a in await uow.attempts.list_for_turn("turn_1")] == ["att_1"]
    nxt = await acquire(kit, "turn_2", expected=1)
    assert isinstance(nxt, SessionLease) and nxt.sticky_spec == STICKY


async def test_expired_lease_is_taken_over_and_the_old_one_is_lost(kit: DatabaseKit) -> None:
    await setup(kit)
    old = await acquire(kit, "turn_1")
    assert isinstance(old, SessionLease)
    await kit.clock.advance(31)
    new = await acquire(kit, "turn_2")
    assert isinstance(new, SessionLease)
    async with kit.db.uow(principal()) as uow:
        assert not await uow.sessions.renew(old, 30)
    with pytest.raises(LeaseLost):
        async with kit.db.uow(principal()) as uow:
            await uow.sessions.finalize_turn(old, turn("turn_1"), [attempt("att_1")], [], [], None)
    async with kit.db.uow(principal()) as uow:
        assert await uow.turns.get("turn_1") is None  # rolled back
