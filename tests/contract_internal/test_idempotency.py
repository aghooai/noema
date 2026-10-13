"""IdempotencyStore conformance (spec-02 §7, spec-11 §5 state machine, IMPL-077)."""

from __future__ import annotations

import anyio
import pytest

from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Conflict, InFlight, New, Replay, StoredResponse

from .kits import IdempotencyKit, principal
from .registry import kit_fixture

kit = kit_fixture("idempotency")
ROUTE = "/v1/turns"
OK = StoredResponse(200, (("content-type", "text/event-stream"),), None, "turn_1")


async def begin_new(kit: IdempotencyKit, key: str = "k1", fp: str = "fp1") -> New:
    result = await kit.store.begin(principal(), "POST", ROUTE, key, fp, resource_id="turn_1")
    assert isinstance(result, New)
    return result


async def test_new_then_in_flight_then_replay(kit: IdempotencyKit) -> None:
    new = await begin_new(kit)
    dup = await kit.store.begin(principal(request_id="req_2"), "POST", ROUTE, "k1", "fp1")
    assert isinstance(dup, InFlight) and dup.resource_id == "turn_1"
    await kit.store.complete(new.token, OK)
    await kit.store.complete(new.token, OK)  # idempotent
    replay = await kit.store.begin(principal(request_id="req_3"), "POST", ROUTE, "k1", "fp1")
    assert isinstance(replay, Replay) and replay.response == OK


async def test_other_fingerprint_conflicts(kit: IdempotencyKit) -> None:
    await begin_new(kit)
    assert isinstance(await kit.store.begin(principal(), "POST", ROUTE, "k1", "other"), Conflict)


async def test_scope_is_project_method_route(kit: IdempotencyKit) -> None:
    await begin_new(kit)
    assert isinstance(await kit.store.begin(principal(), "POST", "/v1/decisions", "k1", "fp1"), New)
    assert isinstance(await kit.store.begin(principal(project_id="p2"), "POST", ROUTE, "k1", "fp1"), New)


async def test_not_stored_is_taken_over(kit: IdempotencyKit) -> None:
    new = await begin_new(kit)
    await kit.store.complete(new.token, None)
    assert isinstance(await kit.store.begin(principal(), "POST", ROUTE, "k1", "fp1"), New)


async def test_lapsed_lease_is_taken_over_and_heartbeat_keeps_it(kit: IdempotencyKit) -> None:
    new = await begin_new(kit)
    await kit.clock.advance(20)
    assert await kit.store.heartbeat(new.token)
    await kit.clock.advance(20)
    assert isinstance(await kit.store.begin(principal(), "POST", ROUTE, "k1", "fp1"), InFlight)
    await kit.clock.advance(31)
    taken = await kit.store.begin(principal(request_id="req_9"), "POST", ROUTE, "k1", "fp1")
    assert isinstance(taken, New)
    assert not await kit.store.heartbeat(new.token)  # the old owner lost the lease


async def test_await_result_wakes_on_complete(kit: IdempotencyKit) -> None:
    new = await begin_new(kit)
    dup = await kit.store.begin(principal(request_id="req_2"), "POST", ROUTE, "k1", "fp1")
    assert isinstance(dup, InFlight)
    got: list[StoredResponse] = []
    async with kit.clock.autojump(), anyio.create_task_group() as tg:

        async def waiter() -> None:
            got.append(
                await kit.store.await_result(dup.token, deadline=Deadline(kit.clock.monotonic() + 30, kit.clock))
            )

        tg.start_soon(waiter)
        await kit.clock.sleep(2)
        await kit.store.heartbeat(new.token)
        await kit.store.complete(new.token, OK)
    assert got == [OK]


async def test_await_result_deadline_and_not_stored(kit: IdempotencyKit) -> None:
    new = await begin_new(kit)
    dup = await kit.store.begin(principal(request_id="req_2"), "POST", ROUTE, "k1", "fp1")
    assert isinstance(dup, InFlight)
    async with kit.clock.autojump():
        with pytest.raises(NoemaError) as err:
            await kit.store.await_result(dup.token, deadline=Deadline(kit.clock.monotonic() + 5, kit.clock))
    assert err.value.code is ErrorCode.DEADLINE_EXCEEDED
    await kit.store.complete(new.token, None)
    with pytest.raises(NoemaError) as err:
        await kit.store.await_result(dup.token, deadline=Deadline(kit.clock.monotonic() + 5, kit.clock))
    assert err.value.code is ErrorCode.INTERNAL_ERROR


async def test_large_bodies_replay_byte_identical(kit: IdempotencyKit) -> None:
    new = await begin_new(kit)
    body = b"x" * (3 << 20)
    await kit.store.complete(new.token, StoredResponse(200, (("content-type", "application/json"),), body, None))
    replay = await kit.store.begin(principal(), "POST", ROUTE, "k1", "fp1")
    assert isinstance(replay, Replay) and replay.response.body == body
