"""G12 on the compose stack: an idempotent duplicate of a running streamed turn, sent to the OTHER api replica,
attaches to the owner's event log (the Redis mirror) instead of running the turn again (spec-04 §4.2 G12, R2-R5;
spec-11 idempotency)."""

from __future__ import annotations

import uuid

import anyio
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import LiveTurn, deltas_text, kinds, live_turn, mock_calls, mock_seq, tag, turn_body
from tests.e2e.suite.turns import invariants, stored_turn

from noema.contracts.validate import SseEvent

pytestmark = [pytest.mark.e2e]


def frames(events: list[SseEvent]) -> list[tuple[str | None, str, object]]:
    return [(e.id, e.event, e.data) for e in events]


async def test_g12_duplicate_attaches_on_the_other_replica(stack: Stack, mock: Mock) -> None:
    await mock.load(sc.document("e2e-G12", [sc.stream(sc.SOL, first_text_ms=300, step_ms=200)], sc.router()))
    body = turn_body(f"What is the capital of France? ({tag()})")
    key = {"Idempotency-Key": f"e2e-{uuid.uuid4()}"}
    before = await mock_seq(mock)
    duplicate: dict[str, LiveTurn] = {}

    async def attach() -> None:
        async with live_turn(stack.client(1), body, headers=key) as dup:
            assert dup.response.headers.get("x-aghoo-idempotent-replay") == "true"
            await dup.rest()
        duplicate["r2"] = dup

    async with live_turn(stack.client(0), body, headers=key) as original:
        await original.until(lambda e: e.event == "generation.committed")
        async with anyio.create_task_group() as tg:  # R2: attach while the turn streams
            tg.start_soon(attach)
            await original.rest()
    assert "x-aghoo-idempotent-replay" not in original.response.headers
    invariants(original.events)
    r2 = duplicate["r2"]
    assert frames(r2.events) == frames(original.events), (kinds(r2.events), kinds(original.events))
    assert deltas_text(original.events) == sc.TEXT
    calls = await mock_calls(mock, before)
    assert [c["op"] for c in calls].count("converse-stream") == 1  # the duplicate called no model
    stored = await stored_turn(stack, original.turn_id)
    assert stored["text"] == sc.TEXT

    async with live_turn(stack.client(1), body, headers=key) as r3:  # R3: after completion, a replay
        assert r3.response.headers.get("x-aghoo-idempotent-replay") == "true"
        await r3.rest()
    assert frames(r3.events) == frames(original.events)
    later = (await mock_calls(mock, before))[len(calls) :]  # a sampled labeling job may add labeler calls only
    assert [c for c in later if c["op"] in ("converse-stream", "chat") or c["model"] in (sc.LUNA, sc.HAIKU)] == []

    other = turn_body(f"Something else entirely ({tag()})")  # R5: same key, another body
    reused = await stack.client(0).request("POST", "/v1/turns", json=other, headers=key, expect=409)
    assert reused.json()["error"]["code"] == "idempotency_key_reused"
