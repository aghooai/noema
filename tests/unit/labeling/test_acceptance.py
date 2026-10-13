"""Gate Q6 (spec-10 §3): every acceptance rule A1-A8 rejects at its boundary and accepts just inside it (ECE 0.0501,
NLL above uncalibrated, n_test 299, a class with n = 29 flagged, class consensus 0.59, lift 0.1001), with the
thresholds read from the config; plus the whole fit-and-gate pipeline on synthetic well- and mis-calibrated data."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.fakes.config import patched_example, snapshot_from
from tests.unit.labeling.synth import LABELERS, VIEW, labeled_set, sampled_items

from noema.labeling.acceptance import RULES, SubsetEvidence, Thresholds, evaluate_gate
from noema.labeling.evaluation import fit_and_evaluate

pytestmark = pytest.mark.unit
CLASSES = ("billing", "shipping")
FULL = SubsetEvidence(full=True, share=1.0)


def thresholds(**acceptance: float) -> Thresholds:
    snap = snapshot_from(patched_example({"labeling": {"acceptance": acceptance}} if acceptance else {}))
    return Thresholds.from_config(snap.labeling)


def holdout(**overrides: Any) -> dict[str, Any]:
    h: dict[str, Any] = {
        "dataset_id": "lrun_1",
        "label_source": "llm_consensus",
        "labelers": list(LABELERS),
        "consensus_rate": 0.8,
        "consensus_rate_by_class": {"billing": 0.8, "shipping": 0.7},
        "n": 300,
        "n_by_class": {"billing": 200, "shipping": 100},
        "ece": 0.05,
        "brier": 0.2,
        "nll": 0.5,
        "nll_uncalibrated": 0.6,
        "flagged_classes": [],
        "scope": {"target": "llm_consensus_agreement", "retained_share": 0.8, "slices": [{"name": "all", "n": 300}]},
        "family_overlap": {"max_member_lift": 0.1, "threshold": 0.1},
        "evaluated_at": "2026-10-01T00:00:00.000Z",
    }
    h.update(overrides)
    return h


def gate(h: dict[str, Any] | None, subset: SubsetEvidence = FULL, t: Thresholds | None = None) -> tuple[str, ...]:
    return evaluate_gate(h, CLASSES, t or thresholds(), subset).failed_rules


def test_thresholds_come_from_the_config_snapshot() -> None:
    t = thresholds()
    assert (t.ece_max, t.n_min, t.class_n_min, t.consensus_min, t.family_overlap_lift_max, t.subset_min_share) == (
        0.05,
        300,
        30,
        0.6,
        0.10,
        0.05,
    )
    stricter = thresholds(ece_max=0.03, n_min=500)
    assert stricter.ece_max == 0.03 and stricter.n_min == 500
    assert gate(holdout(), t=stricter) == ("ece_max", "n_min")


def test_inside_every_boundary_passes() -> None:
    result = evaluate_gate(holdout(), CLASSES, thresholds(), FULL)
    assert result.passed and result.failed_rules == ()
    assert result.wire() == {"passed": True, "failed_rules": []}


BOUNDARIES: list[tuple[str, Callable[[], dict[str, Any]]]] = [
    ("ece_max", lambda: holdout(ece=0.0501)),
    ("nll_not_worse", lambda: holdout(nll=0.6001, nll_uncalibrated=0.6)),
    ("n_min", lambda: holdout(n=299, n_by_class={"billing": 199, "shipping": 100})),
    ("n_min", lambda: holdout(n_by_class={"billing": 200, "shipping": 99})),  # does not sum to n
    ("n_min", lambda: holdout(n_by_class={"billing": 300})),  # does not cover the declared classes
    ("flagged_classes", lambda: holdout(n_by_class={"billing": 271, "shipping": 29})),  # 29 must be listed
    ("flagged_classes", lambda: holdout(flagged_classes=["shipping"])),  # listed although n = 100
    ("class_consensus_min", lambda: holdout(consensus_rate_by_class={"billing": 0.8, "shipping": 0.59})),
    ("class_consensus_min", lambda: holdout(consensus_rate=0.59)),
    ("class_consensus_min", lambda: holdout(consensus_rate_by_class={"billing": 0.8})),
    ("family_overlap_lift", lambda: holdout(family_overlap={"max_member_lift": 0.1001, "threshold": 0.1})),
    ("family_overlap_lift", lambda: holdout(family_overlap={"max_member_lift": 0.05, "threshold": 0.2})),
    ("label_source", lambda: holdout(label_source="human")),
    ("label_source", lambda: holdout(labelers=[LABELERS[0], LABELERS[0]])),
]


@pytest.mark.parametrize(("rule", "make"), BOUNDARIES, ids=[f"{r}-{i}" for i, (r, _) in enumerate(BOUNDARIES)])
def test_each_rule_rejects_at_its_boundary(rule: str, make: Callable[[], dict[str, Any]]) -> None:
    assert gate(make()) == (rule,)


def test_flagged_class_with_29_labels_is_listed_not_a_failure() -> None:
    flagged = holdout(n_by_class={"billing": 271, "shipping": 29}, flagged_classes=["shipping"])
    assert gate(flagged) == ()
    assert gate(holdout(n_by_class={"billing": 270, "shipping": 30})) == ()


def test_nll_equal_to_uncalibrated_passes() -> None:
    assert gate(holdout(nll=0.6, nll_uncalibrated=0.6)) == ()


def test_member_subset_share() -> None:
    assert gate(holdout(), SubsetEvidence(full=False, share=0.0499)) == ("member_subset_share",)
    assert gate(holdout(), SubsetEvidence(full=False, share=0.05)) == ()


def test_missing_holdout_or_fields_fail_closed() -> None:
    assert gate(None) == RULES[:-1]
    broken = holdout()
    del broken["ece"]
    broken["nll"] = "0.5"
    broken["family_overlap"] = None
    assert gate(broken) == ("ece_max", "nll_not_worse", "family_overlap_lift")


def _evaluate(**kwargs: Any) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    ls = labeled_set(4000, **kwargs)
    ev = fit_and_evaluate(
        ls,
        sampled_items(ls),
        dataset_id="lrun_synthetic",
        labelers=LABELERS,
        lift_threshold=0.10,
        class_n_min=30,
        view_signature=VIEW,
        evaluated_at=datetime(2026, 10, 1, tzinfo=UTC),
        epsilon=1e-4,
    )
    failed = evaluate_gate(ev.holdout, ls.options, thresholds(), FULL).failed_rules
    return ev.holdout, failed


@pytest.mark.parametrize("kind", ["choice", "predicate", "score"])
@pytest.mark.parametrize("temp", [0.4, 2.5])
def test_miscalibrated_but_correctable_members_are_accepted_after_fitting(kind: str, temp: float) -> None:
    h, failed = _evaluate(kind=kind, temp=temp, seed=21)
    assert h is not None and failed == (), (failed, h)
    assert h["nll"] < h["nll_uncalibrated"] and 2000 <= h["n"] <= 2004  # ceil per class
    assert h["family_overlap"] == {"max_member_lift": 0.0, "threshold": 0.10}  # both labelers agree: no lift spread


@pytest.mark.parametrize("kind", ["choice", "predicate", "score"])
def test_confidence_anticorrelated_with_correctness_is_rejected(kind: str) -> None:
    h, failed = _evaluate(kind=kind, inverted=True, seed=3)
    assert h is not None and "ece_max" in failed and h["ece"] > 0.05


def test_too_few_test_labels_are_rejected() -> None:
    ls = labeled_set(590, temp=0.4, seed=4)
    ev = fit_and_evaluate(
        ls,
        sampled_items(ls),
        dataset_id="lrun_small",
        labelers=LABELERS,
        lift_threshold=0.10,
        class_n_min=30,
        view_signature=VIEW,
        evaluated_at=datetime(2026, 10, 1, tzinfo=UTC),
        epsilon=1e-4,
    )
    assert ev.holdout is not None and isinstance(n := ev.holdout["n"], int) and n < 300
    assert "n_min" in evaluate_gate(ev.holdout, ls.options, thresholds(), FULL).failed_rules
