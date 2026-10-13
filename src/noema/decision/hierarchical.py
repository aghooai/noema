"""The hierarchical choice method: group formation, expansion and the two-round combination (spec-06 §19,
08 §3.4; IMPL-027).

Pure functions over 4-dp units: the expansion reads the emitted group units (largest remainder), each expanded
group's candidates are rounded to exactly its group mass, and the mass of unexpanded groups is `unresolved_mass`,
never spread over options.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from noema.core.protocols.decision import GroupSpec, OptionSpec

from .pool import UNITS, lr_units

HIER_EXPAND_UNITS: Final = 9_000  # the 0.9 coverage threshold in units of the stage-1 mass
HIER_MAX_EXPANDED: Final = 3
HIER_ROUND1_SHARE: Final = 0.5  # round 1 closes within the first half of the stage budget (R)


def form_groups(options: Sequence[OptionSpec], supplied: Sequence[str | None], limit: int) -> tuple[GroupSpec, ...]:
    """Request-supplied groups (order of first appearance) or a contiguous balanced server split into
    `ceil(K / L)` groups labeled `options a-b` (1-based declared positions)."""
    parts: list[tuple[str, list[OptionSpec]]] = []
    if supplied and supplied[0] is not None:
        labels = list(dict.fromkeys(s for s in supplied if s is not None))
        parts = [(lab, [o for o, s in zip(options, supplied, strict=True) if s == lab]) for lab in labels]
    else:
        k = len(options)
        g = -(-k // limit)
        size, extra = divmod(k, g)
        pos = 0
        for i in range(g):
            n = size + (1 if i < extra else 0)
            parts.append((f"options {pos + 1}-{pos + n}", list(options[pos : pos + n])))
            pos += n
    return tuple(
        GroupSpec(id=f"g{i + 1}", label=lab, options=tuple(opts), description=", ".join(o.key for o in opts))
        for i, (lab, opts) in enumerate(parts)
    )


def group_options(groups: Sequence[GroupSpec]) -> tuple[OptionSpec, ...]:
    """The round-1 options: one per group (`key = value = label`, `description` = its option keys)."""
    return tuple(
        OptionSpec(key=g.label, value=g.label, description=g.description, index=i) for i, g in enumerate(groups)
    )


def need(groups: Sequence[GroupSpec]) -> int:
    """The largest option set a member must carry: `max(G, largest group)` (spec-06 §19.3)."""
    return max(len(groups), max(len(g.options) for g in groups))


def expand(
    pool: Mapping[str, float], gids: Sequence[str], sizes: Mapping[str, int]
) -> tuple[list[str], dict[str, int]]:
    """`(E, units)`: the minimal prefix of groups by emitted units (ties -> declared order) covering >= 9,000
    units and holding >= 2 options in total (`sizes[g]` = the options of `g`; R1: a lone dominant one-option group
    also expands the next one), at most 3; `units` are the emitted group masses (they sum to exactly 10,000)."""
    units = lr_units(pool, gids, UNITS)
    order = sorted(range(len(gids)), key=lambda i: (-units[gids[i]], i))
    expanded: list[str] = []
    cum = options = 0
    for i in order[:HIER_MAX_EXPANDED]:
        expanded.append(gids[i])
        cum += units[gids[i]]
        options += sizes[gids[i]]
        if cum >= HIER_EXPAND_UNITS and options >= 2:
            break
    return expanded, units


@dataclass(frozen=True, slots=True)
class Combined:
    """A scored hierarchical combination: the re-pooled group masses `pool`, the expanded groups `expanded`, the
    emitted group `units`, the pooled joint `joint` over the expanded options, the unresolved mass `u` and the
    candidate units (each expanded group's candidates sum to its group units)."""

    pool: dict[str, float]
    expanded: list[str]
    units: dict[str, int]
    joint: dict[str, float]
    u: float
    cand_units: dict[str, int]
    u_units: int
    weights: dict[str, float]


def combine(
    groups: Sequence[GroupSpec],
    members: Sequence[str],
    weights: Mapping[str, float],
    p1: Mapping[str, Mapping[str, float]],
    p2: Mapping[str, Mapping[str, Mapping[str, float]]],
    expanded_run: Sequence[str],
) -> Combined | None:
    """Pool the members ok in both rounds (`members`; `p2[m][g][o]`, 1.0 for one-option groups). None when the
    re-pooled expansion needs a group round 2 never asked about (the answer is `unavailable / no_quorum`)."""
    gids = [g.id for g in groups]
    opts = {g.id: [o.key for o in g.options] for g in groups}
    total = sum(weights[m] for m in members)
    wt = {m: weights[m] / total for m in members}
    pool = {g: sum(wt[m] * p1[m].get(g, 0.0) for m in members) for g in gids}
    exp, units = expand(pool, gids, {g: len(opts[g]) for g in gids})
    if not set(exp) <= set(expanded_run):
        return None
    joint = {o: sum(wt[m] * p1[m].get(g, 0.0) * p2[m][g].get(o, 0.0) for m in members) for g in exp for o in opts[g]}
    u = sum(pool[g] for g in gids if g not in exp)
    cand_units: dict[str, int] = {}
    for g in exp:
        cand_units |= lr_units(joint, opts[g], units[g])
    return Combined(pool, exp, units, joint, u, cand_units, sum(units[g] for g in gids if g not in exp), wt)


def member_joint(
    groups: Sequence[GroupSpec], expanded: Sequence[str], p1: Mapping[str, float], p2: Mapping[str, Mapping[str, float]]
) -> tuple[dict[str, float], float, dict[str, int], int]:
    """One member's `(joint_m, u_m, emitted units, emitted unresolved units)` over the expanded groups: the
    distribution is rounded group by group to the member's own emitted group units."""
    gids = [g.id for g in groups]
    opts = {g.id: [o.key for o in g.options] for g in groups}
    joint = {o: p1.get(g, 0.0) * p2[g].get(o, 0.0) for g in expanded for o in opts[g]}
    u = sum(p1.get(g, 0.0) for g in gids if g not in expanded)
    gunits = lr_units(p1, gids, UNITS)
    emitted: dict[str, int] = {}
    for g in expanded:
        emitted |= lr_units(joint, opts[g], gunits[g])
    return joint, u, emitted, UNITS - sum(gunits[g] for g in expanded)
