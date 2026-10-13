"""`FakeWorkGraphPlanner` (spec-02 §8, spec-11 §1.1) and `FakeTokenEstimator` (spec-11 §2, IMPL-029c).

Both are pure and follow the spec formulas exactly, so the spec-11 §1.1 call vectors (T-WG-1…5) hold for the fake
too, including the escalation set Q_k (platform questions filtered by `P_{k-1}.escalation.questions`, request
questions always pass, K <= L(P_k)).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from typing import Final

from noema.config.models import (
    DECISION_PROMPT_OVERHEAD,
    EFFORT_RESERVE,
    GEN_DEFAULT_MAX_TOKENS,
    DecisionMemberCfg,
    DecisionProfileCfg,
    ModelCfg,
)
from noema.core.principal import Principal
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.ops import DecisionWork, GenerationWork, QuestionWork, WorkGraph
from noema.core.values import CallKind, ContentBlock, DocumentBlock, ImageBlock, TextBlock

GENERATION_ATTEMPT_WAVES: Final = 4  # A1, restart, 2 failure attempts (IMPL-016)
MAX_STAGES: Final = 8


def flat_limit(snapshot: ConfigSnapshot, profile: DecisionProfileCfg) -> int:
    """L(profile) = min max_choices over the members' templates (spec-06 §3.1)."""
    return min(snapshot.template(m.template).max_choices for m in profile.members)


def decision_max_tokens(snapshot: ConfigSnapshot, member: DecisionMemberCfg, k: int) -> int:
    """max_tokens of one decision call: logprob-codes 8; verbalized min(64 + 12K + R, cap) (spec-06 §5.4-5.5)."""
    template = snapshot.template(member.template)
    if template.elicitation == "token_logprobs":
        return 8
    model = snapshot.model(member.model)
    reserve = 0
    if model.controls.reasoning:
        reserve = max(model.reasoning_reserve_tokens, EFFORT_RESERVE[member.effort or "none"])
    value = 64 + 12 * k + reserve
    return min(value, model.max_output_tokens_cap) if model.max_output_tokens_cap is not None else value


def generation_out(snapshot: ConfigSnapshot, alias: str, member_max: int | None, pins: Sequence[int]) -> int:
    """out(m) of spec-11 §1.1 (IMPL-084)."""
    model: ModelCfg = snapshot.model(alias)
    cap = model.max_output_tokens_cap
    own = member_max if member_max is not None else cap if cap is not None else GEN_DEFAULT_MAX_TOKENS
    value = max([*pins, own]) + model.reasoning_reserve_tokens
    return min(cap, value) if cap is not None else value


class FakeWorkGraphPlanner:
    """Implements `WorkGraphPlanner` (spec-11 §1.1)."""

    def plan(
        self,
        snapshot: ConfigSnapshot,
        principal: Principal,
        *,
        decisions: Sequence[DecisionWork],
        generation: GenerationWork | None,
    ) -> WorkGraph:
        calls: Counter[tuple[str, CallKind]] = Counter()
        tokens: Counter[str] = Counter()
        total = tokens_in = tokens_out = 0
        for work in decisions:
            for profile, deadline_ms, ks in self._stages(snapshot, work):
                for m in profile.members:
                    backup = int(
                        m.backup_after_ms is not None
                        and m.latency_p95_ms is not None
                        and m.backup_after_ms + m.latency_p95_ms <= deadline_ms
                    )
                    for k, hierarchical in ((q.k, h) for q, h in ks):
                        n = (4 if hierarchical else 1) * (1 + backup)
                        k_call = snapshot.template(m.template).max_choices if hierarchical else k
                        mt = decision_max_tokens(snapshot, m, k_call)
                        calls[(m.model, work.kind)] += n
                        tokens[m.model] += n * (work.input_tokens + DECISION_PROMPT_OVERHEAD + mt)
                        total += n
                        tokens_in += n * (work.input_tokens + DECISION_PROMPT_OVERHEAD)
                        tokens_out += n * mt
        if generation is not None:
            total, tokens_in, tokens_out = self._generation(
                snapshot, generation, calls, tokens, (total, tokens_in, tokens_out)
            )
        return WorkGraph(
            calls=dict(calls),
            tokens_by_model=dict(tokens),
            total_calls=total,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            streams=1 if generation is not None else 0,
        )

    def _stages(
        self, snapshot: ConfigSnapshot, work: DecisionWork
    ) -> list[tuple[DecisionProfileCfg, int, list[tuple[QuestionWork, bool]]]]:
        p0 = snapshot.decision_profile(work.profile_id)
        l0 = flat_limit(snapshot, p0)
        questions = [(q, q.k > l0) for q in work.questions]
        stages = [(p0, work.stage0_deadline_ms, questions)]
        prev = p0
        while (
            work.escalation != "off"
            and work.escalate_budget_ms is not None
            and prev.escalation is not None
            and len(stages) < MAX_STAGES
        ):
            target = snapshot.decision_profile(prev.escalation.to_profile)
            if target.deadline_ms > work.escalate_budget_ms:
                break
            lt = flat_limit(snapshot, target)
            allowed = prev.escalation.questions  # None = every platform question (IMPL-068)
            questions = [
                (q, False)
                for q, _ in questions
                if q.k <= lt and (q.source == "request" or allowed is None or q.name in allowed)
            ]
            if not questions:
                break
            stages.append((target, target.deadline_ms, questions))
            prev = target
        return stages

    def _generation(
        self,
        snapshot: ConfigSnapshot,
        work: GenerationWork,
        calls: Counter[tuple[str, CallKind]],
        tokens: Counter[str],
        totals: tuple[int, int, int],
    ) -> tuple[int, int, int]:
        pins = [p for p in (work.max_output_tokens,) if p is not None]
        pins += [b.then.max_output_tokens for b in snapshot.routing.bindings if b.then.max_output_tokens is not None]
        per_alias: dict[str, int] = {}
        out_by_alias: dict[str, int] = {}
        width_max = 1
        for profile_id in work.candidate_profiles:
            gp = snapshot.generation_profile(profile_id)
            width = len(gp.members) if gp.mode == "race" else 1
            width_max = max(width_max, width)
            for member in gp.members:
                per_alias[member.model] = max(per_alias.get(member.model, 0), GENERATION_ATTEMPT_WAVES * width)
                out = generation_out(snapshot, member.model, member.max_output_tokens, pins)
                out_by_alias[member.model] = max(out_by_alias.get(member.model, 0), out)
        for alias, n in per_alias.items():
            calls[(alias, "generation")] += n
            tokens[alias] += n * (work.input_tokens + out_by_alias[alias])
        waves = GENERATION_ATTEMPT_WAVES * width_max
        total, tokens_in, tokens_out = totals
        worst_out = max(out_by_alias.values(), default=0)
        return total + waves, tokens_in + waves * work.input_tokens, tokens_out + waves * worst_out


class FakeTokenEstimator:
    """Implements `TokenEstimator` (IMPL-029c): ceil(chars / chars_per_token) per system string and text block,
    min(1600, ceil(w·h/750)) per image, 1500 per document page."""

    def __init__(self, snapshot_chars_per_token: float = 4.0) -> None:
        self._cpt = snapshot_chars_per_token

    def estimate(self, model_alias: str, system: Sequence[str], blocks: Sequence[ContentBlock]) -> int:
        total = sum(math.ceil(len(s) / self._cpt) for s in system)
        for block in blocks:
            if isinstance(block, TextBlock):
                total += math.ceil(len(block.text) / self._cpt)
            elif isinstance(block, ImageBlock):
                total += min(1600, math.ceil(block.width * block.height / 750))
            elif isinstance(block, DocumentBlock):
                total += 1500 * block.pages
        return total
