"""Binding evaluation (spec-04 §5.1; spec-08 §3.1; IMPL-002, IMPL-018, IMPL-024).

`evaluate()` walks `routing.bindings` in config order over the answers that are final (IMPL-002): the first
matching binding that sets a pre-commit key wins it; the tightening keys take the most restrictive value over every
match. It is pure and allocation-light (it runs once at S1 and once at the final snapshot).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal

if TYPE_CHECKING:
    from noema.config.models import BindingCfg, BindingWhenCfg
    from noema.core.protocols.decision import AnswerSummary

PRE: Final = ("generation_profile", "effort", "max_output_tokens", "conditioning")
ORDER: Final[Mapping[str, tuple[object, ...]]] = {
    "verify_answer": (False, True),
    "disable_tools": (False, True),
    "spotlight": ("standard", "strict"),
    "response_cache": ("on", "off"),
    "log_content": ("full", "redacted", "none"),
}
_UNSCORED: Final = frozenset({"unavailable", "refusal"})

LogContent = Literal["full", "redacted", "none"]


@dataclass(frozen=True, slots=True)
class Tightening:
    """The post-commit (tightening) keys: least restrictive defaults, raised by matching bindings (spec-04 §5.1)."""

    verify_answer: bool = False
    disable_tools: bool = False
    spotlight: Literal["standard", "strict"] = "standard"
    response_cache: Literal["on", "off"] = "on"
    log_content: LogContent = "full"

    def tighten(self, key: str, value: object) -> Tightening:
        """This tightening with `key` raised to `value` when `value` is more restrictive."""
        levels = ORDER[key]
        if value not in levels or levels.index(value) <= levels.index(getattr(self, key)):
            return self
        changes: dict[str, Any] = {key: value}
        return replace(self, **changes)

    def union(self, other: Tightening) -> Tightening:
        """The most restrictive value of each key over both."""
        out = self
        for key in ORDER:
            out = out.tighten(key, getattr(other, key))
        return out


@dataclass(frozen=True, slots=True)
class Evaluation:
    """The result of one evaluation: the pre-commit keys set by bindings (first match wins), the tightening, and
    the matched bindings (`bindings[i]`, ascending) with their indexes."""

    decided: Mapping[str, object]
    tight: Tightening
    matched: tuple[int, ...]

    @property
    def matched_names(self) -> tuple[str, ...]:
        return tuple(f"bindings[{i}]" for i in self.matched)


def matches(when: BindingWhenCfg, answers: Mapping[str, AnswerSummary], final_names: frozenset[str]) -> bool:
    """spec-04 §5.1 `matches`: every listed condition must hold; provisional, unscored or missing answers never
    match. `mode` and `eval_ref` are config-time only."""
    name = when.answer
    if name not in final_names:
        return False
    a = answers.get(name)
    if a is None or a.type in _UNSCORED or not a.top:
        return False
    if when.source == "request" and when.answer_type is not None and a.type != when.answer_type:
        return False
    top_value, top_p = a.top[0]
    if when.value_in is not None and top_value not in when.value_in:
        return False
    if when.level_in is not None and top_value not in when.level_in:
        return False
    if when.p_max_gte is not None and top_p < when.p_max_gte:
        return False
    if when.probability_gte is not None and (
        a.type not in ("predicate", "noul") or a.probability is None or a.probability < when.probability_gte
    ):
        return False
    if when.margin_gte is not None and (a.margin is None or a.margin < when.margin_gte):
        return False
    if when.confidence_band is not None and a.band != when.confidence_band:
        return False
    return not (when.requires_calibrated and not a.calibrated)


def evaluate(
    bindings: Sequence[BindingCfg],
    answers: Mapping[str, AnswerSummary],
    final_names: frozenset[str],
    policy: Tightening,
) -> Evaluation:
    """spec-04 §5.1 `evaluate`. `policy` is the starting tightening (Phase 1: `log_content` from the data class's
    `store_content`, metadata_only -> "none")."""
    decided: dict[str, object] = {}
    matched: list[int] = []
    tight = policy
    for i, b in enumerate(bindings):
        if not matches(b.when, answers, final_names):
            continue
        matched.append(i)
        then = b.then
        for key in PRE:
            value = getattr(then, key)
            if value is not None and key not in decided:
                decided[key] = value
        for key in ORDER:
            value = getattr(then, key)
            if value is not None:
                tight = tight.tighten(key, value)
    return Evaluation(decided, tight, tuple(matched))


def policy_for(store_content: Literal["full", "redacted", "metadata_only"]) -> Tightening:
    """The evaluation policy of a data class: its `store_content` as the starting `log_content`."""
    return Tightening(log_content="none" if store_content == "metadata_only" else store_content)


def content_policy(log_content: LogContent) -> Literal["full", "redacted", "metadata_only"]:
    """`log_content` as a stored `content_policy` (spec-04 §10)."""
    return "metadata_only" if log_content == "none" else log_content
