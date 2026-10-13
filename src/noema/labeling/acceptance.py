"""The acceptance gate A1-A8 (ADR-003 §3; IMPL-039, IMPL-073).

The gate reads the recorded `HoldoutMetrics` (the same wire dict the response validator's `holdout_errors` sees,
so the served artifact and the validator cannot disagree) plus the member-subset evidence, with every threshold from
the config snapshot (`labeling.acceptance` and `labeling.family_overlap_lift_max`); nothing is hard-coded. An
artifact moves `fitted -> accepted` only if every rule holds; the failed rules use the contract names:

| Rule | Name | Holds when |
|---|---|---|
| A1 | ece_max | `ece <= ece_max` (the per-type ECE of IMPL-073) |
| A2 | nll_not_worse | `nll <= nll_uncalibrated` |
| A3 | n_min | `n >= n_min`; `n_by_class` covers exactly the declared classes and sums to `n` |
| A4 | flagged_classes | `flagged_classes` = the classes with `n_by_class < class_n_min` (listed, not a failure) |
| A5 | class_consensus_min | every class rate and the overall rate `>= consensus_min` (exactly the declared classes) |
| A6 | family_overlap_lift | `max_member_lift <= threshold <= family_overlap_lift_max` |
| A7 | label_source | `label_source == "llm_consensus"` and at least 2 distinct labelers |
| A8 | member_subset_share | the full member set, or a subset with traffic share `>= subset_min_share` |
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from noema.core.values import JsonValue

if TYPE_CHECKING:
    from noema.config.models import LabelingCfg

RuleName = Literal[
    "ece_max",
    "nll_not_worse",
    "n_min",
    "flagged_classes",
    "class_consensus_min",
    "family_overlap_lift",
    "label_source",
    "member_subset_share",
]
RULES: Final[tuple[RuleName, ...]] = (
    "ece_max",
    "nll_not_worse",
    "n_min",
    "flagged_classes",
    "class_consensus_min",
    "family_overlap_lift",
    "label_source",
    "member_subset_share",
)


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The gate thresholds of one config snapshot."""

    ece_max: float
    n_min: int
    class_n_min: int
    consensus_min: float
    family_overlap_lift_max: float
    subset_min_share: float

    @classmethod
    def from_config(cls, labeling: LabelingCfg) -> Thresholds:
        """`labeling.acceptance` plus `labeling.family_overlap_lift_max`."""
        acc = labeling.acceptance
        return cls(
            ece_max=acc.ece_max,
            n_min=acc.n_min,
            class_n_min=acc.class_n_min,
            consensus_min=acc.consensus_min,
            family_overlap_lift_max=labeling.family_overlap_lift_max,
            subset_min_share=acc.subset_min_share,
        )


@dataclass(frozen=True, slots=True)
class SubsetEvidence:
    """A8 inputs: whether the binding's subset is the stage's full member set, and its share of the stage's answered
    (uniform-sampled) traffic over the label window."""

    full: bool
    share: float


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    failed_rules: tuple[RuleName, ...]

    def wire(self) -> dict[str, JsonValue]:
        """The contract `Acceptance` object (the `noema.calibrator.acceptance` column)."""
        return {"passed": self.passed, "failed_rules": list(self.failed_rules)}


def _num(h: Mapping[str, JsonValue], key: str) -> float:
    value = h[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(key)
    return float(value)


def _counts(h: Mapping[str, JsonValue], key: str) -> dict[str, float]:
    value = h[key]
    if not isinstance(value, dict):
        raise TypeError(key)
    out: dict[str, float] = {}
    for k, v in value.items():
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise TypeError(key)
        out[k] = float(v)
    return out


def _rules(h: Mapping[str, JsonValue], classes: set[str], t: Thresholds) -> dict[RuleName, Callable[[], bool]]:
    def n_min() -> bool:
        n, by_class = _num(h, "n"), _counts(h, "n_by_class")
        return n >= t.n_min and set(by_class) == classes and sum(by_class.values()) == n

    def flagged() -> bool:
        listed = h["flagged_classes"]
        if not isinstance(listed, list) or not all(isinstance(c, str) for c in listed):
            return False
        expected = sorted(c for c, k in _counts(h, "n_by_class").items() if k < t.class_n_min)
        return sorted(str(c) for c in listed) == expected

    def consensus() -> bool:
        rates = _counts(h, "consensus_rate_by_class")
        overall = _num(h, "consensus_rate")
        return (
            set(rates) == classes and overall >= t.consensus_min and all(r >= t.consensus_min for r in rates.values())
        )

    def family() -> bool:
        fo = h["family_overlap"]
        if not isinstance(fo, dict):
            return False
        lift, threshold = _num(fo, "max_member_lift"), _num(fo, "threshold")
        return threshold <= t.family_overlap_lift_max and lift <= threshold

    def source() -> bool:
        labelers = h["labelers"]
        return h["label_source"] == "llm_consensus" and isinstance(labelers, list) and len(set(map(str, labelers))) >= 2

    return {
        "ece_max": lambda: _num(h, "ece") <= t.ece_max,
        "nll_not_worse": lambda: _num(h, "nll") <= _num(h, "nll_uncalibrated"),
        "n_min": n_min,
        "flagged_classes": flagged,
        "class_consensus_min": consensus,
        "family_overlap_lift": family,
        "label_source": source,
    }


def evaluate_gate(
    holdout: Mapping[str, JsonValue] | None, classes: Sequence[str], thresholds: Thresholds, subset: SubsetEvidence
) -> GateResult:
    """Every rule of the module table; a missing or ill-typed holdout field fails the rule that reads it."""
    failed: list[RuleName] = []
    checks = _rules(holdout, set(classes), thresholds) if holdout is not None else {}
    for rule in RULES[:-1]:
        check = checks.get(rule)
        try:
            ok = check is not None and check()
        except (KeyError, TypeError, ValueError):
            ok = False
        if not ok:
            failed.append(rule)
    if not (subset.full or subset.share >= thresholds.subset_min_share):
        failed.append("member_subset_share")
    return GateResult(passed=not failed, failed_rules=tuple(failed))
