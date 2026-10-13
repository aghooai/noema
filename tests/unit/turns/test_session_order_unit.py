"""Session ordering on the fakes (spec-04 §2; IMPL-022): the T-SESS-1…6 behaviors on the FakeClock with the
in-memory coordinator (the Redis implementation runs them in tests/integration/turns/test_session_order.py)."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import SessionLease
from noema.turns import session_order

from .harness import STICKY_SOL, USER_BODY, TurnHarness, make_harness, principal, run_turn, running

pytestmark = pytest.mark.unit


def _h() -> TurnHarness:
    h = make_harness()
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    return h


async def _acquire(h: TurnHarness, turn_id: str, *, expected: int | None = None) -> SessionLease:
    return await session_order.acquire(
        h.world.db,
        h.coordinator,
        h.clock,
        principal(),
        session_id="ses_1",
        turn_id=turn_id,
        expected_seq=expected,
        queue_timeout_ms=2000,
    )


async def test_sess_1_fifo_order_under_five_contenders() -> None:
    h = _h()
    order: list[str] = []
    async with running(h):
        first = await _acquire(h, "turn_0")

        async def contender(i: int) -> None:
            await h.clock.sleep(i * 0.01)  # arrival order 1..5
            lease = await _acquire(h, f"turn_{i}")
            order.append(lease.turn_id)
            await h.clock.sleep(0.1)
            await session_order.release(h.world.db, h.coordinator, principal(), lease)

        async with anyio.create_task_group() as tg:
            for i in range(1, 6):
                tg.start_soon(contender, i)
            await h.clock.sleep(0.2)  # every contender is queued (a newcomer at the release instant may barge)
            await session_order.release(h.world.db, h.coordinator, principal(), first)
    assert order == [f"turn_{i}" for i in range(1, 6)]


async def test_sess_2_conflict_and_not_found_do_not_wait() -> None:
    h = _h()
    async with running(h):
        t0 = h.clock.monotonic()
        with pytest.raises(NoemaError) as conflict:
            await _acquire(h, "turn_x", expected=2)
        assert conflict.value.code is ErrorCode.SESSION_CONFLICT and conflict.value.details == {"last_seq": 3}
        with pytest.raises(NoemaError) as missing:
            await session_order.acquire(
                h.world.db,
                h.coordinator,
                h.clock,
                principal(),
                session_id="ses_nope",
                turn_id="t",
                expected_seq=None,
                queue_timeout_ms=2000,
            )
        assert missing.value.code is ErrorCode.SESSION_NOT_FOUND
        assert h.clock.monotonic() == t0


async def test_sess_3_busy_at_the_queue_timeout() -> None:
    h = _h()
    async with running(h):
        await _acquire(h, "turn_holder")
        t0 = h.clock.monotonic()
        with pytest.raises(NoemaError) as busy:
            await _acquire(h, "turn_waiter")
        assert busy.value.code is ErrorCode.SESSION_BUSY and busy.value.retry_after_s == 1
        assert abs((h.clock.monotonic() - t0) * 1000 - 2000) <= 100
        assert h.coordinator.state.queues["ses_1"] == []  # the ticket left the queue


async def test_sess_4_expired_lease_is_taken_over() -> None:
    h = _h()
    async with running(h):
        await _acquire(h, "turn_dead_pod")
        await h.clock.sleep(31)  # the holder never renewed (pod killed)
        lease = await _acquire(h, "turn_next")
    assert lease.turn_id == "turn_next" and lease.last_seq == 3


async def test_sess_5_pending_finalize_blocks_even_after_lease_expiry() -> None:
    h = _h()
    async with running(h):
        await _acquire(h, "turn_a")
        await h.coordinator.mark_session_pending("ses_1", "turn_a")
        await h.clock.sleep(31)
        with pytest.raises(NoemaError) as busy:
            await _acquire(h, "turn_b")
        assert busy.value.code is ErrorCode.SESSION_BUSY
        h.coordinator.state.pending.clear()  # the drainer applied the record
        h.coordinator.state.down = True  # and Redis down: polling only, no pending check
        assert (await _acquire(h, "turn_c")).turn_id == "turn_c"


async def test_sess_6_store_false_never_queues_and_is_not_delayed() -> None:
    from tests.golden.sse_scenarios import document, router_rules, stream

    sol = stream("us.openai.gpt-6.1-sol", first_event_ms=10, first_text_ms=50, chunks=["x"])
    h = make_harness(scenario=document("s", [sol], router_rules()))
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    async with running(h):
        await _acquire(h, "turn_holder")  # a store:true turn holds the session
        runner = await run_turn(h, {**USER_BODY, "session_id": "ses_1", "store": False})
    assert runner.result is not None and runner.result.status == "completed"
    assert "ses_1" not in h.coordinator.state.queues
    session = h.world.db.store.rows("session")[0]
    assert session["last_seq"] == 3 and session["active_turn_id"] == "turn_holder"


async def test_acquire_then_reads_in_the_leasing_unit_of_work_and_a_failed_read_rolls_the_lease_back() -> None:
    # E sprint A: step 10 takes the lease and reads the history in one transaction
    h = _h()
    opened: list[object] = []
    original = h.world.db.uow

    def counting(p: Any) -> Any:
        opened.append(p)
        return original(p)

    h.world.db.uow = counting  # type: ignore[method-assign,assignment]

    async def read(uow: object, held: SessionLease) -> tuple[str, int]:
        return held.turn_id, held.last_seq

    async def broken(uow: object, held: SessionLease) -> None:
        raise NoemaError(ErrorCode.INTERNAL_ERROR, "history read failed")

    async def attempt(turn_id: str, then: session_order.Then) -> tuple[SessionLease, object]:
        return await session_order.acquire_then(
            h.world.db,
            h.coordinator,
            h.clock,
            principal(),
            session_id="ses_1",
            turn_id=turn_id,
            expected_seq=None,
            queue_timeout_ms=2000,
            then=then,
        )

    async with running(h):
        with pytest.raises(NoemaError):
            await attempt("turn_a", broken)
        lease, held = await attempt("turn_b", read)  # turn_a's lease was rolled back: free at once
    assert (lease.turn_id, held) == ("turn_b", ("turn_b", 3))
    assert len(opened) == 2  # one unit of work per attempt: the lease and the read together
