"""TurnEvent payload constructors for the golden SSE fixtures (openapi `TurnEvent` oneOf, rev 6).

Ids are normalized (spec-04 §4.2): `turn_1`, `att_N` in start order, `dec_1` (router), `dec_2` (verifier),
`ses_1`. Every constructor returns a complete, schema-valid payload; `Ev` adds the virtual time (`at`, ms from
t_d0) and the dotted paths the orchestrator tests compare exactly (`exact`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from tests.fakes.clock import DEFAULT_WALL

from noema.core.errors import ERROR_TABLE, ErrorCode

CREATED_AT: Final = int(DEFAULT_WALL.timestamp())  # the FakeClock epoch, 2026-10-10T12:00:00Z
MODEL_IDS: Final[Mapping[str, str]] = {
    "sol-6.1": "us.openai.gpt-6.1-sol",
    "opus-5.5": "us.anthropic.claude-opus-5-5",
    "glm-5.3": "us.zai.glm-5.3",
}
PROFILE_MODES: Final[Mapping[str, str]] = {
    "sol-fast": "single",
    "opus-deep": "fallback",
    "glm-reason": "fallback",
    "race-sol-glm": "race",
}
Json = dict[str, Any]


@dataclass
class Ev:
    """One expected frame: `at` ms from t_d0, the payload, and the paths compared exactly."""

    at: float
    data: Json
    exact: tuple[str, ...] = ()
    note: str | None = None

    @property
    def type(self) -> str:
        return str(self.data["type"])


def usage(input_tokens: int = 0, output_tokens: int = 0, *, cost: float = 0.0, estimated: bool = False) -> Json:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": round(cost, 6),
        "cost_estimated": True,
        "tokens_estimated": estimated,
    }


def add_usage(*parts: Mapping[str, Any]) -> Json:
    out = usage()
    for p in parts:
        for k in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "reasoning_tokens",
        ):
            out[k] += int(p[k])
        out["cost_usd"] = round(out["cost_usd"] + float(p["cost_usd"]), 6)
        out["tokens_estimated"] = out["tokens_estimated"] or bool(p["tokens_estimated"])
    return out


def tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


@dataclass
class Routing:
    """A `RoutingDecision` (every required field; Phase-1 values)."""

    route_id: str
    selection: str
    decision_id: str | None = None
    matched_bindings: Sequence[str] = ()
    basis: Sequence[Json] = ()
    params: Mapping[str, Any] = field(default_factory=dict)
    served_spec_differs: bool | None = None
    reason: str = ""
    strategy: str = "holdback"
    conditioning: Json | None = None

    def wire(self) -> Json:
        return {
            "route_id": self.route_id,
            "generation_profile": self.route_id,
            "mode": PROFILE_MODES[self.route_id],
            "selection": self.selection,
            "strategy": self.strategy,
            "decision_id": self.decision_id,
            "policy_version": "1",
            "matched_bindings": list(self.matched_bindings),
            "basis": list(self.basis),
            "params": dict(self.params),
            "conditioning": self.conditioning,
            "served_spec_differs": self.served_spec_differs,
            "reason": self.reason,
            "fallbacks": [],
        }


def basis(name: str, value: Any, probability: float, margin: float, band: str = "high") -> Json:
    return {
        "name": name,
        "value": value,
        "probability": probability,
        "margin": margin,
        "band": band,
        "status": "uncalibrated",
    }


def turn_created(session_id: str | None = "ses_1") -> Json:
    return {"type": "turn.created", "turn_id": "turn_1", "session_id": session_id, "created_at": CREATED_AT}


def preflight(est_input_tokens: int) -> Json:
    return {
        "type": "preflight.completed",
        "modalities": ["text"],
        "est_input_tokens": est_input_tokens,
        "degradations": [],
    }


def route_selected(attempt: str, routing: Routing, speculative: bool = True) -> Json:
    return {
        "type": "route.selected",
        "attempt_id": attempt,
        "routing": routing.wire(),
        "speculative": speculative,
        "degradations": [],
    }


def route_confirmed(attempt: str, routing: Routing) -> Json:
    return {"type": "route.confirmed", "attempt_id": attempt, "routing": routing.wire()}


def started(attempt: str, route_id: str, alias: str, speculative: bool) -> Json:
    return {
        "type": "generation.started",
        "attempt_id": attempt,
        "route_id": route_id,
        "model": MODEL_IDS[alias],
        "speculative": speculative,
        "degradations": [],
    }


def committed(attempt: str, reason: str) -> Json:
    return {"type": "generation.committed", "attempt_id": attempt, "reason": reason}


def cancelled(attempt: str, reason: str) -> Json:
    return {"type": "generation.cancelled", "attempt_id": attempt, "reason": reason}


def delta(attempt: str, seq: int, text: str) -> Json:
    return {"type": "generation.delta", "attempt_id": attempt, "seq": seq, "text": text}


def completed(attempt: str, finish_reason: str, use: Json) -> Json:
    return {"type": "generation.completed", "attempt_id": attempt, "finish_reason": finish_reason, "usage": use}


def decision_completed(result: Json, arrived: str) -> Json:
    return {"type": "decision.completed", "decision": result, "arrived": arrived}


def decision_updated(result: Json, changed: Sequence[str]) -> Json:
    return {"type": "decision.updated", "decision": result, "changed": list(changed)}


def turn_verified(result: Json) -> Json:
    return {"type": "turn.verified", "verification": result}


def error(code: ErrorCode, message: str) -> Json:
    return {"type": "error", "code": code.value, "message": message, "retryable": ERROR_TABLE[code].retryable}


def turn_completed(
    use: Json,
    *,
    ttft_ms: int | None,
    decision_ms: int | None,
    decision_lead_ms: int | None,
    holdback_ms: int | None,
    e2e_ms: int,
    spec_restarts: int = 0,
    wasted_output_tokens: int = 0,
) -> Json:
    return {
        "type": "turn.completed",
        "turn_id": "turn_1",
        "usage": use,
        "timings": {
            "ttft_ms": ttft_ms,
            "decision_ms": decision_ms,
            "decision_lead_ms": decision_lead_ms,
            "holdback_ms": holdback_ms,
            "e2e_ms": e2e_ms,
            "spec_restarts": spec_restarts,
            "wasted_output_tokens": wasted_output_tokens,
        },
    }
