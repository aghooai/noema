"""Converse JSON answers, the pre-byte faults, auth and the request records (spec-10 §2.1, §2.1a, §2.4)."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError
from tests.unit.mockbedrock.helpers import (
    AKID,
    DEEPSEEK,
    HAIKU,
    LUNA,
    OPUS,
    SOL,
    USER_HI,
    app_question,
    bedrock_client,
    scenario,
    sigv4_headers,
    sigv4_scoped,
    user,
)
from tools.mockbedrock import MockBedrock, VirtualClock
from types_aiobotocore_bedrock_runtime import BedrockRuntimeClient

QUESTION = app_question(["coding", "factual_qa", "chitchat"])
DECISION_MSG = user({"text": QUESTION})


async def test_verbalized_answer_maps_values_to_letters(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    mock.load_scenario(
        scenario({"match": {"model": HAIKU, "op": "converse"}, "answer": {"dist": {"coding": 0.7, "chitchat": 0.3}}})
    )
    resp = await bedrock.converse(modelId=HAIKU, messages=DECISION_MSG, inferenceConfig={"maxTokens": 100})
    assert resp["output"]["message"]["content"] == [{"text": '{"A": 0.7, "B": 0.0, "C": 0.3}'}]
    assert resp["stopReason"] == "end_turn"
    assert set(resp["usage"]) == {
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "cacheReadInputTokens",
        "cacheWriteInputTokens",
    }
    assert resp["metrics"]["latencyMs"] == 0


async def test_harmony_final_markers_for_luna(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    mock.load_scenario(scenario({"match": {"model": LUNA}, "answer": {"dist": {"coding": 1.0}, "harmony_final": True}}))
    resp = await bedrock.converse(modelId=LUNA, messages=DECISION_MSG)
    text = resp["output"]["message"]["content"][0]["text"]
    assert text == '<|channel|>final<|message|>{"A": 1.0, "B": 0.0, "C": 0.0}<|return|>'


@pytest.mark.parametrize(
    "raw",
    [
        '{"A": 0.44, "B": 0.01, "C": 0.53, "D": 0.02}',
        '```json\n{"A": 0.5, "B": 0.6}\n```',
        '{"A": 0.5, "A": 0.5, "B": 0.0}',
        "I can't help with that.",
        '<|channel|>analysis<|message|>x<|end|><|channel|>final<|message|>{"A":0.9,"B":0.1}<|return|>',
        '{"A": 0.2, "B": 0.2}',
        '{"A": 0.7}',
        '{"A": 0.5, "B": 0.5, "C": 0}',
        '{"A": 1.2, "B": 0.1}',
        '{"A": true, "B": 0}',
    ],
)
async def test_t_vb_1_texts_arrive_verbatim(mock: MockBedrock, bedrock: BedrockRuntimeClient, raw: str) -> None:
    mock.load_scenario(scenario({"match": {"model": HAIKU}, "answer": {"raw_text": raw}}))
    resp = await bedrock.converse(modelId=HAIKU, messages=DECISION_MSG)
    assert resp["output"]["message"]["content"] == [{"text": raw}]


async def test_explicit_usage_and_deepseek_usage_shape(mock: MockBedrock, bedrock: BedrockRuntimeClient) -> None:
    usage = {"input_tokens": 10, "output_tokens": 4, "cached_input_tokens": 3, "cache_write_input_tokens": 2}
    mock.load_scenario(
        scenario(
            {"match": {"model": SOL}, "answer": {"raw_text": "ok"}, "stream": {"usage": usage}},
            {"match": {"model": DEEPSEEK}, "answer": {"raw_text": "ok"}},
        )
    )
    resp = await bedrock.converse(modelId=SOL, messages=USER_HI)
    assert resp["usage"] == {
        "inputTokens": 10,
        "outputTokens": 4,
        "totalTokens": 19,
        "cacheReadInputTokens": 3,
        "cacheWriteInputTokens": 2,
    }
    resp = await bedrock.converse(modelId=DEEPSEEK, messages=USER_HI)
    assert set(resp["usage"]) == {"inputTokens", "outputTokens", "totalTokens"}  # probe: no cache fields


@pytest.mark.parametrize(
    ("fault", "stop", "content"),
    [
        ({"kind": "refusal"}, "content_filtered", []),
        ({"kind": "empty_text"}, "end_turn", []),
    ],
)
async def test_refusal_and_empty_text(
    mock: MockBedrock, bedrock: BedrockRuntimeClient, fault: dict[str, Any], stop: str, content: list[Any]
) -> None:
    mock.load_scenario(scenario({"match": {"model": HAIKU}, "answer": {"raw_text": "x"}, "fault": fault}))
    resp = await bedrock.converse(modelId=HAIKU, messages=USER_HI)
    assert resp["stopReason"] == stop
    assert resp["output"]["message"]["content"] == content
    assert mock.requests()[-1]["outcome"] == "fault"


async def test_reasoning_only_returns_a_signed_reasoning_block(
    mock: MockBedrock, bedrock: BedrockRuntimeClient
) -> None:
    """Claude hides its reasoning on Converse JSON: an empty text with a signature (live Haiku 5.5, Wave E)."""
    mock.load_scenario(
        scenario({"match": {"model": OPUS}, "answer": {"raw_text": "x"}, "fault": {"kind": "reasoning_only"}})
    )
    resp = await bedrock.converse(modelId=OPUS, messages=USER_HI)
    (block,) = resp["output"]["message"]["content"]
    assert resp["stopReason"] == "max_tokens"
    assert block["reasoningContent"]["reasoningText"]["text"] == ""
    assert block["reasoningContent"]["reasoningText"]["signature"]


@pytest.mark.parametrize(
    ("fault", "status", "code", "message"),
    [
        ({"kind": "throttling"}, 429, "ThrottlingException", "Too many requests, please wait before trying again."),
        (
            {"kind": "access_denied"},
            403,
            "AccessDeniedException",
            "You don't have access to the model with the specified model ID.",
        ),
        ({"kind": "validation", "message": "bad thing"}, 400, "ValidationException", "bad thing"),
        ({"kind": "http_5xx", "status": 500}, 500, "InternalServerException", None),
        (
            {"kind": "http_5xx", "status": 503},
            503,
            "ServiceUnavailableException",
            "Bedrock is unable to process your request.",
        ),
        ({"kind": "model_timeout"}, 408, "ModelTimeoutException", None),
        ({"kind": "exception_event"}, 424, "ModelStreamErrorException", None),
    ],
)
async def test_pre_byte_faults_on_converse(
    mock: MockBedrock, bedrock: BedrockRuntimeClient, fault: dict[str, Any], status: int, code: str, message: str | None
) -> None:
    mock.load_scenario(scenario({"match": {"model": HAIKU}, "answer": {"raw_text": "x"}, "fault": fault}))
    with pytest.raises(ClientError) as err:
        await bedrock.converse(modelId=HAIKU, messages=USER_HI)
    assert err.value.response["ResponseMetadata"]["HTTPStatusCode"] == status
    assert err.value.response["Error"]["Code"] == code
    if message is not None:
        assert err.value.response["Error"]["Message"] == message
    assert err.value.response["ResponseMetadata"]["RequestId"]


async def test_throttling_retry_after_header(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(scenario({"match": {"model": SOL}, "fault": {"kind": "throttling", "retry_after_s": 2}}))
    resp = await http.post(f"/model/{SOL}/converse-stream", json={"messages": USER_HI}, headers=sigv4_headers())
    assert resp.status_code == 429
    assert resp.headers["x-amzn-errortype"] == "ThrottlingException"
    assert resp.headers["retry-after"] == "2"
    assert resp.json() == {"message": "Too many requests, please wait before trying again."}


async def test_connect_reset_is_a_tcp_reset(mock: MockBedrock, http: httpx.AsyncClient) -> None:
    mock.load_scenario(scenario({"match": {"model": SOL}, "fault": {"kind": "connect_reset"}}))
    with pytest.raises((httpx.ReadError, httpx.RemoteProtocolError)):
        await http.post(f"/model/{SOL}/converse", json={"messages": USER_HI}, headers=sigv4_headers())
    assert mock.requests()[-1]["outcome"] == "reset"


@pytest.mark.parametrize("fault", ["truncated_json", "malformed"])
async def test_broken_json_bodies(mock: MockBedrock, http: httpx.AsyncClient, fault: str) -> None:
    mock.load_scenario(scenario({"match": {"model": SOL}, "answer": {"raw_text": "hello"}, "fault": {"kind": fault}}))
    resp = await http.post(f"/model/{SOL}/converse", json={"messages": USER_HI}, headers=sigv4_headers())
    assert resp.status_code == 200
    with pytest.raises(ValueError, match=r".*"):
        resp.json()


async def test_hang_holds_until_the_client_gives_up(mock: MockBedrock) -> None:
    mock.load_scenario(scenario({"match": {"model": SOL}, "fault": {"kind": "hang_before_first_byte"}}))
    async with httpx.AsyncClient(base_url=mock.endpoint_url, timeout=0.2) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.post(f"/model/{SOL}/converse", json={"messages": USER_HI}, headers=sigv4_headers())
    assert mock.requests()[-1]["outcome"] == "hang"
    assert mock.requests()[-1]["t_first_byte_ns"] is None


@pytest.mark.parametrize("session_token", [None])
async def test_sigv4_without_session_token_is_unrecognized(mock: MockBedrock, session_token: str | None) -> None:
    mock.load_scenario(scenario({"match": {"model": SOL}, "answer": {"raw_text": "x"}}))
    async with bedrock_client(mock.endpoint_url, session_token=session_token) as client:
        with pytest.raises(ClientError) as err:
            await client.converse(modelId=SOL, messages=USER_HI)
    assert err.value.response["Error"]["Code"] == "UnrecognizedClientException"
    assert err.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
    assert mock.requests()[-1]["outcome"] == "rejected"


async def test_record_has_akid_redacted_headers_and_blob_digests(
    mock: MockBedrock, bedrock: BedrockRuntimeClient
) -> None:
    mock.load_scenario(scenario({"match": {"model": HAIKU}, "answer": {"raw_text": "ok"}}))
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    msg = user({"text": "[image-1] «nonce:n-42»"}, {"image": {"format": "png", "source": {"bytes": png}}})
    await bedrock.converse(modelId=HAIKU, messages=msg)
    (rec,) = mock.requests()
    assert rec["akid"] == AKID
    assert rec["auth"] == {
        "scheme": "sigv4",
        "akid": AKID,
        "region": "us-east-1",
        "service": "bedrock",
        "session_token": True,
    }
    assert "authorization" not in rec["headers"]
    assert "x-amz-security-token" not in rec["headers"]
    blob = rec["body"]["messages"][0]["content"][1]["image"]["source"]["bytes"]
    assert blob == {"$bytes_len": len(png), "sha256": hashlib.sha256(png).hexdigest()}
    assert base64.b64encode(png).decode() not in str(rec)
    assert rec["nonce"] == "n-42"
    assert (rec["op"], rec["model"], rec["status"], rec["outcome"], rec["rule"]) == ("converse", HAIKU, 200, "ok", 0)


@pytest.mark.parametrize(
    ("variant", "text"),
    [
        ("missing_letter", '{"A": 0.6, "B": 0.4}'),
        ("sum_out_of_range", '{"A": 0.12, "B": 0.08, "C": 0.0}'),
        ("not_json", "I think the answer is A."),
    ],
)
async def test_invalid_verbalized_variants(
    mock: MockBedrock, bedrock: BedrockRuntimeClient, variant: str, text: str
) -> None:
    answer = {"dist": {"coding": 0.6, "factual_qa": 0.4}}
    fault = {"kind": "invalid_verbalized", "variant": variant}
    mock.load_scenario(scenario({"match": {"model": HAIKU}, "answer": answer, "fault": fault}))
    resp = await bedrock.converse(modelId=HAIKU, messages=DECISION_MSG)
    assert resp["output"]["message"]["content"] == [{"text": text}]


@pytest.mark.parametrize(
    ("region", "service", "message"),
    [
        ("eu-west-9", "s3", "Credential should be scoped to correct service: 'bedrock'."),
        ("us-east-1", "bedrock-runtime", "Credential should be scoped to correct service: 'bedrock'."),
        ("us-west-2", "bedrock", "Credential should be scoped to a valid region, not 'us-west-2'."),
    ],
)
async def test_sigv4_credential_scope_must_be_bedrock_in_the_region(
    mock: MockBedrock, http: httpx.AsyncClient, region: str, service: str, message: str
) -> None:
    """Regression: any region/service scope was accepted, so a mis-signing adapter passed against the mock."""
    mock.load_scenario(scenario({"match": {"model": SOL}, "answer": {"raw_text": "x"}}))
    headers = sigv4_scoped(region, service)
    resp = await http.post(f"/model/{SOL}/converse", json={"messages": USER_HI}, headers=headers)
    assert resp.status_code == 403
    assert resp.headers["x-amzn-errortype"] == "InvalidSignatureException"
    assert resp.json() == {"message": message}
    rec = mock.requests()[-1]
    assert (rec["outcome"], rec["auth"]["region"], rec["auth"]["service"]) == ("rejected", region, service)


@pytest.mark.parametrize("stream", [False, True])
async def test_a_raw_colon_in_the_model_label_fails_the_signature(
    mock: MockBedrock, http: httpx.AsyncClient, stream: bool
) -> None:
    """Wave E fidelity: botocore always percent-encodes ':' in the model id it signs, so a request whose path
    carries a raw ':' was re-quoted after signing; Bedrock answers it 403 InvalidSignatureException."""
    versioned = f"{SOL}-v1:0"
    mock.load_scenario(scenario({"match": {"model": versioned}, "answer": {"raw_text": "x"}}))
    op = "converse-stream" if stream else "converse"
    raw = await http.post(f"/model/{versioned}/{op}", json={"messages": USER_HI}, headers=sigv4_headers())
    assert raw.status_code == 403
    assert raw.headers["x-amzn-errortype"] == "InvalidSignatureException"
    assert mock.requests()[-1]["outcome"] == "rejected"
    encoded = await http.post(f"/model/{SOL}-v1%3A0/{op}", json={"messages": USER_HI}, headers=sigv4_headers())
    assert encoded.status_code == 200  # the form botocore signs and sends
    assert (mock.requests()[-1]["model"], mock.requests()[-1]["outcome"]) == (versioned, "ok")


async def test_the_accepted_region_is_configurable(clock: VirtualClock) -> None:
    async with MockBedrock(clock=clock, region="us-west-2") as mock, httpx.AsyncClient(base_url=mock.endpoint_url) as c:
        mock.load_scenario(scenario({"match": {"model": SOL}, "answer": {"raw_text": "x"}}))
        ok = await c.post(
            f"/model/{SOL}/converse", json={"messages": USER_HI}, headers=sigv4_scoped("us-west-2", "bedrock")
        )
        assert ok.status_code == 200
        assert (
            await c.post(f"/model/{SOL}/converse", json={"messages": USER_HI}, headers=sigv4_headers())
        ).status_code == 403


@pytest.mark.parametrize(
    ("name", "status"),
    [
        ("ModelNotReadyException", 429),
        ("ResourceNotFoundException", 404),
        ("AccessDeniedException", 403),
        ("ModelTimeoutException", 408),
        ("ServiceQuotaExceededException", 400),
        ("ModelErrorException", 424),
    ],
)
async def test_exception_event_names_without_a_fault_kind(
    mock: MockBedrock, bedrock: BedrockRuntimeClient, name: str, status: int
) -> None:
    """Regression: names outside the five stream exceptions came out as HTTP 500 (spec-10 §2.1a statuses)."""
    fault = {"kind": "exception_event", "type": name, "message": f"{name} text"}
    mock.load_scenario(scenario({"match": {"model": HAIKU}, "answer": {"raw_text": "x"}, "fault": fault}))
    with pytest.raises(ClientError) as err:
        await bedrock.converse(modelId=HAIKU, messages=USER_HI)
    assert err.value.response["ResponseMetadata"]["HTTPStatusCode"] == status
    assert err.value.response["Error"] == {"Code": name, "Message": f"{name} text"}


async def test_converse_stream_hang_before_first_byte_sends_no_head(mock: MockBedrock) -> None:
    mock.load_scenario(scenario({"match": {"model": SOL}, "fault": {"kind": "hang_before_first_byte"}}))
    async with httpx.AsyncClient(base_url=mock.endpoint_url, timeout=0.2) as client:
        with pytest.raises(httpx.ReadTimeout):  # raised while waiting for the status line
            async with client.stream(
                "POST", f"/model/{SOL}/converse-stream", json={"messages": USER_HI}, headers=sigv4_headers()
            ):
                pytest.fail("a response head arrived")
    rec = mock.requests()[-1]
    assert (rec["outcome"], rec["status"], rec["t_first_byte_ns"], rec["fault"]) == (
        "hang",
        None,
        None,
        "hang_before_first_byte",
    )
