"""Timing and determinism on a `VirtualClock` (spec-10 §2.2, §2.4, §5): the stream schedule, fixed and probe
latencies, seeded draws independent of arrival order, the DeepSeek tail and the Haiku latency band, `window_ms`,
`rate`, load stamps and nonces, and the stall/hang faults."""

from __future__ import annotations

import re
import statistics
from typing import Any

import httpx
import pytest
from tests.unit.mockbedrock.helpers import (
    DEEPSEEK,
    GLM,
    HAIKU,
    OPUS,
    SOL,
    USER_HI,
    bearer_headers,
    scenario,
    sigv4_headers,
    user,
)
from tools.mockbedrock import MockBedrock, VirtualClock
from types_aiobotocore_bedrock_runtime import BedrockRuntimeClient

MS = 1_000_000


def since_recv(rec: dict[str, object], key: str) -> float:
    value, start = rec[key], rec["t_req_recv_ns"]
    assert isinstance(value, int)
    assert isinstance(start, int)
    return (value - start) / MS


async def test_stream_schedule_is_exact(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    spec = {
        "first_event_ms": 200,
        "reasoning": {"deltas": 5, "every_ms": 20},
        "first_text_ms": 720,
        "text": "x" * 64,
        "tokens_per_s": 40,
        "chunk_tokens": 4,
    }
    mock.load_scenario(scenario({"match": {"model": OPUS, "op": "converse-stream"}, "stream": spec}))
    resp = await bedrock.converse_stream(modelId=OPUS, messages=USER_HI)
    events = [e async for e in resp["stream"]]
    (rec,) = mock.requests()
    assert since_recv(rec, "t_first_byte_ns") == 200
    assert since_recv(rec, "t_first_text_sent_ns") == 720
    # 64 chars = 4 chunks of 4 tokens at 40 tokens/s: the last chunk at 720 + 3 * 100 ms
    assert events[-1]["metadata"]["metrics"]["latencyMs"] == 1020
    assert since_recv(rec, "t_done_ns") == 1020


async def test_fixed_latency_counts_from_arrival(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    mock.load_scenario(scenario({"match": {"model": HAIKU}, "answer": {"raw_text": "x"}, "latency_ms": 345}))
    resp = await bedrock.converse(modelId=HAIKU, messages=USER_HI)
    assert resp["metrics"]["latencyMs"] == 345
    assert since_recv(mock.requests()[0], "t_first_byte_ns") == 345


async def probe_latencies(mock: MockBedrock, http: httpx.AsyncClient, prompts: list[str]) -> list[float]:
    out = []
    for prompt in prompts:
        body = {"model": DEEPSEEK, "messages": [{"role": "user", "content": prompt}]}
        assert (await http.post("/v1/chat/completions", json=body, headers=bearer_headers())).status_code == 200
        out.append(since_recv(mock.requests()[-1], "t_first_byte_ns"))
    return out


async def test_draws_repeat_per_body_and_ignore_arrival_order(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    doc = scenario({"match": {"model": DEEPSEEK}, "answer": {"raw_text": "x"}}, fixed_ms=None)
    mock.load_scenario(doc)
    first = await probe_latencies(mock, http, ["a", "b", "a"])
    assert first[0] != first[2]  # k counts identical bodies (a backup gets its own draw)
    mock.load_scenario(doc)
    second = await probe_latencies(mock, http, ["b", "a", "a"])
    assert second == [first[1], first[0], first[2]]


async def test_deepseek_tail_and_haiku_shares(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(scenario({"match": {}, "answer": {"raw_text": "x"}}, fixed_ms=None))
    deepseek = await probe_latencies(mock, http, [f"q{i}" for i in range(400)])
    tail = [v for v in deepseek if v >= 4000]
    assert 2 <= len(tail) <= 20  # 2% of 400 = 8 expected
    assert all(4000 <= v <= 12000 for v in tail)
    body = sorted(v for v in deepseek if v < 4000)
    assert 270 < statistics.quantiles(body, n=20)[0] < 330  # live 299-553 ms (Wave E2 probe row 300-560)
    assert 520 < statistics.quantiles(body, n=20)[-1] < 610
    haiku = []
    for i in range(200):
        msg = user({"text": f"h{i}"})
        resp = await http.post(f"/model/{HAIKU}/converse", json={"messages": msg}, headers=sigv4_headers())
        haiku.append(resp.json()["metrics"]["latencyMs"])
    # live 647-942 ms (Wave E2 probe row 640-950): under 5% pass the router's 1,000 ms deadline
    assert 600 < statistics.quantiles(haiku, n=20)[0] < 690 and 900 < statistics.quantiles(haiku, n=20)[-1] < 1010
    assert sum(v > 1000 for v in haiku) / len(haiku) < 0.05


async def test_window_ms_scopes_a_fault(mock: MockBedrock, clock: VirtualClock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(
        scenario(
            {"match": {"model": SOL}, "fault": {"kind": "http_5xx", "status": 503, "window_ms": [1000, 2000]}},
            {"match": {"model": SOL}, "answer": {"raw_text": "ok"}},
        )
    )
    body = {"messages": USER_HI}
    statuses = []
    for _ in range(3):
        statuses.append((await http.post(f"/model/{SOL}/converse", json=body, headers=sigv4_headers())).status_code)
        clock.advance_ms(1000)
    assert statuses == [200, 503, 200]


async def test_rate_is_seeded(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    doc = scenario(
        {"match": {"model": SOL}, "fault": {"kind": "throttling", "rate": 0.5}},
        {"match": {"model": SOL}, "answer": {"raw_text": "ok"}},
    )
    runs = []
    for _ in range(2):
        mock.load_scenario(doc)
        codes = []
        for i in range(40):
            msg = user({"text": f"r{i}"})
            resp = await http.post(f"/model/{SOL}/converse", json={"messages": msg}, headers=sigv4_headers())
            codes.append(resp.status_code)
        runs.append(codes)
    assert runs[0] == runs[1]
    assert 8 <= runs[0].count(429) <= 32


async def test_stamps_and_nonce(mock: MockBedrock, clock: VirtualClock, bedrock: BedrockRuntimeClient) -> None:
    mock.load_scenario(
        scenario(
            {
                "match": {"model": SOL},
                "stream": {"first_text_ms": 300, "text": "abcdefgh", "chunk_tokens": 1, "stamp": True},
            }
        )
    )
    msg = user({"text": "hello «nonce:7f3a»"})
    resp = await bedrock.converse_stream(modelId=SOL, messages=msg)
    texts = [e["contentBlockDelta"]["delta"]["text"] async for e in resp["stream"] if "contentBlockDelta" in e]
    stamps = [int(m) for t in texts for m in re.findall(r"«ts:(\d+)»", t)]
    assert "".join(re.sub(r"«ts:\d+»", "", t) for t in texts) == "abcdefgh"
    (rec,) = mock.requests()
    assert rec["nonce"] == "7f3a"
    assert stamps[0] == rec["t_first_text_sent_ns"] == rec["t_req_recv_ns"] + 300 * MS
    assert stamps == sorted(stamps)


async def read_stream(
    client: httpx.AsyncClient, path: str, body: dict[str, Any], headers: dict[str, str], into: bytearray
) -> None:
    async with client.stream("POST", path, json=body, headers=headers) as resp:
        async for data in resp.aiter_bytes():
            into += data


@pytest.mark.parametrize("op", ["converse-stream", "chat-stream"])
async def test_stall_after_n_chunks(mock: MockBedrock, op: str) -> None:
    model = SOL if op == "converse-stream" else DEEPSEEK
    mock.load_scenario(
        scenario(
            {
                "match": {"model": model},
                "stream": {"chunks": ["a", "b", "c", "d", "e"]},
                "fault": {"kind": "stall_after", "n": 3},
            }
        )
    )
    body: dict[str, Any]
    if op == "converse-stream":
        path, body, headers = f"/model/{SOL}/converse-stream", {"messages": USER_HI}, sigv4_headers()
    else:
        path = "/v1/chat/completions"
        body, headers = (
            {"model": DEEPSEEK, "messages": [{"role": "user", "content": "q"}], "stream": True},
            bearer_headers(),
        )
    received = bytearray()
    async with httpx.AsyncClient(base_url=mock.endpoint_url, timeout=0.3) as client:
        with pytest.raises(httpx.ReadTimeout):
            await read_stream(client, path, body, headers, received)
    for chunk in (b"a", b"b", b"c"):
        assert b'"' + chunk + b'"' in received
    assert b'"d"' not in received
    assert mock.requests()[-1]["outcome"] == "hang"


def reasoning_deltas(events: list[Any]) -> list[Any]:
    return [e for e in events if "reasoningContent" in e.get("contentBlockDelta", {}).get("delta", {})]


async def test_probe_defaults_reasoning_cadence(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    mock.load_scenario(scenario({"match": {}, "stream": {"text": "abcd"}}, fixed_ms=None))
    resp = await bedrock.converse_stream(modelId=OPUS, messages=USER_HI, outputConfig={"effort": "medium"})
    events = [e async for e in resp["stream"]]
    first_text = since_recv(mock.requests()[-1], "t_first_text_sent_ns")
    assert 1600 < first_text < 14500  # Opus above low: first text 2000-11600 ms (p5-p95)
    deltas = reasoning_deltas(events)
    assert len(deltas) - 1 == int((first_text - 200) // 20)  # every 20 ms after the 200 ms first event (+ signature)


@pytest.mark.parametrize(("model", "lo", "hi"), [(OPUS, 1000, 2200), (GLM, 350, 1000), (SOL, 630, 1250)])
async def test_probe_defaults_stream_no_reasoning_at_low(
    mock: MockBedrock, bedrock: BedrockRuntimeClient, model: str, lo: float, hi: float
) -> None:
    """Wave E live smoke: Opus, GLM and Sol at effort low streamed no reasoning (L2, L8), at the live first-text
    latencies (the probe rows of the low efforts follow the live cassettes)."""
    mock.load_scenario(scenario({"match": {}, "stream": {"text": "abcd"}}, fixed_ms=None))
    resp = await bedrock.converse_stream(modelId=model, messages=USER_HI, outputConfig={"effort": "low"})
    events = [e async for e in resp["stream"]]
    assert reasoning_deltas(events) == []
    assert lo * 0.8 < since_recv(mock.requests()[-1], "t_first_text_sent_ns") < hi * 1.25


async def test_probe_defaults_sol_high_hides_its_reasoning(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    """Live L7: Sol at effort high streams one `redactedContent` delta before the text, which comes seconds later."""
    mock.load_scenario(scenario({"match": {}, "stream": {"text": "abcd"}}, fixed_ms=None))
    resp = await bedrock.converse_stream(
        modelId=SOL, messages=USER_HI, outputConfig={"effort": "high"}, inferenceConfig={"maxTokens": 512}
    )
    events = [e async for e in resp["stream"]]
    (delta,) = reasoning_deltas(events)
    assert set(delta["contentBlockDelta"]["delta"]["reasoningContent"]) == {"redactedContent"}
    assert since_recv(mock.requests()[-1], "t_first_text_sent_ns") > 1600
    usage = next(e["metadata"]["usage"] for e in events if "metadata" in e)
    assert usage["outputTokens"] <= 512
