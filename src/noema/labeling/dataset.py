"""Labeled examples for fitting and evaluation (ADR-003 §3, 06 §2.3).

One `LabeledSet` holds the consensus-labeled examples of one calibrator binding: the question kind, its declared
option keys (member-distribution keys: "true"/"false", choice values or level labels, in declared order), the
binding's member subset and, per example, each member's raw distribution over its measured options with its
unresolved mass, the member weights and the consensus label. `arrays()` turns a selection into numpy arrays, and
`split()` is the time-ordered, class-stratified split of 06 §2.3 step 1 (the test set is the most recent slice).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

import numpy as np

from noema.labeling.calibrators.temperature import BoolArray, FloatArray, IndexArray

QuestionKind = Literal["choice", "predicate", "score"]
TEST_SHARE: Final = 0.5  # share of each class (most recent first) held out as the test split


@dataclass(frozen=True, slots=True)
class MemberObs:
    """One member's raw distribution over its measured options (sums to `1 - unresolved`)."""

    dist: Mapping[str, float]
    unresolved: float = 0.0


@dataclass(frozen=True, slots=True)
class Example:
    """One labeled answer: the members of the binding's subset, their weights (renormalized per example), the
    consensus label (an option key) and when the item was labeled."""

    members: Mapping[str, MemberObs]
    weights: Mapping[str, float]
    label: str
    at: datetime


@dataclass(frozen=True, slots=True)
class LabeledSet:
    """The examples of one binding (see the module docstring)."""

    kind: QuestionKind
    options: tuple[str, ...]
    member_ids: tuple[str, ...]
    examples: tuple[Example, ...]

    def __post_init__(self) -> None:
        if len(self.options) < 2:
            raise ValueError("a question has at least two options")
        if self.kind == "predicate" and set(self.options) != {"true", "false"}:
            raise ValueError("predicate options are 'true' and 'false'")
        known = set(self.options)
        for ex in self.examples:
            if ex.label not in known:
                raise ValueError(f"label {ex.label!r} is not a declared option")
            if set(ex.members) != set(self.member_ids):
                raise ValueError("every example carries exactly the binding's members")


@dataclass(frozen=True, slots=True)
class Arrays:
    """Per-member arrays over the declared options (n x K): raw probabilities `p` (0 where unmeasured), the measured
    `mask`, unresolved masses `u` (n), normalized weights `w` (n), and the label indices `y` (n)."""

    p: Mapping[str, FloatArray]
    mask: Mapping[str, BoolArray]
    u: Mapping[str, FloatArray]
    w: Mapping[str, FloatArray]
    y: IndexArray


def arrays(ls: LabeledSet, idx: Sequence[int] | None = None) -> Arrays:
    """The arrays of the examples at `idx` (all when None), in that order."""
    chosen = ls.examples if idx is None else tuple(ls.examples[i] for i in idx)
    col = {k: j for j, k in enumerate(ls.options)}
    n, k = len(chosen), len(ls.options)
    p: dict[str, FloatArray] = {}
    mask: dict[str, BoolArray] = {}
    u: dict[str, FloatArray] = {}
    raw_w = np.zeros((n, len(ls.member_ids)))
    for mi, m in enumerate(ls.member_ids):
        pm, mm, um = np.zeros((n, k)), np.zeros((n, k), dtype=np.bool_), np.zeros(n)
        for i, ex in enumerate(chosen):
            obs = ex.members[m]
            um[i] = obs.unresolved
            raw_w[i, mi] = max(float(ex.weights.get(m, 0.0)), 0.0)
            for key, value in obs.dist.items():
                j = col.get(key)
                if j is not None:
                    pm[i, j] = value
                    mm[i, j] = True
        p[m], mask[m], u[m] = pm, mm, um
    totals = raw_w.sum(axis=1, keepdims=True)
    equal = np.full_like(raw_w, 1.0 / max(len(ls.member_ids), 1))
    norm = np.where(totals > 0.0, raw_w / np.where(totals > 0.0, totals, 1.0), equal)
    w = {m: norm[:, mi] for mi, m in enumerate(ls.member_ids)}
    y = np.array([col[ex.label] for ex in chosen], dtype=np.intp)
    return Arrays(p=p, mask=mask, u=u, w=w, y=y)


def split(ls: LabeledSet, test_share: float = TEST_SHARE) -> tuple[list[int], list[int]]:
    """(calibration indices, test indices): per class, the most recent `ceil(test_share * n_c)` examples (by `at`,
    then position) are the test split; both lists are in time order."""
    by_class: dict[str, list[int]] = {}
    for i, ex in enumerate(ls.examples):
        by_class.setdefault(ex.label, []).append(i)
    calib: list[int] = []
    test: list[int] = []
    for members in by_class.values():
        members.sort(key=lambda i: (ls.examples[i].at, i))
        n_test = math.ceil(test_share * len(members))
        calib.extend(members[: len(members) - n_test])
        test.extend(members[len(members) - n_test :])
    order = lambda i: (ls.examples[i].at, i)  # noqa: E731
    return sorted(calib, key=order), sorted(test, key=order)
