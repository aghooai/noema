"""Holdout metrics (IMPL-073, ADR-003 §3): the per-type ECE test vectors, equal-mass bins = min(15, n), Brier, NLL,
reliability bins and the deterministic bootstrap."""

from __future__ import annotations

from typing import cast

import numpy as np
import pytest

from noema.core.values import JsonValue
from noema.labeling.metrics import (
    binned_ece,
    bootstrap_ci,
    brier,
    nll,
    question_ece,
    reliability_bins,
    scores,
)

pytestmark = pytest.mark.unit


def as_dict(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def test_adr003_vector_choice_top_label() -> None:
    probs = np.array([[0.9, 0.1, 0.0], [0.6, 0.4, 0.0], [0.8, 0.1, 0.1]])
    assert question_ece("choice", ("a", "b", "c"), probs, np.array([0, 1, 0])) == pytest.approx(0.3)


def test_adr003_vector_predicate() -> None:
    probs = np.array([[0.9, 0.1], [0.2, 0.8], [0.7, 0.3]])
    assert question_ece("predicate", ("true", "false"), probs, np.array([0, 1, 1])) == pytest.approx(1 / 3)


def test_adr003_vector_predicate_with_false_first() -> None:
    probs = np.array([[0.1, 0.9], [0.8, 0.2], [0.3, 0.7]])
    assert question_ece("predicate", ("false", "true"), probs, np.array([1, 0, 0])) == pytest.approx(1 / 3)


def test_adr003_vector_score_cumulative() -> None:
    probs = np.array([[0.2, 0.5, 0.3]])
    assert question_ece("score", ("lo", "mid", "hi"), probs, np.array([1])) == pytest.approx(0.25)


def test_bins_are_equal_mass_and_at_most_fifteen() -> None:
    conf = np.linspace(0.05, 0.95, 30)
    outcome = (np.arange(30) % 2).astype(np.float64)
    bins = reliability_bins(
        "predicate", ("true", "false"), np.stack([conf, 1 - conf], axis=1), (outcome == 0).astype(np.intp)
    )
    assert len(bins) == 15 and {as_dict(b)["n"] for b in bins} == {2}
    assert binned_ece(np.array([]), np.array([])) == 0.0
    three = reliability_bins("choice", ("a", "b"), np.array([[0.9, 0.1], [0.6, 0.4], [0.8, 0.2]]), np.array([0, 1, 0]))
    assert [as_dict(b)["n"] for b in three] == [1, 1, 1]
    assert three[0] == {"lower": 0.6, "upper": 0.6, "n": 1, "mean_predicted": 0.6, "observed": 0.0}


def test_perfect_predictions_have_zero_error() -> None:
    probs = np.array([[1.0, 0.0], [0.0, 1.0]])
    y = np.array([0, 1])
    s = scores("choice", ("a", "b"), probs, y)
    assert s.ece == pytest.approx(0.0) and s.brier == pytest.approx(0.0) and s.nll == pytest.approx(0.0, abs=1e-9)


def test_brier_and_nll() -> None:
    probs = np.array([[0.7, 0.2, 0.1]])
    y = np.array([1])
    assert brier("choice", ("a", "b", "c"), probs, y) == pytest.approx(0.49 + 0.64 + 0.01)
    assert brier("predicate", ("true", "false"), np.array([[0.7, 0.3]]), np.array([1])) == pytest.approx(0.49)
    assert nll(probs, y) == pytest.approx(-np.log(0.2))
    assert nll(np.array([[1.0, 0.0]]), np.array([1])) == pytest.approx(-np.log(1e-12))  # censored label is floored


def test_unresolved_mass_is_never_credited() -> None:
    probs = np.array([[0.4, 0.2]])  # u = 0.4
    assert nll(probs, np.array([0])) == pytest.approx(-np.log(0.4))


def test_bootstrap_is_deterministic_and_brackets_the_point_estimate() -> None:
    rng = np.random.default_rng(3)
    p = rng.uniform(0.05, 0.95, 400)
    y = (rng.uniform(size=400) < p).astype(np.intp)
    probs = np.stack([p, 1 - p], axis=1)
    labels = 1 - y  # column 0 = true
    first = bootstrap_ci("predicate", ("true", "false"), probs, labels, seed=11)
    again = bootstrap_ci("predicate", ("true", "false"), probs, labels, seed=11)
    assert first == again
    point = scores("predicate", ("true", "false"), probs, labels)
    for name, value in (("ece", point.ece), ("brier", point.brier), ("nll", point.nll)):
        low, high = cast(list[float], first[name])
        assert low <= value + 0.02 and high >= value - 0.02
    assert bootstrap_ci("choice", ("a", "b"), np.zeros((0, 2)), np.zeros(0, dtype=np.intp), seed=1) == {}
