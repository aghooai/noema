"""Escalation triggers, eligibility and the profile chain (spec-06 §9; IMPL-005, IMPL-025, IMPL-068).

Triggers read the emitted 4-dp values of a closed stage answer. The config key `any_refused` is recorded as
`refused`; `triggered_by` lists the union in the canonical order.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from noema.config.models import DecisionProfileCfg, EscalationWhenCfg
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.decision import CompiledQuestion
from noema.core.values import JsonValue

TRIGGER_ORDER: Final = ("confidence_below", "margin_below", "refused", "unavailable")


def fired_triggers(answer: Mapping[str, JsonValue], when: EscalationWhenCfg) -> tuple[str, ...]:
    """The triggers that fire on one closed answer, in canonical order."""
    fired: list[str] = []
    kind = answer.get("type")
    conf = answer.get("confidence")
    if isinstance(conf, Mapping):
        value, margin = conf.get("value"), conf.get("margin")
        if when.confidence_below is not None and isinstance(value, float | int) and value < when.confidence_below:
            fired.append("confidence_below")
        if when.margin_below is not None and isinstance(margin, float | int) and margin < when.margin_below:
            fired.append("margin_below")
    quorum = answer.get("quorum")
    refused = quorum.get("refused", 0) if isinstance(quorum, Mapping) else 0
    if when.any_refused and (kind == "refusal" or (isinstance(refused, int) and refused >= 1)):
        fired.append("refused")
    if when.unavailable and kind == "unavailable":
        fired.append("unavailable")
    return tuple(fired)


def union(triggers: list[tuple[str, ...]]) -> tuple[str, ...]:
    """The union of several answers' triggers, canonical order."""
    seen = {t for ts in triggers for t in ts}
    return tuple(t for t in TRIGGER_ORDER if t in seen)


def filtered_in(q: CompiledQuestion, profile: DecisionProfileCfg) -> bool:
    """`escalation.questions` filters platform questions only; request questions are always eligible."""
    esc = profile.escalation
    if esc is None:
        return False
    return q.source != "platform" or esc.questions is None or q.name in esc.questions


def chain(snapshot: ConfigSnapshot, profile: DecisionProfileCfg) -> list[DecisionProfileCfg]:
    """Every profile reached by following `escalation.to_profile` from `profile` (cycles stop the walk)."""
    out: list[DecisionProfileCfg] = []
    seen = {profile.id}
    current = profile
    while current.escalation is not None and current.escalation.to_profile not in seen:
        current = snapshot.decision_profile(current.escalation.to_profile)
        seen.add(current.id)
        out.append(current)
    return out
