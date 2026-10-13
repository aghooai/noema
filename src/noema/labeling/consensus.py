"""Label-quality statistics over sampled items, kept and dropped (06 §2.2; ADR-003 §3 rules A5, A6).

- `consensus_rate` = kept / sampled (the contract's "share of all sampled items kept").
- `consensus_rate_by_class[c]` = kept items labeled c / items where at least one labeler answered c (ok status):
  how often an item that some labeler put in class c survives the unanimous rule. A class no labeler ever chose has
  rate 0.0, which fails A5 (a zero rate is no evidence).
- `retained_share` = kept / sampled over the uniform-random slice only (`HoldoutMetrics.scope.retained_share`).
- Family-overlap lift of member m against labeler l: `agree(m, l) - chance(m, l)` over the items where both answered,
  with `chance = sum_k pi_m(k) pi_l(k)` from their own marginals; the member's lift is `max_l - min_l` and the
  binding's `max_member_lift` is the largest over its members, clamped to [-1, 1]. Without any overlapping item it is
  1.0 (no evidence of independence).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LabelerVote:
    """One labeler's answer: `value` is the option key iff `status == "ok"`."""

    model: str
    status: str
    value: str | None


@dataclass(frozen=True, slots=True)
class SampledItem:
    """One sampled item of a binding: kept or not, its consensus label, the slice, the labeler votes and each ok
    member's argmax option."""

    kept: bool
    label: str | None
    sample_reason: str
    votes: tuple[LabelerVote, ...]
    member_top: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ConsensusStats:
    rate: float | None
    by_class: dict[str, float]
    kept_by_class: dict[str, int]


def consensus_stats(items: Sequence[SampledItem], options: Sequence[str]) -> ConsensusStats:
    """The consensus rates (module docstring) and the kept count per declared class."""
    kept = Counter(i.label for i in items if i.kept and i.label is not None)
    involved: Counter[str] = Counter()
    for item in items:
        for value in {v.value for v in item.votes if v.status == "ok" and v.value is not None}:
            involved[value] += 1
    by_class = {c: (kept[c] / involved[c] if involved[c] else 0.0) for c in options}
    rate = sum(1 for i in items if i.kept) / len(items) if items else None
    return ConsensusStats(rate, by_class, {c: kept[c] for c in options})


def retained_share(items: Sequence[SampledItem]) -> float:
    """Kept share of the uniform-random slice (0.0 when it is empty)."""
    uniform = [i for i in items if i.sample_reason == "uniform"]
    return sum(1 for i in uniform if i.kept) / len(uniform) if uniform else 0.0


def _lift(pairs: list[tuple[str, str]]) -> float:
    n = len(pairs)
    agree = sum(1 for a, b in pairs if a == b) / n
    pm = Counter(a for a, _ in pairs)
    pl = Counter(b for _, b in pairs)
    chance = sum((pm[k] / n) * (pl[k] / n) for k in pm)
    return agree - chance


def family_overlap_lift(items: Sequence[SampledItem], member_ids: Sequence[str], labelers: Sequence[str]) -> float:
    """`max_member_lift` (module docstring)."""
    best: float | None = None
    for m in member_ids:
        lifts: list[float] = []
        for model in labelers:
            pairs = [
                (item.member_top[m], vote.value)
                for item in items
                if m in item.member_top
                for vote in item.votes
                if vote.model == model and vote.status == "ok" and vote.value is not None
            ]
            if pairs:
                lifts.append(_lift(pairs))
        if len(lifts) >= 2:
            spread = max(lifts) - min(lifts)
            best = spread if best is None else max(best, spread)
    if best is None:
        return 1.0
    return min(max(best, -1.0), 1.0)
