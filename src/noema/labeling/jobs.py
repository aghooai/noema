"""Job handlers of the `jobs` process (spec-01 §1, spec-11 §4): `labeling.item`, `labeling.run`, `labeling.drift`.

`make_handlers(services)` follows the `ServiceSet` convention (`noema.core.protocols.services`); the entrypoint
merges the owners' maps and expands `--kinds labeling.*`. The weekly `labeling.drift` instance is kept by the
periodic scheduler (`jobs.ensure_periodic(JOB_DRIFT, DRIFT_PERIOD_S)`, tenant '__platform__').
"""

from __future__ import annotations

from typing import Final

from noema.core.protocols.services import JobHandlers, ServiceSet
from noema.labeling.deps import JOB_DRIFT, JOB_ITEM, JOB_RUN, LabelingDeps
from noema.labeling.drift import DriftJob
from noema.labeling.items import ItemJob
from noema.labeling.runs import RunJob

DRIFT_PERIOD_S: Final = 7 * 24 * 3600


def deps_from(services: ServiceSet) -> LabelingDeps:
    """The labeling job dependencies of a process's services."""
    return LabelingDeps(
        clock=services.clock,
        ids=services.ids,
        config=services.config,
        db=services.db,
        jobs=services.jobs,
        adapters=services.adapters,
        admission=services.admission,
        estimator=services.estimator,
        cost=services.cost,
        files=services.files,
        views=services.views,
        telemetry=services.telemetry,
        redis=services.redis,
        keys=services.keys,
    )


def make_handlers(services: ServiceSet) -> JobHandlers:
    """`{labeling.item, labeling.run, labeling.drift}` -> handler."""
    deps = deps_from(services)
    return {JOB_ITEM: ItemJob(deps), JOB_RUN: RunJob(deps), JOB_DRIFT: DriftJob(deps)}
