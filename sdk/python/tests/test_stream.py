"""TurnStream / AsyncTurnStream behavior: reconciliation, retries before the first event, abort and resume."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from aghoo import Aghoo, AghooConnectionError, AghooError, AsyncAghoo, CancelResult, ErrorEvent, GenerationDelta
from support import fixture_json, goldens, respond, sse_frame

G = goldens()
G01, G07, G10 = G["G01"], G["G07"], G["G10"]
INPUT: list[Any] = [{"role": "user", "content": [{"type": "input_text", "text": "Hi"}]}]
SSE = {"content-type": "text/event-stream"}


def body(events: list[dict[str, Any]]) -> bytes:
    return b"".join(sse_frame(e["id"], e["type"], e["data"]) for e in events)


def sse(events: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, headers=SSE, content=body(events))


def test_only_the_committed_attempt_is_text_and_rerouted_discards(mock: respx.MockRouter, client: Aghoo) -> None:
    done = G01["events"][-1]["data"]
    evs = [
        {"type": "turn.created", "turn_id": "turn_9", "session_id": None, "created_at": 1},
        {"type": "generation.delta", "attempt_id": "att_1", "seq": 0, "text": "spec "},
        {"type": "generation.committed", "attempt_id": "att_2", "reason": "spec_agrees"},
        {"type": "generation.delta", "attempt_id": "att_2", "seq": 0, "text": "Hello "},
        {"type": "generation.delta", "attempt_id": "att_2", "seq": 1, "text": "wrong "},
        {
            "type": "generation.rerouted",
            "from_attempt_id": "att_2",
            "to_attempt_id": "att_3",
            "reason": "x",
            "discard_from_seq": 1,
        },
        {"type": "generation.delta", "attempt_id": "att_3", "seq": 1, "text": "world"},
        done,
    ]
    mock.post("/v1/turns").mock(
        return_value=sse([{"id": i + 1, "type": e["type"], "data": e} for i, e in enumerate(evs)])
    )
    assert client.turns.stream(input=INPUT).final_text() == "Hello world"


def test_error_events_are_yielded_and_partial_text_is_final(mock: respx.MockRouter, client: Aghoo) -> None:
    mock.post("/v1/turns").mock(return_value=sse(G10["events"]))
    with client.turns.stream(input=INPUT) as s:
        errors = [ev for ev in s if isinstance(ev, ErrorEvent)]
    assert [(e.code, e.retryable) for e in errors] == [("upstream_stalled", True)]
    assert s.final_text() == "The capital of France is Paris. It is also "


def test_retries_before_the_first_event_with_one_key(
    mock: respx.MockRouter, client: Aghoo, sleeps: list[float]
) -> None:
    route = mock.post("/v1/turns").mock(
        side_effect=[respond("createDecision.503"), httpx.Response(200, headers=SSE, content=b""), sse(G01["events"])]
    )
    assert "The capital of France" in client.turns.stream(input=INPUT).final_text()
    assert route.call_count == 3
    assert len({c.request.headers["idempotency-key"] for c in route.calls}) == 1
    assert sleeps == [1.0, 0.5]


def test_no_retry_after_the_first_event(mock: respx.MockRouter, client: Aghoo, sleeps: list[float]) -> None:
    route = mock.post("/v1/turns").mock(return_value=sse(G01["events"][:3]))
    s = client.turns.stream(input=INPUT)
    seen: list[str] = []
    with pytest.raises(AghooConnectionError, match=r"before turn\.completed"):
        seen.extend(ev.type for ev in s)
    assert seen == ["turn.created", "preflight.completed", "route.selected"]
    assert route.call_count == 1
    with pytest.raises(AghooConnectionError):
        s.final_text()


def test_malformed_data_and_non_retryable_errors(mock: respx.MockRouter, client: Aghoo, sleeps: list[float]) -> None:
    mock.post("/v1/turns").mock(return_value=httpx.Response(200, headers=SSE, content=b"data: {nope\n\n"))
    with pytest.raises(AghooConnectionError, match="malformed"):
        client.turns.stream(input=INPUT).final_text()
    route = mock.post("/v1/turns").mock(side_effect=lambda r: respond("createTurn.422"))
    route.reset()
    with pytest.raises(AghooError) as info, client.turns.stream(input=INPUT, strategy="hedge"):
        pass
    assert (info.value.code, info.value.param, route.call_count) == ("feature_not_available", "strategy", 1)


def test_break_then_final_text_continues_and_close_ends_it(mock: respx.MockRouter, client: Aghoo) -> None:
    mock.post("/v1/turns").mock(side_effect=lambda r: sse(G01["events"]))
    with client.turns.stream(input=INPUT) as s:
        for ev in s:
            if isinstance(ev, GenerationDelta):
                break
        assert s.final_text().startswith("The capital of France is Paris.")
    s2 = client.turns.stream(input=INPUT)
    next(s2)
    s2.close()
    with pytest.raises(AghooConnectionError):
        s2.final_text()


def test_abort_sends_one_cancel_and_the_stream_ends_with_turn_completed(mock: respx.MockRouter, client: Aghoo) -> None:
    mock.post("/v1/turns").mock(return_value=sse(G07["events"]))
    cancel = mock.post("/v1/turns/turn_1/cancel").mock(side_effect=lambda r: respond("cancelTurn.202"))
    with client.turns.stream(input=INPUT) as s:
        types: list[str] = []
        for ev in s:
            types.append(ev.type)
            if isinstance(ev, GenerationDelta):
                assert s.abort() == CancelResult(turn_id="turn_1", status="cancelling")
                s.abort()
    assert types[-1] == "turn.completed"
    assert cancel.call_count == 1
    assert cancel.calls[0].request.headers["idempotency-key"]
    assert s.final_text() == "The capital of France is Paris. "


def test_abort_right_after_opening_cancels_with_the_first_events_turn_id(mock: respx.MockRouter, client: Aghoo) -> None:
    mock.post("/v1/turns").mock(return_value=sse(G07["events"]))
    cancel = mock.post("/v1/turns/turn_1/cancel").mock(side_effect=lambda r: respond("cancelTurn.202"))
    with client.turns.stream(input=INPUT) as s:
        assert s.turn_id == "turn_1"  # turn.created arrives inside the retry loop of the open
        assert s.abort() == CancelResult(turn_id="turn_1", status="cancelling")
        assert cancel.call_count == 1
        types = [ev.type for ev in s]
        assert (types[0], types[-1]) == ("turn.created", "turn.completed")  # nothing is skipped
    assert cancel.call_count == 1
    assert s.abort() is not None  # after turn.completed: no second request
    assert cancel.call_count == 1
    unopened = client.turns.stream(input=INPUT)
    assert unopened.abort() is None
    assert (unopened.final_text(), unopened.decision(), list(unopened)) == ("", None, [])
    assert mock.calls.call_count == 2  # the unopened stream sent nothing


def test_create_retrieve_cancel_and_resume(mock: respx.MockRouter, client: Aghoo) -> None:
    created = mock.post("/v1/turns").mock(side_effect=lambda r: respond("createTurn.200"))
    t = client.turns.create(input=INPUT, store=True)
    assert json.loads(created.calls[0].request.content) == {"input": INPUT, "store": True, "stream": False}
    assert t.attempts[0].outcome == "committed"
    mock.get("/v1/turns/turn_1").mock(side_effect=lambda r: respond("getTurn.200"))
    assert client.turns.retrieve("turn_1").routing.route_id == "sol-fast"
    resume = mock.get("/v1/turns/turn_1/events").mock(side_effect=lambda r: respond("resumeTurnEvents.501"))
    s = client.turns.stream(input=INPUT, last_event_id="7", turn_id="turn_1")
    assert s.turn_id == "turn_1"
    with pytest.raises(AghooError, match="not served"):
        s.final_text()
    assert resume.calls[0].request.headers["last-event-id"] == "7"
    mock.post("/v1/turns/turn_1/cancel").mock(side_effect=lambda r: respond("cancelTurn.202"))
    resumed = client.turns.stream(input=INPUT, last_event_id="7", turn_id="turn_1")
    assert resumed.abort() == CancelResult(turn_id="turn_1", status="cancelling")  # the turn exists: cancel it
    with pytest.raises(TypeError, match="turn_id"):
        client.turns.stream(input=INPUT, last_event_id="7")


async def test_async_retries_abort_and_drop(mock: respx.MockRouter, aclient: AsyncAghoo, sleeps: list[float]) -> None:
    route = mock.post("/v1/turns").mock(side_effect=[respond("createTurn.500"), sse(G07["events"])])
    cancel = mock.post("/v1/turns/turn_1/cancel").mock(side_effect=lambda r: respond("cancelTurn.202"))
    async with aclient.turns.stream(input=INPUT) as s:
        async for ev in s:
            if isinstance(ev, GenerationDelta):
                assert (await s.abort()) == CancelResult(turn_id="turn_1", status="cancelling")
    assert (route.call_count, cancel.call_count, sleeps) == (2, 1, [0.25])
    assert await s.final_text() == "The capital of France is Paris. "
    route.mock(side_effect=lambda r: sse(G01["events"][:2]))
    with pytest.raises(AghooConnectionError):
        await aclient.turns.stream(input=INPUT).final_text()
    unopened = aclient.turns.stream(input=INPUT)
    assert await unopened.abort() is None
    assert (await unopened.final_text(), await unopened.decision()) == ("", None)


async def test_async_create_resume_and_errors(mock: respx.MockRouter, aclient: AsyncAghoo) -> None:
    mock.post("/v1/turns").mock(side_effect=lambda r: respond("createTurn.200"))
    assert (await aclient.turns.create(input=INPUT)).text == "Paris is the capital of France."
    mock.get("/v1/turns/turn_1/events").mock(side_effect=lambda r: respond("resumeTurnEvents.501"))
    with pytest.raises(AghooError, match="not served"):
        async with aclient.turns.stream(input=INPUT, last_event_id="3", turn_id="turn_1"):
            pass
    mock.post("/v1/turns/turn_1/cancel").mock(side_effect=lambda r: respond("cancelTurn.202"))
    assert (await aclient.turns.cancel("turn_1")).status == "cancelling"
    mock.get("/v1/turns/turn_1").mock(side_effect=lambda r: respond("getTurn.200"))
    assert (await aclient.turns.retrieve("turn_1")).seq == 5
    mock.post("/v1/turns").mock(return_value=httpx.Response(200, headers=SSE, content=b"data: {nope\n\n"))
    with pytest.raises(AghooConnectionError, match="malformed"):
        await aclient.turns.stream(input=INPUT).decision()
    assert fixture_json("cancelTurn.202")["body"]["status"] == "cancelling"
