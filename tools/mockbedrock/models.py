"""The model catalog of the wire mock: per-model fidelity rules from `.orchestration/context/03-env-probes-gates.md`
and `probes/results/*.json` (spec-10 §2.2). Probe strings are verbatim; strings the probes did not capture are
marked `MOCK` and documented in docs/modules/qa-tools.md."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

Family = Literal["anthropic", "openai", "zai", "deepseek"]
EFFORTS_ALL: Final = frozenset({"none", "low", "medium", "high", "xhigh", "max"})
EFFORTS_NO_NONE: Final = frozenset({"low", "medium", "high", "xhigh", "max"})

# --- Converse ValidationException texts (probe strings unless marked MOCK) -------------------------------------------
TEMPERATURE_DEPRECATED: Final = "The model returned the following errors: `temperature` is deprecated for this model."
TEMPERATURE_UNSUPPORTED: Final = "This model doesn't support the temperature field. Remove temperature and try again."
VIDEO_UNSUPPORTED: Final = (
    "This model doesn't support the video content block that you provided. Update the content block and try again."
)
IMAGE_UNSUPPORTED: Final = (
    "This model doesn't support the image content block that you provided. Update the content block and try again."
)
DOCUMENTS_UNSUPPORTED: Final = "This model doesn't support documents."
INVALID_MODEL_ID: Final = "The provided model identifier is invalid."
REASONING_EFFORT_UNKNOWN: Final = (
    'The model returned the following errors: {"error":{"code":"unknown_parameter","message":"Unknown parameter: '
    '\'reasoning_effort\'.","param":"reasoning_effort","type":"invalid_request_error"}}'
)
_KEYS_ANTHROPIC: Final = (
    "text",
    "image",
    "toolUse",
    "toolResult",
    "document",
    "video",
    "cachePoint",
    "reasoningContent",
    "citationsContent",
    "searchResult",
)
_KEYS_ZAI: Final = tuple(k for k in _KEYS_ANTHROPIC if k != "citationsContent")
_KEYS_OTHER: Final = tuple(k for k in _KEYS_ZAI if k != "cachePoint")


def anthropic_effort_error(value: str) -> str:
    """Opus/Haiku/Sonnet: an effort outside low..max (probe text, `none`)."""
    return (
        f"The model returned the following errors: unknown variant `{value}`, expected one of `low`, `medium`, "
        "`high`, `xhigh`, `max`, `Unhandled` at line 1 column 163"
    )


def openai_effort_error(value: str, model_id: str, supported: tuple[str, ...]) -> str:
    """Sol/Luna/Astra: an unsupported effort (probe text; Sol's tail was truncated in the probe summary)."""
    listed = ", ".join(f"'{s}'" for s in supported[:-1]) + f", and '{supported[-1]}'"
    inner = (
        f'{{"error":{{"code":"unsupported_value","message":"Unsupported value: \'{value}\' is not supported with the '
        f'\'{model_id}\' model. Supported values are: {listed}.","param":"reasoning.effort",'
        '"type":"invalid_request_error"}}'
    )
    return "The model returned the following errors: " + inner


def zai_effort_error(value: str, supported: tuple[str, ...]) -> str:
    """GLM: an unsupported effort (live text, Wave E live smoke 2026-10-11: `none` is rejected, param
    `reasoning_effort`, no model id in the message)."""
    listed = ", ".join(f"'{s}'" for s in supported[:-1]) + f", and '{supported[-1]}'"
    inner = (
        f'{{"error":{{"code":"unsupported_value","message":"Unsupported value: \'{value}\' is not supported with '
        f'this model. Supported values are: {listed}.","param":"reasoning_effort","type":"invalid_request_error"}}}}'
    )
    return "The model returned the following errors: " + inner


@dataclass(frozen=True, slots=True)
class ConverseModel:
    """Converse-side behavior of one Bedrock model id."""

    family: Family
    temperature_error: str | None
    efforts: frozenset[str] | None  # None: effort is not validated
    effort_order: tuple[str, ...]
    images: bool
    documents: bool
    block_keys: tuple[str, ...]
    cache_usage: bool  # Converse usage carries cacheRead/WriteInputTokens
    signature: bool  # reasoning blocks end with a signature delta (Claude)

    def effort_error(self, value: str, model_id: str) -> str | None:
        """The ValidationException text for `outputConfig.effort = value`, or None if accepted."""
        if self.efforts is None or value in self.efforts:
            return None
        if self.family == "anthropic":
            return anthropic_effort_error(value)
        if self.family == "zai":
            return zai_effort_error(value, self.effort_order)
        return openai_effort_error(value, model_id, self.effort_order)

    def block_keys_error(self, i: int, j: int) -> str:
        """The service error for a content block with no supported key (e.g. `audio`)."""
        return (
            f"ContentBlock object at messages.{i}.content.{j} must set one of the following keys: "
            + ", ".join(self.block_keys)
            + "."
        )


_ORDER_ALL: Final = ("none", "low", "medium", "high", "xhigh", "max")
_ORDER_NO_NONE: Final = ("low", "medium", "high", "xhigh", "max")
_ANTHROPIC: Final = ConverseModel(
    "anthropic", TEMPERATURE_DEPRECATED, EFFORTS_NO_NONE, _ORDER_NO_NONE, True, True, _KEYS_ANTHROPIC, True, True
)
_LUNA: Final = ConverseModel(
    "openai", TEMPERATURE_UNSUPPORTED, EFFORTS_ALL, _ORDER_ALL, True, False, _KEYS_OTHER, True, False
)
_SOL: Final = ConverseModel(
    "openai", TEMPERATURE_UNSUPPORTED, EFFORTS_NO_NONE, _ORDER_NO_NONE, True, False, _KEYS_OTHER, True, False
)
# GLM-5.3 rejected `none` on 2026-10-11 (accepted on the 2026-10-09 probes; live cassettes 01/03 of
# tests/cassettes/live-20261011T073404), and accepts low..max including xhigh.
_GLM: Final = ConverseModel("zai", None, EFFORTS_NO_NONE, _ORDER_NO_NONE, False, False, _KEYS_ZAI, True, False)
_DEEPSEEK: Final = ConverseModel("deepseek", None, None, (), False, False, _KEYS_OTHER, False, False)
# (base id prefix after the inference-profile region, spec)
_CONVERSE: Final[tuple[tuple[str, ConverseModel], ...]] = (
    ("anthropic.claude-", _ANTHROPIC),
    ("openai.gpt-6-luna", _LUNA),
    ("openai.gpt-5.6-luna", _LUNA),
    ("openai.gpt-6.1-sol", _SOL),
    ("openai.gpt-6-astra", _SOL),
    ("zai.glm-", _GLM),
    ("deepseek.v3", _DEEPSEEK),
)
_PROFILE_PREFIXES: Final = ("us.", "global.", "eu.", "apac.")


def base_model_id(model_id: str) -> str:
    """The foundation-model id of an inference-profile id (`us.anthropic.x` -> `anthropic.x`)."""
    for prefix in _PROFILE_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix) :]
    return model_id


def converse_model(model_id: str) -> ConverseModel | None:
    """The Converse rules of `model_id`, or None for an unknown model (ValidationException)."""
    base = base_model_id(model_id)
    for prefix, spec in _CONVERSE:
        if base.startswith(prefix):
            return spec
    return None


# --- Mantle (OpenAI-compatible) catalog: `GET /v1/models` of us-east-1 (probes/results/capabilities.json) ----------
MANTLE_MODELS: Final[frozenset[str]] = frozenset(
    {
        "anthropic.claude-fable-5",
        "anthropic.claude-haiku-4-5",
        "anthropic.claude-haiku-5-5",
        "anthropic.claude-opus-4-7",
        "anthropic.claude-opus-4-8",
        "anthropic.claude-opus-5",
        "anthropic.claude-opus-5-5",
        "anthropic.claude-sonnet-5",
        "anthropic.claude-sonnet-5-5",
        "deepseek.v3.1",
        "deepseek.v3.2",
        "google.gemma-3-12b-it",
        "google.gemma-3-27b-it",
        "google.gemma-3-4b-it",
        "google.gemma-4-26b-a4b",
        "google.gemma-4-31b",
        "google.gemma-4-e2b",
        "minimax.minimax-m2",
        "minimax.minimax-m2.1",
        "minimax.minimax-m2.5",
        "mistral.devstral-2-123b",
        "mistral.magistral-small-2509",
        "mistral.mistral-large-3-675b-instruct",
        "moonshotai.kimi-k2-thinking",
        "moonshotai.kimi-k2.5",
        "nvidia.nemotron-super-3-120b",
        "openai.gpt-5.4",
        "openai.gpt-5.5",
        "openai.gpt-5.6-luna",
        "openai.gpt-5.6-sol",
        "openai.gpt-6-astra",
        "openai.gpt-6-luna",
        "openai.gpt-6-sol",
        "openai.gpt-6.1-sol",
        "openai.gpt-oss-120b",
        "openai.gpt-oss-20b",
        "qwen.qwen3-235b-a22b-2507",
        "qwen.qwen3-32b",
        "xai.grok-4.3",
        "zai.glm-4.6",
        "zai.glm-4.7",
        "zai.glm-5",
    }
)
# Models whose chat-completions route answers "isn't supported on this route" (probe: Luna, gemma-4-26b).
_CHAT_ROUTE_REJECTED: Final = frozenset({"openai.gpt-6-luna", "openai.gpt-5.6-luna", "google.gemma-4-26b-a4b"})
# Models only reachable through `/openai/v1/responses` (Luna, us-east-1; probes/results/luna_routes.json).
RESPONSES_MODELS: Final = frozenset({"openai.gpt-6-luna", "openai.gpt-5.6-luna"})
LUNA_EFFORTS: Final = ("none", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True, slots=True)
class MantleReject:
    """An OpenAI-compatible error answer: HTTP status, `error.code`, `error.message`."""

    status: int
    code: str | None
    message: str
    param: str | None = None


def not_found(model: str) -> MantleReject:
    return MantleReject(404, "not_found_error", f"The model '{model}' does not exist")


def chat_route_error(model: str) -> MantleReject | None:
    """Why `POST /v1/chat/completions` rejects `model` (None: the model answers)."""
    if model not in MANTLE_MODELS:
        return not_found(model)
    if model.startswith("anthropic."):
        return MantleReject(
            400, "validation_error", f"The model '{model}' does not support the '/v1/chat/completions' API"
        )
    if model in _CHAT_ROUTE_REJECTED:
        return MantleReject(400, "validation_error", f"model `{model}` isn't supported on this route")
    return None


def responses_route_error(model: str, *, openai_prefix: bool) -> MantleReject | None:
    """Why the Responses API rejects `model`: only Luna answers, and only on `/openai/v1/responses`."""
    if model not in MANTLE_MODELS:
        return not_found(model)
    if openai_prefix and model in RESPONSES_MODELS:
        return None
    return MantleReject(400, "validation_error", f"The model '{model}' does not support the '/v1/responses' API")


DECISIONS_REJECT: Final = MantleReject(
    400, "validation_error", "The requested model is not supported in the Decisions API."
)
LOGPROBS_REASONING: Final = MantleReject(
    400, "unsupported_parameter", "logprobs are not supported with reasoning models.", "top_logprobs"
)


def luna_effort_error(value: str, model: str) -> MantleReject:
    listed = ", ".join(f"'{s}'" for s in LUNA_EFFORTS[:-1]) + f", and '{LUNA_EFFORTS[-1]}'"
    return MantleReject(
        400,
        "unsupported_value",
        f"Unsupported value: '{value}' is not supported with the '{model}' model. Supported values are: {listed}.",
        "reasoning.effort",
    )
