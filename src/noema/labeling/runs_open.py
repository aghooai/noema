"""Opening labeling runs (spec-03 §2 `labeling_run`, the `labeling_run_open` index of revision 0002; ADR-003 §5).

A run is one question definition: a question signature, a profile version and a labeler set. The index allows one
running run per (tenant, stage profile, version, question name, question signature), so definitions that share a
name (facade questions are all `q0`, `q1`, ...; tenants reuse names such as "intent") each keep their own running run
(`LabelRepo.open_run(..., question_signature)`). Only a labeler-set change leaves a running run of another definition
in the slot: it is closed (its fit scheduled first when it may be due, so a failed enqueue leaves it open for the item
job's retry) and a new run opens. Two jobs racing for the slot: the loser's write violates the index and it re-reads
the winner's run in a fresh transaction.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final

from noema.core.protocols.storage import Row
from noema.core.values import JsonValue
from noema.labeling.deps import (
    JOB_PRIORITY,
    JOB_RUN,
    RUN_MAX_AGE,
    RUN_REFRESH_DELAY_S,
    LabelingDeps,
    fit_min_kept,
)
from noema.labeling.targets import Target

_log: Final = logging.getLogger("noema.labeling.runs")

ZERO_COUNTS: Final[dict[str, JsonValue]] = {
    "sampled": 0,
    "sampled_uniform": 0,
    "sampled_uncertainty": 0,
    "kept": 0,
    "dropped_disagreed": 0,
    "dropped_refused": 0,
    "dropped_parse_error": 0,
    "dropped_labeler_error": 0,
}
ZERO_USAGE: Final[dict[str, JsonValue]] = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "reasoning_tokens": 0,
    "cost_usd": 0.0,
    "cost_estimated": False,
    "tokens_estimated": False,
}


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def maybe_due(run: Row, now: datetime, n_min: int) -> bool:
    """Whether a run may have reached a closing limit, from its stored counts (an upper bound of its kept uniform
    labels; the counts lag the items by at most one pending refresh, which then closes and fits the run itself)."""
    counts = run.get("counts")
    kept = _int(counts.get("kept")) if isinstance(counts, dict) else 0
    uniform = _int(counts.get("sampled_uniform")) if isinstance(counts, dict) else 0
    started = run.get("started_at")
    old = isinstance(started, datetime) and now - started >= RUN_MAX_AGE
    return old or min(kept, uniform) >= fit_min_kept(n_min)


def same_definition(run: Row, signature: str, version: str, labelers: Sequence[str]) -> bool:
    """The run labels this question definition (signature and profile version) with this labeler set."""
    stored = run.get("labelers")
    same_labelers = isinstance(stored, list | tuple) and [str(x) for x in stored] == list(labelers)
    return run.get("question_signature") == signature and str(run.get("profile_version")) == version and same_labelers


def _matches(run: Row, t: Target, labelers: Sequence[str]) -> bool:
    return same_definition(run, t.question.signature, t.profile_version, labelers)


async def ensure_run(d: LabelingDeps, tenant_id: str, t: Target, labelers: Sequence[str]) -> Row:
    """The running run of the target's question definition: the open one, or a new one."""
    sig = t.question.signature
    async with d.db.tenant_uow(tenant_id) as uow:
        run = await uow.labels.open_run(t.profile_id, t.profile_version, t.question.name, sig)
    if run is not None and _matches(run, t, labelers):
        return run
    now = d.clock.now()
    if run is not None:
        await _close(d, tenant_id, run, now, d.config.current().labeling.acceptance.n_min)
    try:
        async with d.db.tenant_uow(tenant_id) as uow:
            return await uow.labels.create_run(_new_run(d, t, labelers, now))
    except Exception:  # a racing job took the slot first (labeling_run_open); its transaction committed
        async with d.db.tenant_uow(tenant_id) as uow:
            winner = await uow.labels.open_run(t.profile_id, t.profile_version, t.question.name, sig)
        if winner is not None and _matches(winner, t, labelers):
            return winner
        raise


async def _close(d: LabelingDeps, tenant_id: str, run: Row, now: datetime, n_min: int) -> None:
    """Close the open run of the previous labeler set; a run that may be due gets its fit scheduled first."""
    run_id = str(run["id"])
    if maybe_due(run, now, n_min):
        at = now + timedelta(seconds=RUN_REFRESH_DELAY_S)
        await d.jobs.enqueue(
            JOB_RUN, tenant_id, {"run_id": run_id}, priority=JOB_PRIORITY, run_after=at, dedupe_key=run_id
        )
    async with d.db.tenant_uow(tenant_id) as uow:
        await uow.labels.update_run(run_id, status="completed", completed_at=now)
    _log.info("labeling run closed: the labeler set changed", extra={"run_id": run_id})


def _new_run(d: LabelingDeps, t: Target, labelers: Sequence[str], now: datetime) -> dict[str, object]:
    return {
        "id": d.ids.new("lrun"),
        "profile_id": t.profile_id,
        "profile_version": t.profile_version,
        "question_name": t.question.name,
        "question_signature": t.question.signature,
        "status": "running",
        "labelers": list(labelers),
        "rule": "unanimous",
        "counts": dict(ZERO_COUNTS),
        "consensus_rate": None,
        "consensus_rate_by_class": {},
        "n_by_class": {},
        "usage": dict(ZERO_USAGE),
        "started_at": now,
    }
