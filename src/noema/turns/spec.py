"""spec0, the decided spec and their comparison (spec-04 §3, §5.1, §7; IMPL-013, IMPL-014, IMPL-015, IMPL-026).

spec0 comes from the session's `sticky_spec` (or `routing.default_profile`), with the request pins applied; the
decided spec comes from the bindings at S1, then the request overrides, then the hard filter. A1 never carries a
conditioning block (IMPL-014).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, cast

from noema.core.protocols.generation import AttemptSpec, Conditioning
from noema.core.values import Effort, JsonValue
from noema.turns import conditioning as cond
from noema.turns.bindings import Evaluation, Tightening, evaluate

if TYPE_CHECKING:
    from noema.core.protocols.config import ConfigSnapshot
    from noema.core.protocols.decision import AnswerSummary, DecisionSnapshot

Selection = Literal["rule", "pinned", "sticky", "default", "fallback"]
AnswerMode = Literal["direct", "cover_top2_and_clarify", "clarify_first"]
NO_CONDITIONING: Final = "none"
_ROUTE_REF: Final = re.compile(r"^intent_routes\[([a-z][a-z0-9_]*)\]$")
_UNSCORED: Final = frozenset({"unavailable", "refusal"})


@dataclass(frozen=True, slots=True)
class Pins:
    """The request overrides of 03:L131: `generation.profile` (not "auto"), `effort`, `max_output_tokens`,
    `conditioning` ("auto" | "off" | an answer mode) and `verify`."""

    generation_profile: str | None = None
    effort: Effort | None = None
    max_output_tokens: int | None = None
    conditioning: str | None = None
    verify: bool | None = None


@dataclass(frozen=True, slots=True)
class Spec0:
    """spec0 and where it came from (`selection` sticky | default | pinned)."""

    spec: AttemptSpec
    selection: Selection
    reason: str


@dataclass(frozen=True, slots=True)
class Decided:
    """The decided spec at S1 (spec-04 §5.1) with its routing facts."""

    spec: AttemptSpec
    conditioning: Conditioning | None
    answer_mode: AnswerMode
    selection: Selection
    reason: str
    evaluation: Evaluation
    basis: tuple[dict[str, JsonValue], ...]
    unavailable: bool


def compute_spec0(
    snapshot: ConfigSnapshot,
    sticky: Mapping[str, JsonValue] | None,
    modalities: Sequence[str],
    pins: Pins,
    usable: Callable[[str], bool],
) -> Spec0:
    """IMPL-014: the sticky spec when it exists for the same modality set and its profile is still usable, else
    `(default_profile, null, null, none)`; then the request pins replace the matching fields."""
    routing = snapshot.routing
    profile, effort, max_tokens = routing.default_profile, None, None
    selection: Selection = "default"
    if sticky is not None and sorted(_strs(sticky.get("modalities"))) == sorted(modalities):
        sticky_profile = sticky.get("generation_profile")
        if isinstance(sticky_profile, str) and usable(sticky_profile):
            profile, selection = sticky_profile, "sticky"
            raw_effort = sticky.get("effort")
            effort = cast("Effort", raw_effort) if isinstance(raw_effort, str) else None
            raw_max = sticky.get("max_output_tokens")
            max_tokens = raw_max if isinstance(raw_max, int) and not isinstance(raw_max, bool) else None
    if pins.generation_profile is not None:
        profile, selection = pins.generation_profile, "pinned"
    if pins.effort is not None:
        effort = pins.effort
    if pins.max_output_tokens is not None:
        max_tokens = pins.max_output_tokens
    return Spec0(AttemptSpec(profile, effort, max_tokens, NO_CONDITIONING), selection, f"spec0 from {selection}")


def _strs(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def same_spec(a: AttemptSpec, b: AttemptSpec, *, include_conditioning: bool) -> bool:
    """AttemptSpec equality over the 4-tuple; without the conditioning signature when
    `restart_on_conditioning_change` is false (IMPL-014)."""
    if include_conditioning:
        return a == b
    return (a.generation_profile, a.effort, a.max_output_tokens) == (
        b.generation_profile,
        b.effort,
        b.max_output_tokens,
    )


def sticky_json(spec: AttemptSpec, answer_mode: AnswerMode, modalities: Sequence[str]) -> dict[str, JsonValue]:
    """The `session.sticky_spec` JSON (spec-04 §7)."""
    mods: list[JsonValue] = []
    mods.extend(sorted(modalities))
    return {
        "generation_profile": spec.generation_profile,
        "effort": spec.effort,
        "max_output_tokens": spec.max_output_tokens,
        "answer_mode": answer_mode,
        "modalities": mods,
    }


def basis_entry(a: AnswerSummary) -> dict[str, JsonValue]:
    """One `RoutingDecision.basis` entry of a scored answer."""
    value, probability = a.top[0]
    return {
        "name": a.name,
        "value": value,
        "probability": round(float(probability), 4),
        "margin": round(float(a.margin or 0.0), 4),
        "band": a.band or "low",
        "status": "calibrated" if a.calibrated else "uncalibrated",
    }


def _basis(
    snapshot: ConfigSnapshot, ev: Evaluation, answers: Mapping[str, AnswerSummary]
) -> tuple[dict[str, JsonValue], ...]:
    seen: list[str] = []
    for i in ev.matched:
        name = snapshot.routing.bindings[i].when.answer
        a = answers.get(name)
        if name not in seen and a is not None and a.type not in _UNSCORED and a.top:
            seen.append(name)
    return tuple(basis_entry(answers[n]) for n in seen)


def _rule_reason(snapshot: ConfigSnapshot, ev: Evaluation, answers: Mapping[str, AnswerSummary]) -> str:
    parts = []
    for i in ev.matched:
        name = snapshot.routing.bindings[i].when.answer
        a = answers.get(name)
        value = a.top[0][0] if a is not None and a.top else None
        shown = ("true" if value else "false") if isinstance(value, bool) else str(value)
        parts.append(f"bindings[{i}]: {name} {shown}")
    return "; ".join(parts)


def _profile_from_bindings(
    snapshot: ConfigSnapshot, ev: Evaluation, answers: Mapping[str, AnswerSummary]
) -> tuple[str, Selection, str]:
    routing = snapshot.routing
    target = ev.decided.get("generation_profile")
    if isinstance(target, str):
        ref = _ROUTE_REF.match(target)
        if ref is None:
            return target, "rule", _rule_reason(snapshot, ev, answers)
        a = answers.get(ref.group(1))
        value = a.top[0][0] if a is not None and a.top else None
        routed = routing.intent_routes.get(str(value)) if value is not None else None
        if routed is not None:
            return routed, "rule", _rule_reason(snapshot, ev, answers)
        return routing.default_profile, "default", f"no intent route for {value!s}: default_profile"
    intent = answers.get("intent")
    if intent is not None and intent.type not in _UNSCORED and intent.band == "low":
        return routing.uncertain_profile, "default", "intent band low: uncertain_profile"
    return routing.default_profile, "default", "no binding set the profile: default_profile"


def decide(
    snapshot: ConfigSnapshot,
    s1: DecisionSnapshot,
    *,
    commit_critical: Sequence[str],
    request_names: Collection[str],
    spec0: Spec0,
    pins: Pins,
    policy: Tightening,
    usable: Callable[[str], bool],
) -> Decided:
    """The decided spec at S1 (spec-04 §3 and §5.1). `commit_critical` in answer order; `usable(profile)` is the
    hard filter (allowed models and context fit). Raises nothing: when the default profile is not usable either,
    the caller reports `no_route_satisfies_constraints`."""
    answers = s1.answers
    ev = evaluate(snapshot.routing.bindings, answers, s1.final_names, policy)
    tight = ev.tight
    if pins.verify:
        tight = tight.tighten("verify_answer", True)
    critical = [answers[n] for n in commit_critical if n in answers]
    unavailable = bool(critical) and all(a.type == "unavailable" for a in critical)
    conditioning: Conditioning | None = None
    answer_mode: AnswerMode = "direct"
    if unavailable:
        spec = spec0.spec
        reason = f"decision unavailable: {critical[0].reason or 'unavailable'}"
        selection: Selection = spec0.selection
        matched_ev = Evaluation({}, tight, ev.matched)
        return Decided(spec, None, "direct", selection, reason, matched_ev, _basis(snapshot, ev, answers), True)
    profile, selection, reason = _profile_from_bindings(snapshot, ev, answers)
    effort = cast("Effort | None", ev.decided.get("effort"))
    raw_max = ev.decided.get("max_output_tokens")
    max_tokens = raw_max if isinstance(raw_max, int) else None
    mode = cast("str | None", ev.decided.get("conditioning"))
    if pins.generation_profile is not None:
        profile, selection, reason = pins.generation_profile, "pinned", "pinned by the request"
    if pins.effort is not None:
        effort = pins.effort
    if pins.max_output_tokens is not None:
        max_tokens = pins.max_output_tokens
    if pins.conditioning == "off":
        mode = None
    elif pins.conditioning in cond.BLOCK_MODES or pins.conditioning == "direct":
        mode = pins.conditioning
    if not usable(profile):
        filtered = profile
        profile, selection = snapshot.routing.default_profile, "default"
        reason = f"{filtered} filtered (allowed models or context window): default_profile"
    if mode in cond.BLOCK_MODES:
        block_mode = cast("cond.BlockMode", mode)
        conditioning = cond.build(block_mode, answers, commit_critical, request_names)
        answer_mode = block_mode
    signature = conditioning.signature if conditioning is not None else NO_CONDITIONING
    spec = AttemptSpec(profile, effort, max_tokens, signature)
    final_ev = Evaluation(ev.decided, tight, ev.matched)
    return Decided(spec, conditioning, answer_mode, selection, reason, final_ev, _basis(snapshot, ev, answers), False)
