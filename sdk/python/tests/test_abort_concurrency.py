"""abort() from another thread or task while a stream is opening (03 §3.4: an explicit abort is final). A POST in
flight is cancelled as soon as its turn.created arrives; a retry backoff ends without a new POST. Deterministic:
the mock and the backoff block on events that the test releases."""

from __future__ import annotations

import threading
from typing import Any

import anyio
import httpx
import pytest
import respx

from aghoo import Aghoo, AsyncAghoo, _retry
from support import goldens, respond, sse_frame

G07 = goldens()["G07"]
INPUT: list[Any] = [{"role": "user", "content": [{"type": "input_text", "text": "Hi"}]}]


def sse() -> httpx.Response:
    content = b"".join(sse_frame(e["id"], e["type"], e["data"]) for e in G07["events"])
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)


def test_sync_abort_from_another_thread_while_the_post_is_in_flight(mock: respx.MockRouter, client: Aghoo) -> None:
    received, release = threading.Event(), threading.Event()

    def admit(request: httpx.Request) -> httpx.Response:
        received.set()
        assert release.wait(10)
        return sse()

    mock.post("/v1/turns").mock(side_effect=admit)
    cancel = mock.post("/v1/turns/turn_1/cancel").mock(side_effect=lambda r: respond("cancelTurn.202"))
    s = client.turns.stream(input=INPUT)
    types: list[str] = []

    def consume() -> None:
        with s:
            types.extend(ev.type for ev in s)

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert received.wait(10)
    assert s.abort() is None  # the turn id is not known yet: the open sends the cancel with turn.created
    release.set()
    consumer.join(10)
    assert cancel.call_count == 1
    assert (types[0], types[-1]) == ("turn.created", "turn.completed")  # the stream then ends normally
    assert s.abort() is not None  # after turn.completed: nothing more is sent
    assert cancel.call_count == 1


def test_sync_abort_during_the_retry_backoff_sends_no_new_post(
    mock: respx.MockRouter, client: Aghoo, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeping = threading.Event()

    def backoff(delay: float, stop: threading.Event | None) -> None:
        sleeping.set()
        assert stop is not None
        assert stop.wait(10)  # ends when abort() sets it

    monkeypatch.setattr(_retry, "sleep", backoff)
    route = mock.post("/v1/turns").mock(side_effect=lambda r: respond("createDecision.429"))
    s = client.turns.stream(input=INPUT)
    texts: list[str] = []
    consumer = threading.Thread(target=lambda: texts.append(s.final_text()))
    consumer.start()
    assert sleeping.wait(10)
    assert s.abort() is None
    consumer.join(10)
    assert texts == [""]  # no turn exists: the stream ends empty, without an error
    assert route.call_count == 1  # and no cancel either (an unmocked request would fail the test)
    assert (list(s), s.decision(), s.turn_id) == ([], None, None)


async def test_async_abort_from_another_task_while_the_post_is_in_flight(
    mock: respx.MockRouter, aclient: AsyncAghoo
) -> None:
    received, release = anyio.Event(), anyio.Event()

    async def admit(request: httpx.Request) -> httpx.Response:
        received.set()
        await release.wait()
        return sse()

    mock.post("/v1/turns").mock(side_effect=admit)
    cancel = mock.post("/v1/turns/turn_1/cancel").mock(side_effect=lambda r: respond("cancelTurn.202"))
    s = aclient.turns.stream(input=INPUT)
    types: list[str] = []

    async def consume() -> None:
        async with s:
            types.extend([ev.type async for ev in s])

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await received.wait()
            assert await s.abort() is None
            release.set()
    assert cancel.call_count == 1
    assert (types[0], types[-1]) == ("turn.created", "turn.completed")


async def test_async_abort_during_the_retry_backoff_sends_no_new_post(
    mock: respx.MockRouter, aclient: AsyncAghoo, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeping = anyio.Event()

    async def backoff(delay: float, stop: anyio.Event | None) -> None:
        sleeping.set()
        assert stop is not None
        await stop.wait()

    monkeypatch.setattr(_retry, "async_sleep", backoff)
    route = mock.post("/v1/turns").mock(side_effect=lambda r: respond("createDecision.429"))
    s = aclient.turns.stream(input=INPUT)
    texts: list[str] = []

    async def consume() -> None:
        texts.append(await s.final_text())

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await sleeping.wait()
            assert await s.abort() is None
    assert (texts, route.call_count, s.turn_id) == ([""], 1, None)


async def test_the_real_backoffs_end_when_stop_is_set() -> None:
    stop, astop = threading.Event(), anyio.Event()
    stop.set()
    astop.set()
    with anyio.fail_after(5):  # a 60 s backoff returns at once
        _retry.sleep(60, stop)
        await _retry.async_sleep(60, astop)
