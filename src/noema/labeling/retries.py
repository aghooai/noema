"""What an earlier attempt of a `labeling.item` job stored (spec-11 §4; the item key of spec-03 `label_item`).

A decision's answer can sit in any run of its question definition: a run can close at its limits (or because the
labeler set changed) between two attempts. A retry therefore reads every item of the decision in one indexed query
(`LabelRepo.items_for_decision`, on `label_item_by_decision`) and keeps those of the target's question definition
(question name, the run's question signature, stage profile and version), so it never labels (and bills) an answer
twice.
"""

from __future__ import annotations

from collections.abc import Sequence

from noema.core.protocols.storage import Row, UnitOfWork
from noema.labeling.targets import Target


def _holds(run: Row, t: Target) -> bool:
    return (
        run.get("question_signature") == t.question.signature
        and str(run.get("profile_id")) == t.profile_id
        and str(run.get("profile_version")) == t.profile_version
    )


async def stored_runs(uow: UnitOfWork, decision_id: str, todo: Sequence[Target]) -> dict[str, str]:
    """Question name -> the id of the run holding the decision's item for that target's definition (the earliest
    stored item when a definition holds two); targets with nothing stored are absent."""
    by_name = {t.question.name: t for t in todo}
    runs: dict[str, Row] = {}
    out: dict[str, str] = {}
    for item in await uow.labels.items_for_decision(decision_id):  # id ASC: the earliest item first
        t = by_name.get(str(item.get("question_name")))
        if t is None or t.question.name in out:
            continue
        run_id = str(item["run_id"])
        if run_id not in runs:
            runs[run_id] = await uow.labels.update_run(run_id)  # no changes: a read of the run row
        if _holds(runs[run_id], t):
            out[t.question.name] = run_id
    return out
