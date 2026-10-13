"""spec-10 §2.1a self-test: every ConverseStream frame decodes with botocore's EventStreamBuffer, a flipped bit in
either CRC raises in botocore, and the real aiobotocore parser reads the mock's streams (events, exceptions)."""

from __future__ import annotations

import struct
import zlib
from collections.abc import AsyncIterable
from typing import Any, cast

import httpx
import orjson
import pytest
from botocore.eventstream import ChecksumMismatch, EventStreamBuffer, EventStreamMessage
from botocore.exceptions import EventStreamError
from tests.unit.mockbedrock.helpers import GLM, OPUS, SOL, USER_HI, scenario, sigv4_headers
from tools.mockbedrock import MockBedrock
from tools.mockbedrock.eventstream import encode_headers, event_frame, exception_frame, exception_type_of
from types_aiobotocore_bedrock_runtime import BedrockRuntimeClient


async def drain(stream: AsyncIterable[Any], into: list[dict[str, Any]]) -> None:
    """Collect the parsed events into `into` (so a test sees what arrived before an exception)."""
    async for ev in stream:
        into.append(dict(ev))  # noqa: PERF401 (keeps the events read before the error)


def decode(data: bytes) -> list[tuple[dict[str, Any], bytes]]:
    """(headers, payload) of every message (the botocore stubs mistype `EventStreamMessage.headers`)."""
    buf = EventStreamBuffer()
    buf.add_data(data)
    messages: list[EventStreamMessage] = list(buf)
    return [(cast("dict[str, Any]", m.headers), m.payload) for m in messages]


def test_frame_layout_matches_the_spec() -> None:
    frame = event_frame("messageStart", {"role": "assistant"})
    total, headers_len, prelude_crc = struct.unpack(">III", frame[:12])
    assert total == len(frame)
    assert prelude_crc == zlib.crc32(frame[:8])
    assert struct.unpack(">I", frame[-4:])[0] == zlib.crc32(frame[:-4])
    payload = frame[12 + headers_len : -4]
    assert payload == b'{"role":"assistant"}'
    ((headers, _),) = decode(frame)
    assert headers == {":event-type": "messageStart", ":content-type": "application/json", ":message-type": "event"}


@pytest.mark.parametrize("corrupt", ["prelude_crc", "message_crc"])
def test_a_flipped_crc_bit_raises_in_botocore(corrupt: Any) -> None:
    with pytest.raises(ChecksumMismatch):
        decode(event_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "x"}}, corrupt=corrupt))


def test_exception_frame_headers_and_payload() -> None:
    data = exception_frame("throttlingException", {"message": "slow down"})
    ((headers, payload),) = decode(data)
    assert headers[":message-type"] == "exception"
    assert headers[":exception-type"] == "throttlingException"
    assert payload == b'{"message":"slow down"}'
    buf = EventStreamBuffer()
    buf.add_data(data)
    assert next(iter(buf)).to_response_dict()["status_code"] == 400
    assert exception_type_of("ModelStreamErrorException") == "modelStreamErrorException"


def test_overlong_header_is_refused() -> None:
    with pytest.raises(ValueError, match="too long"):
        encode_headers((("x" * 256, "v"),))


async def test_every_wire_frame_decodes_in_spec_order(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(
        scenario(
            {
                "match": {"model": OPUS, "op": "converse-stream"},
                "stream": {"reasoning": {"deltas": 2}, "first_text_ms": 100, "text": "abcdefghijklmnopqrst"},
            }
        )
    )
    resp = await http.post(f"/model/{OPUS}/converse-stream", json={"messages": USER_HI}, headers=sigv4_headers())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.amazon.eventstream"
    msgs = decode(resp.content)
    kinds = [h[":event-type"] for h, _ in msgs]
    assert kinds == [
        "messageStart",
        "contentBlockDelta",
        "contentBlockDelta",
        "contentBlockDelta",  # Claude reasoning signature
        "contentBlockStop",
        "contentBlockDelta",
        "contentBlockDelta",
        "contentBlockStop",
        "messageStop",
        "metadata",
    ]
    assert b'"signature"' in msgs[3][1]
    assert msgs[4][1] == b'{"contentBlockIndex":0}'
    assert b'"contentBlockIndex":1' in msgs[5][1]


async def test_aiobotocore_parses_a_reasoning_stream(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    mock.load_scenario(
        scenario(
            {
                "match": {"model": GLM, "op": "converse-stream"},
                "stream": {"reasoning": {"deltas": 3}, "text": "The capital is Paris.", "usage": "auto"},
            }
        )
    )
    resp = await bedrock.converse_stream(modelId=GLM, messages=USER_HI, outputConfig={"effort": "medium"})
    events = [ev async for ev in resp["stream"]]
    reasoning = [e for e in events if "reasoningContent" in e.get("contentBlockDelta", {}).get("delta", {})]
    text = "".join(e["contentBlockDelta"]["delta"].get("text", "") for e in events if "contentBlockDelta" in e)
    assert len(reasoning) == 3  # GLM: no signature delta
    assert text == "The capital is Paris."
    assert events[0] == {"messageStart": {"role": "assistant"}}
    assert events[-2] == {"messageStop": {"stopReason": "end_turn"}}
    usage = events[-1]["metadata"]["usage"]
    assert usage["outputTokens"] == usage["totalTokens"] - usage["inputTokens"] == 6 + 12  # text + 3 x 4 reasoning


async def test_pad_members_are_ignored_by_botocore(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    mock.load_scenario(scenario({"match": {"model": SOL}, "stream": {"text": "padded", "pad": 5}}))
    resp = await bedrock.converse_stream(modelId=SOL, messages=USER_HI)
    events = [ev async for ev in resp["stream"]]
    assert events[1] == {"contentBlockDelta": {"delta": {"text": "padded"}, "contentBlockIndex": 0}}


async def test_pad_reaches_exception_frames(
    mock: MockBedrock, bedrock: BedrockRuntimeClient, http: httpx.AsyncClient
) -> None:
    fault = {"kind": "exception_event", "type": "throttlingException", "after": 1, "message": "slow down"}
    mock.load_scenario(scenario({"match": {"model": SOL}, "stream": {"chunks": ["a", "b"], "pad": 4}, "fault": fault}))
    raw = await http.post(f"/model/{SOL}/converse-stream", json={"messages": USER_HI}, headers=sigv4_headers())
    frames = decode(raw.content)
    assert frames[-1][0][":message-type"] == "exception"
    assert all(len(orjson.loads(payload)["p"]) == 4 for _, payload in frames)
    resp = await bedrock.converse_stream(modelId=SOL, messages=USER_HI)
    with pytest.raises(EventStreamError) as err:
        await drain(resp["stream"], [])
    assert err.value.response["Error"] == {"Code": "throttlingException", "Message": "slow down"}


@pytest.mark.parametrize(
    ("kind", "code"),
    [("ModelStreamErrorException", "modelStreamErrorException"), ("throttlingException", "throttlingException")],
)
async def test_exception_event_raises_event_stream_error(
    mock: MockBedrock, bedrock: BedrockRuntimeClient, kind: str, code: str
) -> None:
    mock.load_scenario(
        scenario(
            {
                "match": {"model": SOL},
                "stream": {"text": "x" * 64},
                "fault": {"kind": "exception_event", "type": kind, "after": 2},
            }
        )
    )
    resp = await bedrock.converse_stream(modelId=SOL, messages=USER_HI)
    seen: list[dict[str, Any]] = []
    with pytest.raises(EventStreamError) as err:
        await drain(resp["stream"], seen)
    assert err.value.response["Error"]["Code"] == code
    assert [e for e in seen if "contentBlockDelta" in e] == seen[1:]  # start + exactly 2 text deltas
    assert len(seen) == 3


@pytest.mark.parametrize("variant", ["bad_crc", "bad_sse_line"])
async def test_malformed_frame_raises_checksum_mismatch(
    mock: MockBedrock, bedrock: BedrockRuntimeClient, variant: str
) -> None:
    mock.load_scenario(
        scenario(
            {"match": {"model": SOL}, "stream": {"text": "abc"}, "fault": {"kind": "malformed", "variant": variant}}
        )
    )
    resp = await bedrock.converse_stream(modelId=SOL, messages=USER_HI)
    with pytest.raises(ChecksumMismatch):
        await drain(resp["stream"], [])


async def test_truncated_json_frame_has_valid_crcs(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(
        scenario({"match": {"model": SOL}, "stream": {"text": "abc"}, "fault": {"kind": "truncated_json"}})
    )
    resp = await http.post(f"/model/{SOL}/converse-stream", json={"messages": USER_HI}, headers=sigv4_headers())
    msgs = decode(resp.content)
    assert msgs[-1][1] == b'{"contentBlockIndex": 0, "delta": {"te'


@pytest.mark.parametrize(("fault", "stop"), [("reasoning_only", "max_tokens"), ("refusal", "content_filtered")])
async def test_reasoning_only_and_refusal_streams(
    mock: MockBedrock, bedrock: BedrockRuntimeClient, fault: str, stop: str
) -> None:
    mock.load_scenario(
        scenario(
            {
                "match": {"model": OPUS},
                "stream": {"text": "never", "reasoning": {"deltas": 2}},
                "fault": {"kind": fault},
            }
        )
    )
    resp = await bedrock.converse_stream(modelId=OPUS, messages=USER_HI)
    events = [e async for e in resp["stream"]]
    assert not [e for e in events if "text" in e.get("contentBlockDelta", {}).get("delta", {})]
    assert {"contentBlockStop": {"contentBlockIndex": 0}} in events
    assert events[-2] == {"messageStop": {"stopReason": stop}}


async def test_openai_family_reasoning_is_one_redacted_delta(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    """Wave E fidelity (live cassette 34-sol-6.1-stream): Sol hides its reasoning on ConverseStream; block 0 is one
    `reasoningContent.redactedContent` delta (no reasoning text, no signature), the text follows in block 1."""
    mock.load_scenario(scenario({"match": {"model": SOL}, "stream": {"reasoning": {"deltas": 3}, "text": "Paris."}}))
    resp = await bedrock.converse_stream(modelId=SOL, messages=USER_HI, outputConfig={"effort": "high"})
    events = [ev async for ev in resp["stream"]]
    reasoning = [e["contentBlockDelta"] for e in events if "reasoningContent" in e.get("contentBlockDelta", {}).get(
        "delta", {})]  # fmt: skip
    assert len(reasoning) == 1 and set(reasoning[0]["delta"]["reasoningContent"]) == {"redactedContent"}
    assert reasoning[0]["contentBlockIndex"] == 0 and {"contentBlockStop": {"contentBlockIndex": 0}} in events
    text = [e["contentBlockDelta"] for e in events if "text" in e.get("contentBlockDelta", {}).get("delta", {})]
    assert "".join(t["delta"]["text"] for t in text) == "Paris." and {t["contentBlockIndex"] for t in text} == {1}


@pytest.mark.parametrize("stream", [False, True])
async def test_output_tokens_never_exceed_max_tokens(
    mock: MockBedrock, bedrock: BedrockRuntimeClient, stream: bool
) -> None:
    """Wave E fidelity (mock cassette 02-opus-5.5-stream: outputTokens 401 for maxTokens 256, which Bedrock cannot
    return): the planned reasoning plus text is capped at the request's maxTokens."""
    rule = {"match": {"model": OPUS}, "stream": {"reasoning": {"deltas": 100}, "text": "x" * 40, "usage": "auto"}}
    mock.load_scenario(scenario({**rule, "answer": {"raw_text": "x" * 40}}))
    params: dict[str, Any] = {"modelId": OPUS, "messages": USER_HI, "inferenceConfig": {"maxTokens": 256}}
    if stream:
        resp = await bedrock.converse_stream(**params)
        usage = [ev async for ev in resp["stream"]][-1]["metadata"]["usage"]
    else:
        usage = (await bedrock.converse(**params))["usage"]
    assert usage["outputTokens"] <= 256 and usage["totalTokens"] == usage["inputTokens"] + usage["outputTokens"]
