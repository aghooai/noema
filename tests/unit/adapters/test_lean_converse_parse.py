"""E sprint A: the lean Converse path parses and classifies every raw response exactly as the aiobotocore client did
(success bodies of every shape, including ones only botocore's parser can normalize, and every spec-02 §4.3 error
form), but for one deliberate difference: a 2xx body that is no Converse response (empty, not JSON, not an object,
or one botocore's parser cannot take) is the retryable `malformed:response` of Mantle, where aiobotocore made it an
empty `end_turn` answer or a fatal AttributeError. Its parameter validation raises exactly where botocore's
`ParamValidator` does."""

from __future__ import annotations

from typing import Any

import orjson
import pytest
from botocore.exceptions import ParamValidationError
from botocore.model import ServiceModel
from botocore.validate import ParamValidator
from tests.unit.adapters.lean_reference import converse_model, loader, reference

from noema.adapters._converse_wire import check_params, parse_response
from noema.adapters._unary import RawResponse
from noema.adapters.classify import MSG_MALFORMED, classify
from noema.adapters.converse import _response_of
from noema.core.errors import ErrorKind

J = {"content-type": "application/json"}


def ok(content: Any, stop: str = "end_turn", usage: Any = None, **extra: Any) -> bytes:
    body = {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop, **extra}
    body["usage"] = usage if usage is not None else {"inputTokens": 9, "outputTokens": 3, "totalTokens": 12}
    return orjson.dumps(body)


def err(
    status: int, name: str | None, *, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None
) -> RawResponse:
    h = {**J, **(headers or {})}
    if name is not None:
        h["x-amzn-errortype"] = name
    return RawResponse(status, h, orjson.dumps(body if body is not None else {"message": "probe text"}))


TEXT = [{"text": '{"A": 0.7, "B": 0.3}'}]
CASES: dict[str, RawResponse] = {
    "text": RawResponse(200, J, ok(TEXT)),
    "reasoning_and_refusal": RawResponse(
        200,
        J,
        ok(
            [{"reasoningContent": {"reasoningText": {"text": "r" * 41, "signature": "s"}}}, *TEXT],
            "guardrail_intervened",
        ),
    ),
    "cache_usage": RawResponse(
        200,
        J,
        ok(
            TEXT,
            usage={
                "inputTokens": 5,
                "outputTokens": 2,
                "totalTokens": 7,
                "cacheReadInputTokens": 100,
                "cacheWriteInputTokens": 30,
            },
        ),
    ),
    "unknown_members": RawResponse(
        200, J, ok([*TEXT, {"citationsContent": {"x": 1}}], metrics={"latencyMs": 5}, trace={"a": 1}, extra=[1])
    ),
    "usage_strings": RawResponse(200, J, ok(TEXT, usage={"inputTokens": "5", "outputTokens": "2"})),
    "usage_floats": RawResponse(200, J, ok(TEXT, usage={"inputTokens": 5.0, "outputTokens": 2})),
    "usage_bool": RawResponse(200, J, ok(TEXT, usage={"inputTokens": True, "outputTokens": 2})),
    "no_usage_no_stop": RawResponse(200, J, orjson.dumps({"output": {"message": {"content": TEXT}}})),
    "empty_body": RawResponse(200, J, b""),
    "html_body": RawResponse(200, {}, b"<html>oops</html>"),
    "json_list": RawResponse(200, J, b"[1, 2]"),
    "json_string": RawResponse(200, J, b'"x"'),
    "nan": RawResponse(200, J, b'{"output": {"message": {"content": [{"text": "a"}]}}, "metrics": {"latencyMs": NaN}}'),
    "content_object": RawResponse(200, J, orjson.dumps({"output": {"message": {"content": {"text": "a"}}}})),
    "content_strings": RawResponse(200, J, orjson.dumps({"output": {"message": {"content": ["text", "a"]}}})),
    "output_null": RawResponse(200, J, orjson.dumps({"output": None, "stopReason": "max_tokens"})),
    "204": RawResponse(204, {}, b""),
    "300": RawResponse(300, J, ok(TEXT)),
    "validation": err(400, "ValidationException"),
    "validation_type_in_body": err(400, None, body={"__type": "com.amazon.coral#ValidationException", "message": "x"}),
    "code_in_body": err(400, None, body={"code": "ValidationException:http://x", "Message": "m"}),
    "access_denied": err(403, "AccessDeniedException:http://internal.amazon.com/coral/"),
    "unrecognized_client": err(403, "UnrecognizedClientException"),
    "expired_token": err(403, "ExpiredTokenException"),
    "not_found": err(404, "ResourceNotFoundException"),
    "model_timeout": err(408, "ModelTimeoutException"),
    "model_error": err(424, "ModelErrorException"),
    "throttling": err(429, "ThrottlingException", headers={"retry-after": "3"}),
    "throttling_date": err(429, "ThrottlingException", headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
    "quota": err(429, "ServiceQuotaExceededException"),
    "not_ready": err(429, "ModelNotReadyException"),
    "internal": err(500, "InternalServerException"),
    "unavailable": err(503, "ServiceUnavailableException"),
    "html_5xx": RawResponse(502, {}, b"<html><body>Bad Gateway</body></html>"),
    "empty_5xx": RawResponse(503, {}, b""),
    "teapot": RawResponse(418, {}, b""),
    "throttle_status_only": RawResponse(429, J, b"{}"),
}


def outcome(make: Any) -> tuple[str, Any]:
    try:
        return "ok", _response_of(make(), "us-east-1", 0)
    except Exception as exc:
        error = classify(exc)
        return "error", (error.kind, error.message, error.provider_code, error.cause, error.retry_after_s)


@pytest.mark.parametrize("name", list(CASES))
async def test_responses_parse_and_classify_as_botocore_does(name: str) -> None:
    raw = CASES[name]
    params = {
        "modelId": "m",
        "messages": [{"role": "user", "content": [{"text": "a"}]}],
        "inferenceConfig": {"maxTokens": 8},
    }
    async with reference() as ref:
        ref.answer = raw
        try:
            botocore_result: Any = await ref.converse(params)
        except Exception as exc:
            botocore_result = exc
    wire = converse_model()
    expected = outcome(lambda: _raise(botocore_result))
    if name in NOT_CONVERSE:  # deliberate: aiobotocore made these an empty answer or a fatal AttributeError
        assert expected != MALFORMED
        expected = MALFORMED
    assert outcome(lambda: parse_response(raw, wire)) == expected


# 2xx bodies that are no Converse response: malformed:response (retryable, as on Mantle), not botocore's outcome
NOT_CONVERSE = frozenset(
    {"empty_body", "html_body", "json_list", "json_string", "nan", "content_object", "content_strings", "204"}
)
MALFORMED = ("error", (ErrorKind.RETRYABLE, MSG_MALFORMED, "malformed:response", None, None))


def _raise(value: Any) -> Any:
    if isinstance(value, Exception):
        raise value
    return value


def _input_shape() -> Any:
    model = ServiceModel(loader().load_service_model("bedrock-runtime", "service-2"), service_name="bedrock-runtime")
    return model.operation_model("Converse").input_shape


def params(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "modelId": "m",
        "messages": [{"role": "user", "content": [{"text": "a"}]}],
        "inferenceConfig": {"maxTokens": 1},
    }
    return {**base, **over}


def user(*blocks: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"role": "user", "content": list(blocks)}]


VALIDATION: dict[str, dict[str, Any]] = {
    "ok": params(),
    "model_id_empty": params(modelId=""),
    "model_id_int": params(modelId=5),
    "text_empty": params(messages=user({"text": ""})),
    "text_int": params(messages=user({"text": 5})),
    "text_none": params(messages=user({"text": None})),
    "role_int": params(messages=[{"role": 1, "content": [{"text": "a"}]}]),
    "no_messages": params(messages=[]),
    "no_content": params(messages=user()),
    "system_empty_text": params(system=[{"text": ""}]),
    "system_ok": params(system=[{"text": "s"}]),
    "max_tokens_0": params(inferenceConfig={"maxTokens": 0}),
    "max_tokens_bool": params(inferenceConfig={"maxTokens": True}),
    "max_tokens_float": params(inferenceConfig={"maxTokens": 1.5}),
    "temperature_negative": params(inferenceConfig={"maxTokens": 1, "temperature": -0.1}),
    "temperature_above_1": params(inferenceConfig={"maxTokens": 1, "temperature": 2.0}),
    "temperature_nan": params(inferenceConfig={"maxTokens": 1, "temperature": float("nan")}),
    "temperature_int": params(inferenceConfig={"maxTokens": 1, "temperature": 1}),
    "temperature_str": params(inferenceConfig={"maxTokens": 1, "temperature": "0.5"}),
    "stop_empty": params(inferenceConfig={"maxTokens": 1, "stopSequences": [""]}),
    "stop_int": params(inferenceConfig={"maxTokens": 1, "stopSequences": [1]}),
    "stop_ok": params(inferenceConfig={"maxTokens": 1, "stopSequences": ["x"]}),
    "effort_empty": params(outputConfig={"effort": ""}),
    "effort_int": params(outputConfig={"effort": 3}),
    "image_ok": params(messages=user({"image": {"format": "png", "source": {"bytes": b"p"}}})),
    "image_empty_bytes": params(messages=user({"image": {"format": "png", "source": {"bytes": b""}}})),
    "image_str_bytes": params(messages=user({"image": {"format": "png", "source": {"bytes": "p"}}})),
    "image_bad_format": params(messages=user({"image": {"format": "bmp", "source": {"bytes": b"p"}}})),
    "image_format_int": params(messages=user({"image": {"format": 1, "source": {"bytes": b"p"}}})),
    "document_ok": params(messages=user({"document": {"format": "pdf", "name": "n", "source": {"bytes": b"%"}}})),
    "document_empty_name": params(
        messages=user({"document": {"format": "pdf", "name": "", "source": {"bytes": b"%"}}})
    ),
}


@pytest.mark.parametrize("name", list(VALIDATION))
def test_parameter_validation_raises_where_botocore_does(name: str) -> None:
    case = VALIDATION[name]
    botocore_rejects = ParamValidator().validate(case, _input_shape()).has_errors()
    try:
        check_params(case)
    except ParamValidationError:
        lean_rejects = True
    else:
        lean_rejects = False
    assert lean_rejects is botocore_rejects


# the `min` constraints check_params enforces (min 0 ones are no-ops); botocore checks no blob range, max or pattern
ENFORCED_MINS = {
    "modelId": 1,
    "system[].text": 1,
    "inferenceConfig.maxTokens": 1,
    "inferenceConfig.temperature": 0,
    "inferenceConfig.stopSequences": 0,
    "inferenceConfig.stopSequences[]": 1,
    "messages[].content[].document.name": 1,
    "outputConfig.effort": 0,
}
EMITTED = (
    "modelId",
    "messages[].role",
    "messages[].content[].text",
    "messages[].content[].image.format",
    "messages[].content[].document.format",
    "messages[].content[].document.name",
    "system[].text",
    "inferenceConfig.maxTokens",
    "inferenceConfig.temperature",
    "inferenceConfig.stopSequences",
    "inferenceConfig.stopSequences[]",
    "outputConfig.effort",
    "messages",
    "messages[].content",
    "system",
)
RANGED = frozenset({"string", "list", "integer", "long", "float", "double"})


def test_the_validated_constraints_are_the_service_models() -> None:
    # a botocore update that adds a `min` (or a hostLabel) to a member converse_params emits fails here first
    found: dict[str, int] = {}
    for path in EMITTED:
        shape = _input_shape()
        for part in path.split("."):
            name, is_list = part.removesuffix("[]"), part.endswith("[]")
            shape = shape.members[name]
            if is_list:
                shape = shape.member
        assert not shape.serialization.get("hostLabel"), path
        if shape.type_name in RANGED and "min" in shape.metadata:
            found[path] = shape.metadata["min"]
    assert found == ENFORCED_MINS
