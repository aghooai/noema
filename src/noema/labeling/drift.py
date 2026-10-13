"""The weekly `labeling.drift` job (ADR-003 §5; 06 §2.3 step 6; spec-11 §4).

For every active artifact, and for every tenant a platform artifact serves (not the tenants whose own active artifact
of the same binding takes precedence), the job takes that tenant's uniform random slice of consensus labels that
match the artifact's binding, applies the artifact exactly as the engine does (the runtime transforms), and computes
the per-type ECE with the bias-corrected (basic) bootstrap 95% lower bound. The slice is the last `DRIFT_WINDOW`; a
week with fewer than `DRIFT_MIN_N` labels widens it week by week up to `DRIFT_MAX_WEEKS`, and an artifact that is
still too thin on every slice is logged at WARNING ("calibrator drift not evaluated"). `ece > drift_ece_max`
downgrades the artifact (`drift_ece`), else a lower bound above `ece_max` does (`drift_ci`):
`SystemCalls.set_calibrator_status(..., "downgraded")` bumps the scope's epoch, so pods serve `uncalibrated` within
the registry staleness bound. A platform artifact is downgraded globally (no per-tenant suppression in Phase 1). The
slice is one `LabelRepo.kept_items` read per tenant (the definition's kept uniform labels since the widest slice's
start), filtered to the binding's member subset and input view, one item per decision. Each artifact is checked in a
`labeling.drift` span; the gauge `aghoo_calibration_ece` carries every measured ECE, a downgrade is counted in
`aghoo_calibrator_downgrades_total{reason}` and logged at WARNING (the alert). A refused
downgrade (already downgraded, retired or gone) is skipped; any other failure is re-raised after the remaining
artifacts were checked, so the queue retries the job. The numeric work runs in a worker thread. The job also
schedules `labeling.run` for runs older than `RUN_MAX_AGE`.
"""

from __future__ import annotations

import functools
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import anyio.to_thread
import numpy as np

from noema.config.models import AcceptanceCfg
from noema.core.protocols.labeling import Calibrator, CalibratorBindingKey
from noema.core.protocols.storage import Job, Row
from noema.db.system import AdminError
from noema.labeling.artifact import calibrator_from_row
from noema.labeling.dataset import QuestionKind
from noema.labeling.deps import (
    DRIFT_ACTOR,
    DRIFT_MAX_WEEKS,
    DRIFT_MIN_N,
    DRIFT_WINDOW,
    JOB_PRIORITY,
    JOB_RUN,
    LIST_PAGE,
    RUN_MAX_AGE,
    LabelingDeps,
)
from noema.labeling.lifecycle import DowngradeReason
from noema.labeling.metrics import ece_lower_bound, question_ece
from noema.labeling.records import ItemFacts, item_facts, option_key

_log: Final = logging.getLogger("noema.labeling.drift")
_REFUSED: Final = frozenset({"forbidden", "not_found", "invalid_state"})  # someone else moved the artifact first


def calibrated(cal: Calibrator, facts: ItemFacts) -> list[float]:
    """The artifact's probabilities for one item over the declared options (per-member, pool, pooled)."""
    total = sum(max(w, 0.0) for w in facts.weights.values())
    weights = {
        m: (max(facts.weights.get(m, 0.0), 0.0) / total if total > 0 else 1.0 / len(facts.members))
        for m in facts.members
    }
    pool: dict[str, float] = {}
    u = 0.0
    for m, obs in facts.members.items():
        q = cal.member(m).apply(obs.dist, obs.unresolved)
        for o, v in q.items():
            pool[o] = pool.get(o, 0.0) + weights[m] * v
        u += weights[m] * obs.unresolved
    out = cal.pooled.apply(pool, u)
    return [out.get(o, 0.0) for o in facts.options]


def verdict(ece: float, ci_low: float, acceptance: AcceptanceCfg) -> DowngradeReason | None:
    """`drift_ece` above `drift_ece_max`, else `drift_ci` when the CI lower bound exceeds `ece_max`."""
    if ece > acceptance.drift_ece_max:
        return "drift_ece"
    if ci_low > acceptance.ece_max:
        return "drift_ci"
    return None


@dataclass(frozen=True, slots=True)
class SliceEce:
    """One tenant slice of an artifact: its label count, the weeks it spans, and the ECE with its CI lower bound
    (None when even the widest slice has fewer than `DRIFT_MIN_N` labels)."""

    n: int
    weeks: int
    ece: float | None = None
    ci_low: float | None = None


def measure(cal: Calibrator, items: Sequence[Row], now: datetime, seed: int) -> SliceEce:
    """The narrowest slice (1..`DRIFT_MAX_WEEKS` weeks back from `now`) with at least `DRIFT_MIN_N` usable labels,
    measured; pure and CPU-bound (the job runs it in a worker thread)."""
    points: list[tuple[datetime, list[float], int]] = []
    options: Sequence[str] = ()
    kind: QuestionKind = "choice"
    for item in items:
        facts, label, at = item_facts(item), option_key(item.get("value")), item.get("created_at")
        if facts is None or label not in facts.options or not isinstance(at, datetime):
            continue
        if set(facts.members) != set(cal.key.member_subset):
            continue
        if not options:
            options, kind = facts.options, facts.kind
        if facts.options == options:
            points.append((at, calibrated(cal, facts), options.index(label)))
    for weeks in range(1, DRIFT_MAX_WEEKS + 1):
        chosen = [(p, y) for at, p, y in points if at >= now - weeks * DRIFT_WINDOW]
        if len(chosen) >= DRIFT_MIN_N:
            probs = np.array([p for p, _ in chosen], dtype=np.float64)
            y = np.array([label for _, label in chosen], dtype=np.intp)
            ece = question_ece(kind, options, probs, y)
            return SliceEce(len(chosen), weeks, ece, ece_lower_bound(kind, options, probs, y, seed=seed))
    return SliceEce(len(points), DRIFT_MAX_WEEKS)


class DriftJob:
    """The `labeling.drift` handler (module docstring)."""

    def __init__(self, deps: LabelingDeps) -> None:
        self._d = deps

    async def __call__(self, job: Job) -> None:
        acceptance = self._d.config.current().labeling.acceptance
        async with self._d.db.system_uow() as calls:
            epochs = await calls.calibrator_epochs()
            rows = await calls.active_calibrators(sorted(epochs)) if epochs else ()
        tenants = sorted(k for k in epochs if k != "platform")
        now = self._d.clock.now()
        parsed: list[tuple[Calibrator, str, str]] = []
        for row in rows:
            try:
                parsed.append((calibrator_from_row(row), str(row["question_name"]), str(row.get("tenant_id"))))
            except (KeyError, TypeError, ValueError):
                continue
        parsed.sort(key=lambda p: p[0].scope != "tenant")  # tenant artifacts first: they shadow platform ones
        shadowed: set[tuple[str, CalibratorBindingKey]] = set()  # (tenant, binding) served by a tenant artifact
        failures: list[Exception] = []
        for cal, name, owner in parsed:
            served = [owner] if cal.scope == "tenant" else [t for t in tenants if (t, cal.key) not in shadowed]
            downgraded = False
            attrs = {"aghoo.profile": cal.key.profile_id, "aghoo.profile_version": cal.key.profile_version}
            attrs["aghoo.member.question"] = name
            if cal.scope == "tenant":
                attrs["aghoo.tenant"] = owner
            try:
                with self._d.telemetry.span("labeling.drift", **attrs):
                    downgraded = await self._evaluate(cal, served, now, acceptance)
            except Exception as exc:  # transient (db): check the other artifacts, then let the queue retry the job
                failures.append(exc)
            if cal.scope == "tenant" and not downgraded:
                shadowed.add((owner, cal.key))
        for tenant in tenants:
            await self._close_stale_runs(tenant, now)
        if failures:
            raise failures[0]

    async def _evaluate(self, cal: Calibrator, served: Sequence[str], now: datetime, acceptance: AcceptanceCfg) -> bool:
        """Check the artifact on each served tenant's slice; True once it was downgraded."""
        best_n = 0  # the largest thin slice (for the "not evaluated" warning)
        evaluated = False
        for tenant in served:
            items = await self._slice(tenant, cal, now - DRIFT_MAX_WEEKS * DRIFT_WINDOW)
            seed = int.from_bytes(
                hashlib.sha256(f"{cal.id}|{tenant}|{now.date().isoformat()}".encode()).digest()[:8], "big"
            )
            m = await anyio.to_thread.run_sync(functools.partial(measure, cal, items, now, seed))
            if m.ece is None or m.ci_low is None:
                best_n = max(best_n, m.n)
                continue
            evaluated = True
            self._d.telemetry.instruments.set(
                "aghoo_calibration_ece",
                m.ece,
                profile=cal.key.profile_id,
                signature=cal.key.question_signature,
                view_signature=cal.key.input_view_signature,
            )
            reason = verdict(m.ece, m.ci_low, acceptance)
            if reason is not None and await self._downgrade(cal, tenant, reason, m):
                return True
        if served and not evaluated:  # a platform artifact every tenant shadows serves nobody
            _log.warning(
                "calibrator drift not evaluated: too few uniform labels",
                extra={
                    "calibrator_id": cal.id,
                    "scope": cal.scope,
                    "n": best_n,
                    "min_n": DRIFT_MIN_N,
                    "window_days": DRIFT_MAX_WEEKS * DRIFT_WINDOW.days,
                },
            )
        return False

    async def _slice(self, tenant: str, cal: Calibrator, since: datetime) -> list[Row]:
        """The tenant's kept uniform items of the binding labeled at or after `since`, one per decision (the earliest
        stored item): one `LabelRepo.kept_items` read of the definition, filtered to the subset and the view."""
        key = cal.key
        out: dict[str, Row] = {}
        async with self._d.db.tenant_uow(tenant) as uow:
            rows = await uow.labels.kept_items(key.profile_id, key.profile_version, key.question_signature, since)
        for item in rows:  # created_at then id ASC
            subset = item.get("member_subset")
            if (
                isinstance(subset, list | tuple)
                and tuple(sorted(str(m) for m in subset)) == key.member_subset
                and item.get("input_view_signature") == key.input_view_signature
            ):
                out.setdefault(str(item.get("decision_id")), item)
        return list(out.values())

    async def _downgrade(self, cal: Calibrator, tenant: str, reason: DowngradeReason, m: SliceEce) -> bool:
        """True when this job downgraded the artifact; False when the definer function refused (someone else moved
        it first). Any other failure propagates (the job is retried)."""
        try:
            async with self._d.db.system_uow() as calls:
                await calls.set_calibrator_status(DRIFT_ACTOR, cal.id, "downgraded", reason=reason)
        except AdminError as exc:
            if exc.reason not in _REFUSED:
                raise
            _log.warning("calibrator downgrade refused", extra={"calibrator_id": cal.id, "error": str(exc)})
            return False
        self._d.telemetry.instruments.add("aghoo_calibrator_downgrades_total", reason=reason)
        _log.warning(
            "calibrator downgraded by drift",
            extra={
                "calibrator_id": cal.id,
                "scope": cal.scope,
                "tenant_id": tenant,
                "reason": reason,
                "n": m.n,
                "window_days": m.weeks * DRIFT_WINDOW.days,
            },
        )
        return True

    async def _close_stale_runs(self, tenant: str, now: datetime) -> None:
        stale: list[str] = []
        after: str | None = None
        async with self._d.db.tenant_uow(tenant) as uow:
            while True:
                page = await uow.labels.list_runs(
                    profile_id=None, question_name=None, status="running", after=after, limit=LIST_PAGE
                )
                for run in page.data:
                    started = run.get("started_at")
                    if isinstance(started, datetime) and now - started >= RUN_MAX_AGE:
                        stale.append(str(run["id"]))
                if not page.has_more or page.next_cursor is None:
                    break
                after = page.next_cursor
        for run_id in stale:
            await self._d.jobs.enqueue(JOB_RUN, tenant, {"run_id": run_id}, priority=JOB_PRIORITY, dedupe_key=run_id)
