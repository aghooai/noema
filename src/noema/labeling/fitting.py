"""Calibrator fitting (06 §2.3 step 2; ADR-003 §1, §4; spec-06 §8.1, §8.3).

One artifact holds both parts, fitted in order on the calibration split:
1. a per-member temperature `T_m` on each member's log-probabilities (logprob or verbalized; stated zeros get the
   epsilon floor), fitted on the rows where the label is one of the member's measured options;
2. the linear pool of the tempered members (`p = sum w_m q_m`, `u = sum w_m u_m`, weights renormalized per row);
3. the pooled transform: a temperature for choice and score questions, Platt for predicates.
`predict` applies the same steps (or only the raw pool, for the uncalibrated baseline) to produce the emitted
probabilities over the declared options; the unresolved mass is preserved, never spread.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np

from noema.labeling.calibrators.platt import fit_platt, platt_inputs
from noema.labeling.calibrators.temperature import (
    BoolArray,
    FloatArray,
    fit_temperature,
    log_probs,
    softmax_masked,
)
from noema.labeling.calibrators.transforms import DEFAULT_EPSILON
from noema.labeling.dataset import Arrays, LabeledSet


@dataclass(frozen=True, slots=True)
class FittedParams:
    """The fitted artifact parameters (`noema.calibrator` columns `method`, `member_t`, `pooled`, `epsilon`)."""

    method: Literal["temperature", "platt"]
    member_t: Mapping[str, float]
    pooled: Mapping[str, float]
    epsilon: float = DEFAULT_EPSILON


def _tempered(p: FloatArray, u: FloatArray, mask: BoolArray, t: float, epsilon: float) -> FloatArray:
    z = log_probs(p, u, mask, epsilon)
    q: FloatArray = softmax_masked(z, mask, 1.0 / t) * (1.0 - u)[:, None]
    return np.where(mask, q, 0.0)


def _pool(ls: LabeledSet, a: Arrays, q: Mapping[str, FloatArray]) -> tuple[FloatArray, FloatArray, BoolArray]:
    n, k = a.y.shape[0], len(ls.options)
    p, u, mask = np.zeros((n, k)), np.zeros(n), np.zeros((n, k), dtype=np.bool_)
    for m in ls.member_ids:
        w = a.w[m]
        p += w[:, None] * q[m]
        u += w * a.u[m]
        mask |= a.mask[m]
    return p, u, mask


def _true_index(ls: LabeledSet) -> int:
    return ls.options.index("true")


def predict(ls: LabeledSet, a: Arrays, params: FittedParams | None) -> FloatArray:
    """The emitted probabilities (n x K, declared order, rows sum to 1 - u): the raw linear pool when `params` is
    None, else the fully calibrated distribution."""
    if params is None:
        q = {m: np.where(a.mask[m], a.p[m], 0.0) for m in ls.member_ids}
        return _pool(ls, a, q)[0]
    eps = params.epsilon
    q = {
        m: _tempered(a.p[m], a.u[m], a.mask[m], params.member_t.get(m, 1.0), eps) if m in params.member_t else a.p[m]
        for m in ls.member_ids
    }
    pooled, u, mask = _pool(ls, a, q)
    if params.method == "temperature":
        return _tempered(pooled, u, mask, params.pooled["temperature"], eps)
    ti = _true_index(ls)
    x = platt_inputs(pooled[:, ti], u, eps)
    cal = 1.0 / (1.0 + np.exp(-(params.pooled["a"] * x + params.pooled["b"])))
    out = np.zeros_like(pooled)
    out[:, ti] = cal * (1.0 - u)
    out[:, 1 - ti] = (1.0 - cal) * (1.0 - u)
    return out


def fit_params(ls: LabeledSet, a: Arrays, epsilon: float = DEFAULT_EPSILON) -> FittedParams:
    """Fit the per-member temperatures, then the pooled transform, on the examples in `a`."""
    rows = np.arange(a.y.shape[0])
    member_t: dict[str, float] = {}
    for m in ls.member_ids:
        ok = a.mask[m][rows, a.y]
        z = log_probs(a.p[m], a.u[m], a.mask[m], epsilon)
        member_t[m] = fit_temperature(z[ok], a.mask[m][ok], a.y[ok])
    q = {m: _tempered(a.p[m], a.u[m], a.mask[m], member_t[m], epsilon) for m in ls.member_ids}
    pooled, u, mask = _pool(ls, a, q)
    if ls.kind == "predicate":
        ti = _true_index(ls)
        x = platt_inputs(pooled[:, ti], u, epsilon)
        y = (a.y == ti).astype(np.float64)
        slope, intercept = fit_platt(x, y)
        return FittedParams("platt", member_t, {"a": slope, "b": intercept}, epsilon)
    ok = mask[rows, a.y]
    z = log_probs(pooled, u, mask, epsilon)
    t = fit_temperature(z[ok], mask[ok], a.y[ok])
    return FittedParams("temperature", member_t, {"temperature": t}, epsilon)
