"""The `labeling.run` job: refresh, close and fit one labeling run (ADR-003 §1, §3; 06 §2.3; spec-11 §4).

1. Recompute the run's aggregates from its items (`aggregate.py`) and store them (the run job is their only writer);
   the gauges `aghoo_label_consensus_ratio` and `aghoo_label_class_share` (the uniform slice's label distribution,
   the 02 §5.5 shift-alert input) carry them. The job runs in a `labeling.run` span.
2. Close a running run once it holds `fit_min_kept(n_min)` kept uniform labels or is `RUN_MAX_AGE` old.
3. Fit a completed run only when it is due by itself (one of those two limits). A run closed early (its labeler set
   changed, `runs_open.py`, or parked by the rotation before revision 0002) is refreshed but not fitted; its labels
   count in every later fit window of the definition. The fit covers every
   binding (member subset x input-view signature) with enough kept uniform labels among the items of the runs of the
   same question definition started within `LABEL_WINDOW` (`window.py`, one item per decision): per-member and
   pooled calibration on the calibration split, holdout on the most recent split, the gate A1-A8 with this
   snapshot's thresholds (A8 against the stage's full member set and the subset's uniform-traffic share), then
   `CalibratorRepo.insert` (fitted) and `update` (accepted | rejected), counted in
   `aghoo_calibrator_fits_total{status}`. The numeric fit runs in a worker thread.
   Artifact ids derive from (tenant, binding, window end = the newest kept uniform label of the binding), so the same
   labels never mint a second artifact: a retried, repeated or overlapping job finds it and stops. Activation stays a
   two-person CLI step.
Only flat bindings are fitted (hierarchical answers are never labeled in Phase 1).
"""

from __future__ import annotations

import functools
import hashlib
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Final

import anyio.to_thread

from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import encode_ulid
from noema.core.protocols.labeling import CalibratorBindingKey
from noema.core.protocols.storage import Job, PermanentJobError, Row
from noema.core.protocols.telemetry import Span
from noema.labeling.acceptance import SubsetEvidence, Thresholds, evaluate_gate
from noema.labeling.aggregate import RunAggregate, aggregate
from noema.labeling.artifact import artifact_row
from noema.labeling.calibrators.transforms import DEFAULT_EPSILON
from noema.labeling.dataset import LabeledSet
from noema.labeling.deps import LABEL_WINDOW, RUN_MAX_AGE, LabelingDeps, fit_min_kept
from noema.labeling.evaluation import fit_and_evaluate
from noema.labeling.lifecycle import gate_status
from noema.labeling.window import WindowItem, window_items

_log: Final = logging.getLogger("noema.labeling.runs")


def calibrator_id(tenant_id: str, key: CalibratorBindingKey, window_end: str) -> str:
    """`cal_` + 26 Crockford characters derived from (tenant, binding, newest kept uniform label id): deterministic,
    so fitting the same labels again finds the artifact instead of minting a duplicate."""
    binding = "|".join(
        (
            key.profile_id,
            key.profile_version,
            key.question_signature,
            ",".join(key.member_subset),
            key.input_view_signature,
        )
    )
    digest = hashlib.sha256(f"{tenant_id}|{binding}|{key.choice_method}|{window_end}".encode()).digest()
    return "cal_" + encode_ulid(int.from_bytes(digest[:16], "big"))


def _strs(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list | tuple) else []


def _at(row: Row, key: str) -> datetime | None:
    value = row.get(key)
    return value if isinstance(value, datetime) else None


class RunJob:
    """The `labeling.run` handler (module docstring)."""

    def __init__(self, deps: LabelingDeps) -> None:
        self._d = deps

    async def __call__(self, job: Job) -> None:
        run_id = job.payload.get("run_id")
        if not isinstance(run_id, str) or not run_id.startswith("lrun_"):
            raise PermanentJobError("malformed labeling.run payload")
        with self._d.telemetry.span("labeling.run", **{"aghoo.tenant": job.tenant_id}) as span:
            await self._refresh(job.tenant_id, run_id, span)

    async def _refresh(self, tenant: str, run_id: str, span: Span) -> None:
        async with self._d.db.tenant_uow(tenant) as uow:
            try:
                run = await uow.labels.update_run(run_id)
            except NoemaError as exc:
                if exc.code is ErrorCode.NOT_FOUND:
                    return
                raise
            items = await uow.labels.items(run_id)
        span.set("aghoo.profile", str(run["profile_id"]))
        span.set("aghoo.profile_version", str(run["profile_version"]))
        span.set("aghoo.member.question", str(run["question_name"]))
        agg, changes, due = self._changes(run, items)
        async with self._d.db.tenant_uow(tenant) as uow:
            # an item committed since the read above had its refresh deduped against this running job (one live
            # job per run): re-read in the transaction that writes, so it is in this aggregate (Wave E)
            fresh = await uow.labels.items(run_id)
            if len(fresh) != len(items):
                agg, changes, due = self._changes(run, fresh)
            run = await uow.labels.update_run(run_id, **changes)
        for cls, rate in agg.consensus_rate_by_class.items():
            if isinstance(rate, float):
                self._d.telemetry.instruments.set(
                    "aghoo_label_consensus_ratio",
                    rate,
                    profile=str(run["profile_id"]),
                    question=str(run["question_name"]),
                    **{"class": cls},
                )
        for cls, share in agg.class_share.items():
            self._d.telemetry.instruments.set(
                "aghoo_label_class_share",
                share,
                profile=str(run["profile_id"]),
                question=str(run["question_name"]),
                **{"class": cls},
            )
        if run.get("status") == "completed" and due:
            await self._fit(tenant, run)

    def _changes(self, run: Row, items: Sequence[Row]) -> tuple[RunAggregate, dict[str, object], bool]:
        """The aggregate of `items`, the run's column changes, and whether the run is due to close (and fit)."""
        agg = aggregate(items)
        now = self._d.clock.now()
        changes = agg.changes()
        started = _at(run, "started_at")
        n_min = self._d.config.current().labeling.acceptance.n_min
        due = agg.kept_uniform >= fit_min_kept(n_min) or (started is not None and now - started >= RUN_MAX_AGE)
        if run.get("status") == "running" and due:
            changes.update(status="completed", completed_at=now)
        return agg, changes, due

    async def _fit(self, tenant: str, run: Row) -> None:
        snapshot = self._d.config.current()
        labeling = snapshot.labeling
        try:
            profile = snapshot.decision_profile(str(run["profile_id"]))
        except NoemaError:
            return
        if str(profile.version) != str(run["profile_version"]):
            _log.info("not fitting a retired profile version", extra={"run_id": run["id"]})
            return
        full = tuple(sorted(f"m{i + 1}" for i in range(len(profile.members))))
        thresholds = Thresholds.from_config(labeling)
        now = self._d.clock.now()
        async with self._d.db.tenant_uow(tenant) as uow:
            window = await window_items(uow, run, (_at(run, "completed_at") or now) - LABEL_WINDOW, now)
        uniform = [w.subset for w in window if w.uniform]
        groups: dict[tuple[tuple[str, ...], str], list[WindowItem]] = {}
        for w in window:
            if w.subset:
                groups.setdefault((w.subset, w.view), []).append(w)
        for (subset, view), group in sorted(groups.items(), key=lambda kv: kv[0]):
            share = sum(1 for s in uniform if s == subset) / max(len(uniform), 1)
            await self._fit_binding(tenant, run, subset, view, group, thresholds, SubsetEvidence(subset == full, share))

    async def _fit_binding(
        self,
        tenant: str,
        run: Row,
        subset: tuple[str, ...],
        view: str,
        group: Sequence[WindowItem],
        thresholds: Thresholds,
        evidence: SubsetEvidence,
    ) -> None:
        kept_uniform = [w for w in group if w.kept and w.uniform]
        if len(kept_uniform) < fit_min_kept(thresholds.n_min):
            return
        facts = next((w.facts for w in reversed(group) if w.facts is not None), None)
        if facts is None:
            return
        examples = [w.example for w in kept_uniform if w.example is not None]
        try:
            ls = LabeledSet(facts.kind, facts.options, subset, tuple(examples))
        except ValueError as exc:
            _log.warning(
                "inconsistent labeled items; binding not fitted", extra={"run_id": run["id"], "error": str(exc)}
            )
            return
        key = CalibratorBindingKey(
            str(run["profile_id"]), str(run["profile_version"]), str(run["question_signature"]), subset, view
        )
        cal_id = calibrator_id(tenant, key, max(w.item_id for w in kept_uniform))
        async with self._d.db.tenant_uow(tenant) as uow:
            if await uow.calibrators.get(cal_id) is not None:
                return
        labeling = self._d.config.current().labeling
        sampled = [w.sampled for w in group if w.sampled is not None]
        now = self._d.clock.now()
        fit = functools.partial(
            fit_and_evaluate,
            ls,
            sampled,
            dataset_id=str(run["id"]),
            labelers=_strs(run.get("labelers")),
            lift_threshold=labeling.family_overlap_lift_max,
            class_n_min=thresholds.class_n_min,
            view_signature=view,
            evaluated_at=now,
            epsilon=DEFAULT_EPSILON,
        )
        ev = await anyio.to_thread.run_sync(fit)  # CPU-bound numpy: off the jobs process's event loop
        gate = evaluate_gate(ev.holdout, ls.options, thresholds, evidence)
        row = artifact_row(
            calibrator_id=cal_id,
            key=key,
            question_name=str(run["question_name"]),
            params=ev.params,
            holdout=ev.holdout,
            acceptance=gate.wire(),
            reliability=ev.reliability,
            fitted_at=now,
        )
        status = gate_status(gate.passed)
        async with self._d.db.tenant_uow(tenant) as uow:
            await uow.calibrators.insert(row)
            await uow.calibrators.update(cal_id, status=status, **({"accepted_at": now} if gate.passed else {}))
        self._d.telemetry.instruments.add("aghoo_calibrator_fits_total", status=status)
        ece = ev.holdout.get("ece") if ev.holdout is not None else None
        if isinstance(ece, float):
            self._d.telemetry.instruments.set(
                "aghoo_calibration_ece",
                ece,
                profile=key.profile_id,
                signature=key.question_signature,
                view_signature=view,
            )
        _log.info(
            "calibrator fitted",
            extra={"calibrator_id": cal_id, "status": status, "failed_rules": ",".join(gate.failed_rules)},
        )
