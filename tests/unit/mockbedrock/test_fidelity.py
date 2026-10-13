"""spec-10 §2.2 fidelity rules: per-model parameter validation with the probes' exact ValidationException texts,
media support, the Bedrock limits (spec-07 §4.5 payload rule) and validation before any rule applies."""

from __future__ import annotations

import base64
import struct
from typing import Any

import pytest
from botocore.exceptions import ClientError
from tests.unit.mockbedrock.helpers import DEEPSEEK, GLM, HAIKU, LUNA, OPUS, SOL, SONNET, USER_HI, scenario
from tools.mockbedrock import MockBedrock
from tools.mockbedrock.models import converse_model
from tools.mockbedrock.validate import Invalid, b64_size, check_converse
from types_aiobotocore_bedrock_runtime import BedrockRuntimeClient

ALL = [HAIKU, OPUS, SONNET, LUNA, SOL, GLM, DEEPSEEK]
DEPRECATED = "The model returned the following errors: `temperature` is deprecated for this model."
UNSUPPORTED = "This model doesn't support the temperature field. Remove temperature and try again."
OPUS_NONE = (
    "The model returned the following errors: unknown variant `none`, expected one of `low`, `medium`, `high`, "
    "`xhigh`, `max`, `Unhandled` at line 1 column 163"
)
SOL_NONE_PREFIX = (
    'The model returned the following errors: {"error":{"code":"unsupported_value","message":"Unsupported value: '
    "'none' is not supported with the 'us.openai.gpt-6.1-sol' model. Supported values are: 'low',"
)
# Live text (tests/cassettes/live-20261011T073404/01-glm-5.3-complete.json, Wave E live smoke)
GLM_NONE = (
    'The model returned the following errors: {"error":{"code":"unsupported_value","message":"Unsupported value: '
    "'none' is not supported with this model. Supported values are: 'low', 'medium', 'high', 'xhigh', and 'max'.\","
    '"param":"reasoning_effort","type":"invalid_request_error"}}'
)
LUNA_MINIMAL = (
    "Unsupported value: 'minimal' is not supported with the 'us.openai.gpt-6-luna' model. Supported values are: "
    "'none', 'low', 'medium', 'high', 'xhigh', and 'max'."
)
KEYS_CLAUDE = (
    "text, image, toolUse, toolResult, document, video, cachePoint, reasoningContent, citationsContent, searchResult."
)
KEYS_GLM = "text, image, toolUse, toolResult, document, video, cachePoint, reasoningContent, searchResult."
KEYS_OTHER = "text, image, toolUse, toolResult, document, video, reasoningContent, searchResult."


def check(model: str, body: dict[str, Any]) -> str | None:
    spec = converse_model(model)
    assert spec is not None
    try:
        check_converse(model, spec, body)
    except Invalid as exc:
        return exc.message
    return None


def png(width: int, height: int, extra: int = 0) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x00" * extra


def image(data: bytes) -> dict[str, Any]:
    return {"image": {"format": "png", "source": {"bytes": base64.b64encode(data).decode()}}}


def document(name: str = "doc-1", size: int = 16) -> dict[str, Any]:
    data = base64.b64encode(b"%PDF" + b"0" * (size - 4)).decode()
    return {"document": {"format": "pdf", "name": name, "source": {"bytes": data}}}


def user(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": list(blocks)}]}


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (HAIKU, DEPRECATED),
        (OPUS, DEPRECATED),
        (SONNET, DEPRECATED),
        (LUNA, UNSUPPORTED),
        (SOL, UNSUPPORTED),
        (GLM, None),
        (DEEPSEEK, None),
    ],
)
def test_temperature(model: str, expected: str | None) -> None:
    assert check(model, {"messages": USER_HI, "inferenceConfig": {"temperature": 0.7}}) == expected


@pytest.mark.parametrize(
    ("model", "effort", "ok"),
    [(m, e, True) for m in (SOL, OPUS, HAIKU) for e in ("low", "medium", "high", "max")]
    + [(GLM, e, True) for e in ("low", "medium", "high", "xhigh", "max")]
    + [(LUNA, e, True) for e in ("none", "low", "medium", "high", "xhigh", "max")]
    + [(SOL, "none", False), (OPUS, "none", False), (HAIKU, "none", False), (LUNA, "minimal", False)]
    + [(GLM, "none", False)],
)
def test_effort_table(model: str, effort: str, ok: bool) -> None:
    error = check(model, {"messages": USER_HI, "outputConfig": {"effort": effort}})
    assert (error is None) == ok


def test_effort_rejection_texts() -> None:
    assert check(OPUS, {"messages": USER_HI, "outputConfig": {"effort": "none"}}) == OPUS_NONE
    assert check(HAIKU, {"messages": USER_HI, "outputConfig": {"effort": "none"}}) == OPUS_NONE
    sol = check(SOL, {"messages": USER_HI, "outputConfig": {"effort": "none"}})
    assert sol is not None
    assert sol.startswith(SOL_NONE_PREFIX)
    luna = check(LUNA, {"messages": USER_HI, "outputConfig": {"effort": "minimal"}})
    assert luna is not None
    assert LUNA_MINIMAL in luna
    assert check(GLM, {"messages": USER_HI, "outputConfig": {"effort": "none"}}) == GLM_NONE


def test_reasoning_effort_field_is_unknown_for_openai_models() -> None:
    error = check(LUNA, {"messages": USER_HI, "additionalModelRequestFields": {"reasoning_effort": "none"}})
    assert error is not None
    assert "Unknown parameter: 'reasoning_effort'." in error
    assert check(GLM, {"messages": USER_HI, "additionalModelRequestFields": {"reasoning_effort": "none"}}) is None


@pytest.mark.parametrize(
    ("model", "keys"), [(HAIKU, KEYS_CLAUDE), (GLM, KEYS_GLM), (SOL, KEYS_OTHER), (DEEPSEEK, KEYS_OTHER)]
)
def test_audio_blocks_are_rejected_by_every_model(model: str, keys: str) -> None:
    audio = {"audio": {"format": "wav", "source": {"bytes": "AAAA"}}}
    assert (
        check(model, user(audio))
        == f"ContentBlock object at messages.0.content.0 must set one of the following keys: {keys}"
    )


@pytest.mark.parametrize("model", ALL)
def test_video_is_rejected_by_every_model(model: str) -> None:
    video = {"video": {"format": "mp4", "source": {"bytes": "AAAA"}}}
    assert check(model, user({"text": "x"}, video)) == (
        "This model doesn't support the video content block that you provided. Update the content block and try again."
    )


@pytest.mark.parametrize(
    ("model", "ok"), [(HAIKU, True), (OPUS, True), (LUNA, True), (SOL, True), (GLM, False), (DEEPSEEK, False)]
)
def test_image_support(model: str, ok: bool) -> None:
    error = check(model, user({"text": "[image-1]"}, image(png(10, 10))))
    assert error == (
        None
        if ok
        else (
            "This model doesn't support the image content block that you provided. "
            "Update the content block and try again."
        )
    )


@pytest.mark.parametrize(
    ("model", "ok"),
    [(HAIKU, True), (OPUS, True), (SONNET, True), (LUNA, False), (SOL, False), (GLM, False), (DEEPSEEK, False)],
)
def test_document_support(model: str, ok: bool) -> None:
    error = check(model, user({"text": "[doc-1]"}, document()))
    assert error == (None if ok else "This model doesn't support documents.")


def test_image_limits() -> None:
    assert check(HAIKU, user({"text": "x"}, *[image(png(8, 8))] * 20)) is None
    assert "Too many images" in (check(HAIKU, user({"text": "x"}, *[image(png(8, 8))] * 21)) or "")
    assert "exceed 8000 pixels" in (check(HAIKU, user({"text": "x"}, image(png(8001, 10)))) or "")
    assert check(HAIKU, user({"text": "x"}, image(png(8000, 8000)))) is None
    assert "3750000 byte limit" in (check(HAIKU, user({"text": "x"}, image(png(10, 10, 3_750_001)))) or "")
    assert check(HAIKU, user({"text": "x"}, image(b"\x89PNG not really"))) is None  # undecodable header: no size rule


def test_document_limits_and_name_rule() -> None:
    assert check(OPUS, user({"text": "x"}, *[document(f"doc-{i}") for i in range(5)])) is None
    assert "Too many documents" in (check(OPUS, user({"text": "x"}, *[document(f"doc-{i}") for i in range(6)])) or "")
    assert "4500000 byte limit" in (check(OPUS, user({"text": "x"}, document(size=4_500_001))) or "")
    assert "text block" in (check(OPUS, user(document())) or "")
    for name in ("doc 1 (a) [b]", "x" * 200):
        assert check(OPUS, user({"text": "x"}, document(name))) is None
    for name in ("doc  1", "report.pdf", "x" * 201, "a_b", ""):
        assert "document.name" in (check(OPUS, user({"text": "x"}, document(name))) or ""), name


def test_media_only_in_user_messages_and_role_order() -> None:
    body = {"messages": [*USER_HI, {"role": "assistant", "content": [{"text": "see"}, image(png(4, 4))]}]}
    assert "only supported in user messages" in (check(HAIKU, body) or "")
    assert (check(HAIKU, {"messages": [{"role": "assistant", "content": [{"text": "x"}]}]}) or "").startswith(
        "A conversation must start with a user message."
    )
    assert (check(HAIKU, {"messages": USER_HI + USER_HI}) or "").startswith("A conversation must alternate")


def test_payload_cap_counts_base64_and_text_bytes() -> None:
    assert b64_size(3_000_000) == 4_000_000
    images = [image(b"\x00" * 3_000_000)] * 5  # 20,000,000 b64 bytes
    assert check(HAIKU, user(*images)) is None
    error = check(HAIKU, {**user(*images, {"text": "x"}), "system": []})
    assert error is not None
    assert "exceeds the maximum of 20000000 bytes" in error
    assert "20000001 bytes" in error


async def test_wire_rejection_reaches_the_client_before_rule_matching(
    mock: MockBedrock, bedrock: BedrockRuntimeClient
) -> None:
    mock.load_scenario(scenario({"match": {"model": SOL, "nth": 1}, "answer": {"raw_text": "first"}}))
    with pytest.raises(ClientError) as err:
        await bedrock.converse(modelId=SOL, messages=USER_HI, inferenceConfig={"temperature": 0.2})
    assert err.value.response["Error"] == {"Message": UNSUPPORTED, "Code": "ValidationException"}
    resp = await bedrock.converse(modelId=SOL, messages=USER_HI)  # the rejected call did not consume nth=1
    assert resp["output"]["message"]["content"] == [{"text": "first"}]
    with pytest.raises(ClientError) as err:
        await bedrock.converse(modelId="us.example.unknown-1", messages=USER_HI)
    assert err.value.response["Error"]["Message"] == "The provided model identifier is invalid."
