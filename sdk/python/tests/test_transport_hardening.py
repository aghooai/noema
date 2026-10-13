"""Transport hardening: replayed errors are not retried, dot or empty ids never reach the wire, a stream response is
closed when its open is interrupted, and invalid UTF-8 in a stream is replaced instead of raising."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import anyio
import httpx
import pytest
import respx

from aghoo import Aghoo, AghooError, AsyncAghoo, GenerationDelta
from support import API_KEY, BASE, fixture_json, goldens, respond, sse_frame

INPUT: list[Any] = [{"role": "user", "content": [{"type": "input_text", "text": "Hi"}]}]
SSE = {"content-type": "text/event-stream"}
REPLAYED = "x-aghoo-idempotent-replay"


def replayed_503(request: httpx.Request) -> httpx.Response:
    fx = fixture_json("createDecision.503")
    return httpx.Response(fx["status"], headers={**fx["headers"], REPLAYED: "true"}, json=fx["body"])


def test_an_error_replayed_from_the_idempotency_store_is_not_retried(
    mock: respx.MockRouter, client: Aghoo, sleeps: list[float]
) -> None:
    decisions = mock.post("/v1/decisions").mock(side_effect=replayed_503)
    with pytest.raises(AghooError) as info:
        client.decisions.create([], input="x")
    assert (info.value.status, info.value.retryable, decisions.call_count, sleeps) == (503, True, 1, [])
    turns = mock.post("/v1/turns").mock(side_effect=replayed_503)
    with pytest.raises(AghooError):
        client.turns.stream(input=INPUT).final_text()
    assert (turns.call_count, sleeps) == (1, [])
    mock.post("/v1/decisions").mock(side_effect=lambda r: respond("createDecision.503"))  # not replayed: retried
    with pytest.raises(AghooError):
        client.decisions.create([], input="x")
    assert sleeps == [1.0, 1.0]


async def test_async_replayed_errors_are_not_retried(
    mock: respx.MockRouter, aclient: AsyncAghoo, sleeps: list[float]
) -> None:
    route = mock.post("/v1/decisions").mock(side_effect=replayed_503)
    with pytest.raises(AghooError):
        await aclient.decisions.create([], input="x")
    turns = mock.post("/v1/turns").mock(side_effect=replayed_503)
    with pytest.raises(AghooError):
        await aclient.turns.stream(input=INPUT).final_text()
    assert (route.call_count, turns.call_count, sleeps) == (1, 1, [])


BAD_IDS = ["", ".", ".."]


def test_empty_and_dot_ids_raise_before_any_request(mock: respx.MockRouter, client: Aghoo) -> None:
    calls: list[Callable[[str], object]] = [
        client.sessions.delete,
        client.sessions.retrieve,
        client.turns.cancel,
        client.turns.retrieve,
        client.files.delete,
        lambda i: client.files.derivative(i, "text"),
        lambda i: list(client.sessions.turns(i)),
        lambda i: client.turns.stream(input=INPUT, last_event_id="1", turn_id=i).final_text(),
    ]
    for call in calls:
        for bad in BAD_IDS:
            with pytest.raises(ValueError, match="ids must not be"):
                call(bad)
    assert mock.calls.call_count == 0
    mock.get("/v1/sessions/..ses.1").mock(side_effect=lambda r: respond("getSession.200"))
    client.sessions.retrieve("..ses.1")  # dots inside an id are fine


async def test_async_empty_and_dot_ids_raise(mock: respx.MockRouter, aclient: AsyncAghoo) -> None:
    for bad in BAD_IDS:
        with pytest.raises(ValueError, match="ids must not be"):
            await aclient.sessions.delete(bad)
        with pytest.raises(ValueError, match="ids must not be"):
            await aclient.turns.cancel(bad)
    assert mock.calls.call_count == 0


class _Interrupt(BaseException):
    """Stands for KeyboardInterrupt or a cancellation while the first event is read."""


def test_the_stream_response_is_closed_when_its_open_is_interrupted() -> None:
    closed: list[bool] = []

    class Body(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            if not closed:  # the first read is interrupted
                raise _Interrupt
            yield b""

        def close(self) -> None:
            closed.append(True)

    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers=SSE, stream=Body())))
    with Aghoo(api_key=API_KEY, base_url=BASE, http_client=http) as c, pytest.raises(_Interrupt):
        c.turns.stream(input=INPUT).final_text()
    assert closed == [True]


async def test_a_cancelled_async_open_closes_the_stream_response() -> None:
    started, closed = anyio.Event(), []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            started.set()
            await anyio.sleep_forever()
            yield b""  # pragma: no cover

        async def aclose(self) -> None:
            closed.append(True)

    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers=SSE, stream=Body())))
    async with AsyncAghoo(api_key=API_KEY, base_url=BASE, http_client=http) as c:
        s = c.turns.stream(input=INPUT)
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                tg.start_soon(s.final_text)
                await started.wait()  # the open is reading the first event
                tg.cancel_scope.cancel()
    assert closed == [True]


def test_invalid_utf8_in_a_stream_is_replaced_not_raised(mock: respx.MockRouter, client: Aghoo) -> None:
    g01 = goldens()["G01"]["events"]
    frames = [sse_frame(e["id"], e["type"], e["data"]) for e in g01]
    delta = next(i for i, e in enumerate(g01) if e["type"] == "generation.delta")
    frames[delta] = frames[delta].replace(b'"text":"', b'"text":"\xff', 1)  # one invalid UTF-8 byte
    mock.post("/v1/turns").mock(return_value=httpx.Response(200, headers=SSE, content=b"".join(frames)))
    with client.turns.stream(input=INPUT) as s:
        texts = [ev.text for ev in s if isinstance(ev, GenerationDelta)]
    assert texts[0] == "\ufffd" + g01[delta]["data"]["text"]
