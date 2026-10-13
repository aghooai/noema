"""Tolerant turn models: TurnEvent (a discriminated union with an UnknownEvent fallback) and TurnObject.

Same style as the vendored decision models (`_decision_types`): unknown fields are ignored, unknown enum values stay
plain strings, and an event `type` this SDK doesn't know becomes UnknownEvent (keep reading; never treat it as text).
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import ConfigDict, Discriminator, Field, Tag, TypeAdapter, model_validator

from ._decision_types import DecisionResult, Response, Usage


class Degradation(Response):
    file_id: str | None = None
    part_index: int | None = None
    attempt_id: str | None = None
    model: str | None = None
    from_: Annotated[str, Field(alias="from")]
    to: str
    reason: str


class Conditioning(Response):
    answer_mode: str
    signature: str
    summary: str


class RoutingBasis(Response):
    name: str
    value: str | bool | float
    probability: float
    margin: float
    band: str
    status: str


class RoutingParams(Response):
    effort: str | None = None
    max_output_tokens: int | None = None


class RoutingDecision(Response):
    route_id: str
    generation_profile: str
    mode: Literal["single", "fallback", "race", "best_of_n", "fuse"] | str
    selection: str
    strategy: str
    decision_id: str | None
    policy_version: str
    matched_bindings: list[str]
    basis: list[RoutingBasis]
    params: RoutingParams
    conditioning: Conditioning | None
    served_spec_differs: bool | None
    reason: str
    fallbacks: list[str]


class TurnTimings(Response):
    ttft_ms: int | None
    decision_ms: int | None
    decision_lead_ms: int | None
    holdback_ms: int | None
    e2e_ms: int
    spec_restarts: int
    wasted_output_tokens: int


class TurnCreated(Response):
    type: Literal["turn.created"]
    turn_id: str
    session_id: str | None
    created_at: int


class PreflightCompleted(Response):
    type: Literal["preflight.completed"]
    modalities: list[str]
    est_input_tokens: int
    degradations: list[Degradation]


class RouteSelected(Response):
    type: Literal["route.selected"]
    attempt_id: str
    routing: RoutingDecision
    speculative: bool
    degradations: list[Degradation]


class DecisionCompleted(Response):
    type: Literal["decision.completed"]
    decision: DecisionResult
    arrived: str


class DecisionUpdated(Response):
    type: Literal["decision.updated"]
    decision: DecisionResult
    changed: list[str]


class RouteConfirmed(Response):
    type: Literal["route.confirmed"]
    attempt_id: str
    routing: RoutingDecision


class GenerationStarted(Response):
    type: Literal["generation.started"]
    attempt_id: str
    route_id: str
    model: str
    speculative: bool
    degradations: list[Degradation]


class GenerationDelta(Response):
    type: Literal["generation.delta"]
    attempt_id: str
    seq: int
    text: str


class GenerationCommitted(Response):
    type: Literal["generation.committed"]
    attempt_id: str
    reason: str


class GenerationCancelled(Response):
    type: Literal["generation.cancelled"]
    attempt_id: str
    reason: str


class GenerationRerouted(Response):
    type: Literal["generation.rerouted"]
    from_attempt_id: str
    to_attempt_id: str
    reason: str
    discard_from_seq: int | None


class GenerationCompleted(Response):
    type: Literal["generation.completed"]
    attempt_id: str
    finish_reason: str
    usage: Usage


class TurnVerified(Response):
    type: Literal["turn.verified"]
    verification: DecisionResult


class TurnCompleted(Response):
    type: Literal["turn.completed"]
    turn_id: str
    usage: Usage
    timings: TurnTimings


class ErrorEvent(Response):
    """An error after the stream started; `turn.completed` still follows. Codes are an open enum."""

    type: Literal["error"]
    code: str
    message: str
    retryable: bool


class UnknownEvent(Response):
    """An event type this SDK version doesn't know (a later additive change): keep reading, ignore it."""

    model_config = ConfigDict(extra="allow", frozen=True)
    type: Literal["unknown"]
    raw_type: str
    raw: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _wrap(cls, value: Any) -> Any:
        if isinstance(value, dict) and "raw_type" not in value:
            return {"type": "unknown", "raw_type": str(value.get("type")), "raw": dict(value)}
        return value


_KNOWN: Final[dict[str, type[Response]]] = {
    "turn.created": TurnCreated,
    "preflight.completed": PreflightCompleted,
    "route.selected": RouteSelected,
    "decision.completed": DecisionCompleted,
    "decision.updated": DecisionUpdated,
    "route.confirmed": RouteConfirmed,
    "generation.started": GenerationStarted,
    "generation.delta": GenerationDelta,
    "generation.committed": GenerationCommitted,
    "generation.cancelled": GenerationCancelled,
    "generation.rerouted": GenerationRerouted,
    "generation.completed": GenerationCompleted,
    "turn.verified": TurnVerified,
    "turn.completed": TurnCompleted,
    "error": ErrorEvent,
}


def _event_tag(value: Any) -> str:
    kind = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    return kind if isinstance(kind, str) and kind in _KNOWN else "unknown"


TurnEvent = Annotated[
    Annotated[TurnCreated, Tag("turn.created")]
    | Annotated[PreflightCompleted, Tag("preflight.completed")]
    | Annotated[RouteSelected, Tag("route.selected")]
    | Annotated[DecisionCompleted, Tag("decision.completed")]
    | Annotated[DecisionUpdated, Tag("decision.updated")]
    | Annotated[RouteConfirmed, Tag("route.confirmed")]
    | Annotated[GenerationStarted, Tag("generation.started")]
    | Annotated[GenerationDelta, Tag("generation.delta")]
    | Annotated[GenerationCommitted, Tag("generation.committed")]
    | Annotated[GenerationCancelled, Tag("generation.cancelled")]
    | Annotated[GenerationRerouted, Tag("generation.rerouted")]
    | Annotated[GenerationCompleted, Tag("generation.completed")]
    | Annotated[TurnVerified, Tag("turn.verified")]
    | Annotated[TurnCompleted, Tag("turn.completed")]
    | Annotated[ErrorEvent, Tag("error")]
    | Annotated[UnknownEvent, Tag("unknown")],
    Discriminator(_event_tag),
]
TURN_EVENT: Final[TypeAdapter[TurnEvent]] = TypeAdapter(TurnEvent)


def parse_event(obj: Any) -> TurnEvent:
    """Decoded SSE `data` -> TurnEvent. `generation.delta`, the per-token hot path, is built without validation."""
    if isinstance(obj, dict) and obj.get("type") == "generation.delta":
        attempt_id, seq, text = obj.get("attempt_id"), obj.get("seq"), obj.get("text")
        if isinstance(attempt_id, str) and type(seq) is int and isinstance(text, str):
            return GenerationDelta.model_construct(type="generation.delta", attempt_id=attempt_id, seq=seq, text=text)
    return TURN_EVENT.validate_python(obj)


class TurnAttempt(Response):
    attempt_id: str
    route_id: str
    model: str
    outcome: Literal["committed", "cancelled", "failed", "not_selected"] | str
    usage: Usage


class TurnObject(Response):
    """A non-streaming or stored turn: the server-side reconciliation of its events."""

    turn_id: str
    session_id: str | None
    seq: int | None
    text: str
    decision: DecisionResult | None
    routing: RoutingDecision
    attempts: list[TurnAttempt]
    verification: DecisionResult | None
    degradations: list[Degradation]
    usage: Usage
    timings: TurnTimings
