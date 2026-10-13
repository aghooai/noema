"""Holdout metrics (ADR-003 §3 rule A1, IMPL-073; 06 §2.2).

ECE per question type, always with `bins = min(15, n)` equal-mass bins by sorted confidence and
`ECE = sum_b (n_b / n) * |mean conf_b - mean outcome_b|`:
- choice: top-label ECE (confidence = the largest emitted probability, ties to declared order; outcome = the top
  option is the label);
- predicate: ECE of P(true) against 1[label = true];
- score: the mean over k = 0..K-2 of the ECE of the cumulative P(level <= k) against 1[label <= k].
Brier: multiclass `sum_k (p_k - 1[y = k])^2` for choice and score, `(P(true) - y)^2` for predicates (range [0, 2]).
NLL: `-ln max(p_label, 1e-12)` on the emitted probabilities (the unresolved mass is never credited).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

from noema.core.values import JsonValue
from noema.labeling.calibrators.temperature import FloatArray, IndexArray
from noema.labeling.dataset import QuestionKind

ECE_BINS: Final = 15
NLL_FLOOR: Final = 1e-12
BOOTSTRAP_RESAMPLES: Final = 200


@dataclass(frozen=True, slots=True)
class Scores:
    ece: float
    brier: float
    nll: float


def _bins(conf: FloatArray) -> list[IndexArray]:
    n = conf.shape[0]
    order = np.argsort(conf, kind="stable")
    return [b for b in np.array_split(order, min(ECE_BINS, n)) if b.size] if n else []


def binned_ece(conf: FloatArray, outcome: FloatArray) -> float:
    """ECE over equal-mass bins (0.0 for no rows)."""
    n = conf.shape[0]
    if n == 0:
        return 0.0
    return float(sum(b.size / n * abs(conf[b].mean() - outcome[b].mean()) for b in _bins(conf)))


def _pairs(
    kind: QuestionKind, options: Sequence[str], probs: FloatArray, y: IndexArray
) -> list[tuple[FloatArray, FloatArray]]:
    """The (confidence, outcome) series whose ECEs are averaged (one series except for scores)."""
    if kind == "predicate":
        ti = list(options).index("true")
        return [(probs[:, ti], (y == ti).astype(np.float64))]
    if kind == "choice":
        top = np.argmax(probs, axis=1)
        return [(probs[np.arange(y.shape[0]), top], (top == y).astype(np.float64))]
    cum = np.cumsum(probs, axis=1)
    return [(cum[:, k], (y <= k).astype(np.float64)) for k in range(len(options) - 1)]


def question_ece(kind: QuestionKind, options: Sequence[str], probs: FloatArray, y: IndexArray) -> float:
    """The A1 ECE of the question type (module docstring)."""
    series = _pairs(kind, options, probs, y)
    return float(np.mean([binned_ece(c, o) for c, o in series])) if series else 0.0


def brier(kind: QuestionKind, options: Sequence[str], probs: FloatArray, y: IndexArray) -> float:
    """Mean Brier score (module docstring)."""
    n = y.shape[0]
    if n == 0:
        return 0.0
    onehot = np.zeros_like(probs)
    onehot[np.arange(n), y] = 1.0
    if kind == "predicate":
        ti = list(options).index("true")
        return float(np.mean((probs[:, ti] - onehot[:, ti]) ** 2))
    return float(np.mean(np.sum((probs - onehot) ** 2, axis=1)))


def nll(probs: FloatArray, y: IndexArray) -> float:
    """Mean negative log-likelihood of the labels."""
    n = y.shape[0]
    if n == 0:
        return 0.0
    return float(np.mean(-np.log(np.maximum(probs[np.arange(n), y], NLL_FLOOR))))


def scores(kind: QuestionKind, options: Sequence[str], probs: FloatArray, y: IndexArray) -> Scores:
    """ECE, Brier and NLL together."""
    return Scores(question_ece(kind, options, probs, y), brier(kind, options, probs, y), nll(probs, y))


def reliability_bins(kind: QuestionKind, options: Sequence[str], probs: FloatArray, y: IndexArray) -> list[JsonValue]:
    """The contract `Bin`s of the A1 series (scores: all cumulative series pooled), lowest confidence first."""
    series = _pairs(kind, options, probs, y)
    if not series:
        return []
    conf = np.concatenate([c for c, _ in series])
    outcome = np.concatenate([o for _, o in series])
    out: list[JsonValue] = []
    for b in _bins(conf):
        c = conf[b]
        out.append(
            {
                "lower": _prob(float(c.min())),
                "upper": _prob(float(c.max())),
                "n": int(b.size),
                "mean_predicted": _prob(float(c.mean())),
                "observed": _prob(float(outcome[b].mean())),
            }
        )
    return out


def bootstrap_ci(
    kind: QuestionKind, options: Sequence[str], probs: FloatArray, y: IndexArray, *, seed: int
) -> dict[str, JsonValue]:
    """Percentile 95% bootstrap intervals `[low, high]` of ece, brier and nll (deterministic for a seed)."""
    n = y.shape[0]
    if n == 0:
        return {}
    rng = np.random.Generator(np.random.PCG64(seed))
    samples = np.array(
        [
            (lambda s: (s.ece, s.brier, s.nll))(scores(kind, options, probs[idx], y[idx]))
            for idx in (rng.integers(0, n, size=n) for _ in range(BOOTSTRAP_RESAMPLES))
        ]
    )
    low, high = np.percentile(samples, 2.5, axis=0), np.percentile(samples, 97.5, axis=0)
    out: dict[str, JsonValue] = {}
    for i, name in enumerate(("ece", "brier", "nll")):
        out[name] = [round(float(low[i]), 6), round(float(high[i]), 6)]
    return out


def ece_lower_bound(
    kind: QuestionKind, options: Sequence[str], probs: FloatArray, y: IndexArray, *, seed: int
) -> float:
    """The basic (reverse-percentile) bootstrap 95% lower bound of the ECE, `max(0, 2 ECE - q97.5(ECE*))`.

    The binned ECE is biased upward (bin noise never cancels), so the percentile interval of a well-calibrated
    small slice excludes 0; the basic interval corrects for that bias, which the drift rule "the CI lower bound
    exceeds ece_max" needs (ADR-003 §5)."""
    n = y.shape[0]
    if n == 0:
        return 0.0
    point = question_ece(kind, options, probs, y)
    rng = np.random.Generator(np.random.PCG64(seed))
    resampled = [
        question_ece(kind, options, probs[idx], y[idx])
        for idx in (rng.integers(0, n, size=n) for _ in range(BOOTSTRAP_RESAMPLES))
    ]
    return max(0.0, 2.0 * point - float(np.percentile(resampled, 97.5)))


def _prob(v: float) -> float:
    return round(min(max(v, 0.0), 1.0), 6)
