"""TurnEventLog conformance (spec-02 §7, IMPL-021): ids from 1, pre-serialized frames, subscribe with `after`,
end at turn.completed, seal + TTL, the remote mirror, and the abandon-watchdog idle clock."""

from __future__ import annotations

from collections.abc import AsyncIterator

import anyio
import orjson
import pytest

from noema.core.protocols.events import SseFrame

from .kits import EventLogKit
from .registry import kit_fixture

kit = kit_fixture("event_log")
T = "turn_1"


def fill(kit: EventLogKit, n_deltas: int = 2, *, complete: bool = True) -> None:
    kit.log.append(T, {"type": "turn.created", "turn_id": T, "session_id": None, "created_at": 1})
    for i in range(n_deltas):
        kit.log.append(T, {"type": "generation.delta", "attempt_id": "att_1", "seq": i, "text": f"t{i}"})
    if complete:
        kit.log.append(T, {"type": "turn.completed", "turn_id": T})


async def frames_of(it: AsyncIterator[SseFrame]) -> list[SseFrame]:
    return [f async for f in it]


async def test_ids_and_wire_format(kit: EventLogKit) -> None:
    assert kit.log.append(T, {"type": "turn.created", "turn_id": T}) == 1
    assert kit.log.append(T, {"type": "preflight.completed", "x": "é"}) == 2
    assert kit.log.is_local(T)
    kit.log.append(T, {"type": "turn.completed", "turn_id": T})
    frames = await frames_of(kit.log.subscribe(T))
    first = frames[0]
    assert (first.id, first.type) == (1, "turn.created")
    assert first.payload == orjson.dumps({"type": "turn.created", "turn_id": T})
    assert first.wire == b"id: 1\nevent: turn.created\ndata: " + first.payload + b"\n\n"


async def test_subscribe_after_and_end_at_completion(kit: EventLogKit) -> None:
    fill(kit)
    frames = await frames_of(kit.log.subscribe(T, after=2))
    assert [f.id for f in frames] == [3, 4] and frames[-1].type == "turn.completed"


async def test_live_frames_reach_subscribers(kit: EventLogKit) -> None:
    fill(kit, 1, complete=False)
    got: list[int] = []
    async with anyio.create_task_group() as tg:

        async def sub() -> None:
            got.extend([f.id async for f in kit.log.subscribe(T)])

        tg.start_soon(sub)
        await kit.clock.settle()
        kit.log.append(T, {"type": "generation.delta", "attempt_id": "att_1", "seq": 1, "text": "x"})
        kit.log.append(T, {"type": "turn.completed", "turn_id": T})
    assert got == [1, 2, 3, 4]


async def test_unknown_turn_is_empty(kit: EventLogKit) -> None:
    assert not kit.log.is_local("turn_nope")
    assert await frames_of(kit.log.subscribe("turn_nope")) == []


async def test_append_after_seal_is_a_bug(kit: EventLogKit) -> None:
    fill(kit)
    kit.log.seal(T, ttl_s=300)
    with pytest.raises(RuntimeError):
        kit.log.append(T, {"type": "generation.delta"})


async def test_sealed_log_expires(kit: EventLogKit) -> None:
    fill(kit)
    kit.log.seal(T, ttl_s=300)
    await kit.clock.advance(299)
    assert kit.log.is_local(T) and len(await frames_of(kit.log.subscribe(T))) == 4
    await kit.clock.advance(2)
    assert not kit.log.is_local(T)


async def test_remote_attach_from_another_pod(kit: EventLogKit) -> None:
    if kit.other_pod is None:
        pytest.skip("single process")
    fill(kit)
    assert not kit.other_pod.is_local(T)
    remote = await kit.other_pod.attach_remote(T, after=1)
    assert remote is not None
    frames = await frames_of(remote)
    assert [f.id for f in frames] == [2, 3, 4]
    local = await frames_of(kit.log.subscribe(T, after=1))
    assert [f.wire for f in frames] == [f.wire for f in local]  # byte-identical (G12b)
    assert await kit.other_pod.attach_remote("turn_missing") is None


async def test_idle_seconds(kit: EventLogKit) -> None:
    fill(kit, complete=False)
    await kit.clock.advance(5)
    idle = await kit.log.idle_seconds(T)
    assert idle is not None and idle >= 5
    async with anyio.create_task_group() as tg:

        async def sub() -> None:
            async for _ in kit.log.subscribe(T):
                pass

        tg.start_soon(sub)
        await kit.clock.settle()
        assert await kit.log.idle_seconds(T) is None
        kit.log.append(T, {"type": "turn.completed", "turn_id": T})
    await kit.clock.advance(3)
    idle = await kit.log.idle_seconds(T)
    assert idle is not None and 3 <= idle < 5
