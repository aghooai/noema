"""Mantle chat completions (JSON + SSE) read by the real openai client, logprob scenarios (T-LP-1), the route
quirks of the probes and the two Mantle auth modes (spec-10 §2.1a, §2.2; ADR-001 §6)."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from typing import Any

import httpx
import openai
import orjson
import pytest
from tests.unit.mockbedrock.helpers import (
    AKID_B,
    DEEPSEEK,
    app_question,
    bearer_headers,
    scenario,
    short_term_token,
    sigv4_headers,
)
from tools.mockbedrock import MockBedrock

LETTERS = ["A", "B", "C", "D", "E", "F"]
SIX = app_question(["a", "b", "c", "d", "e", "f"])
T_LP_1 = {
    "dist": {"a": 0.62, "b": 0.21, "c": 0.03, "d": 0.04, "e": 0.02},
    "whitespace_variants": {"c": 0.06},
    "eos_mass": 0.015,
    "other_tokens": {" ": 0.004},
    "censor": ["f"],
}


@pytest.fixture
async def client(mock: MockBedrock) -> AsyncIterator[openai.AsyncOpenAI]:
    async with openai.AsyncOpenAI(base_url=mock.mantle_url, api_key=short_term_token(), max_retries=0) as c:
        yield c


def parse_logprob(top: list[tuple[str, float]], letters: list[str]) -> tuple[dict[str, float], list[str], float]:
    """spec-06 §5.6 over one answer token's top list (the decision parser, restated for the mock self-test)."""
    mass = dict.fromkeys(letters, 0.0)
    other = 0.0
    for token, lp in top:
        c = token.strip()
        if c in mass:
            mass[c] += math.exp(lp)
        else:
            other += math.exp(lp)
    s = sum(mass.values())
    t = max(0.0, 1.0 - s - other)
    censored = [c for c in letters if mass[c] == 0.0]
    return {c: mass[c] / (s + t) for c in letters if mass[c] > 0}, censored, t / (s + t)


async def test_t_lp_1_censored_tail(mock: MockBedrock, client: openai.AsyncOpenAI) -> None:
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK, "op": "chat"}, "answer": T_LP_1}))
    resp = await client.chat.completions.create(
        model=DEEPSEEK, messages=[{"role": "user", "content": SIX}], max_tokens=8, logprobs=True, top_logprobs=20
    )
    choice = resp.choices[0]
    assert choice.message.content == "A"
    assert choice.finish_reason == "stop"
    assert choice.logprobs is not None
    assert choice.logprobs.content is not None
    (token,) = choice.logprobs.content
    assert token.token == "A"
    assert token.bytes == [65]
    top = [(t.token, t.logprob) for t in token.top_logprobs]
    assert [t for t, _ in top] == ["A", "B", " C", "D", "C", "E", "<|end|>", " "]
    p, censored, unresolved = parse_logprob(top, LETTERS)
    assert {k: round(v, 6) for k, v in p.items()} == {
        "A": 0.632008,
        "B": 0.214067,
        "C": 0.091743,
        "D": 0.040775,
        "E": 0.020387,
    }
    assert censored == ["F"]
    assert round(unresolved, 6) == 0.001019


async def test_split_tokens_and_top_k(mock: MockBedrock, client: openai.AsyncOpenAI) -> None:
    answer = {"dist": {"a": 0.5, "b": 0.3}, "split_tokens": {"b": 0.15}, "top_k": 2}
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "answer": answer}))
    resp = await client.chat.completions.create(
        model=DEEPSEEK, messages=[{"role": "user", "content": SIX}], logprobs=True, top_logprobs=20
    )
    assert resp.choices[0].logprobs is not None
    content = resp.choices[0].logprobs.content or []
    assert [t.token for t in content[0].top_logprobs] == ["A", "B"]  # top_k caps the list ("b" split fragment cut)
    resp = await client.chat.completions.create(
        model=DEEPSEEK, messages=[{"role": "user", "content": SIX}], logprobs=True, top_logprobs=3
    )
    assert resp.choices[0].logprobs is not None
    assert [t.token for t in (resp.choices[0].logprobs.content or [])[0].top_logprobs] == ["A", "B"]


async def test_verbalized_json_usage_and_refusal(mock: MockBedrock, client: openai.AsyncOpenAI) -> None:
    usage = {"input_tokens": 7, "output_tokens": 3, "cached_input_tokens": 2}
    mock.load_scenario(
        scenario(
            {"match": {"model": DEEPSEEK, "nth": 1}, "answer": {"dist": {"a": 1.0}}, "stream": {"usage": usage}},
            {"match": {"model": DEEPSEEK}, "answer": {"raw_text": "x"}, "fault": {"kind": "refusal"}},
        )
    )
    resp = await client.chat.completions.create(model=DEEPSEEK, messages=[{"role": "user", "content": SIX}])
    assert resp.choices[0].message.content is not None
    assert resp.choices[0].message.content.startswith('{"A": 1.0')
    assert resp.choices[0].logprobs is None
    assert resp.usage is not None
    assert (resp.usage.prompt_tokens, resp.usage.completion_tokens, resp.usage.total_tokens) == (9, 3, 12)
    assert resp.usage.prompt_tokens_details is not None
    assert resp.usage.prompt_tokens_details.cached_tokens == 2
    resp = await client.chat.completions.create(model=DEEPSEEK, messages=[{"role": "user", "content": SIX}])
    assert resp.choices[0].finish_reason == "content_filter"
    assert resp.choices[0].message.refusal


async def test_chat_json_has_a_service_tier(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    """Wave E fidelity (live DeepSeek chat JSON carries a top-level `service_tier`)."""
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "answer": {"raw_text": "x"}}))
    body = {"model": DEEPSEEK, "messages": [{"role": "user", "content": "hi"}]}
    resp = await http.post("/v1/chat/completions", json=body, headers=bearer_headers())
    assert resp.status_code == 200 and resp.json()["service_tier"] == "default"


async def test_stream_with_usage_via_openai_client(mock: MockBedrock, client: openai.AsyncOpenAI) -> None:
    mock.load_scenario(
        scenario({"match": {"model": DEEPSEEK, "op": "chat-stream"}, "stream": {"text": "Paris is the capital."}})
    )
    stream = await client.chat.completions.create(
        model=DEEPSEEK, messages=[{"role": "user", "content": "q"}], stream=True, stream_options={"include_usage": True}
    )
    chunks = [c async for c in stream]
    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert text == "Paris is the capital."
    assert chunks[0].choices[0].delta.role == "assistant"
    assert [c.choices[0].finish_reason for c in chunks if c.choices][-1] == "stop"
    assert chunks[-1].choices == []
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.completion_tokens == 6


def sse_lines(body: bytes) -> list[bytes]:
    return [line for line in body.split(b"\n\n") if line]


async def test_raw_sse_framing_without_usage(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "stream": {"chunks": ["a", "b"]}}))
    body = {"model": DEEPSEEK, "messages": [{"role": "user", "content": "q"}], "stream": True}
    resp = await http.post("/v1/chat/completions", json=body, headers=bearer_headers())
    assert resp.headers["content-type"].startswith("text/event-stream")
    lines = sse_lines(resp.content)
    assert all(line.startswith(b"data: ") for line in lines)
    assert lines[-1] == b"data: [DONE]"
    assert b"usage" not in resp.content  # no include_usage: no usage anywhere


@pytest.mark.parametrize(
    ("variant", "last"),
    [("bad_sse_line", b'{"id": "chatcmpl-'), ("no_done", b'data: {"id"'), ("truncated", b'"choi')],
)
async def test_malformed_sse(mock: MockBedrock, http: httpx.AsyncClient, variant: str, last: bytes) -> None:
    fault: dict[str, Any] = (
        {"kind": "truncated_json"} if variant == "truncated" else {"kind": "malformed", "variant": variant}
    )
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "stream": {"chunks": ["a", "b", "c"]}, "fault": fault}))
    body = {"model": DEEPSEEK, "messages": [{"role": "user", "content": "q"}], "stream": True}
    resp = await http.post("/v1/chat/completions", json=body, headers=bearer_headers())
    lines = sse_lines(resp.content)
    assert b"[DONE]" not in resp.content
    assert last in lines[-1]
    assert b'"content":"b"' not in resp.content  # at most one text chunk before the break


@pytest.mark.parametrize(
    ("path", "model", "status", "message"),
    [
        (
            "/v1/chat/completions",
            "anthropic.claude-haiku-5-5",
            400,
            "The model 'anthropic.claude-haiku-5-5' does not support the '/v1/chat/completions' API",
        ),
        ("/v1/chat/completions", "openai.gpt-6-luna", 400, "model `openai.gpt-6-luna` isn't supported on this route"),
        ("/v1/chat/completions", "us.deepseek.v3.2", 404, "The model 'us.deepseek.v3.2' does not exist"),
        ("/v1/decisions", DEEPSEEK, 400, "The requested model is not supported in the Decisions API."),
        (
            "/v1/responses",
            "openai.gpt-6-luna",
            400,
            "The model 'openai.gpt-6-luna' does not support the '/v1/responses' API",
        ),
    ],
)
async def test_route_quirks(
    mock: MockBedrock, http: httpx.AsyncClient, path: str, model: str, status: int, message: str
) -> None:
    mock.load_scenario(scenario({"match": {}, "answer": {"raw_text": "never"}}))
    body = {"model": model, "messages": [{"role": "user", "content": "q"}], "input": "q"}
    resp = await http.post(path, json=body, headers=bearer_headers())
    assert resp.status_code == status
    error = resp.json()["error"]
    assert error["message"] == message
    assert error["type"] == "invalid_request_error"
    assert mock.requests()[-1]["outcome"] == "rejected"


async def test_luna_answers_only_on_openai_responses(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(scenario({"match": {"model": "openai.gpt-6-luna", "op": "chat"}, "answer": {"raw_text": "C"}}))
    ok = await http.post(
        "/openai/v1/responses", json={"model": "openai.gpt-6-luna", "input": "q"}, headers=bearer_headers()
    )
    assert ok.status_code == 200
    assert ok.json()["output"][1]["content"][0]["text"] == "C"
    cases: list[tuple[dict[str, Any], str]] = [
        ({"top_logprobs": 5}, "logprobs are not supported with reasoning models."),
        (
            {"reasoning": {"effort": "minimal"}},
            "Unsupported value: 'minimal' is not supported with the 'openai.gpt-6-luna'",
        ),
    ]
    for extra, message in cases:
        resp = await http.post(
            "/openai/v1/responses", json={"model": "openai.gpt-6-luna", "input": "q", **extra}, headers=bearer_headers()
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["message"].startswith(message)


async def test_mantle_request_validation(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "answer": {"raw_text": "x"}}))
    cases: list[tuple[dict[str, Any], str]] = [
        ({"top_logprobs": 21, "logprobs": True}, "Expected a value <= 20, but got 21 instead."),
        ({"top_logprobs": 2}, "requires 'logprobs' to be true"),
        ({"stream_options": {"include_usage": True}}, "only allowed when 'stream' is enabled"),
        (
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}]},
            "Only text",
        ),
    ]
    for extra, fragment in cases:
        body = {"model": DEEPSEEK, "messages": [{"role": "user", "content": "q"}], **extra}
        resp = await http.post("/v1/chat/completions", json=body, headers=bearer_headers())
        assert resp.status_code == 400, extra
        assert fragment in resp.json()["error"]["message"]


async def test_auth_modes(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    body = {"model": DEEPSEEK, "messages": [{"role": "user", "content": "q"}]}
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "answer": {"raw_text": "x"}}))
    assert (await http.post("/v1/chat/completions", json=body, headers=bearer_headers(AKID_B))).status_code == 200
    assert mock.requests()[-1]["akid"] == AKID_B
    assert mock.requests()[-1]["auth"]["scheme"] == "bearer_short_term"
    long_term = {"authorization": "Bearer ABSKbG9uZy10ZXJtLWtleQ=="}
    assert (await http.post("/v1/chat/completions", json=body, headers=long_term)).status_code == 401
    assert (await http.post("/v1/chat/completions", json=body, headers=sigv4_headers())).status_code == 401
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "answer": {"raw_text": "x"}}, mantle_auth="sigv4"))
    assert (await http.post("/v1/chat/completions", json=body, headers=sigv4_headers())).status_code == 200
    resp = await http.post("/v1/chat/completions", json=body, headers=bearer_headers())
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


async def test_chat_stream_exception_event(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    fault = {"kind": "exception_event", "type": "internalServerException", "after": 1}
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "stream": {"chunks": ["a", "b"]}, "fault": fault}))
    body = {"model": DEEPSEEK, "messages": [{"role": "user", "content": "q"}], "stream": True}
    resp = await http.post("/v1/chat/completions", json=body, headers=bearer_headers())
    lines = sse_lines(resp.content)
    assert b'"content":"a"' in resp.content
    assert lines[-1].startswith(b'data: {"error":')
    assert b"[DONE]" not in resp.content
    assert mock.requests()[-1]["error"] == "InternalServerException"


CHAT = {"model": DEEPSEEK, "messages": [{"role": "user", "content": "q"}]}
CHAT_STREAM = {**CHAT, "stream": True}


MANTLE_PRE_BYTE: list[tuple[dict[str, Any], int, str, str | None, str | None, str | None]] = [
    ({"kind": "throttling", "retry_after_s": 3}, 429, "rate_limit_error", "rate_limit_exceeded", None, "3"),
    (
        {"kind": "access_denied"},
        403,
        "permission_error",
        "access_denied",
        "You don't have access to the model with the specified model ID.",
        None,
    ),
    (
        {"kind": "validation", "message": "bad thing"},
        400,
        "invalid_request_error",
        "validation_error",
        "bad thing",
        None,
    ),
    ({"kind": "http_5xx", "status": 500}, 500, "server_error", None, None, None),
    ({"kind": "http_5xx", "status": 503}, 503, "server_error", None, None, None),
    ({"kind": "model_timeout"}, 408, "timeout_error", "timeout", None, None),
    ({"kind": "exception_event"}, 424, "server_error", None, None, None),
    ({"kind": "exception_event", "type": "ModelNotReadyException"}, 429, "rate_limit_error", None, None, None),
]


@pytest.mark.parametrize(
    ("fault", "status", "type_", "code", "message", "retry_after", "stream"),
    [
        (*case, stream)
        for case in MANTLE_PRE_BYTE
        for stream in (False, True)
        if not (stream and case[0]["kind"] == "exception_event")  # streamed: a mid-stream error line (below)
    ],
)
async def test_pre_byte_faults_on_mantle(
    mock: MockBedrock,
    http: httpx.AsyncClient,
    fault: dict[str, Any],
    status: int,
    type_: str,
    code: str | None,
    message: str | None,
    retry_after: str | None,
    stream: bool,
) -> None:
    """The Mantle error shapes the DeepSeek decision member depends on (F2, F8, F9): status, `error.type`/`code`,
    `retry-after`, `x-request-id`; exception_event only fails a non-streaming call before the body."""
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "answer": {"raw_text": "x"}, "fault": fault}))
    resp = await http.post("/v1/chat/completions", json=CHAT_STREAM if stream else CHAT, headers=bearer_headers())
    assert resp.status_code == status
    error = resp.json()["error"]
    assert (error["type"], error["code"]) == (type_, code)
    if message is not None:
        assert error["message"] == message
    assert resp.headers.get("retry-after") == retry_after
    assert resp.headers["x-request-id"]
    rec = mock.requests()[-1]
    assert (rec["op"], rec["status"], rec["outcome"]) == ("chat-stream" if stream else "chat", status, "fault")


async def test_mantle_throttling_reaches_the_openai_client(mock: MockBedrock, client: openai.AsyncOpenAI) -> None:
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "fault": {"kind": "throttling", "retry_after_s": 2}}))
    with pytest.raises(openai.RateLimitError) as err:
        await client.chat.completions.create(model=DEEPSEEK, messages=[{"role": "user", "content": "q"}])
    assert err.value.response.headers["retry-after"] == "2"
    assert err.value.type == "rate_limit_error"


@pytest.mark.parametrize("stream", [False, True])
async def test_mantle_connect_reset(mock: MockBedrock, http: httpx.AsyncClient, stream: bool) -> None:
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "fault": {"kind": "connect_reset"}}))
    with pytest.raises((httpx.ReadError, httpx.RemoteProtocolError)):
        await http.post("/v1/chat/completions", json=CHAT_STREAM if stream else CHAT, headers=bearer_headers())
    assert (mock.requests()[-1]["outcome"], mock.requests()[-1]["status"]) == ("reset", None)


@pytest.mark.parametrize("stream", [False, True])
async def test_mantle_hang_before_first_byte_sends_no_head(mock: MockBedrock, stream: bool) -> None:
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "fault": {"kind": "hang_before_first_byte"}}))
    async with httpx.AsyncClient(base_url=mock.endpoint_url, timeout=0.2) as c:
        with pytest.raises(httpx.ReadTimeout):  # raised while waiting for the status line
            async with c.stream(
                "POST", "/v1/chat/completions", json=CHAT_STREAM if stream else CHAT, headers=bearer_headers()
            ):
                pytest.fail("a response head arrived")
    rec = mock.requests()[-1]
    assert (rec["outcome"], rec["status"], rec["t_first_byte_ns"]) == ("hang", None, None)


THREE = app_question(["coding", "factual_qa", "chitchat"])


@pytest.mark.parametrize(
    ("fault", "content", "finish", "reasoning"),
    [
        ({"kind": "reasoning_only"}, "", "length", True),
        ({"kind": "empty_text"}, "", "stop", False),
        ({"kind": "invalid_verbalized", "variant": "missing_letter"}, '{"A": 0.6, "B": 0.4}', "stop", False),
        (
            {"kind": "invalid_verbalized", "variant": "sum_out_of_range"},
            '{"A": 0.12, "B": 0.08, "C": 0.0}',
            "stop",
            False,
        ),
        ({"kind": "invalid_verbalized", "variant": "not_json"}, "I think the answer is A.", "stop", False),
    ],
)
async def test_mantle_answer_faults(
    mock: MockBedrock, http: httpx.AsyncClient, fault: dict[str, Any], content: str, finish: str, reasoning: bool
) -> None:
    answer = {"dist": {"coding": 0.6, "factual_qa": 0.4}}
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "answer": answer, "fault": fault}))
    body = {"model": DEEPSEEK, "messages": [{"role": "user", "content": THREE}]}
    resp = await http.post("/v1/chat/completions", json=body, headers=bearer_headers())
    assert resp.status_code == 200
    (choice,) = resp.json()["choices"]
    assert (choice["message"]["content"], choice["finish_reason"]) == (content, finish)
    assert bool(choice["message"].get("reasoning_content")) is reasoning
    assert mock.requests()[-1]["outcome"] == "fault"


async def test_mantle_reasoning_only_stream(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    rule = {
        "match": {"model": DEEPSEEK},
        "stream": {"text": "never", "reasoning": {"deltas": 3}},
        "fault": {"kind": "reasoning_only"},
    }
    mock.load_scenario(scenario(rule))
    resp = await http.post("/v1/chat/completions", json=CHAT_STREAM, headers=bearer_headers())
    chunks = [orjson.loads(line[len(b"data: ") :]) for line in sse_lines(resp.content)[:-1]]
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert sum(1 for d in deltas if d.get("reasoning_content")) == 3
    assert not [d for d in deltas if d.get("content")]
    assert chunks[-1]["choices"][0]["finish_reason"] == "length"
    assert sse_lines(resp.content)[-1] == b"data: [DONE]"


async def test_split_token_fragment_is_listed(mock: MockBedrock, client: openai.AsyncOpenAI) -> None:
    answer = {"dist": {"a": 0.5, "b": 0.3}, "split_tokens": {"b": 0.15}}
    mock.load_scenario(scenario({"match": {"model": DEEPSEEK}, "answer": answer}))
    resp = await client.chat.completions.create(
        model=DEEPSEEK, messages=[{"role": "user", "content": SIX}], logprobs=True, top_logprobs=20
    )
    assert resp.choices[0].logprobs is not None
    (token,) = resp.choices[0].logprobs.content or []
    top = {t.token: round(math.exp(t.logprob), 6) for t in token.top_logprobs}
    assert list(top) == ["A", "B", "b"]  # "b" is the split fragment of option B ("b" + "illing")
    assert top["b"] == 0.15


async def test_pad_reaches_every_sse_payload(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    """Regression: the usage chunk (and the error line of a broken stream) skipped the `pad` member."""
    rules: list[dict[str, Any]] = [
        {"match": {"model": DEEPSEEK, "nth": 1}, "stream": {"chunks": ["a", "b"], "pad": 5}},
        {
            "match": {"model": DEEPSEEK},
            "stream": {"chunks": ["a", "b"], "pad": 5},
            "fault": {"kind": "exception_event", "type": "internalServerException", "after": 1},
        },
    ]
    mock.load_scenario(scenario(*rules))
    body = {**CHAT_STREAM, "stream_options": {"include_usage": True}}
    for expect_usage in (True, False):
        resp = await http.post("/v1/chat/completions", json=body, headers=bearer_headers())
        payloads = [orjson.loads(line[len(b"data: ") :]) for line in sse_lines(resp.content) if line != b"data: [DONE]"]
        assert all(len(p["p"]) == 5 and p["p"].isalpha() for p in payloads)
        assert ("usage" in payloads[-1] and payloads[-1]["choices"] == []) is expect_usage
        assert ("error" in payloads[-1]) is not expect_usage
