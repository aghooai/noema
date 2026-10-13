"""Pure per-attempt rules: request parameters, error mapping, usage estimates and the wire `Usage` object.

- `effective_effort`: the profile-level effort override (IMPL-015);
- `attempt_max_tokens`: `ModelRequest.max_tokens` (spec-04 §5.1, IMPL-084);
- `walk_error`: the request code of a failure kind (spec-04 §6 step 4, IMPL-076);
- `estimate_usage`: the cancelled-call estimate (IMPL-020);
- `throttle_backoff_s`: the full-jitter wait of the throttling retry (IMPL-016);
- `usage_wire`: the openapi `Usage` object.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal

from noema.config.models import GEN_DEFAULT_MAX_TOKENS
from noema.core.errors import ErrorCode, ErrorKind
from noema.core.protocols.adapters import StopReason
from noema.core.values import Cost, Degradation, Effort, JsonValue, UsageRecord

if TYPE_CHECKING:
    from noema.config.models import GenMemberCfg, ModelCfg

FinishReason = Literal["stop", "length", "content_filter"]
FINISH: Final[Mapping[StopReason, FinishReason]] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "content_filter": "content_filter",
    "other": "stop",
}
THROTTLE_BASE_S: Final = 0.5  # full jitter uniform(0, 0.5 s · 2^k) (spec-04 §6, IMPL-016)
_MICRO: Final = Decimal("0.000001")


def effective_effort(
    override: Effort | None, member: GenMemberCfg, model: ModelCfg
) -> tuple[Effort | None, Degradation | None]:
    """The effort an attempt sends and the `param_unsupported` degradation when it differs from the request
    (IMPL-015). `override` None: the member's configured effort. A value the model does not accept is never sent:
    the member's own effort (or the model default) is used instead and recorded as `from: effort:<asked>`,
    `to: effort:<used | default>`."""
    accepted = model.controls.effort
    wanted = override if override is not None else member.effort
    if wanted is None or wanted in accepted:
        return wanted, None
    fallback = member.effort if member.effort is not None and member.effort in accepted else None
    to = f"effort:{fallback}" if fallback is not None else "effort:default"
    return fallback, Degradation(from_=f"effort:{wanted}", to=to, reason="param_unsupported")


def attempt_max_tokens(spec_max: int | None, member: GenMemberCfg, model: ModelCfg) -> int:
    """`min(cap, (spec.max_output_tokens ?? member.max_output_tokens ?? cap ?? 4096) + reasoning_reserve_tokens)`
    with `cap = models.<alias>.max_output_tokens_cap` (no cap: no `min`)."""
    cap = model.max_output_tokens_cap
    base = next(
        (v for v in (spec_max, member.max_output_tokens, cap) if v is not None),
        GEN_DEFAULT_MAX_TOKENS,
    )
    total = base + model.reasoning_reserve_tokens
    return total if cap is None else min(cap, total)


def walk_error(kind: ErrorKind | None) -> ErrorCode:
    """The request code of the last pre-commit failure (IMPL-076): throttled and retryable (timeouts and text-less
    stops included) -> all_routes_unavailable; tenant_access -> tenant_access_misconfigured; fatal and
    invalid_request -> internal_error. None (no attempt could start) -> all_routes_unavailable."""
    if kind is None or kind in (ErrorKind.THROTTLED, ErrorKind.RETRYABLE):
        return ErrorCode.ALL_ROUTES_UNAVAILABLE
    if kind is ErrorKind.TENANT_ACCESS:
        return ErrorCode.TENANT_ACCESS_MISCONFIGURED
    return ErrorCode.INTERNAL_ERROR


def estimate_usage(
    *, input_tokens: int, text_chars: int, reasoning_chars: int, chars_per_token: float, overrun_tokens: int
) -> UsageRecord:
    """IMPL-020: input = the TokenEstimator estimate of the prompt; output = counted text plus reasoning
    (chars ÷ chars-per-token, each rounded up) plus the overrun allowance; `tokens_estimated` is true."""
    reasoning = math.ceil(reasoning_chars / chars_per_token)
    text = math.ceil(text_chars / chars_per_token)
    return UsageRecord(
        input_tokens=input_tokens,
        output_tokens=text + reasoning + overrun_tokens,
        reasoning_tokens=reasoning,
        tokens_estimated=True,
    )


def throttle_backoff_s(retry_after_s: float | None, failure_attempts: int, rng: random.Random) -> float:
    """The provider's `retry_after_s`, else full jitter `uniform(0, 0.5 s · 2^k)` with k = failure-driven attempts
    so far (IMPL-016)."""
    if retry_after_s is not None:
        return max(0.0, retry_after_s)
    return rng.uniform(0.0, THROTTLE_BASE_S * (2**failure_attempts))


def usage_wire(usage: UsageRecord, cost: Cost) -> dict[str, JsonValue]:
    """The openapi `Usage` object (cost as a JSON number with 6 decimals)."""
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "cost_usd": float(cost.usd.quantize(_MICRO)),
        "cost_estimated": cost.estimated,
        "tokens_estimated": usage.tokens_estimated,
    }
