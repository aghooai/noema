"""The Converse request and response of the lean unary path (`ConverseAdapter.complete`), byte for byte what
aiobotocore's `bedrock-runtime` client builds and parses (proved by tests/unit/adapters/test_lean_converse.py).

- Request: `POST {endpoint}/model/{percent-encoded modelId}/converse`; the body is botocore's rest-json
  serialization of `wire.converse_params` without `modelId` (`json.dumps`, its default separators and
  `ensure_ascii`, blobs base64); `Content-Type: application/json`; SigV4 as `botocore.auth.SigV4Auth` signs it
  (signing name `bedrock`, the credentials' region; `noema.adapters._sigv4`, Wave E3), then the SDK's unsigned
  `amz-sdk-invocation-id` / `amz-sdk-request`.
  The endpoint comes from botocore's own endpoint rules and partitions (`EndpointProvider`).
- Parameter validation: exactly the constraints botocore's `ParamValidator` enforces on the shapes
  `converse_params` emits (leaf types, `min` lengths and values; it checks no `max`, pattern or enum), raised as
  the same `ParamValidationError`.
- Response: a 2xx body whose fields `_response_of` reads have the modeled JSON types is used as decoded; any other
  body goes through botocore's `RestJSONParser` with the operation's output shape (its exact result); an error
  status (>= 300) becomes the `ClientError` botocore raises (`RestJSONParser` error parse: `x-amzn-errortype`,
  `__type`, `code`, generic 5xx), classified by `classify` as before.
"""

from __future__ import annotations

import base64
import decimal
import json
import math
import uuid
from collections.abc import Mapping
from typing import Any, Final

import orjson
from botocore.endpoint_provider import EndpointProvider
from botocore.exceptions import ClientError, EventStreamError, ParamValidationError
from botocore.model import ServiceModel, Shape
from botocore.parsers import EventStreamJSONParser, RestJSONParser
from botocore.utils import percent_encode

from noema.adapters._sigv4 import sign_post
from noema.adapters._unary import RawResponse
from noema.adapters.classify import MSG_MALFORMED, StreamFault, stream_error
from noema.core.protocols.credentials import TenantCredentials

SERVICE: Final = "bedrock-runtime"
SIGNING_NAME: Final = "bedrock"
OPERATION: Final = "Converse"
STREAM_OPERATION: Final = "ConverseStream"
USER_AGENT: Final = "noema"
Json = dict[str, Any]


class ConverseModel:
    """What the lean path needs from botocore's data (built once from the shared data loader, in a worker
    thread): the endpoint rules and the Converse output shape."""

    def __init__(self, loader: Any) -> None:
        self._provider = EndpointProvider(
            ruleset_data=loader.load_service_model(SERVICE, "endpoint-rule-set-1"),
            partition_data=loader.load_data("partitions"),
        )
        service = ServiceModel(loader.load_service_model(SERVICE, "service-2"), service_name=SERVICE)
        output = service.operation_model(OPERATION).output_shape
        stream_output = service.operation_model(STREAM_OPERATION).output_shape
        assert output is not None
        assert stream_output is not None
        self.output_shape: Shape = output
        self.stream_shape: Shape = stream_output.members["stream"]
        self.event_parser = EventStreamJSONParser()
        self._endpoints: dict[str, str] = {}

    def endpoint(self, region: str) -> str:
        """botocore's endpoint for the region (no FIPS, no dual-stack: the client defaults)."""
        url = self._endpoints.get(region)
        if url is None:
            resolved = self._provider.resolve_endpoint(Region=region, UseFIPS=False, UseDualStack=False)
            url = self._endpoints[region] = str(resolved.url).rstrip("/")
        return url


def converse_url(endpoint: str, model_id: str, *, stream: bool = False) -> str:
    return f"{endpoint}/model/{percent_encode(model_id)}/{'converse-stream' if stream else 'converse'}"


def _param_error(path: str) -> ParamValidationError:
    return ParamValidationError(report=f"Invalid parameter {path}")


def check_params(params: Mapping[str, Any]) -> None:
    """botocore's ParamValidator on `converse_params` output (see the module docstring)."""
    model_id = params["modelId"]
    if not isinstance(model_id, str) or not model_id:
        raise _param_error("modelId")
    for message in params["messages"]:
        if not isinstance(message["role"], str):
            raise _param_error("messages.role")
        for block in message["content"]:
            if "text" in block:
                if not isinstance(block["text"], str):
                    raise _param_error("messages.content.text")
                continue
            media = block["image"] if "image" in block else block["document"]
            if not isinstance(media["format"], str) or not isinstance(
                media["source"]["bytes"], bytes | bytearray | str
            ):
                raise _param_error("messages.content.media")
            if "document" in block and (not isinstance(media["name"], str) or not media["name"]):
                raise _param_error("messages.content.document.name")
    for block in params.get("system", ()):
        if not isinstance(block["text"], str) or not block["text"]:
            raise _param_error("system.text")
    inference = params["inferenceConfig"]
    max_tokens = inference["maxTokens"]
    if not isinstance(max_tokens, int) or max_tokens < 1:
        raise _param_error("inferenceConfig.maxTokens")
    temperature = inference.get("temperature")
    if temperature is not None and (not isinstance(temperature, int | float | decimal.Decimal) or temperature < 0):
        raise _param_error("inferenceConfig.temperature")
    for stop in inference.get("stopSequences", ()):
        if not isinstance(stop, str) or not stop:
            raise _param_error("inferenceConfig.stopSequences")
    output = params.get("outputConfig")
    if output is not None and not isinstance(output["effort"], str):
        raise _param_error("outputConfig.effort")


def _wire_block(block: Json) -> Json:
    if "text" in block:
        return block
    kind = "image" if "image" in block else "document"
    media = dict(block[kind])
    data = media["source"]["bytes"]
    raw = data.encode("utf-8") if isinstance(data, str) else data  # botocore's _get_base64
    media["source"] = {"bytes": base64.b64encode(raw).strip().decode("utf-8")}
    return {kind: media}


def converse_body(params: Mapping[str, Any], *, media: bool) -> bytes:
    """botocore's rest-json body of `params` (`modelId` is a URI label, not in the body). `media`: some block is
    an image or document (their bytes are base64-encoded); text-only requests are dumped as they are."""
    body: Json = {k: v for k, v in params.items() if k != "modelId"}
    if media:
        body["messages"] = [
            {"role": m["role"], "content": [_wire_block(b) for b in m["content"]]} for m in params["messages"]
        ]
    temperature = body["inferenceConfig"].get("temperature")
    if temperature is not None and type(temperature) is not int:  # botocore's float serializer
        body["inferenceConfig"] = {**body["inferenceConfig"], "temperature": _float(temperature)}
    return json.dumps(body).encode("utf-8")


def _float(value: float | decimal.Decimal) -> float | str:
    number = float(value) if isinstance(value, decimal.Decimal) else value
    if math.isinf(number) or math.isnan(number):
        return "NaN" if math.isnan(number) else ("Infinity" if number > 0 else "-Infinity")
    return number


def signed_headers(creds: TenantCredentials, url: str, body: bytes) -> dict[str, str]:
    """SigV4 (signing name `bedrock`, the credentials' region) over the request botocore sends (`_sigv4.sign_post`,
    byte for byte SigV4Auth's), plus the SDK's unsigned headers."""
    token = creds.aws.session_token
    headers = sign_post(
        access_key=creds.aws.access_key_id,
        secret_key=creds.aws.secret_access_key.get_secret_value(),
        token=token.get_secret_value() if token is not None else None,
        region=creds.region,
        service=SIGNING_NAME,
        url=url,
        body=body,
    )
    headers["User-Agent"] = USER_AGENT
    headers["amz-sdk-invocation-id"] = str(uuid.uuid4())
    headers["amz-sdk-request"] = "attempt=1"
    return headers


def _typed(data: object) -> bool:
    """The fields `_response_of` reads have their modeled JSON types (so botocore's parser would return them
    unchanged): objects where structures are, a list of objects for the content, integers for the usage."""
    if not isinstance(data, dict):
        return False
    usage = data.get("usage")
    if usage is not None and not (isinstance(usage, dict) and all(type(v) is int for v in usage.values())):
        return False
    output = data.get("output")
    if output is None:
        return True
    message = output.get("message") if isinstance(output, dict) else False
    if message is None:
        return True
    content = message.get("content") if isinstance(message, dict) else False
    if content is None:
        return True
    return isinstance(content, list) and all(isinstance(b, dict) for b in content)


def _response(raw: RawResponse) -> dict[str, Any]:
    return {"status_code": raw.status, "headers": raw.headers, "body": raw.body}


def malformed_response() -> StreamFault:
    """A 2xx Converse response that is no Converse response (retryable, as Mantle's `malformed:response`)."""
    return StreamFault(stream_error("malformed:response", MSG_MALFORMED))


def _json_object(body: bytes) -> bool:
    """botocore's JSON decoding (`json.loads`, which takes NaN and lone surrogates) gives an object."""
    try:
        return isinstance(json.loads(body.decode("utf-8")), dict)
    except ValueError:
        return False


def parse_response(raw: RawResponse, model: ConverseModel) -> Mapping[str, Any]:
    """The parsed Converse response, or raises the `ClientError` botocore raises for an error status. A 2xx body
    that is not a JSON object, or one botocore's parser cannot take, raises `malformed_response()` (aiobotocore made
    an empty or unparsable body an empty `end_turn` answer, a wrong-shaped one a fatal AttributeError)."""
    if raw.status >= 300:
        parsed = RestJSONParser().parse(_response(raw), model.output_shape)
        raise ClientError(parsed, OPERATION)  # botocore's own parsed error dict
    try:
        data: object = orjson.loads(raw.body)
    except orjson.JSONDecodeError:
        data = None
    if _typed(data):
        assert isinstance(data, dict)
        return data
    if not isinstance(data, dict) and not _json_object(raw.body):
        raise malformed_response()
    try:
        parsed_body: Mapping[str, Any] = RestJSONParser().parse(_response(raw), model.output_shape)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise malformed_response() from None
    return parsed_body


def _typed_event(event_type: str, payload: object) -> bool:
    """A ConverseStream event payload whose fields `_ConverseRaw.feed` reads have their modeled JSON types (so
    botocore's event parser would return them unchanged)."""
    if not isinstance(payload, dict):
        return False
    if event_type == "contentBlockDelta":
        delta = payload.get("delta")
        return isinstance(delta, dict) and isinstance(delta.get("reasoningContent", {}), dict)
    if event_type == "metadata":
        usage = payload.get("usage")
        return usage is None or (isinstance(usage, dict) and all(type(v) is int for v in usage.values()))
    return event_type in _PLAIN_EVENTS


_PLAIN_EVENTS: Final = frozenset({"messageStart", "messageStop", "contentBlockStart", "contentBlockStop"})


def parse_event(headers: Mapping[str, Any], body: bytes, model: ConverseModel) -> Mapping[str, Any]:
    """One ConverseStream message (its headers and payload) as aiobotocore's event stream parses it: `{event_type:
    payload}` (empty for an event type the model does not know, which the stream skips); an `exception` or `error`
    message raises the `EventStreamError` it raises. A payload botocore's parser cannot take (a `delta` that is not
    an object, a list) is `StreamFault(malformed:chunk)` (F8; aiobotocore surfaced the parser's AttributeError, a
    fatal)."""
    message_type = headers.get(":message-type")
    if message_type != "exception" and message_type != "error":
        event_type = str(headers.get(":event-type"))
        try:
            payload: object = orjson.loads(body)
        except orjson.JSONDecodeError:
            payload = None
        if _typed_event(event_type, payload):
            return {event_type: payload}
    error = message_type == "error" or message_type == "exception"  # EventStreamMessage.to_response_dict
    response = {"status_code": 400 if error else 200, "headers": headers, "body": body}
    try:
        parsed: Mapping[str, Any] = model.event_parser.parse(response, model.stream_shape)
    except (AttributeError, KeyError, TypeError, ValueError):  # a payload of the wrong shape (F8), not a crash
        raise StreamFault(stream_error("malformed:chunk", MSG_MALFORMED)) from None
    if response["status_code"] != 200:
        raise EventStreamError(parsed, STREAM_OPERATION)  # type: ignore[arg-type]  # botocore's parsed error
    return parsed
