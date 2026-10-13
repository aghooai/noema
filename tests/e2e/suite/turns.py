"""Turn helpers of the e2e suite: one validated turn (P-SSE-1 checked), its stored object, an opus-deep session."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tests.e2e.conftest import CheckedClient, Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import Json, LiveTurn, data, eventually, first, live_turn, tag, turn_body
from tests.golden.compare import sse_invariants

from noema.contracts.validate import SseEvent


def invariants(events: list[SseEvent], *, reached_t_d0: bool = True) -> None:
    payloads = [data(e) for e in events]
    problems = sse_invariants(payloads, reached_t_d0=reached_t_d0)
    assert problems == [], (problems, [e.event for e in events])


async def run_turn(
    client: CheckedClient, text: str, *, headers: Mapping[str, str] | None = None, **extra: Any
) -> LiveTurn:
    """One streamed turn read to its end; the stream is contract-validated and P-SSE-1 must hold."""
    async with live_turn(client, turn_body(text, **extra), headers=headers) as turn:
        await turn.rest()
    invariants(turn.events)
    return turn


def started(events: list[SseEvent]) -> list[Json]:
    return [data(e) for e in events if e.event == "generation.started"]


async def stored_turn(stack: Stack, turn_id: str, *, replica: int = 1, timeout_s: float = 20.0) -> Json:
    """getTurn on `replica` (default: api-2, usually not the pod that ran the turn) once it is stored."""
    return await eventually(stack.client(replica), f"/v1/turns/{turn_id}", lambda b: True, timeout_s=timeout_s)


async def new_session(client: CheckedClient, **body: Any) -> str:
    session = (await client.request("POST", "/v1/sessions", json=body, expect=201)).json()
    return str(session["id"])


async def opus_session(stack: Stack, mock: Mock) -> tuple[str, list[SseEvent]]:
    """A new session whose first turn decided opus-deep (high, 4000) over the speculative sol-fast attempt (the
    G02 restart), so the session's sticky spec is opus-deep; returns the session id and that turn's events."""
    session_id = await new_session(stack.client(0))
    sol = sc.stream(sc.SOL, first_text_ms=300)
    opus = sc.stream(sc.OPUS, first_text_ms=700, reasoning=20)
    await mock.load(sc.document("e2e-opus-session", [sol, opus], sc.router(intent=sc.CODING, level=sc.HARD)))
    turn = await run_turn(stack.client(0), f"Write a sort function. ({tag()})", session_id=session_id)
    assert first(turn.events, "generation.committed")["reason"] == "spec_agrees", turn.timeline()
    assert [a["route_id"] for a in started(turn.events)] == ["sol-fast", "opus-deep"], turn.timeline()
    return session_id, turn.events
