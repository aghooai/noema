"""Tenant isolation of getTurn / cancelTurn (spec-09 §3, §7; spec-04 §11): a tenant_admin of another tenant sees
none of a tenant's turns (404 `not_found`, never a 202) on every lookup branch: the local running registry, a
running turn on another pod (`turn_owner`), a finished `store: false` turn in its resume window, and a stored
turn. The fake authorizer, like the real one, makes a tenant_admin see every row it is given (RLS scopes the
rows), so these checks must be the orchestrator's."""

from __future__ import annotations

from typing import Any

import pytest
from tests.golden.sse_scenarios import document, router_rules, stream

from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import ALL_SCOPES, Principal

from .harness import (
    STICKY_SOL,
    USER_BODY,
    CoordinatorState,
    make_harness,
    principal,
    run_turn,
    running,
    second_pod,
    types_of,
)

pytestmark = pytest.mark.unit

SOL = "us.openai.gpt-6.1-sol"
EVIL = Principal("t_evil", "p_evil", "prn_evil", "key_evil", frozenset({"tenant_admin"}), ALL_SCOPES, "req_x")


def _h(state: CoordinatorState | None = None):  # type: ignore[no-untyped-def]
    sol = stream(SOL, first_event_ms=100, first_text_ms=720, chunks=[f"c{i} " for i in range(5)], step_ms=500)
    h = make_harness(scenario=document("iso", [sol], router_rules()), state=state)
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    return h


async def _refused(call: Any) -> ErrorCode:
    with pytest.raises(NoemaError) as err:
        await call
    return err.value.code


async def test_foreign_tenant_admin_cannot_cancel_a_running_turn_on_the_same_pod() -> None:
    h = _h()
    codes: list[ErrorCode] = []

    async def attack(runner: Any) -> None:
        await h.clock.sleep(1.2)
        codes.append(await _refused(h.service.cancel(EVIL, runner.turn_id)))

    async with running(h):
        runner = await run_turn(h, during=attack)
    assert codes == [ErrorCode.NOT_FOUND]
    assert runner.result is not None and runner.result.status == "completed"
    assert "generation.cancelled" not in types_of(h, runner)


async def test_same_tenant_admin_still_cancels_a_running_turn() -> None:
    h = _h()
    admin = principal("prn_admin", roles=("tenant_admin",), project="p_other")
    answers: list[dict[str, Any]] = []

    async def cancel(runner: Any) -> None:
        await h.clock.sleep(1.2)
        answers.append(await h.service.cancel(admin, runner.turn_id))

    async with running(h):
        runner = await run_turn(h, during=cancel)
    assert answers == [{"turn_id": runner.turn_id, "status": "cancelling"}]
    assert runner.result is not None and runner.result.status == "cancelled"


async def test_foreign_tenant_admin_cannot_cancel_a_turn_running_on_another_pod() -> None:
    state = CoordinatorState()
    h = _h(state)
    other = second_pod(h)
    codes: list[ErrorCode] = []

    async def attack(runner: Any) -> None:
        await h.clock.sleep(1.2)
        codes.append(await _refused(other.service.cancel(EVIL, runner.turn_id)))

    await other.runtime.open()
    try:
        async with running(h):
            runner = await run_turn(h, during=attack)
    finally:
        await other.runtime.close()
    assert codes == [ErrorCode.NOT_FOUND] and state.published == []
    assert runner.result is not None and runner.result.status == "completed"


async def test_foreign_tenant_admin_sees_no_finished_or_stored_turn() -> None:
    h = _h()
    async with running(h):
        ephemeral = await run_turn(h, {**USER_BODY, "store": False})
        stored = await run_turn(h)
        assert await _refused(h.service.cancel(EVIL, ephemeral.turn_id)) is ErrorCode.NOT_FOUND
        assert await _refused(h.service.cancel(EVIL, stored.turn_id)) is ErrorCode.NOT_FOUND
        assert await _refused(h.service.get(EVIL, stored.turn_id)) is ErrorCode.NOT_FOUND
        own = await h.service.get(principal(), stored.turn_id)
    assert own["turn_id"] == stored.turn_id
