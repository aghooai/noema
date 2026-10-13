"""The frozen `ModelRequest` -> wire mapping (spec-02 §4.2; goldens in `tests/golden/{decision,generation}_requests/`)
and the response normalization shared by both adapters (stop reasons, refusals, usage).

Pre-wire rules (`check_request`) reject, before any credential lookup or byte, what Bedrock would reject or what
the frozen table forbids: logprobs on Converse, media on Mantle chat, `temperature` for a model whose
`controls.temperature` is false, an effort outside `controls.effort`, a modality the model lacks, `top_logprobs`
above `controls.top_logprobs_max`, `max_tokens` outside `1..max_output_tokens_cap`. Never sent: `textFormat`
(IMPL-054), `cachePoint` (IMPL-017), `additionalModelRequestFields`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Final, Literal

from noema.config.models import ModelCfg
from noema.core.errors import ClassifiedError, ErrorKind
from noema.core.protocols.adapters import AdapterError, ModelRequest, StopReason
from noema.core.values import ContentBlock, DocumentBlock, ImageBlock, TextBlock, UsageRecord

AdapterKind = Literal["bedrock-converse", "bedrock-mantle-chat"]
Json = dict[str, Any]
CHARS_PER_REASONING_TOKEN: Final = 4.0  # IMPL-029c constant, used for the reasoning-token breakdown estimate

CONVERSE_STOP: Final[Mapping[str, StopReason]] = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
    "model_context_window_exceeded": "max_tokens",
}
CONVERSE_REFUSALS: Final = frozenset({"content_filtered", "guardrail_intervened"})
MANTLE_STOP: Final[Mapping[str, StopReason]] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "content_filter",
}


def invalid(message: str) -> AdapterError:
    """A pre-wire rejection (`invalid_request`, no provider code: nothing was sent)."""
    return AdapterError(ClassifiedError(ErrorKind.INVALID_REQUEST, message))


def check_request(req: ModelRequest, model: ModelCfg, kind: AdapterKind) -> None:
    """Raise `AdapterError(invalid_request)` for a request the frozen mapping or the model cannot take."""
    if model.adapter != kind:
        raise invalid("the model is served by another adapter")
    if not req.messages:
        raise invalid("a request needs at least one message")
    cap = model.max_output_tokens_cap
    if req.max_tokens < 1 or (cap is not None and req.max_tokens > cap):
        raise invalid("max_tokens is outside the model's range")
    controls = model.controls
    if req.temperature is not None and not controls.temperature:
        raise invalid("the model does not accept temperature")
    if req.effort is not None and req.effort not in controls.effort:
        raise invalid("the model does not accept this effort")
    if kind == "bedrock-converse":
        if req.logprobs or req.top_logprobs is not None:
            raise invalid("logprobs are not available on Converse")
        for message in req.messages:
            for block in message.content:
                if isinstance(block, TextBlock):
                    continue
                needed = "image" if isinstance(block, ImageBlock) else "document"
                if needed not in model.modalities:
                    raise invalid(f"the model does not accept {needed} blocks")
                if message.role != "user":
                    raise invalid("media blocks are allowed only in user messages")
        return
    if any(not isinstance(b, TextBlock) for m in req.messages for b in m.content):
        raise invalid("Mantle chat takes text blocks only")
    if req.logprobs and not controls.logprobs:
        raise invalid("the model does not return logprobs")
    if req.top_logprobs is not None:
        top_max = controls.top_logprobs_max
        if not req.logprobs or req.top_logprobs < 0 or (top_max is not None and req.top_logprobs > top_max):
            raise invalid("top_logprobs is outside the model's range")


# --- Converse ------------------------------------------------------------------------------------------------------
def _converse_block(block: ContentBlock) -> Json:
    if isinstance(block, TextBlock):
        return {"text": block.text}
    if isinstance(block, ImageBlock):
        return {"image": {"format": block.format, "source": {"bytes": block.data}}}
    assert isinstance(block, DocumentBlock)
    return {"document": {"format": block.format, "name": block.name, "source": {"bytes": block.data}}}


def converse_params(req: ModelRequest, model: ModelCfg) -> Json:
    """The Converse / ConverseStream call parameters (identical for both operations; bytes stay raw, botocore
    base64-encodes them)."""
    inference: Json = {"maxTokens": req.max_tokens}
    if req.temperature is not None:
        inference["temperature"] = req.temperature
    if req.stop:
        inference["stopSequences"] = list(req.stop)
    params: Json = {
        "modelId": model.id,
        "messages": [{"role": m.role, "content": [_converse_block(b) for b in m.content]} for m in req.messages],
        "inferenceConfig": inference,
    }
    if req.system:
        params["system"] = [{"text": s} for s in req.system]
    if req.effort is not None:
        params["outputConfig"] = {"effort": req.effort}
    return params


# --- Mantle chat ---------------------------------------------------------------------------------------------------
def mantle_body(req: ModelRequest, model: ModelCfg, *, stream: bool) -> Json:
    """The `POST {base}/chat/completions` JSON body."""
    messages: list[Json] = [{"role": "system", "content": "".join(req.system)}] if req.system else []
    messages.extend(
        {"role": m.role, "content": "".join(b.text for b in m.content if isinstance(b, TextBlock))}
        for m in req.messages
    )
    body: Json = {"model": model.id, "messages": messages, "max_tokens": req.max_tokens}
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.logprobs:
        body["logprobs"] = True
        if req.top_logprobs is not None:  # spec-02 §4.2 sends `"top_logprobs": n`, never null
            body["top_logprobs"] = req.top_logprobs
    if req.stop:
        body["stop"] = list(req.stop)
    body["stream"] = stream
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


# --- usage ---------------------------------------------------------------------------------------------------------
def _int(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def reasoning_estimate(reasoning_chars: int, output_tokens: int, *, hidden_text_chars: int | None = None) -> int:
    """Reasoning tokens from counted reasoning characters (Bedrock reports no breakdown), capped at the output.
    `hidden_text_chars`: the answer's text characters when Bedrock hid reasoning (a signature-only or
    `redactedContent` block, whose characters cannot be counted): then at least the output left after the text's
    estimated tokens (Wave E: live Haiku and Sol reported 0)."""
    counted = min(output_tokens, math.ceil(reasoning_chars / CHARS_PER_REASONING_TOKEN)) if reasoning_chars else 0
    if hidden_text_chars is None:
        return counted
    return max(counted, output_tokens - min(output_tokens, math.ceil(hidden_text_chars / CHARS_PER_REASONING_TOKEN)))


def converse_usage(
    raw: Mapping[str, Any], reasoning_chars: int, *, hidden_text_chars: int | None = None
) -> UsageRecord:
    """Converse `usage`: input = inputTokens + cacheReadInputTokens + cacheWriteInputTokens (03:L314); output
    includes reasoning (`reasoning_estimate`)."""
    cached, written, output = (
        _int(raw, "cacheReadInputTokens"),
        _int(raw, "cacheWriteInputTokens"),
        _int(raw, "outputTokens"),
    )
    return UsageRecord(
        input_tokens=_int(raw, "inputTokens") + cached + written,
        output_tokens=output,
        cached_input_tokens=cached,
        cache_write_input_tokens=written,
        reasoning_tokens=reasoning_estimate(reasoning_chars, output, hidden_text_chars=hidden_text_chars),
    )


def mantle_usage(raw: Mapping[str, Any], reasoning_chars: int) -> UsageRecord:
    """Mantle `usage`: prompt_tokens (cache reads included), completion_tokens, prompt_tokens_details.cached_tokens
    and completion_tokens_details.reasoning_tokens when present."""
    output = _int(raw, "completion_tokens")
    prompt_details = raw.get("prompt_tokens_details")
    completion_details = raw.get("completion_tokens_details")
    cached = _int(prompt_details, "cached_tokens") if isinstance(prompt_details, Mapping) else 0
    reasoning = _int(completion_details, "reasoning_tokens") if isinstance(completion_details, Mapping) else 0
    return UsageRecord(
        input_tokens=_int(raw, "prompt_tokens"),
        output_tokens=output,
        cached_input_tokens=cached,
        reasoning_tokens=min(output, reasoning) if reasoning else reasoning_estimate(reasoning_chars, output),
    )
