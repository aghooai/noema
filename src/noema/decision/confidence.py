"""Heuristic confidence and bands (spec-06 §11; IMPL-028, IMPL-039, IMPL-079).

Computed on the final probabilities before rounding (calibrated if calibrated) and the unresolved mass; emitted at
4 dp. `n` counts the candidates plus one unresolved bucket when the emitted unresolved mass is > 0.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Literal

from noema.config.models import ConfidenceBandsCfg
from noema.core.values import JsonValue

from .pool import r4

Band = Literal["high", "medium", "low"]


def normalized_max(p_max: float, n: int) -> float:
    """`(p_max - 1/n) / (1 - 1/n)`, floored at 0 (a top candidate below 1/n only occurs with a large unresolved
    bucket; the contract's `Prob` range wins there)."""
    return max(0.0, (p_max - 1 / n) / (1 - 1 / n))


def ordinal_dispersion(probs_by_index: Sequence[float]) -> float:
    """`max(0, 1 - Σ p_i |i - m| / MAD_u)` with `m` the mode (ties -> lowest index) and `MAD_u` the mean absolute
    deviation of the uniform distribution around its centre (IMPL-028)."""
    k = len(probs_by_index)
    mode = max(range(k), key=lambda i: (probs_by_index[i], -i))
    mad_u = sum(abs(i - (k - 1) / 2) for i in range(k)) / k
    return max(0.0, 1 - sum(p * abs(i - mode) for i, p in enumerate(probs_by_index)) / mad_u)


def normalized_entropy(buckets: Sequence[float]) -> float:
    """`-Σ p ln p / ln n` over `n = len(buckets)` buckets."""
    n = len(buckets)
    if n < 2:
        return 0.0
    return -sum(p * math.log(p) for p in buckets if p > 0) / math.log(n)


def jensen_shannon(p: Sequence[float], q: Sequence[float]) -> float:
    """Base-2 Jensen-Shannon divergence of two aligned bucket vectors."""

    def kl(a: Sequence[float], b: Sequence[float]) -> float:
        return sum(x * math.log2(x / y) for x, y in zip(a, b, strict=True) if x > 0)

    mix = [(x + y) / 2 for x, y in zip(p, q, strict=True)]
    return (kl(p, mix) + kl(q, mix)) / 2


def band_of(value: float, bands: ConfidenceBandsCfg) -> Band:
    """`high` if `value >= high_gte`, `low` if `value < low_lt`, else `medium` (from the pinned snapshot)."""
    if value >= bands.high_gte:
        return "high"
    if value < bands.low_lt:
        return "low"
    return "medium"


def confidence(
    *,
    is_score: bool,
    order: Sequence[str],
    prob: Mapping[str, float],
    index: Mapping[str, int],
    u: float,
    u_emitted: bool,
    member_buckets: Sequence[Sequence[float]],
    pool_buckets: Sequence[float],
    agreeing: int,
    responded: int,
    band_basis: float | None,
    bands: ConfidenceBandsCfg,
) -> dict[str, JsonValue]:
    """The `HeuristicConfidence` object. `order`: the candidates (emitted order); `prob`: unrounded final
    probabilities; `index`: declared index of each candidate (scores); `member_buckets`/`pool_buckets`: aligned
    member and pool vectors (candidates, then the unresolved bucket) for the divergence; `band_basis`: the max
    emitted probability when calibrated, else None (the band then reads the emitted value)."""
    n = len(order) + (1 if u_emitted else 0)
    if is_score:
        by_index = sorted(order, key=lambda k: index[k])
        value = ordinal_dispersion([prob.get(k, 0.0) for k in by_index])
        method = "ordinal_dispersion"
    else:
        value = normalized_max(prob.get(order[0], 0.0), n)
        method = "normalized_max"
    buckets = [prob.get(k, 0.0) for k in order] + ([u] if u_emitted else [])
    divergence: float | None = None
    if responded > 1:
        divergence = r4(sum(jensen_shannon(m, pool_buckets) for m in member_buckets) / len(member_buckets))
    emitted_value = r4(value)
    return {
        "kind": "heuristic",
        "value": emitted_value,
        "method": method,
        "band": band_of(emitted_value if band_basis is None else band_basis, bands),
        "margin": r4(prob.get(order[0], 0.0) - prob.get(order[1], 0.0)),
        "normalized_entropy": r4(normalized_entropy(buckets)),
        "agreement": r4(agreeing / responded) if responded else 0.0,
        "member_divergence": divergence,
    }
