"""The linear pool, largest-remainder rounding and member argmax (spec-06 §8.1-§8.2, §19.6; IMPL-060).

Emitted probabilities are 4-dp units (1e-4) that sum exactly to `1 - unresolved_mass`; `raw_score` is a plain
round. Ordering and votes use the emitted values, ties in declared order.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Final

UNITS: Final = 10_000


def r4(x: float) -> float:
    """Plain 4-dp round (raw scores, confidence fields)."""
    return round(x, 4)


def lr_units(p: Mapping[str, float], keys: Sequence[str], target: int) -> dict[str, int]:
    """Largest remainder in 1e-4 units over `keys` (declared order breaks ties), summing to `target`."""
    scaled = {k: round(p.get(k, 0.0) * UNITS, 9) for k in keys}  # 9-dp guard against float noise
    base = {k: math.floor(scaled[k]) for k in keys}
    rem = {k: round(scaled[k] - base[k], 9) for k in keys}
    position = {k: i for i, k in enumerate(keys)}
    extra = max(0, target - sum(base.values()))
    for k in sorted(keys, key=lambda k: (-rem[k], position[k]))[:extra]:
        base[k] += 1
    return base


def unresolved_units(u: float) -> int:
    """The emitted unresolved mass in units: `round(u * 10_000)`."""
    return round(u * UNITS)


def round_units(p: Mapping[str, float], keys: Sequence[str], u: float) -> dict[str, float]:
    """Emitted 4-dp probabilities over `keys` summing to `1 - round(u, 4)` exactly in units."""
    units = lr_units(p, keys, UNITS - unresolved_units(u))
    return {k: units[k] / UNITS for k in keys}


def linear_pool(members: Sequence[tuple[float, Mapping[str, float], float]]) -> tuple[dict[str, float], float, float]:
    """`(p, u, weight_sum)` over `(weight, distribution, unresolved)` triples, weights renormalized over the
    members (`sum(p) + u == 1`); an empty pool or a zero weight sum gives `({}, 0.0, 0.0)`."""
    total = sum(w for w, _, _ in members)
    if total <= 0:
        return {}, 0.0, 0.0
    pooled: dict[str, float] = {}
    u = 0.0
    for w, dist, um in members:
        wt = w / total
        for k, v in dist.items():
            pooled[k] = pooled.get(k, 0.0) + wt * v
        u += wt * um
    return pooled, u, total


def argmax(dist: Mapping[str, float], keys: Sequence[str]) -> str | None:
    """The key of the largest value among the measured keys (absent keys are not candidates); ties in `keys`
    order. None when no key is measured."""
    best: str | None = None
    best_v = -1.0
    for k in keys:
        v = dist.get(k)
        if v is not None and v > best_v:
            best, best_v = k, v
    return best


def ordered(emitted: Mapping[str, float], keys: Sequence[str]) -> list[str]:
    """`keys` sorted by (-emitted probability, declared position)."""
    position = {k: i for i, k in enumerate(keys)}
    return sorted(keys, key=lambda k: (-emitted.get(k, 0.0), position[k]))
