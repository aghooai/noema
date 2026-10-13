"""The fit window of a labeling run (ADR-003 §3; 06 §2.3): the label items of every run of the same question
definition (signature, profile version, labeler set) started within `LABEL_WINDOW`.

Each item row is parsed once into a compact `WindowItem` (the fitting example, the consensus view and the binding)
and the row itself is dropped, so a window holds no labeler answers, excerpts or ensemble documents. A decision
counts once: if a retry stored it in two runs, the earliest stored item wins. `list_runs` is id DESC and run ids are
time-ordered, so paging stops at the first run started before the window. The window needs the dropped items too (the
consensus rates, the lift and the subset shares count every sampled item), so `LabelRepo.kept_items`, which returns
kept items only, does not replace this read; the drift job, which reads kept labels only, uses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from noema.core.protocols.storage import Row, UnitOfWork
from noema.labeling.consensus import SampledItem
from noema.labeling.dataset import Example
from noema.labeling.deps import LIST_PAGE
from noema.labeling.records import ItemFacts, example, item_facts, sampled_item
from noema.labeling.runs_open import same_definition


@dataclass(frozen=True, slots=True)
class WindowItem:
    """One label item of a fit window, parsed once."""

    item_id: str
    decision_id: str
    subset: tuple[str, ...]
    view: str
    uniform: bool
    kept: bool
    facts: ItemFacts | None
    example: Example | None
    sampled: SampledItem | None


def _ulid(item_id: str) -> str:
    return item_id.partition("_")[2]  # kept (lab_) and dropped (ldrop_) ids share one time-ordered ULID space


def window_item(row: Row, default_at: datetime) -> WindowItem:
    """The compact form of one stored item (`default_at` when the row has no `created_at`)."""
    facts = item_facts(row)
    at = row.get("created_at")
    subset = row.get("member_subset")
    return WindowItem(
        item_id=str(row["id"]),
        decision_id=str(row.get("decision_id")),
        subset=tuple(sorted(str(m) for m in subset)) if isinstance(subset, list | tuple) else (),
        view=str(row.get("input_view_signature")),
        uniform=row.get("sample_reason") == "uniform",
        kept=bool(row.get("kept")),
        facts=facts,
        example=example(row, at if isinstance(at, datetime) else default_at, facts),
        sampled=sampled_item(row, facts),
    )


async def window_items(uow: UnitOfWork, run: Row, since: datetime, default_at: datetime) -> list[WindowItem]:
    """The items of the runs of `run`'s definition started at or after `since`, one per decision."""
    signature, version = str(run.get("question_signature")), str(run.get("profile_version"))
    stored = run.get("labelers")
    labelers = [str(x) for x in stored] if isinstance(stored, list | tuple) else []
    by_decision: dict[str, WindowItem] = {}
    after: str | None = None
    while True:
        page = await uow.labels.list_runs(
            profile_id=str(run["profile_id"]),
            question_name=str(run["question_name"]),
            status=None,
            after=after,
            limit=LIST_PAGE,
        )
        for r in page.data:
            started = r.get("started_at")
            if isinstance(started, datetime) and started < since:
                return list(by_decision.values())
            if not isinstance(started, datetime) or not same_definition(r, signature, version, labelers):
                continue
            for row in await uow.labels.items(str(r["id"])):
                w = window_item(row, default_at)
                seen = by_decision.get(w.decision_id)
                if seen is None or _ulid(w.item_id) < _ulid(seen.item_id):
                    by_decision[w.decision_id] = w
        if not page.has_more or page.next_cursor is None:
            return list(by_decision.values())
        after = page.next_cursor
