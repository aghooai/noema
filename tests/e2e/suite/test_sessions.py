"""Sessions on the compose stack: optimistic ordering (expected_seq 409 session_conflict), the per-session lease and
FIFO queue shared by both replicas (Postgres + Valkey: queued, then session_busy past queue_timeout_ms), list and
history pagination (spec-04 §2, T-SESS on real pods)."""

from __future__ import annotations

import anyio
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import Json, data, first, live_turn, tag, turn_body
from tests.e2e.suite.turns import new_session, run_turn

pytestmark = [pytest.mark.e2e]


async def session_seq(stack: Stack, session_id: str, replica: int = 0) -> int:
    session = (await stack.client(replica).request("GET", f"/v1/sessions/{session_id}", expect=200)).json()
    return int(session["last_seq"])


async def test_expected_seq_conflict(stack: Stack, mock: Mock) -> None:
    await mock.load(sc.document("e2e-seq", [sc.stream(sc.SOL, first_text_ms=200, step_ms=20)], sc.router(ms=100)))
    session_id = await new_session(stack.client(0))
    await run_turn(stack.client(0), f"First question ({tag()})", session_id=session_id, expected_seq=0)
    assert await session_seq(stack, session_id, 1) == 1
    stale = turn_body(f"Second question ({tag()})", session_id=session_id, expected_seq=0)
    conflict = await stack.client(1).request("POST", "/v1/turns", json=stale, expect=409)
    assert conflict.json()["error"]["code"] == "session_conflict"
    await run_turn(stack.client(1), f"Second question ({tag()})", session_id=session_id, expected_seq=1)
    assert await session_seq(stack, session_id) == 2


async def test_queued_then_busy_across_replicas(stack: Stack, mock: Mock) -> None:
    """A turn holds the session lease on api-1. A second turn on api-2 waits in the session's FIFO queue: within
    queue_timeout_ms (2 s) it is served after the first; when the holder outlasts it, it is 409 session_busy."""
    session_id = await new_session(stack.client(0))
    short = sc.stream(sc.SOL, first_text_ms=300, step_ms=100)  # ~1 s per turn
    await mock.load(sc.document("e2e-queue", [short], sc.router(ms=100)))
    results: dict[str, Json] = {}

    async def second() -> None:
        turn = await run_turn(stack.client(1), f"Queued question ({tag()})", session_id=session_id)
        results["second"] = data(turn.events[0])

    async with live_turn(stack.client(0), turn_body(f"Held question ({tag()})", session_id=session_id)) as held:
        await held.until(lambda e: e.event == "generation.started")
        async with anyio.create_task_group() as tg:
            tg.start_soon(second)
            await held.rest()
    assert first(held.events, "turn.completed") and results["second"]["session_id"] == session_id
    assert await session_seq(stack, session_id) == 2

    slow = sc.stream(sc.SOL, first_text_ms=300, step_ms=600)  # ~4 s: longer than the 2 s queue timeout
    await mock.load(sc.document("e2e-busy", [slow], sc.router(ms=100)))
    async with live_turn(stack.client(0), turn_body(f"Long question ({tag()})", session_id=session_id)) as held:
        await held.until(lambda e: e.event == "generation.started")
        body = turn_body(f"Impatient question ({tag()})", session_id=session_id)
        with anyio.fail_after(10):
            busy = await stack.client(1).request("POST", "/v1/turns", json=body, expect=409)
        assert busy.json()["error"]["code"] == "session_busy"
        await held.rest()
    assert await session_seq(stack, session_id, 1) == 3


async def test_list_sessions_and_history_pages(stack: Stack, mock: Mock) -> None:
    """listSessions is newest first and paged by an opaque cursor; listSessionTurns pages the history oldest
    first, a user then an assistant message per committed turn."""
    client = stack.client(0, role="developer2")
    created = [await new_session(client, metadata={"e2e": tag()}) for _ in range(3)]
    page1 = (await client.request("GET", "/v1/sessions", params={"limit": 2}, expect=200)).json()
    assert [s["id"] for s in page1["data"]] == created[::-1][:2] and page1["has_more"] is True
    page2 = (
        await stack.client(1, role="developer2").request(
            "GET", "/v1/sessions", params={"limit": 2, "after": page1["next_cursor"]}, expect=200
        )
    ).json()
    assert page2["data"][0]["id"] == created[0]
    bad = await client.request("GET", "/v1/sessions", params={"after": "not-a-cursor"}, expect=400)
    assert bad.json()["error"]["code"] == "schema_violation"

    await mock.load(sc.document("e2e-history", [sc.stream(sc.SOL, first_text_ms=200, step_ms=20)], sc.router(ms=100)))
    questions = [f"History question {i} ({tag()})" for i in range(2)]
    for question in questions:
        await run_turn(client, question, session_id=created[0])
    path = f"/v1/sessions/{created[0]}/turns"
    head = (await client.request("GET", path, params={"limit": 3}, expect=200)).json()
    assert [m["role"] for m in head["data"]] == ["user", "assistant", "user"] and head["has_more"] is True
    tail = (await client.request("GET", path, params={"limit": 3, "after": head["next_cursor"]}, expect=200)).json()
    messages = head["data"] + tail["data"]
    assert [m["text"] for m in messages] == [questions[0], sc.TEXT, questions[1], sc.TEXT]
    assert [m["seq"] for m in messages] == [1, 1, 2, 2] and tail["has_more"] is False
