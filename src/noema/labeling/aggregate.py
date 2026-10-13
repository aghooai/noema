"""Run aggregates recomputed from a run's items (the contract `LabelingRun`; spec-03 `labeling_run`).

Recomputing from the items (instead of incrementing per item) keeps the counts exact under concurrent item jobs and
retries: once a run has completed, kept plus the dropped counts equals sampled by construction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from noema.core.protocols.storage import Row
from noema.core.values import JsonValue
from noema.labeling.consensus import consensus_stats
from noema.labeling.records import item_facts, option_key, sampled_item

_DROPS: Final = ("disagreed", "refused", "parse_error", "labeler_error")
_USAGE_INTS: Final = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "reasoning_tokens",
)


@dataclass(frozen=True, slots=True)
class RunAggregate:
    counts: dict[str, JsonValue]
    consensus_rate: float | None
    consensus_rate_by_class: dict[str, JsonValue]
    n_by_class: dict[str, JsonValue]
    usage: dict[str, JsonValue]
    kept_uniform: int
    class_share: dict[str, float]  # per declared option: its share of the kept uniform labels (empty without any)

    def changes(self) -> dict[str, object]:
        """The `LabelRepo.update_run` columns."""
        return {
            "counts": self.counts,
            "consensus_rate": self.consensus_rate,
            "consensus_rate_by_class": self.consensus_rate_by_class,
            "n_by_class": self.n_by_class,
            "usage": self.usage,
        }


def _usage(items: Sequence[Row]) -> dict[str, JsonValue]:
    ints = dict.fromkeys(_USAGE_INTS, 0)
    cost, cost_estimated, tokens_estimated = 0.0, False, False
    for item in items:
        answers = item.get("labeler_answers")
        for a in answers if isinstance(answers, list) else []:
            if not isinstance(a, dict) or not isinstance(a.get("usage"), dict):
                continue
            for k in _USAGE_INTS:
                ints[k] += int(a["usage"].get(k) or 0)
            tokens_estimated = tokens_estimated or bool(a["usage"].get("tokens_estimated"))
            cost += float(a.get("cost_usd") or 0.0)
            cost_estimated = cost_estimated or bool(a.get("cost_estimated"))
    out: dict[str, JsonValue] = dict(ints)
    out.update(cost_usd=round(cost, 6), cost_estimated=cost_estimated, tokens_estimated=tokens_estimated)
    return out


def aggregate(items: Sequence[Row]) -> RunAggregate:
    """Counts, consensus rates (by class over the declared options of the latest item), kept per class, usage and
    the label class shares of the uniform slice (the label-distribution-shift input, 02 §5.5)."""
    drops = {r: sum(1 for i in items if not i.get("kept") and i.get("drop_reason") == r) for r in _DROPS}
    kept = sum(1 for i in items if i.get("kept"))
    counts: dict[str, JsonValue] = {
        "sampled": len(items),
        "sampled_uniform": sum(1 for i in items if i.get("sample_reason") == "uniform"),
        "sampled_uncertainty": sum(1 for i in items if i.get("sample_reason") == "uncertainty"),
        "kept": kept,
        **{f"dropped_{r}": n for r, n in drops.items()},
    }
    options: tuple[str, ...] = ()
    for item in reversed(items):
        facts = item_facts(item)
        if facts is not None:
            options = facts.options
            break
    sampled = [s for s in (sampled_item(i) for i in items) if s is not None]
    stats = consensus_stats(sampled, options)
    uniform_labels = [
        option_key(i.get("value")) for i in items if i.get("kept") and i.get("sample_reason") == "uniform"
    ]
    kept_uniform = len(uniform_labels)
    share = {c: round(uniform_labels.count(c) / kept_uniform, 6) for c in options} if kept_uniform else {}
    return RunAggregate(
        counts=counts,
        consensus_rate=None if stats.rate is None else round(stats.rate, 6),
        consensus_rate_by_class={c: round(r, 6) for c, r in stats.by_class.items()},
        n_by_class=dict[str, JsonValue](stats.kept_by_class),
        usage=_usage(items),
        kept_uniform=kept_uniform,
        class_share=share,
    )
