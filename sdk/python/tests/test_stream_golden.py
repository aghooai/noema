"""TurnStream (sync and async) against the golden SSE fixtures of spec-04 §4.2."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx

from aghoo import (
    Aghoo,
    AsyncAghoo,
    DecisionResult,
    GenerationDelta,
    TurnEvent,
    UnknownEvent,
)
from support import API_KEY, goldens, sse_body

GOLDENS = goldens()
UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def chunks(body: bytes, size: int = 7) -> Iterator[bytes]:
    for i in range(0, len(body), size):
        yield body[i : i + size]


async def achunks(body: bytes, size: int = 7) -> AsyncIterator[bytes]:
    for c in chunks(body, size):
        yield c


def expected_text(g: dict[str, Any]) -> str:
    """Oracle: Phase 1 sends deltas only for the committed attempt (P-SSE-1), or the stored output_text."""
    stored = (g.get("db") or {}).get("turn", {}).get("output_text")
    if stored is not None:
        return str(stored)
    return "".join(e["data"]["text"] for e in g["events"] if e["type"] == "generation.delta")


def expected_decision(g: dict[str, Any]) -> dict[str, Any] | None:
    last = [e for e in g["events"] if e["type"] in ("decision.completed", "decision.updated")]
    return last[-1]["data"]["decision"] if last else None


def check(g: dict[str, Any], events: list[TurnEvent], text: str, decision: DecisionResult | None) -> None:
    types = [e.type for e in events if not isinstance(e, UnknownEvent)]
    assert types == [e["type"] for e in g["events"]]
    unknown = [e for e in events if isinstance(e, UnknownEvent)]
    assert [u.raw_type for u in unknown] == ["turn.future_event"]
    deltas = [(e.attempt_id, e.seq, e.text) for e in events if isinstance(e, GenerationDelta)]
    raw = [
        (e["data"]["attempt_id"], e["data"]["seq"], e["data"]["text"])
        for e in g["events"]
        if e["type"] == "generation.delta"
    ]
    assert deltas == raw
    assert text == expected_text(g)
    want = expected_decision(g)
    if want is None:
        assert decision is None
    else:
        assert decision is not None
        assert (decision.id, decision.status, [a.name for a in decision.answers]) == (
            want["id"],
            want["status"],
            [a["name"] for a in want["answers"]],
        )


def check_request(request: httpx.Request, g: dict[str, Any]) -> None:
    assert json.loads(request.content) == {**g["request"]["body"], "stream": True}
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["aghoo-version"] == "2026-10-01"
    assert request.headers["accept"] == "text/event-stream"
    assert request.headers["content-type"] == "application/json"
    assert UUID4.match(request.headers["idempotency-key"])


def test_every_fixture_is_covered() -> None:
    assert len(GOLDENS) >= 17


@pytest.mark.parametrize("gid", sorted(GOLDENS))
def test_sync_stream(gid: str, mock: respx.MockRouter, client: Aghoo) -> None:
    g = GOLDENS[gid]
    route = mock.post("/v1/turns").mock(
        side_effect=lambda r: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=chunks(sse_body(g["events"]))
        )
    )
    with client.turns.stream(**g["request"]["body"]) as s:
        events = list(s)
    check(g, events, s.final_text(), s.decision())
    assert s.turn_id == g["events"][0]["data"]["turn_id"]
    assert s.last_event_id == str(g["events"][-1]["id"])
    assert route.call_count == 1
    check_request(route.calls[0].request, g)


@pytest.mark.parametrize("gid", sorted(GOLDENS))
async def test_async_stream(gid: str, mock: respx.MockRouter, aclient: AsyncAghoo) -> None:
    g = GOLDENS[gid]
    route = mock.post("/v1/turns").mock(
        side_effect=lambda r: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=achunks(sse_body(g["events"]))
        )
    )
    async with aclient.turns.stream(**g["request"]["body"]) as s:
        events = [ev async for ev in s]
    check(g, events, await s.final_text(), await s.decision())
    assert route.call_count == 1
    check_request(route.calls[0].request, g)


def test_final_text_consumes_an_unread_stream(mock: respx.MockRouter, client: Aghoo) -> None:
    g = GOLDENS["G02"]
    mock.post("/v1/turns").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse_body(g["events"]))
    )
    s = client.turns.stream(**g["request"]["body"])
    assert s.final_text() == expected_text(g)
    assert s.decision() is not None
    assert list(s) == []
