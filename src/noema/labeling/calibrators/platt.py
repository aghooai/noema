"""Platt fitting for predicates (06 §2.3 step 2; spec-06 §8.3).

`fit_platt` fits `P(true) = sigmoid(a * x + b)` with `x = logit(clamp(P(true) / (1 - u), eps, 1 - eps))` by
Newton's method on the mean log loss plus a tiny ridge (`RIDGE`, which keeps the optimum finite when the classes are
separable), with a backtracking line search. Parameters are clamped to |a|, |b| <= PARAM_MAX.
"""

from __future__ import annotations

import math
from typing import Final

import numpy as np
from numpy.typing import NDArray

RIDGE: Final = 1e-6
PARAM_MAX: Final = 100.0
_MAX_ITER: Final = 100
_TOL: Final = 1e-10

type FloatArray = NDArray[np.float64]


def platt_inputs(p_true: FloatArray, unresolved: FloatArray, epsilon: float) -> FloatArray:
    """`logit(clamp(P(true) / (1 - u), eps, 1 - eps))`."""
    scale = np.where(unresolved < 1.0, 1.0 - unresolved, 1.0)
    p = np.clip(p_true / scale, epsilon, 1.0 - epsilon)
    out: FloatArray = np.log(p / (1.0 - p))
    return out


def _loss(x: FloatArray, y: FloatArray, a: float, b: float) -> float:
    s = a * x + b
    # log(1 + e^s) - y*s, computed stably
    soft = np.logaddexp(0.0, s)
    return float(np.mean(soft - y * s)) + 0.5 * RIDGE * (a * a + b * b)


def fit_platt(x: FloatArray, y: FloatArray) -> tuple[float, float]:
    """`(a, b)` minimizing the regularized log loss of `sigmoid(a x + b)` against `y` in {0, 1}; `(1.0, 0.0)` (the
    identity) for empty input."""
    if x.shape[0] == 0:
        return 1.0, 0.0
    a, b = 1.0, 0.0
    loss = _loss(x, y, a, b)
    for _ in range(_MAX_ITER):
        p = 1.0 / (1.0 + np.exp(-(a * x + b)))
        r = p - y
        w = p * (1.0 - p)
        g_a = float(np.mean(r * x)) + RIDGE * a
        g_b = float(np.mean(r)) + RIDGE * b
        h_aa = float(np.mean(w * x * x)) + RIDGE
        h_ab = float(np.mean(w * x))
        h_bb = float(np.mean(w)) + RIDGE
        det = h_aa * h_bb - h_ab * h_ab
        if det <= 0.0 or not math.isfinite(det):
            d_a, d_b = g_a, g_b  # gradient step
        else:
            d_a = (h_bb * g_a - h_ab * g_b) / det
            d_b = (h_aa * g_b - h_ab * g_a) / det
        step = 1.0
        while step > 1e-8:
            na = min(max(a - step * d_a, -PARAM_MAX), PARAM_MAX)
            nb = min(max(b - step * d_b, -PARAM_MAX), PARAM_MAX)
            new_loss = _loss(x, y, na, nb)
            if new_loss <= loss:
                break
            step *= 0.5
        else:
            break
        done = abs(loss - new_loss) < _TOL and abs(na - a) + abs(nb - b) < 1e-7
        a, b, loss = na, nb, new_loss
        if done:
            break
    return float(a), float(b)
