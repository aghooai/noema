"""Request validation before any scenario rule applies (spec-10 §2.2): a fidelity violation fails like Bedrock even
when a rule matches. Converse failures raise `Invalid` (HTTP 400 `ValidationException`); Mantle failures raise
`MantleInvalid` (an OpenAI-compatible error)."""

from __future__ import annotations

import base64
import binascii
import itertools
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .imagesize import image_size
from .models import (
    DOCUMENTS_UNSUPPORTED,
    IMAGE_UNSUPPORTED,
    LOGPROBS_REASONING,
    LUNA_EFFORTS,
    REASONING_EFFORT_UNKNOWN,
    VIDEO_UNSUPPORTED,
    ConverseModel,
    MantleReject,
    chat_route_error,
    luna_effort_error,
    responses_route_error,
)

MAX_IMAGES: Final = 20
MAX_IMAGE_BYTES: Final = 3_750_000
MAX_IMAGE_SIDE: Final = 8_000
MAX_DOCUMENTS: Final = 5
MAX_DOCUMENT_BYTES: Final = 4_500_000
MAX_PAYLOAD_BYTES: Final = 20_000_000  # spec-07 §4.5: sum of 4*ceil(n/3) per blob + UTF-8 text bytes
IMAGE_FORMATS: Final = frozenset({"png", "jpeg", "gif", "webp"})
DOCUMENT_FORMATS: Final = frozenset({"pdf", "csv", "doc", "docx", "xls", "xlsx", "html", "txt", "md"})
# Converse DocumentBlock.name: alphanumerics, single spaces, hyphens, parentheses, square brackets; <= 200 chars.
DOCUMENT_NAME: Final = re.compile(r"[A-Za-z0-9\-()\[\]]+( [A-Za-z0-9\-()\[\]]+)*")
FIRST_USER: Final = (
    "A conversation must start with a user message. Try again with a conversation that starts with a user message."
)
ALTERNATE: Final = (
    "A conversation must alternate between user and assistant roles. Make sure the conversation alternates "
    "between user and assistant roles and try again."
)
_SYSTEM_KEYS: Final = frozenset({"text", "cachePoint", "guardContent"})


class Invalid(Exception):
    """A Converse `ValidationException` (HTTP 400) with the given message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MantleInvalid(Exception):
    """An OpenAI-compatible rejection."""

    def __init__(self, reject: MantleReject) -> None:
        super().__init__(reject.message)
        self.reject = reject


@dataclass(frozen=True, slots=True)
class ConverseFacts:
    """What the mock needs from a valid Converse request."""

    prompt: str  # system + message text, concatenated (scenario `prompt_contains`)
    effort: str | None
    max_tokens: int | None
    images: int
    documents: int
    payload_bytes: int


def b64_size(n: int) -> int:
    """The base64 length of n bytes (spec-07 §4.5)."""
    return 4 * math.ceil(n / 3)


def _blob(block: Mapping[str, Any], where: str) -> bytes:
    source = block.get("source")
    raw = source.get("bytes") if isinstance(source, Mapping) else None
    if not isinstance(raw, str):
        raise Invalid(f"{where}.source: the wire mock supports source.bytes only")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise Invalid(f"{where}.source.bytes: invalid base64 data") from None


def _image(block: Mapping[str, Any], where: str) -> int:
    fmt = block.get("format")
    if fmt not in IMAGE_FORMATS:
        raise Invalid(f"{where}.format: unsupported image format {fmt!r}")
    data = _blob(block, where)
    if len(data) > MAX_IMAGE_BYTES:
        raise Invalid(f"{where}.source.bytes: the image exceeds the {MAX_IMAGE_BYTES} byte limit ({len(data)} bytes)")
    size = image_size(data)
    if size is not None and max(size) > MAX_IMAGE_SIDE:
        raise Invalid(f"{where}: image dimensions {size[0]}x{size[1]} exceed {MAX_IMAGE_SIDE} pixels per side")
    return b64_size(len(data))


def _document(block: Mapping[str, Any], where: str) -> int:
    fmt = block.get("format")
    if fmt not in DOCUMENT_FORMATS:
        raise Invalid(f"{where}.format: unsupported document format {fmt!r}")
    name = block.get("name")
    if not isinstance(name, str) or len(name) > 200 or DOCUMENT_NAME.fullmatch(name) is None:
        raise Invalid(
            f"{where}.name: the name can only contain alphanumeric characters, single spaces, hyphens, "
            "parentheses and square brackets (at most 200 characters)"
        )
    data = _blob(block, where)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise Invalid(f"{where}.source.bytes: the document exceeds the {MAX_DOCUMENT_BYTES} byte limit")
    return b64_size(len(data))


def _roles(messages: Sequence[Any]) -> None:
    if not messages or not isinstance(messages[0], Mapping) or messages[0].get("role") != "user":
        raise Invalid(FIRST_USER)
    for prev, cur in itertools.pairwise(messages):
        if not isinstance(cur, Mapping) or cur.get("role") not in ("user", "assistant"):
            raise Invalid(ALTERNATE)
        if cur.get("role") == prev.get("role"):
            raise Invalid(ALTERNATE)


def _message_blocks(spec: ConverseModel, i: int, msg: Mapping[str, Any], acc: list[int], text: list[str]) -> None:
    """Validate message i; `acc` = [images, documents, payload bytes]."""
    content = msg.get("content")
    if not isinstance(content, list) or not content:
        raise Invalid(f"messages.{i}.content: a message needs at least one content block")
    has_text = has_document = False
    for j, block in enumerate(content):
        where = f"messages.{i}.content.{j}"
        keys = list(block) if isinstance(block, Mapping) else []
        if len(keys) != 1 or keys[0] not in spec.block_keys:
            raise Invalid(spec.block_keys_error(i, j))
        key = keys[0]
        value = block[key]
        if key == "text":
            if not isinstance(value, str):
                raise Invalid(f"{where}.text: expected a string")
            has_text = True
            text.append(value)
            acc[2] += len(value.encode())
        elif key == "video":
            raise Invalid(VIDEO_UNSUPPORTED)
        elif key in ("image", "document"):
            if key == "image" and not spec.images:
                raise Invalid(IMAGE_UNSUPPORTED)
            if key == "document" and not spec.documents:
                raise Invalid(DOCUMENTS_UNSUPPORTED)
            if msg.get("role") != "user":
                raise Invalid(f"messages.{i}: images and documents are only supported in user messages")
            if not isinstance(value, Mapping):
                raise Invalid(f"{where}.{key}: expected an object")
            if key == "image":
                acc[0] += 1
                acc[2] += _image(value, f"{where}.image")
            else:
                has_document = True
                acc[1] += 1
                acc[2] += _document(value, f"{where}.document")
    if has_document and not has_text:
        raise Invalid(f"messages.{i}: a document must be accompanied by a text block in the same message")


def _object(body: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = body.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise Invalid(f"{key}: expected an object")
    return value


def check_converse(model_id: str, spec: ConverseModel, body: Mapping[str, Any]) -> ConverseFacts:
    """Validate a Converse/ConverseStream body for `model_id` (spec-10 §2.2 order: the service-side content and
    limit rules, then the model-side parameter rules). Raises `Invalid`."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise Invalid(FIRST_USER)
    _roles(messages)
    acc = [0, 0, 0]
    text: list[str] = []
    system = body.get("system", [])
    if not isinstance(system, list):
        raise Invalid("system: expected a list of system content blocks")
    for k, block in enumerate(system):
        keys = list(block) if isinstance(block, Mapping) else []
        if len(keys) != 1 or keys[0] not in _SYSTEM_KEYS:
            raise Invalid(
                f"SystemContentBlock object at system.{k} must set one of the following keys: text, guardContent, "
                "cachePoint."
            )
        if keys[0] == "text":
            text.append(str(block["text"]))
            acc[2] += len(str(block["text"]).encode())
    for i, msg in enumerate(messages):
        _message_blocks(spec, i, msg, acc, text)
    if acc[0] > MAX_IMAGES:
        raise Invalid(f"Too many images in the request: {acc[0]} exceeds the maximum of {MAX_IMAGES}")
    if acc[1] > MAX_DOCUMENTS:
        raise Invalid(f"Too many documents in the request: {acc[1]} exceeds the maximum of {MAX_DOCUMENTS}")
    if acc[2] > MAX_PAYLOAD_BYTES:
        raise Invalid(f"The request payload ({acc[2]} bytes) exceeds the maximum of {MAX_PAYLOAD_BYTES} bytes")
    inference = _object(body, "inferenceConfig")
    if "temperature" in inference and spec.temperature_error is not None:
        raise Invalid(spec.temperature_error)
    output = _object(body, "outputConfig")
    effort = output.get("effort")
    if effort is not None:
        error = spec.effort_error(str(effort), model_id)
        if error is not None:
            raise Invalid(error)
    extra = _object(body, "additionalModelRequestFields")
    if spec.family == "openai" and "reasoning_effort" in extra:
        raise Invalid(REASONING_EFFORT_UNKNOWN)
    max_tokens = inference.get("maxTokens")
    return ConverseFacts(
        prompt="".join(text),
        effort=None if effort is None else str(effort),
        max_tokens=max_tokens if isinstance(max_tokens, int) else None,
        images=acc[0],
        documents=acc[1],
        payload_bytes=acc[2],
    )


# --- Mantle (OpenAI-compatible) ---------------------------------------------------------------------------------------
MAX_TOP_LOGPROBS: Final = 20


@dataclass(frozen=True, slots=True)
class ChatFacts:
    """What the mock needs from a valid chat-completions (or Responses) request."""

    model: str
    prompt: str
    stream: bool
    include_usage: bool
    logprobs: bool
    top_logprobs: int | None
    max_tokens: int | None


def _bad(message: str, param: str | None = None) -> MantleInvalid:
    return MantleInvalid(MantleReject(400, None, message, param))


def _content_text(content: Any, where: str, model: str) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, Mapping) or part.get("type") not in ("text", "input_text"):
                raise _bad(f"Only text content parts are supported by model '{model}'.", where)
            parts.append(str(part.get("text", "")))
        return "".join(parts)
    if content is None:
        return ""
    raise _bad(f"Invalid type for '{where}': expected a string or an array of content parts.", where)


def _model(body: Mapping[str, Any]) -> str:
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise _bad("you must provide a model parameter", "model")
    return model


def _max_tokens(body: Mapping[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _bad(f"Invalid '{key}': expected an integer >= 1.", key)
    return value


def check_chat(body: Mapping[str, Any]) -> ChatFacts:
    """Validate a `POST /v1/chat/completions` body (the model's route rules first)."""
    model = _model(body)
    reject = chat_route_error(model)
    if reject is not None:
        raise MantleInvalid(reject)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _bad("'messages' must be a non-empty array.", "messages")
    texts = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, Mapping) or msg.get("role") not in ("system", "developer", "user", "assistant"):
            raise _bad(f"Invalid value for 'messages[{i}].role'.", f"messages[{i}].role")
        texts.append(_content_text(msg.get("content"), f"messages[{i}].content", model))
    stream = body.get("stream") is True
    options = body.get("stream_options")
    if options is not None and not stream:
        raise _bad("The 'stream_options' parameter is only allowed when 'stream' is enabled.", "stream_options")
    logprobs = body.get("logprobs") is True
    top = body.get("top_logprobs")
    if top is not None:
        if isinstance(top, bool) or not isinstance(top, int) or top < 0:
            raise _bad("Invalid 'top_logprobs': expected an integer >= 0.", "top_logprobs")
        if top > MAX_TOP_LOGPROBS:
            raise _bad(
                "Invalid 'top_logprobs': integer above maximum value. Expected a value <= "
                f"{MAX_TOP_LOGPROBS}, but got {top} instead.",
                "top_logprobs",
            )
        if not logprobs:
            raise _bad("Invalid value for 'top_logprobs': requires 'logprobs' to be true.", "top_logprobs")
    return ChatFacts(
        model=model,
        prompt="".join(texts),
        stream=stream,
        include_usage=isinstance(options, Mapping) and options.get("include_usage") is True,
        logprobs=logprobs,
        top_logprobs=top,
        max_tokens=_max_tokens(body, "max_tokens"),
    )


def check_responses(body: Mapping[str, Any], *, openai_prefix: bool) -> ChatFacts:
    """Validate a non-streaming Responses body (only Luna on `/openai/v1/responses` answers)."""
    model = _model(body)
    reject = responses_route_error(model, openai_prefix=openai_prefix)
    if reject is not None:
        raise MantleInvalid(reject)
    reasoning = body.get("reasoning") or {}
    effort = reasoning.get("effort") if isinstance(reasoning, Mapping) else None
    if effort is not None and effort not in LUNA_EFFORTS:
        raise MantleInvalid(luna_effort_error(str(effort), model))
    if body.get("top_logprobs") is not None or "message.output_text.logprobs" in (body.get("include") or ()):
        raise MantleInvalid(LOGPROBS_REASONING)
    if body.get("stream") is True:
        raise _bad("The wire mock serves non-streaming Responses requests only.", "stream")
    raw = body.get("input")
    texts = [str(body.get("instructions") or "")]
    if isinstance(raw, list):
        for i, item in enumerate(raw):
            content = item.get("content") if isinstance(item, Mapping) else item
            texts.append(_content_text(content, f"input[{i}].content", model))
    else:
        texts.append(_content_text(raw, "input", model))
    return ChatFacts(model, "".join(texts), False, False, False, None, _max_tokens(body, "max_output_tokens"))
