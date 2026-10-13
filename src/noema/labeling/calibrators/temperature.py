"""Temperature fitting (06 §2.3 step 2; ADR-003 §1).

`fit_temperature` minimizes the mean negative log-likelihood of `softmax(z / T)[label]` over the measured options of
each row, where `z = ln max(p / (1 - u), eps)` (the epsilon floor for stated zeros, spec-06 §8.3). With
`beta = 1/T` the objective `mean(logsumexp(beta z) - beta z_y)` is convex, its derivative `mean(E_q[z] - z_y)` is
increasing, and the root is found by a bracketed (safeguarded) Newton iteration on `beta`. The result is clamped to
[T_MIN, T_MAX]; rows with a single measured option carry no information and are ignored.
"""

from __future__ import annotations

from typing import Final

import numpy as np
from numpy.typing import NDArray

T_MIN: Final = 0.05
T_MAX: Final = 20.0
_TOL: Final = 1e-10
_MAX_ITER: Final = 100

type FloatArray = NDArray[np.float64]
type BoolArray = NDArray[np.bool_]
type IndexArray = NDArray[np.intp]


def log_probs(p: FloatArray, unresolved: FloatArray, mask: BoolArray, epsilon: float) -> FloatArray:
    """`ln max(p / (1 - u), eps)` on measured entries, 0 elsewhere (rows with `u >= 1` give the floor)."""
    scale = np.where(unresolved < 1.0, 1.0 - unresolved, 1.0)[:, None]
    floored = np.maximum(np.where(mask, p, 0.0) / scale, epsilon)
    return np.where(mask, np.log(floored), 0.0)


def softmax_masked(z: FloatArray, mask: BoolArray, beta: float) -> FloatArray:
    """Row-wise softmax of `beta * z` over the measured entries (0 elsewhere)."""
    s = np.where(mask, beta * z, -np.inf)
    s = s - s.max(axis=1, keepdims=True)
    w = np.where(mask, np.exp(s), 0.0)
    total: FloatArray = w.sum(axis=1, keepdims=True)
    return w / total


def _gradient(z: FloatArray, mask: BoolArray, zy: FloatArray, beta: float) -> tuple[float, float]:
    w = softmax_masked(z, mask, beta)
    ez = (w * z).sum(axis=1)
    ez2 = (w * z * z).sum(axis=1)
    return float(np.mean(ez - zy)), float(np.mean(ez2 - ez * ez))


def fit_temperature(z: FloatArray, mask: BoolArray, labels: IndexArray) -> float:
    """The NLL-optimal temperature for log-probabilities `z` (n x K), measured `mask` and label indices (every label
    must be measured in its row). 1.0 when no row is informative."""
    informative = mask.sum(axis=1) >= 2
    z, mask, labels = z[informative], mask[informative], labels[informative]
    n = labels.shape[0]
    if n == 0:
        return 1.0
    zy = z[np.arange(n), labels]
    lo, hi = 1.0 / T_MAX, 1.0 / T_MIN
    g_lo, _ = _gradient(z, mask, zy, lo)
    if g_lo >= 0.0:
        return T_MAX
    g_hi, _ = _gradient(z, mask, zy, hi)
    if g_hi <= 0.0:
        return T_MIN
    beta = 1.0
    for _ in range(_MAX_ITER):
        g, h = _gradient(z, mask, zy, beta)
        if abs(g) < _TOL:
            break
        if g > 0.0:
            hi = beta
        else:
            lo = beta
        step = beta - g / h if h > 0.0 else -1.0
        nxt = step if lo < step < hi else 0.5 * (lo + hi)
        if abs(nxt - beta) < _TOL * max(1.0, beta):
            beta = nxt
            break
        beta = nxt
    return float(min(max(1.0 / beta, T_MIN), T_MAX))
