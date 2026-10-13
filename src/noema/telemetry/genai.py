"""GenAI semantic-convention attribute mapper (08 §7, IMPL-006, IMPL-029d).

Maps Noema values (`UsageRecord`, Bedrock ids, stop reasons) to `gen_ai.*` span attributes. Names come from the
pinned `opentelemetry-semantic-conventions` package. The cache-write usage attribute is
`gen_ai.usage.cache_creation.input_tokens` when the pinned package defines it (it does in 0.66b1), else the Noema
fallback `aghoo.usage.cache_write.input_tokens` (IMPL-029d).
"""

from __future__ import annotations

from typing import Final, Literal

from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as _semconv

from noema.core.protocols.adapters import StopReason
from noema.core.values import UsageRecord

CACHE_WRITE_FALLBACK: Final = "aghoo.usage.cache_write.input_tokens"
CACHE_WRITE_ATTRIBUTE: Final[str] = str(
    getattr(_semconv, "GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS", CACHE_WRITE_FALLBACK)
)

PROVIDER_NAME: Final[str] = _semconv.GenAiProviderNameValues.AWS_BEDROCK.value  # "aws.bedrock"
PROVIDER: Final[str] = _semconv.GEN_AI_PROVIDER_NAME
OPERATION: Final[str] = _semconv.GEN_AI_OPERATION_NAME
REQUEST_MODEL: Final[str] = _semconv.GEN_AI_REQUEST_MODEL
FINISH_REASONS: Final[str] = _semconv.GEN_AI_RESPONSE_FINISH_REASONS
TIME_TO_FIRST_CHUNK: Final[str] = _semconv.GEN_AI_RESPONSE_TIME_TO_FIRST_CHUNK
CONVERSATION_ID: Final[str] = _semconv.GEN_AI_CONVERSATION_ID
INPUT_TOKENS: Final[str] = _semconv.GEN_AI_USAGE_INPUT_TOKENS
OUTPUT_TOKENS: Final[str] = _semconv.GEN_AI_USAGE_OUTPUT_TOKENS
CACHE_READ_TOKENS: Final[str] = _semconv.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS
REASONING_TOKENS: Final[str] = _semconv.GEN_AI_USAGE_REASONING_OUTPUT_TOKENS

FinishReason = Literal["stop", "length", "content_filter", "other"]
_FINISH: Final[dict[StopReason, FinishReason]] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "content_filter": "content_filter",
    "other": "other",
}


def request_attributes(model_id: str, *, operation: Literal["chat"] = "chat") -> dict[str, str]:
    """`gen_ai.provider.name`, `gen_ai.operation.name` and `gen_ai.request.model` (the Bedrock id)."""
    return {PROVIDER: PROVIDER_NAME, OPERATION: operation, REQUEST_MODEL: model_id}


def usage_attributes(usage: UsageRecord) -> dict[str, int]:
    """Token usage as `gen_ai.usage.*`; input includes cache reads and writes (03:L314), output includes
    reasoning."""
    return {
        INPUT_TOKENS: usage.input_tokens,
        OUTPUT_TOKENS: usage.output_tokens,
        CACHE_READ_TOKENS: usage.cached_input_tokens,
        CACHE_WRITE_ATTRIBUTE: usage.cache_write_input_tokens,
        REASONING_TOKENS: usage.reasoning_tokens,
    }


def finish_reason(stop: StopReason) -> FinishReason:
    """The finish reason of a provider stop (spec-04 §4: end_turn/stop_sequence -> stop, max_tokens -> length)."""
    return _FINISH[stop]


def response_attributes(stop: StopReason | None, *, time_to_first_chunk_s: float | None) -> dict[str, str | float]:
    """`gen_ai.response.finish_reasons` (a single reason; exported as a one-element string array) and
    `gen_ai.response.time_to_first_chunk` (seconds), each only when known."""
    attrs: dict[str, str | float] = {}
    if stop is not None:
        attrs[FINISH_REASONS] = finish_reason(stop)
    if time_to_first_chunk_s is not None:
        attrs[TIME_TO_FIRST_CHUNK] = time_to_first_chunk_s
    return attrs
