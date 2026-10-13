"""Runtime calibration transforms (spec-06 §8.3, ADR-003 §4).

These run on the decision hot path (one call per answer and member), so they are plain Python over small dicts:
no numpy, no validation. Every transform preserves the unresolved mass `u`: the input distribution sums to `1 - u`
over the measured options (the dict keys) and so does the output.

- Temperature: `q_o = (1-u) * exp(ln max(p_o/(1-u), eps)/T) / sum_o' exp(ln max(p_o'/(1-u), eps)/T)`.
- Platt (predicates): `P_cal(true) = sigmoid(a * logit(clamp(P(true)/(1-u), eps, 1-eps)) + b)`,
  `P_cal(false) = 1 - P_cal(true)`, both scaled by `(1-u)`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

DEFAULT_EPSILON: Final = 1e-4


@dataclass(frozen=True, slots=True)
class IdentityTransform:
    """Leaves the distribution unchanged (a member without a fitted temperature)."""

    def apply(self, dist: Mapping[str, float], unresolved: float) -> dict[str, float]:
        """A copy of `dist`."""
        return dict(dist)


@dataclass(frozen=True, slots=True)
class TemperatureTransform:
    """Temperature scaling on log-probabilities with the epsilon floor for stated zeros."""

    t: float
    epsilon: float = DEFAULT_EPSILON

    def apply(self, dist: Mapping[str, float], unresolved: float) -> dict[str, float]:
        """The tempered distribution over the same keys, summing to `1 - unresolved`."""
        scale = 1.0 - unresolved
        if scale <= 0.0 or not dist:
            return dict(dist)
        inv_t = 1.0 / self.t
        eps = self.epsilon
        logits = {k: math.log(max(v / scale, eps)) * inv_t for k, v in dist.items()}
        top = max(logits.values())
        weights = {k: math.exp(v - top) for k, v in logits.items()}
        norm = scale / sum(weights.values())
        return {k: w * norm for k, w in weights.items()}


@dataclass(frozen=True, slots=True)
class PlattTransform:
    """Platt scaling of P(true) for predicates; the output has exactly the keys "true" and "false"."""

    a: float
    b: float
    epsilon: float = DEFAULT_EPSILON

    def apply(self, dist: Mapping[str, float], unresolved: float) -> dict[str, float]:
        """`{"true": P_cal * (1-u), "false": (1 - P_cal) * (1-u)}`."""
        scale = 1.0 - unresolved
        p = dist.get("true", 0.0) / scale if scale > 0.0 else 0.0
        p = min(max(p, self.epsilon), 1.0 - self.epsilon)
        cal = sigmoid(self.a * math.log(p / (1.0 - p)) + self.b)
        return {"true": cal * scale, "false": (1.0 - cal) * scale}


def sigmoid(x: float) -> float:
    """The logistic function, overflow-safe."""
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)
