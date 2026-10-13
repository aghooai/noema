"""`noema.label_item` rows: what a labeling job writes and what fitting, drift and W4 read back (spec-03 §2).

Column use beyond the spec-03 comments (the contract views pick only their declared fields):
- `labeler_answers`: `[{model, status, value}, ...]` (the W4 `LabelerAnswer`) plus, per entry, `alias`, `input_view`
  (the labeler's view of the media, IMPL-043), `usage` (token counts), `cost_usd`, `cost_estimated` and `error`.
- `ensemble`: `{value, probability, band, calibration_status}` (the W4 `Ensemble`: the served top candidate) plus the
  fitting inputs `kind`, `options` (declared option keys) and `members` (`{member_id: {dist, unresolved, weight}}`
  for the answering stage's ok members), so fitting never depends on the decision row's retention.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from noema.core.values import Cost, JsonValue, UsageRecord
from noema.labeling.consensus import LabelerVote, SampledItem
from noema.labeling.dataset import Example, MemberObs, QuestionKind
from noema.labeling.labelers import LabelerStatus, LabelQuestion, wire_value

DropReason = Literal["disagreed", "refused", "parse_error", "labeler_error"]
_KINDS: Final = frozenset({"choice", "predicate", "score"})


@dataclass(frozen=True, slots=True)
class LabelerResult:
    """One labeler's answer to one question."""

    alias: str
    model_id: str
    status: LabelerStatus
    key: str | None
    input_view: str
    usage: UsageRecord | None = None
    region: str | None = None
    cost: Cost | None = None
    error: str | None = None
    retryable: bool = False


def decide(results: Sequence[LabelerResult]) -> tuple[str | None, DropReason | None]:
    """The unanimous rule (D4): `(label, None)` iff every labeler answered ok with the same option, else
    `(None, reason)` with precedence labeler_error > refused > parse_error > disagreed."""
    statuses = {r.status for r in results}
    if "error" in statuses:
        return None, "labeler_error"
    if "refused" in statuses:
        return None, "refused"
    if "parse_error" in statuses:
        return None, "parse_error"
    keys = {r.key for r in results}
    if len(results) >= 2 and len(keys) == 1:
        return next(iter(keys)), None
    return None, "disagreed"


def _usage_doc(usage: UsageRecord | None) -> JsonValue:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "tokens_estimated": usage.tokens_estimated,
    }


def labeler_answers(q: LabelQuestion, results: Sequence[LabelerResult]) -> list[JsonValue]:
    """The `labeler_answers` column (module docstring)."""
    return [
        {
            "model": r.model_id,
            "status": r.status,
            "value": wire_value(q, r.key) if r.status == "ok" else None,
            "alias": r.alias,
            "input_view": r.input_view,
            "usage": _usage_doc(r.usage),
            "cost_usd": float(r.cost.usd) if r.cost is not None else 0.0,
            "cost_estimated": r.cost.estimated if r.cost is not None else False,
            "error": r.error,
        }
        for r in results
    ]


def option_key(value: object) -> str | None:
    """A stored label or labeler value as an option key (booleans as "true"/"false")."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else None


def votes(row: Mapping[str, object]) -> tuple[LabelerVote, ...]:
    """The labeler votes of an item row."""
    raw = row.get("labeler_answers")
    if not isinstance(raw, list):
        return ()
    return tuple(
        LabelerVote(str(a.get("model")), str(a.get("status")), option_key(a.get("value")))
        for a in raw
        if isinstance(a, dict)
    )


@dataclass(frozen=True, slots=True)
class ItemFacts:
    """What fitting reads from one item row's `ensemble`."""

    kind: QuestionKind
    options: tuple[str, ...]
    members: Mapping[str, MemberObs]
    weights: Mapping[str, float]


def item_facts(row: Mapping[str, object]) -> ItemFacts | None:
    """The fitting inputs of an item row, or None when the row lacks them."""
    ens = row.get("ensemble")
    if not isinstance(ens, dict):
        return None
    kind, options, members = ens.get("kind"), ens.get("options"), ens.get("members")
    if kind not in _KINDS or not isinstance(options, list) or not isinstance(members, dict):
        return None
    obs: dict[str, MemberObs] = {}
    weights: dict[str, float] = {}
    for m, doc in members.items():
        if not isinstance(doc, dict) or not isinstance(doc.get("dist"), dict):
            return None
        dist = {str(k): float(v) for k, v in doc["dist"].items() if isinstance(v, int | float)}
        obs[str(m)] = MemberObs(dist, float(doc.get("unresolved") or 0.0))
        weights[str(m)] = float(doc.get("weight") or 0.0)
    qkind: QuestionKind = "choice" if kind == "choice" else "predicate" if kind == "predicate" else "score"
    return ItemFacts(qkind, tuple(str(o) for o in options), obs, weights)


def sampled_item(row: Mapping[str, object], facts: ItemFacts | None = None) -> SampledItem | None:
    """The consensus/lift view of an item row (members' argmax in declared order); `facts` = `item_facts(row)` when
    the caller has parsed them already."""
    facts = facts or item_facts(row)
    if facts is None:
        return None
    top: dict[str, str] = {}
    for m, obs in facts.members.items():
        measured = [(o, obs.dist[o]) for o in facts.options if o in obs.dist]
        if measured:
            best = max(v for _, v in measured)
            top[m] = next(o for o, v in measured if v == best)
    kept = bool(row.get("kept"))
    return SampledItem(
        kept, option_key(row.get("value")) if kept else None, str(row.get("sample_reason")), votes(row), top
    )


def example(row: Mapping[str, object], at: datetime, facts: ItemFacts | None = None) -> Example | None:
    """The fitting example of a kept item row, or None (`facts` as in `sampled_item`)."""
    facts = facts or item_facts(row)
    label = option_key(row.get("value"))
    if facts is None or not row.get("kept") or label is None or label not in facts.options:
        return None
    return Example(facts.members, facts.weights, label, at)
