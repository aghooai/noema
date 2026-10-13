"""Wire formats for the in-process Bedrock emulator (spec-10 §2.1a): the AWS eventstream encoder, Converse JSON and
Mantle chat chunks rendered from the scenario DSL's plans (`tests/fakes/scenario.py`), and the error responses with
the exact probe ValidationException strings."""

from __future__ import annotations

import base64
import math
import struct
import zlib
from collections.abc import Mapping
from typing import Any, Final

import orjson
from tests.fakes.scenario import Completion, Fault, PlannedEvent

Json = dict[str, Any]
# Exact strings from probes/results/*.json (classify_error tests and the emulator's validation faults).
PROBE_VALIDATION: Final[Mapping[str, str]] = {
    "temperature_deprecated": "The model returned the following errors: `temperature` is deprecated for this model.",
    "temperature_unsupported": "This model doesn't support the temperature field. Remove temperature and try again.",
    "effort_none_claude": (
        "The model returned the following errors: unknown variant `none`, expected one of `low`, `medium`, `high`, "
        "`xhigh`, `max`, `Unhandled` at line 1 column 163"
    ),
    "documents": "This model doesn't support documents.",
    "image_block": (
        "This model doesn't support the image content block that you provided. Update the content block and try again."
    ),
    "video_block": (
        "This model doesn't support the video content block that you provided. Update the content block and try again."
    ),
    "audio_block": (
        "ContentBlock object at messages.0.content.0 must set one of the following keys: text, image, toolUse, "
        "toolResult, document, video, reasoningContent, searchResult."
    ),
}
_STATUS_TYPES: Final[Mapping[int, str]] = {500: "InternalServerException", 503: "ServiceUnavailableException"}
WIRE_STOP_MANTLE: Final[Mapping[str, str]] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
}


def _header(name: str, value: str) -> bytes:
    n, v = name.encode(), value.encode()
    return struct.pack(">B", len(n)) + n + b"\x07" + struct.pack(">H", len(v)) + v


def encode_message(headers: Mapping[str, str], payload: bytes, *, corrupt: bool = False) -> bytes:
    """One AWS eventstream message (prelude, CRC32s); `corrupt` flips a bit of the message CRC."""
    hbytes = b"".join(_header(k, v) for k, v in headers.items())
    total = 12 + len(hbytes) + len(payload) + 4
    prelude = struct.pack(">II", total, len(hbytes))
    head = prelude + struct.pack(">I", zlib.crc32(prelude)) + hbytes + payload
    crc = zlib.crc32(head) ^ (1 if corrupt else 0)
    return head + struct.pack(">I", crc)


def event_frame(event_type: str, body: Json, *, corrupt: bool = False) -> bytes:
    headers = {":message-type": "event", ":event-type": event_type, ":content-type": "application/json"}
    return encode_message(headers, orjson.dumps(body), corrupt=corrupt)


def exception_frame(exception_type: str, message: str = "The model stream failed.") -> bytes:
    headers = {":message-type": "exception", ":exception-type": exception_type, ":content-type": "application/json"}
    return encode_message(headers, orjson.dumps({"message": message}))


def converse_usage(usage: Mapping[str, int] | None) -> Json:
    """The scenario usage (input/output/cached/written tokens) as Converse `usage`."""
    raw = dict(usage or {})
    out: Json = {
        "inputTokens": raw.get("input_tokens", 0),
        "outputTokens": raw.get("output_tokens", 0),
        "totalTokens": raw.get("input_tokens", 0) + raw.get("output_tokens", 0),
    }
    if raw.get("cached_input_tokens"):
        out["cacheReadInputTokens"] = raw["cached_input_tokens"]
    if raw.get("cache_write_input_tokens"):
        out["cacheWriteInputTokens"] = raw["cache_write_input_tokens"]
    return out


def converse_frame(ev: PlannedEvent, block: int) -> bytes:
    """A planned stream event as a ConverseStream frame (`block`: the content block index)."""
    if ev.kind == "start":
        return event_frame("messageStart", {"role": "assistant"})
    if ev.kind == "reasoning":
        delta = {"reasoningContent": {"text": "r" * ev.chars}}
        return event_frame("contentBlockDelta", {"contentBlockIndex": block, "delta": delta})
    if ev.kind == "text":
        return event_frame("contentBlockDelta", {"contentBlockIndex": block, "delta": {"text": ev.text}})
    if ev.kind == "stop":
        return event_frame("messageStop", {"stopReason": ev.stop or "end_turn"})
    if ev.kind == "usage":
        return event_frame("metadata", {"usage": converse_usage(ev.usage), "metrics": {"latencyMs": 1}})
    error = ev.error or "modelStreamErrorException"
    if error.startswith("malformed:"):
        return event_frame("contentBlockDelta", {"contentBlockIndex": block, "delta": {"text": "x"}}, corrupt=True)
    return exception_frame(error)


def converse_body(answer: Completion) -> bytes:
    """A non-streaming Converse response body."""
    if answer.error is not None:
        return b'{"output": {"message": {"role": "assistant", "content": [{"text": "trunc'
    return orjson.dumps(
        {
            "output": {"message": {"role": "assistant", "content": [{"text": answer.text}]}},
            "stopReason": answer.stop,
            "usage": converse_usage(answer.usage),
            "metrics": {"latencyMs": round(answer.latency_ms)},
        }
    )


def converse_error(fault: Fault) -> tuple[int, dict[str, str], bytes]:
    """(status, headers, body) of a pre-byte Converse fault (`connect_reset` is raised, not answered)."""
    kind = fault.kind
    headers: dict[str, str] = {"x-amzn-requestid": "emu-request"}
    if kind == "throttling":
        status, name, message = 429, "ThrottlingException", "Too many requests, please wait before trying again."
        if fault.retry_after_s is not None:
            headers["retry-after"] = str(fault.retry_after_s)
    elif kind == "access_denied":
        status, name, message = 403, "AccessDeniedException", "You don't have access to the model."
    elif kind == "validation":
        status, name, message = 400, "ValidationException", fault.message or PROBE_VALIDATION["documents"]
    elif kind == "model_timeout":
        status, name, message = 408, "ModelTimeoutException", "Model has timed out in processing the request."
    else:
        status, name, message = fault.status, _STATUS_TYPES.get(fault.status, ""), "Service error."
    if name:
        headers["x-amzn-errortype"] = name
    return status, headers, orjson.dumps({"message": message})


def mantle_completion(answer: Completion, top_logprobs: int | None) -> bytes:
    """A non-streaming chat completion body (`logprobs` from the scenario's probabilities)."""
    if answer.error is not None:
        return b'{"id": "chatcmpl-emu", "choices": [{"index": 0, "message": {"content": "tru'
    logprobs: Json | None = None
    if answer.logprobs:
        top = [{"token": t, "logprob": math.log(p), "bytes": list(t.encode())} for t, p in answer.logprobs]
        first = top[0]
        logprobs = {"content": [{**first, "top_logprobs": top[: top_logprobs or len(top)]}]}
    usage = answer.usage or {}
    finish = WIRE_STOP_MANTLE.get(answer.stop, "stop")
    choice = {
        "index": 0,
        "message": {"role": "assistant", "content": answer.text, "refusal": None},
        "logprobs": logprobs,
        "finish_reason": finish,
    }
    body = {
        "id": "chatcmpl-emu",
        "object": "chat.completion",
        "created": 0,
        "model": "emu",
        "choices": [choice],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }
    return orjson.dumps(body)


def mantle_chunk(ev: PlannedEvent, include_usage: bool) -> bytes | None:
    """A planned stream event as one SSE `data:` line (None: nothing on the wire)."""
    if ev.kind == "usage":
        if not include_usage:
            return None
        u = ev.usage or {}
        usage = {
            "prompt_tokens": u.get("input_tokens", 0),
            "completion_tokens": u.get("output_tokens", 0),
            "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0),
        }
        return b"data: " + orjson.dumps({"choices": [], "usage": usage}) + b"\n\n"
    if ev.kind == "error":
        if (ev.error or "").startswith("malformed:"):
            return b'data: {"choices": [{"index": 0, "delta": {"content": "tr\n\n'
        return b"data: " + orjson.dumps({"error": {"message": "stream failed", "type": ev.error}}) + b"\n\n"
    delta: Json = {}
    finish: str | None = None
    if ev.kind == "start":
        delta = {"role": "assistant"}
    elif ev.kind == "reasoning":
        delta = {"reasoning_content": "r" * ev.chars}
    elif ev.kind == "text":
        delta = {"content": ev.text}
    elif ev.kind == "stop":
        finish = WIRE_STOP_MANTLE.get(ev.stop or "end_turn", "stop")
    chunk = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return b"data: " + orjson.dumps(chunk) + b"\n\n"


def golden_json(value: Any) -> Any:
    """Wire params in the golden JSON form (bytes -> {"$bytes_b64": ...})."""
    if isinstance(value, bytes | bytearray):
        return {"$bytes_b64": base64.b64encode(bytes(value)).decode()}
    if isinstance(value, Mapping):
        return {str(k): golden_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [golden_json(v) for v in value]
    return value
