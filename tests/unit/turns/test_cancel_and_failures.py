"""Cancel, disconnect and abandon (spec-04 §11, T18) and the failure rows of spec-04 §12 on the fakes."""

from __future__ import annotations

from typing import Any

import pytest
from tests.golden.sse_scenarios import document, router_rules, stream

from noema.core.errors import ErrorCode, NoemaError

from .harness import (
    STICKY_SOL,
    USER_BODY,
    CoordinatorState,
    first,
    make_harness,
    principal,
    run_turn,
    running,
    second_pod,
    types_of,
)

pytestmark = pytest.mark.unit

SOL = "us.openai.gpt-6.1-sol"
CHUNKS = [f"c{i} " for i in range(5)]


def _h(*, step_ms: float = 100, fault: dict[str, Any] | None = None, sticky: Any = STICKY_SOL, state: Any = None):  # type: ignore[no-untyped-def]
    sol = stream(SOL, first_event_ms=100, first_text_ms=720, chunks=CHUNKS, step_ms=step_ms, fault=fault)
    h = make_harness(scenario=document("c", [sol], router_rules()), state=state)
    h.seed_session("ses_1", last_seq=3, sticky_spec=sticky)
    return h


async def test_cancel_before_s1_emits_the_closed_snapshot_and_keeps_sticky() -> None:
    h = _h(sticky={**STICKY_SOL, "generation_profile": "opus-deep"})
    answers: list[dict[str, Any]] = []

    async def cancel(runner: Any) -> None:
        await h.clock.sleep(0.3)
        answers.append(await h.service.cancel(principal(), runner.turn_id))

    async with running(h):
        runner = await run_turn(h, during=cancel)
    types = types_of(h, runner)
    assert answers == [{"turn_id": runner.turn_id, "status": "cancelling"}]
    assert types[-3:] == ["generation.cancelled", "decision.completed", "turn.completed"]
    assert first(h, runner, "generation.cancelled")[1]["reason"] == "client_abort"
    _, completed = first(h, runner, "decision.completed")
    assert completed["decision"]["status"] in ("completed", "partial")  # the closed snapshot of drun.cancel()
    assert runner.result is not None and runner.result.status == "cancelled" and runner.result.error is None
    session = h.world.db.store.rows("session")[0]
    assert session["last_seq"] == 4 and session["sticky_spec"]["generation_profile"] == "opus-deep"  # kept
    turn = h.world.db.store.rows("turn")[0]
    assert turn["status"] == "cancelled" and turn["routing"]["decision_id"] is None


async def test_second_cancel_and_finished_store_false_answers() -> None:
    h = _h()
    async with running(h):
        runner = await run_turn(h, {**USER_BODY, "session_id": "ses_1", "store": False})
        assert await h.service.cancel(principal(), runner.turn_id) == {"turn_id": runner.turn_id, "status": "completed"}
        with pytest.raises(NoemaError) as foreign:
            await h.service.cancel(principal("prn_other"), runner.turn_id)
        assert foreign.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(NoemaError) as unknown:
            await h.service.cancel(principal(), "turn_unknown")
        assert unknown.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(NoemaError) as gone:
            await h.service.get(principal(), runner.turn_id)  # store:false is never readable
        assert gone.value.code is ErrorCode.NOT_FOUND
    assert h.world.db.store.rows("turn") == []


async def test_cancel_on_another_pod_goes_through_turn_cancel_and_redis_down_is_500() -> None:
    state = CoordinatorState()
    h = _h(step_ms=500, state=state)
    other = second_pod(h)
    answers: list[dict[str, Any]] = []

    async def cancel(runner: Any) -> None:
        await h.clock.sleep(1.2)
        answers.append(await other.service.cancel(principal(), runner.turn_id))

    await other.runtime.open()
    try:
        async with running(h):
            runner = await run_turn(h, during=cancel)
            state.down = True
            with pytest.raises(NoemaError) as down:
                await other.service.cancel(principal(), "turn_elsewhere")
            assert down.value.code is ErrorCode.INTERNAL_ERROR and down.value.spec.retryable
    finally:
        await other.runtime.close()
    assert answers == [{"turn_id": runner.turn_id, "status": "cancelling"}]
    assert state.published == [("t_acme", runner.turn_id)]
    assert runner.result is not None and runner.result.status == "cancelled"
    assert runner.turn_id not in state.owners  # turn_owner deleted at finalize


async def test_abandon_watchdog_cancels_a_turn_without_subscriber() -> None:
    sol = stream(SOL, first_event_ms=100, first_text_ms=720, chunks=[f"c{i} " for i in range(30)], step_ms=20_000)
    h = make_harness(scenario=document("c", [sol], router_rules()))
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    async with running(h):
        runner = await h.start({**USER_BODY, "session_id": "ses_1"}, deadline_s=600)
        await runner.done.wait()
    types = types_of(h, runner)
    assert runner.result is not None and runner.result.status == "abandoned"
    at, cancelled = first(h, runner, "generation.cancelled")
    assert cancelled["reason"] == "client_abort" and 300_000 < at <= 306_000
    assert "error" not in types and h.world.db.store.rows("turn")[0]["status"] == "abandoned"


async def test_client_too_slow_cancels_as_t18_and_stores_failed() -> None:
    h = _h(step_ms=500)

    async def too_slow(runner: Any) -> None:
        await h.clock.sleep(1.5)
        runner.request_cancel("failed")

    async with running(h):
        runner = await run_turn(h, during=too_slow)
    assert first(h, runner, "generation.cancelled")[1]["reason"] == "client_abort"
    assert runner.result is not None and runner.result.status == "failed"


async def test_tenant_access_at_ingress_is_503_and_releases_nothing_held() -> None:
    h = _h()
    h.world.broker.fail("t_acme", "access_denied")
    async with running(h):
        with pytest.raises(NoemaError) as err:
            await h.start({**USER_BODY, "session_id": "ses_1"})
    assert err.value.code is ErrorCode.TENANT_ACCESS_MISCONFIGURED and err.value.spec.status == 503
    assert h.world.db.store.rows("session")[0]["active_turn_id"] is None  # step 9 is before the lock


async def test_generation_tenant_access_never_falls_back() -> None:
    h = _h(fault={"kind": "access_denied"})
    async with running(h):
        runner = await run_turn(h)
    _, err = first(h, runner, "error")
    assert err["code"] == "tenant_access_misconfigured" and err["retryable"] is False
    assert types_of(h, runner).count("generation.started") == 1


async def test_postgres_down_store_true_is_500_and_store_false_without_session_continues() -> None:
    h = _h()
    h.world.db.store.available = False
    async with running(h):
        with pytest.raises(NoemaError) as err:
            await h.start({**USER_BODY, "session_id": "ses_1"})
        assert err.value.code is ErrorCode.INTERNAL_ERROR
        runner = await run_turn(h, {**USER_BODY, "store": False})
    assert runner.result is not None and runner.result.status == "completed"


async def test_finalize_db_failure_goes_to_the_outbox_with_session_pending() -> None:
    h = _h()

    async def db_down(runner: Any) -> None:
        await h.clock.sleep(1.0)  # after the lock, before finalize
        h.world.db.store.available = False

    async with running(h):
        runner = await run_turn(h, during=db_down)
    assert types_of(h, runner)[-1] == "turn.completed" and runner.result is not None
    assert runner.result.status == "completed"
    (record,) = h.world.outbox.of_kind("turn_finalize")
    assert record.id == runner.turn_id and record.session_id == "ses_1"
    assert record.payload["lease"] == {"session_id": "ses_1", "turn_id": runner.turn_id, "last_seq": 3}
    assert h.coordinator.state.pending == {"ses_1": runner.turn_id}
    h.world.db.store.available = True
    await h.world.outbox.drain()
    assert h.world.db.store.rows("session")[0]["last_seq"] == 4
    assert [r["id"] for r in h.world.db.store.rows("turn")] == [runner.turn_id]


async def test_lost_lease_ends_with_internal_error_and_stores_nothing() -> None:
    h = _h(step_ms=8000)

    async def steal(runner: Any) -> None:
        await h.clock.sleep(5)
        row = h.world.db.store.table("session")[("t_acme", "ses_1")]
        row["active_turn_id"] = "turn_thief"  # taken over after a stall

    async with running(h):
        runner = await run_turn(h, during=steal)
    _, err = first(h, runner, "error")
    assert err["code"] == "internal_error" and err["retryable"] is True
    assert h.world.db.store.rows("turn") == []
    assert [line.attempt_id for line in h.world.cost.lines if line.attempt_id]  # usage lines still recorded


async def test_cancel_after_the_turn_ended_with_an_error_keeps_failed() -> None:
    h = _h(fault={"kind": "access_denied"})
    answers: list[dict[str, Any]] = []

    async def cancel(runner: Any) -> None:
        await h.clock.sleep(0.5)  # the error is out; the turn waits for its final decision snapshot
        assert runner.error is not None and not runner.done.is_set()
        answers.append(await h.service.cancel(principal(), runner.turn_id))

    async with running(h):
        runner = await run_turn(h, during=cancel)
    assert answers == [{"turn_id": runner.turn_id, "status": "completed"}]  # not cancelling: the outcome is final
    assert runner.result is not None and runner.result.status == "failed"
    assert runner.result.error is not None and runner.result.error.code is ErrorCode.TENANT_ACCESS_MISCONFIGURED
    assert h.world.db.store.rows("turn")[0]["status"] == "failed"
    assert [e["reason"] for e in h.events(runner.turn_id) if e["type"] == "generation.cancelled"] == ["failed"]


async def test_cancel_after_generation_completed_keeps_completed() -> None:
    sol = stream(SOL, first_event_ms=100, first_text_ms=720, chunks=["a ", "b "], step_ms=100)
    h = make_harness(scenario=document("c", [sol], router_rules()), hold_decisions_until_cancel=True)
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    answers: list[dict[str, Any]] = []

    async def cancel(runner: Any) -> None:
        await h.clock.sleep(1.5)
        assert runner.completed and not runner.done.is_set()
        answers.append(await h.service.cancel(principal(), runner.turn_id))
        runner.request_cancel("abandoned")  # the watchdog, too, leaves a decided outcome alone

    async with running(h):
        runner = await run_turn(h, during=cancel)
    assert answers == [{"turn_id": runner.turn_id, "status": "completed"}]
    assert runner.result is not None and runner.result.status == "completed" and runner.cancel_status is None


async def test_a_lease_renewal_statement_error_counts_as_held(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.fakes.db_repos import FakeSessionRepo

    original = FakeSessionRepo.renew
    calls: list[int] = []

    async def flaky(self: Any, lease: Any, lease_s: int) -> bool:
        calls.append(lease_s)
        if len(calls) == 1:
            raise TimeoutError("canceling statement due to statement timeout")
        return await original(self, lease, lease_s)

    monkeypatch.setattr(FakeSessionRepo, "renew", flaky)
    h = _h(step_ms=5000)  # ~25 s of streaming: renewals at 10 s (fails) and 20 s
    async with running(h):
        runner = await run_turn(h)
    assert len(calls) >= 2  # renewed again after the failure
    assert runner.result is not None and runner.result.status == "completed" and "error" not in types_of(h, runner)
    assert h.world.db.store.rows("turn")[0]["status"] == "completed"


async def test_an_unexpected_watcher_failure_stops_the_turn_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    from noema.turns import session_order

    async def broken(db: Any, who: Any, lease: Any) -> bool:
        raise RuntimeError("bug")

    monkeypatch.setattr(session_order, "renew", broken)
    h = _h(step_ms=3000)
    async with running(h):
        runner = await run_turn(h)
    types = types_of(h, runner)
    _, err = first(h, runner, "error")
    assert err["code"] == "internal_error" and types[-1] == "turn.completed"
    assert first(h, runner, "generation.cancelled")[1]["reason"] == "client_abort"
    assert runner.result is not None and runner.result.status == "failed"
    assert [line.attempt_id for line in h.world.cost.lines if line.attempt_id]  # the run's records were collected
    assert len(runner.records) == types.count("generation.started")


async def test_hung_finalize_is_bounded_then_outboxed_and_the_turn_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    """spec-04 §10 step 2 on a hung (not refused) Postgres: the finalize transaction is bounded by
    `persist.FINALIZE_BUDGET_S`; on expiry it is a DB failure (session_pending, turn_finalize outboxed) and the turn
    still completes: turn.completed within the budget, the reservation released (Wave E fix)."""
    from tests.fakes import db_repos

    from noema.turns import persist

    h = _h()
    real = db_repos.FakeSessionRepo.finalize_turn

    async def hung(self: Any, *args: Any, **kwargs: Any) -> Any:
        await h.clock.sleep(900.0)  # a black-holed socket: no reply until TCP gives up
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(db_repos.FakeSessionRepo, "finalize_turn", hung)
    async with running(h):
        runner = await run_turn(h)
    timeline = h.log.timeline[runner.turn_id]
    (gen_done,) = [t for t, e in timeline if e["type"] == "generation.completed"]
    (done,) = [t for t, e in timeline if e["type"] == "turn.completed"]
    assert done - gen_done <= persist.FINALIZE_BUDGET_S + 0.5
    assert runner.result is not None and runner.result.status == "completed"
    (record,) = h.world.outbox.of_kind("turn_finalize")
    assert record.id == runner.turn_id and record.session_id == "ses_1"
    assert h.coordinator.state.pending == {"ses_1": runner.turn_id}
    assert runner.p.reservation.released  # type: ignore[attr-defined]


async def test_a_long_admission_wait_renews_the_session_lease_before_it_expires() -> None:
    """The lease (`LEASE_S` = 30 s) is taken at step 10, before admission (step 11) may wait: the first renewal is
    due `RENEW_EVERY_S` after ingress (t_in), not after the turn task starts, so a 25 s admission wait does not let
    a same-session turn take the lease over at +32 s (Wave E fix)."""
    from noema.core.protocols.storage import Busy

    from .harness import principal

    h = _h(step_ms=8000)
    real = h.world.admission.reserve

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await h.clock.sleep(25.0)  # admission_queue_timeout_ms may be up to 30,000
        return await real(*args, **kwargs)

    h.world.admission.reserve = slow
    seen: dict[str, Any] = {}

    async def thief(runner: Any) -> None:
        await h.clock.sleep(7.0)  # 32 s after step 10
        async with h.world.db.uow(principal()) as uow:
            seen["acquire"] = await uow.sessions.acquire("ses_1", "turn_thief", expected_seq=None, lease_s=30)

    async with running(h):
        runner = await run_turn(h, during=thief)
    assert isinstance(seen["acquire"], Busy)
    assert runner.result is not None and runner.result.status == "completed" and not runner.lease_lost


async def test_a_waiting_turn_ended_before_s1_keeps_the_sticky_spec_its_lease_read() -> None:
    """spec-04 §7: sticky_spec is written only when S1 was evaluated, else the session keeps its value. A turn that
    waited behind another one on its session (FIFO) and ends before S1 writes back the sticky_spec read with its
    lease, not the stale step-6 one (Wave E fix)."""
    import anyio

    from noema.turns import session_order

    from .harness import principal

    opus = {**STICKY_SOL, "generation_profile": "opus-deep", "effort": "high"}
    h = make_harness(
        scenario=document("r", [stream(SOL, first_event_ms=100, first_text_ms=720, chunks=CHUNKS)], router_rules()),
        hold_decisions_until_cancel=True,
    )
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    async with running(h):
        holder = await session_order.acquire(
            h.world.db, h.coordinator, h.clock, principal(), session_id="ses_1", turn_id="turn_holder",
            expected_seq=None, queue_timeout_ms=2000,
        )  # fmt: skip

        async def holder_finalizes() -> None:  # the other turn's finalize: seq 4, sticky opus-deep
            await h.clock.sleep(0.3)
            row = h.world.db.store.table("session")[("t_acme", "ses_1")]
            row["last_seq"], row["sticky_spec"] = 4, dict(opus)
            await session_order.release(h.world.db, h.coordinator, principal(), holder)

        async def cancel_before_s1(runner: Any) -> None:
            await h.clock.sleep(0.05)
            runner.request_cancel("cancelled")

        async with anyio.create_task_group() as tg:
            tg.start_soon(holder_finalizes)
            runner = await run_turn(h, {**USER_BODY, "session_id": "ses_1"}, during=cancel_before_s1)
    assert runner.decided is None and runner.p.lease is not None and runner.p.lease.last_seq == 4
    session = h.world.db.store.rows("session")[0]
    assert session["last_seq"] == 5 and session["sticky_spec"] == opus


async def test_a_forged_cross_tenant_turn_cancel_is_dropped_with_a_security_event() -> None:
    """SEC-ISO-02: a `turn_cancel` whose tenant_id is not the running turn's is dropped (the turn completes) and
    emits `pubsub_tenant_mismatch`: the counter and an `audit_event` row of the turn's tenant (Wave E fix)."""
    h = _h(step_ms=500)

    async def forged(runner: Any) -> None:
        await h.clock.sleep(1.2)
        await h.service.on_remote_cancel("t_beta", runner.turn_id)

    async with running(h):
        runner = await run_turn(h, during=forged)
    assert runner.result is not None and runner.result.status == "completed"
    telemetry = h.world.telemetry
    assert telemetry.counter("aghoo_security_violations_total", kind="pubsub_tenant_mismatch") == 1
    (audit,) = h.world.outbox.of_kind("audit_event")
    assert audit.tenant_id == "t_acme" and audit.payload["action"] == "security.pubsub_tenant_mismatch"
    assert audit.payload["resource_id"] == runner.turn_id and audit.payload["details"] == {"channel": "turn_cancel"}
