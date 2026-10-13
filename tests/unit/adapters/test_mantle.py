"""Mantle chat (spec-02 §4.2): SSE normalization until `[DONE]` with `stream_options.include_usage`, reasoning
counted, refusals, malformed SSE (truncated JSON, a non-SSE line, a missing `[DONE]`, chunks of the wrong shape),
error chunks, statuses with `Retry-After`, bearer vs SigV4 auth, and cancel."""

from __future__ import annotations

import orjson
import pytest
from tests.unit.adapters.support import DEEPSEEK, TIMEOUTS, Scripted, collect, request, rig

from noema.core.errors import ErrorKind
from noema.core.protocols.adapters import (
    AdapterError,
    ErrorEvent,
    MessageStart,
    ReasoningDelta,
    Stop,
    TextDelta,
    UsageEvent,
)


def chunk(
    delta: dict[str, object] | None = None, finish: str | None = None, usage: dict[str, int] | None = None
) -> bytes:
    body: dict[str, object] = {
        "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]
    }
    if usage is not None:
        body["usage"] = usage
    return b"data: " + orjson.dumps(body) + b"\n\n"


DONE = b"data: [DONE]\n\n"
USAGE = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}


async def run(script: Scripted) -> list[object]:
    async with rig(mantle_transport=script.transport()) as r:
        return [e for _, e in await collect(r, "deepseek-3.2")]


async def test_stream_order_reasoning_usage_and_body() -> None:
    script = Scripted(
        [
            chunk({"role": "assistant"}),
            b": keep-alive comment\n\n",
            chunk({"reasoning_content": "think"}),
            chunk({"content": "Hel"}),
            chunk({"content": "lo"}, finish="stop"),
            chunk(None, usage={**USAGE, "prompt_tokens_details": {"cached_tokens": 4}}),  # type: ignore[dict-item]
            DONE,
        ]
    )
    events = await run(script)
    assert [type(e) for e in events] == [MessageStart, ReasoningDelta, TextDelta, TextDelta, Stop, UsageEvent]
    assert isinstance(events[1], ReasoningDelta) and events[1].chars == 5
    usage = events[-1]
    assert isinstance(usage, UsageEvent)
    assert (usage.usage.input_tokens, usage.usage.cached_input_tokens, usage.usage.output_tokens) == (12, 4, 5)
    assert usage.usage.reasoning_tokens == 2  # ceil(5 chars / 4)
    body = orjson.loads(script.requests[0].content)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert script.requests[0].url == "https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions"


async def test_usage_before_the_stop_is_held_until_after_it() -> None:
    events = await run(Scripted([chunk({"content": "x"}, usage=USAGE), chunk({}, finish="length"), DONE]))
    assert [type(e) for e in events] == [MessageStart, TextDelta, Stop, UsageEvent]
    assert isinstance(events[2], Stop) and events[2].reason == "max_tokens"


async def test_refusal_delta_stops_with_content_filter_and_is_never_forwarded() -> None:
    events = await run(Scripted([chunk({"refusal": "I can't"}), chunk({}, finish="stop"), DONE]))
    assert not any(isinstance(e, TextDelta) for e in events)
    assert [e.reason for e in events if isinstance(e, Stop)] == ["content_filter"]


@pytest.mark.parametrize(
    ("lines", "code"),
    [
        ([chunk({"content": "a"}), b'data: {"choices": [{"delta": {"content": "tr\n\n'], "malformed:json"),
        ([chunk({"content": "a"}), b"garbage line\n\n", DONE], "malformed:sse_line"),
        ([chunk({"content": "a"})], "malformed:missing_done"),
        ([b"data: " + orjson.dumps({"error": {"message": "m", "type": "server_error"}}) + b"\n\n"], "server_error"),
        # adapters review: wrong shapes are retryable malformed:chunk (F8: never fatal), never a non-str TextDelta
        ([b"data: " + orjson.dumps({"choices": ["not an object"]}) + b"\n\n", DONE], "malformed:chunk"),
        ([b"data: " + orjson.dumps({"choices": [{"delta": "a string"}]}) + b"\n\n", DONE], "malformed:chunk"),
        ([b"data: " + orjson.dumps({"choices": {"0": {}}}) + b"\n\n", DONE], "malformed:chunk"),
        ([chunk({"content": 5}), DONE], "malformed:chunk"),
        ([chunk({"content": [{"type": "text", "text": "hi"}]}), DONE], "malformed:chunk"),
        ([chunk({"content": "a"}), chunk({"reasoning_content": {"text": "r"}}), DONE], "malformed:chunk"),
    ],
    ids=[
        "truncated_json",
        "non_sse_line",
        "missing_done",
        "error_chunk",
        "choice_not_object",
        "delta_string",
        "choices_object",
        "content_number",
        "content_list",
        "reasoning_object",
    ],
)
async def test_malformed_and_error_chunks_end_with_one_retryable_error(lines: list[bytes], code: str) -> None:
    events = await run(Scripted(lines))
    last = events[-1]
    assert isinstance(last, ErrorEvent) and last.error.kind is ErrorKind.RETRYABLE and last.error.provider_code == code
    assert sum(isinstance(e, ErrorEvent) for e in events) == 1


@pytest.mark.parametrize("content", [5, [{"type": "text", "text": "hi"}]])
async def test_non_string_content_in_a_completion_is_malformed(content: object) -> None:
    body = orjson.dumps({"choices": [{"index": 0, "message": {"content": content}, "finish_reason": "stop"}]})
    script = Scripted(body=body)
    async with rig(mantle_transport=script.transport()) as r:
        with pytest.raises(AdapterError) as err:
            async with r.clock.autojump():
                await r.mantle.complete(request("deepseek-3.2"), tenant=r.tenant, deadline=r.deadline())
    assert err.value.error.kind is ErrorKind.RETRYABLE and err.value.error.provider_code == "malformed:response"


async def test_failure_after_the_stop_ends_silently() -> None:
    events = await run(Scripted([chunk({"content": "a"}, finish="stop")]))  # no usage, no [DONE]
    assert [type(e) for e in events] == [MessageStart, TextDelta, Stop]


async def test_done_without_finish_reason_stops_as_other() -> None:
    events = await run(Scripted([chunk({"content": "a"}), DONE]))
    assert [e.reason for e in events if isinstance(e, Stop)] == ["other"]


@pytest.mark.parametrize(
    ("status", "headers", "kind", "retry_after"),
    [
        (429, {"retry-after": "7"}, ErrorKind.THROTTLED, 7.0),
        (400, {}, ErrorKind.INVALID_REQUEST, None),
        (403, {}, ErrorKind.TENANT_ACCESS, None),
        (503, {}, ErrorKind.RETRYABLE, None),
    ],
)
async def test_error_statuses_raise_on_enter(
    status: int, headers: dict[str, str], kind: ErrorKind, retry_after: float | None
) -> None:
    script = Scripted(status=status, headers=headers, body=b'{"error": {"message": "no"}}')
    async with rig(mantle_transport=script.transport()) as r:
        with pytest.raises(AdapterError) as err:
            await collect(r, "deepseek-3.2")
    assert err.value.error.kind is kind and err.value.error.retry_after_s == retry_after


async def test_401_invalidates_the_broker_session() -> None:
    script = Scripted(status=401, body=b'{"error": {"message": "expired"}}')
    async with rig(mantle_transport=script.transport()) as r:
        with pytest.raises(AdapterError) as err:
            async with r.clock.autojump():
                await r.mantle.complete(request("deepseek-3.2"), tenant=r.tenant, deadline=r.deadline())
        assert r.broker.invalidations["t_acme"] == 1
        assert r.metric("aghoo_tenant_access_failures_total", tenant="t_acme", cause="access_denied") == 1
    assert err.value.error.cause == "access_denied"


async def test_bearer_auth_uses_the_broker_token() -> None:
    script = Scripted([chunk({"content": "a"}, finish="stop"), DONE])
    async with rig(mantle_transport=script.transport()) as r:
        await collect(r, "deepseek-3.2")
        token = (await r.broker.get("t_acme")).mantle.bearer_token
    assert token is not None
    assert script.requests[0].headers["authorization"] == f"Bearer {token.get_secret_value()}"


async def test_sigv4_auth_signs_with_the_tenant_credentials() -> None:
    script = Scripted([chunk({"content": "a"}, finish="stop"), DONE])
    async with rig(mantle_mode="sigv4", mantle_transport=script.transport()) as r:
        await collect(r, "deepseek-3.2")
        creds = await r.broker.get("t_acme")
    auth = script.requests[0].headers["authorization"]
    assert auth.startswith(f"AWS4-HMAC-SHA256 Credential={creds.aws.access_key_id}/")
    assert "/us-east-1/bedrock/aws4_request" in auth
    assert script.requests[0].headers["x-amz-security-token"] == creds.aws.session_token.get_secret_value()  # type: ignore[union-attr]


async def test_cancel_closes_the_response_without_reading_further() -> None:
    script = Scripted([chunk({"content": "a"})], hang=True)
    async with rig(mantle_transport=script.transport()) as r, r.clock.autojump():
        cm = r.mantle.stream(request("deepseek-3.2"), tenant=r.tenant, timeouts=TIMEOUTS, deadline=r.deadline())
        async with cm as events:
            async for ev in events:
                if isinstance(ev, TextDelta):
                    break
        assert r.mantle.open_streams == 0
    assert script.stream.closed


async def test_emulated_stream_matches_the_planned_events() -> None:
    async with rig() as r:
        r.scenario({"match": {"model": DEEPSEEK, "op": "chat-stream"}, "stream": {"chunks": ["one ", "two"]}})
        events = [e for _, e in await collect(r, "deepseek-3.2", request("deepseek-3.2", temperature=0.3))]
        body = r.emulator.requests[-1].body
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["one ", "two"]
    assert isinstance(events[-1], UsageEvent) and body["temperature"] == 0.3
