"""`WorkGraphPlanner` (spec-11 §1.1): the worst-case calls and tokens of one request, from the pinned snapshot.

Decision calls: every member of every reserved stage (optional and skippable members included) × every question of
that stage × (1 + backup), and 1 + 3 calls per member for a hierarchical question (IMPL-027). Escalation stage k
is reserved iff the request's `escalate_budget_ms` covers `P_k.deadline_ms`; its questions are those of stage k-1
that the escalation may carry (IMPL-068) and that fit `L(P_k)` flat. Generation: 4 attempt waves (A1, the spec
restart and 2 failure attempts, IMPL-016) × the widest candidate profile, at the worst-case `max_tokens`
(IMPL-084).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from noema.config.models import (
    DECISION_PROMPT_OVERHEAD,
    EFFORT_RESERVE,
    GEN_DEFAULT_MAX_TOKENS,
    DecisionMemberCfg,
    DecisionProfileCfg,
)
from noema.core.principal import Principal
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.ops import DecisionWork, GenerationWork, WorkGraph
from noema.core.values import CallKind

LOGPROB_MAX_TOKENS: Final = 8  # logprob-codes@2 (spec-06 §5.4)
HIERARCHICAL_CALLS: Final = 4  # one round-1 call plus at most 3 expanded groups (spec-06 §19.9)
GENERATION_ATTEMPT_WAVES: Final = 4  # A1, the restart, 2 failure attempts (IMPL-016)
MAX_STAGES: Final = 8  # escalation cycles are config-rejected; this bounds a broken chain anyway


@dataclass(frozen=True, slots=True)
class _Stage:
    profile: DecisionProfileCfg
    deadline_ms: int
    questions: tuple[tuple[int, bool], ...]  # (K, hierarchical) per question


def flat_limit(snapshot: ConfigSnapshot, profile: DecisionProfileCfg) -> int:
    """L(profile): the smallest `max_choices` over the members' templates (spec-06 §3.1)."""
    return min(snapshot.template(m.template).max_choices for m in profile.members)


def decision_max_tokens(snapshot: ConfigSnapshot, member: DecisionMemberCfg, k: int) -> int:
    """`max_tokens` of one decision call with K options: 8 for token logprobs, else `min(64 + 12K + R, cap)`
    with `R = max(reasoning_reserve_tokens, EFFORT_RESERVE[effort])` for reasoning models (spec-06 §5.4-§5.5)."""
    if snapshot.template(member.template).elicitation == "token_logprobs":
        return LOGPROB_MAX_TOKENS
    model = snapshot.model(member.model)
    reserve = (
        max(model.reasoning_reserve_tokens, EFFORT_RESERVE[member.effort or "none"]) if model.controls.reasoning else 0
    )
    value = 64 + 12 * k + reserve
    return value if model.max_output_tokens_cap is None else min(value, model.max_output_tokens_cap)


def generation_out(snapshot: ConfigSnapshot, alias: str, member_max: int | None, pins: Sequence[int]) -> int:
    """out(m) (IMPL-084): `min(cap, max(pins ∪ {member ?? cap ?? 4096}) + reasoning_reserve_tokens)`; no cap, no
    min. `pins` = the request's `max_output_tokens` and every binding's `then.max_output_tokens`."""
    model = snapshot.model(alias)
    cap = model.max_output_tokens_cap
    own = member_max if member_max is not None else cap if cap is not None else GEN_DEFAULT_MAX_TOKENS
    value = max([own, *pins]) + model.reasoning_reserve_tokens
    return value if cap is None else min(cap, value)


def backs_up(member: DecisionMemberCfg, deadline_ms: int) -> bool:
    """A backup is reserved iff `backup_after_ms + latency_p95_ms <= deadline_ms` (spec-06 §6.3)."""
    return (
        member.backup_after_ms is not None
        and member.latency_p95_ms is not None
        and member.backup_after_ms + member.latency_p95_ms <= deadline_ms
    )


def stages(snapshot: ConfigSnapshot, work: DecisionWork) -> list[_Stage]:
    """The reserved stages of one decision run: stage 0, then every escalation stage whose deadline fits the
    budget (per stage, not cumulative), each with the questions it may carry (spec-11 §1.1, spec-06 §9.2)."""
    p0 = snapshot.decision_profile(work.profile_id)
    l0 = flat_limit(snapshot, p0)
    current = [(q, q.k > l0) for q in work.questions]
    out = [_Stage(p0, work.stage0_deadline_ms, tuple((q.k, h) for q, h in current))]
    prev = p0
    while (
        work.escalation != "off"
        and work.escalate_budget_ms is not None
        and prev.escalation is not None
        and len(out) < MAX_STAGES
    ):
        target = snapshot.decision_profile(prev.escalation.to_profile)
        if target.deadline_ms > work.escalate_budget_ms:
            break
        limit = flat_limit(snapshot, target)
        allowed = prev.escalation.questions  # None: every platform question (IMPL-068)
        current = [
            (q, False)
            for q, _ in current
            if q.k <= limit and (q.source == "request" or allowed is None or q.name in allowed)
        ]
        if not current:
            break
        out.append(_Stage(target, target.deadline_ms, tuple((q.k, False) for q, _ in current)))
        prev = target
    return out


class DefaultWorkGraphPlanner:
    """Implements `WorkGraphPlanner` (spec-11 §1.1). Sync and pure: the result depends only on the snapshot and
    the work items. `routing.default_profile` is always a generation candidate (the IMPL-016 failure fallback)."""

    def plan(
        self,
        snapshot: ConfigSnapshot,
        principal: Principal,
        *,
        decisions: Sequence[DecisionWork],
        generation: GenerationWork | None,
    ) -> WorkGraph:
        calls: dict[tuple[str, CallKind], int] = {}
        tokens: dict[str, int] = {}
        total = tokens_in = tokens_out = 0
        for work in decisions:
            for stage in stages(snapshot, work):
                for member in stage.profile.members:
                    attempts = 2 if backs_up(member, stage.deadline_ms) else 1
                    template_k = snapshot.template(member.template).max_choices
                    for k, hierarchical in stage.questions:
                        n = attempts * (HIERARCHICAL_CALLS if hierarchical else 1)
                        out = decision_max_tokens(snapshot, member, template_k if hierarchical else k)
                        key = (member.model, work.kind)
                        calls[key] = calls.get(key, 0) + n
                        tokens[member.model] = tokens.get(member.model, 0) + n * (
                            work.input_tokens + DECISION_PROMPT_OVERHEAD + out
                        )
                        total += n
                        tokens_in += n * (work.input_tokens + DECISION_PROMPT_OVERHEAD)
                        tokens_out += n * out
        if generation is not None:
            waves, worst_out = _generation(snapshot, generation, calls, tokens)
            total += waves
            tokens_in += waves * generation.input_tokens
            tokens_out += waves * worst_out
        return WorkGraph(
            calls=MappingProxyType(calls),
            tokens_by_model=MappingProxyType(tokens),
            total_calls=total,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            streams=0 if generation is None else 1,
        )


def _generation(
    snapshot: ConfigSnapshot, work: GenerationWork, calls: dict[tuple[str, CallKind], int], tokens: dict[str, int]
) -> tuple[int, int]:
    """Adds the per-alias generation calls and tokens; returns (key/project call waves, worst out(m))."""
    routing = snapshot.routing
    pins = [p for p in (work.max_output_tokens,) if p is not None]
    pins += [b.then.max_output_tokens for b in routing.bindings if b.then.max_output_tokens is not None]
    per_alias: dict[str, int] = {}
    out_by_alias: dict[str, int] = {}
    widest = 1
    for profile_id in dict.fromkeys((*work.candidate_profiles, routing.default_profile)):
        profile = snapshot.generation_profile(profile_id)
        width = 1 if profile.mode in ("single", "fallback") else len(profile.members)  # race: parallel members
        widest = max(widest, width)
        for member in profile.members:
            per_alias[member.model] = max(per_alias.get(member.model, 0), GENERATION_ATTEMPT_WAVES * width)
            out = generation_out(snapshot, member.model, member.max_output_tokens, pins)
            out_by_alias[member.model] = max(out_by_alias.get(member.model, 0), out)
    for alias, n in per_alias.items():
        key: tuple[str, CallKind] = (alias, "generation")
        calls[key] = calls.get(key, 0) + n
        tokens[alias] = tokens.get(alias, 0) + n * (work.input_tokens + out_by_alias[alias])
    return GENERATION_ATTEMPT_WAVES * widest, max(out_by_alias.values(), default=0)
