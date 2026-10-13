"""`RoutingDecision` payloads (openapi `RoutingDecision`; spec-04 §5.1, §7; IMPL-016).

`route_id` is the profile that serves the attempt (the default profile after an IMPL-016 step-3 fallback);
`generation_profile` is the spec's profile; `fallbacks` lists the profiles fallen back to (Phase 1: at most one).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from noema.core.protocols.generation import AttemptSpec, Conditioning
from noema.core.values import JsonValue
from noema.turns import conditioning as cond

if TYPE_CHECKING:
    from noema.core.protocols.config import ConfigSnapshot


def params_of(spec: AttemptSpec) -> dict[str, JsonValue]:
    """`RoutingDecision.params`: the spec's effort and max_output_tokens when set."""
    out: dict[str, JsonValue] = {}
    if spec.effort is not None:
        out["effort"] = spec.effort
    if spec.max_output_tokens is not None:
        out["max_output_tokens"] = spec.max_output_tokens
    return out


def routing_wire(
    snapshot: ConfigSnapshot,
    spec: AttemptSpec,
    *,
    selection: str,
    strategy: str,
    reason: str,
    decision_id: str | None = None,
    matched: Sequence[str] = (),
    basis: Sequence[dict[str, JsonValue]] = (),
    conditioning: Conditioning | None = None,
    served_spec_differs: bool | None = None,
    route_id: str | None = None,
) -> dict[str, JsonValue]:
    """One `RoutingDecision` (every required field)."""
    served = route_id or spec.generation_profile
    fallbacks: list[JsonValue] = [served] if served != spec.generation_profile else []
    return {
        "route_id": served,
        "generation_profile": spec.generation_profile,
        "mode": snapshot.generation_profile(served).mode,
        "selection": selection,
        "strategy": strategy,
        "decision_id": decision_id,
        "policy_version": str(snapshot.routing.policy_version),
        "matched_bindings": list(matched),
        "basis": [dict(b) for b in basis],
        "params": params_of(spec),
        "conditioning": cond.wire(conditioning),
        "served_spec_differs": served_spec_differs,
        "reason": reason,
        "fallbacks": fallbacks,
    }
