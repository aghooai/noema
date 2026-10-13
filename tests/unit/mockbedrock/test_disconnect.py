"""The `disconnected` outcome: a request is cancelled when its client goes away, while held on a barrier, during
the latency wait or mid-stream (spec-10 §2.2 barriers, §2.4); the handler finishes (`t_done_ns`) and nothing is
written afterwards. A client that half-closes (FIN) is treated as gone: the server cannot tell it from a close."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import orjson
import pytest
from tests.unit.mockbedrock.helpers import (
    DEEPSEEK,
    SOL,
    USER_HI,
    GatedClock,
    bearer_headers,
    scenario,
    sigv4_headers,
    until,
)
from tools.mockbedrock import MockBedrock

MS = 1_000_000
CHAT_BODY: dict[str, Any] = {"model": DEEPSEEK, "messages": [{"role": "user", "content": "q"}]}
CASES: dict[str, tuple[str, str, dict[str, Any]]] = {  # op -> (model, path, body)
    "converse": (SOL, f"/model/{SOL}/converse", {"messages": USER_HI}),
    "converse-stream": (SOL, f"/model/{SOL}/converse-stream", {"messages": USER_HI}),
    "chat": (DEEPSEEK, "/v1/chat/completions", CHAT_BODY),
    "chat-stream": (DEEPSEEK, "/v1/chat/completions", {**CHAT_BODY, "stream": True}),
}


def request_of(op: str) -> tuple[str, str, dict[str, Any], dict[str, str]]:
    model, path, body = CASES[op]
    return model, path, body, sigv4_headers() if op.startswith("converse") else bearer_headers()


@pytest.fixture
def gated() -> GatedClock:
    return GatedClock()


@pytest.fixture
async def gated_mock(gated: GatedClock) -> AsyncIterator[MockBedrock]:
    async with MockBedrock(clock=gated) as server:
        yield server


def last(mock: MockBedrock) -> dict[str, Any]:
    records = mock.requests()
    return records[-1] if records else {}


@pytest.mark.parametrize("op", list(CASES))
async def test_client_gone_while_held_on_a_barrier(mock: MockBedrock, http: httpx.AsyncClient, op: str) -> None:
    model, path, body, headers = request_of(op)
    mock.load_scenario(scenario({"match": {"model": model}, "answer": {"raw_text": "late"}, "barrier": "gate"}))
    task = asyncio.create_task(http.post(path, json=body, headers=headers))
    await until(lambda: last(mock).get("barrier") == "gate", "the request to be held")
    task.cancel()  # the client gives up: httpx closes the connection
    await until(lambda: last(mock).get("t_done_ns") is not None, "the handler to finish")
    rec = last(mock)
    assert (rec["outcome"], rec["status"], rec["t_first_byte_ns"]) == ("disconnected", None, None)
    mock.release("gate")  # releasing later finds nothing to answer
    await asyncio.sleep(0)
    assert last(mock)["outcome"] == "disconnected"


async def test_client_gone_during_the_latency_wait(gated_mock: MockBedrock, gated: GatedClock) -> None:
    gated_mock.load_scenario(scenario({"match": {"model": SOL}, "answer": {"raw_text": "x"}, "latency_ms": 900}))
    gated.gate_ns = gated.now_ns()  # every sleep blocks
    async with httpx.AsyncClient(base_url=gated_mock.endpoint_url, timeout=5.0) as http:
        call = http.post(f"/model/{SOL}/converse", json={"messages": USER_HI}, headers=sigv4_headers())
        task = asyncio.create_task(call)
        await until(lambda: last(gated_mock).get("rule") == 0, "the request to resolve")
        task.cancel()
    await until(lambda: last(gated_mock).get("t_done_ns") is not None, "the handler to finish")
    assert last(gated_mock)["outcome"] == "disconnected"


@pytest.mark.parametrize("op", ["converse-stream", "chat-stream"])
async def test_client_gone_mid_stream(gated_mock: MockBedrock, gated: GatedClock, op: str) -> None:
    model, path, body, headers = request_of(op)
    stream = {"chunks": ["first", "second"], "chunk_tokens": 1, "tokens_per_s": 1}  # "second" is due 1 s later
    gated_mock.load_scenario(scenario({"match": {"model": model}, "stream": stream}))
    gated.gate_ns = gated.now_ns() + 500 * MS
    received = bytearray()
    async with (
        httpx.AsyncClient(base_url=gated_mock.endpoint_url, timeout=5.0) as client,
        client.stream("POST", path, json=body, headers=headers) as resp,
    ):
        assert resp.status_code == 200
        async for data in resp.aiter_bytes():
            received += data
            if b"first" in received:
                break  # leaving the block with the body unread closes the connection
    await until(lambda: last(gated_mock).get("t_done_ns") is not None, "the handler to finish")
    rec = last(gated_mock)
    assert (rec["outcome"], rec["status"]) == ("disconnected", 200)
    assert rec["t_first_text_sent_ns"] is not None
    assert b"second" not in received


async def test_half_closed_client_counts_as_gone(mock: MockBedrock) -> None:
    """Pinned on purpose: after a FIN the server cannot tell a half-close from a full close without writing, and
    honoring half-closes would leave held, hanging and waiting requests of vanished clients alive forever (uvicorn
    and aiohttp also treat EOF as a disconnect)."""
    mock.load_scenario(scenario({"match": {"model": SOL}, "answer": {"raw_text": "late"}, "barrier": "gate"}))
    payload = orjson.dumps({"messages": USER_HI})
    head = [f"POST /model/{SOL}/converse HTTP/1.1", "host: mock", f"content-length: {len(payload)}"]
    head += [f"{k}: {v}" for k, v in sigv4_headers().items()]
    reader, writer = await asyncio.open_connection("127.0.0.1", mock.port)
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + payload)
    await writer.drain()
    await until(lambda: last(mock).get("barrier") == "gate", "the request to be held")
    writer.write_eof()
    await until(lambda: last(mock).get("t_done_ns") is not None, "the handler to finish")
    assert last(mock)["outcome"] == "disconnected"
    assert await reader.read() == b""
    writer.close()
