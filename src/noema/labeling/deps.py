"""The services the labeling jobs use, and their tunables (spec-11 §1.2, §2, §4; ADR-003 §3, §5)."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from noema.core.clock import Clock
from noema.core.ids import IdGen
from noema.core.protocols.adapters import AdapterRegistry
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.media import FileService, InputViewResolver
from noema.core.protocols.ops import Admission, CostMeter, TokenEstimator
from noema.core.protocols.storage import Database, JobQueue
from noema.core.protocols.telemetry import Telemetry
from noema.core.redis_keys import RedisKeys
from noema.labeling.dataset import TEST_SHARE

if TYPE_CHECKING:
    from redis.asyncio import Redis

JOB_ITEM: Final = "labeling.item"
JOB_RUN: Final = "labeling.run"
JOB_DRIFT: Final = "labeling.drift"
JOB_PRIORITY: Final = 200
ITEM_MAX_ATTEMPTS: Final = 5  # the JobQueue default the outbox applies labeling.item jobs with (spec-11 §4)
LABELER_TIMEOUT_S: Final = 120.0
ADMISSION_QUEUE_TIMEOUT_S: Final = 10.0
RUN_REFRESH_DELAY_S: Final = 60.0  # labeling.run debounce after an item (one live job per run: dedupe_key = run_id)
RUN_MAX_AGE: Final = timedelta(days=30)  # a running run closes after a month (the traffic-YYYYMM dataset)
LABEL_WINDOW: Final = timedelta(days=90)  # fitting reads the binding's items of the runs started in this window
DRIFT_WINDOW: Final = timedelta(days=7)
DRIFT_MIN_N: Final = 100  # labels a drift verdict needs; a thinner week widens the slice (ruling R5, ADR-003 §5)
DRIFT_MAX_WEEKS: Final = 4  # the widest slice: the last 4 weeks; still thinner -> "not evaluated" WARNING
DRIFT_ACTOR: Final = "system:labeling.drift"
LIST_PAGE: Final = 200
DAILY_COUNT_TTL_S: Final = 2 * 24 * 3600  # RedisKeys.labeling_daily: EXPIRE 2 days
REDIS_TIMEOUT_S: Final = 1.0  # the daily-cap INCR; a slower or failed Redis fails open (the sampler share holds)


def fit_min_kept(n_min: int) -> int:
    """Kept uniform labels a binding needs before it is fitted (and a run closes): enough for `n_min` test labels."""
    return math.ceil(n_min / TEST_SHARE)


def usage_line_id(job_id: str, attempt: int, question_name: str, model_id: str) -> str:
    """`h(f"labeling:{job_id}:{attempt}:{question_name}:{model_id}")` (spec-11 §2)."""
    return hashlib.sha256(f"labeling:{job_id}:{attempt}:{question_name}:{model_id}".encode()).hexdigest()[:26]


@dataclass(frozen=True, slots=True)
class LabelingDeps:
    """Exactly the `ServiceSet` fields the labeling jobs read (the composition root passes them)."""

    clock: Clock
    ids: IdGen
    config: ConfigRegistry
    db: Database
    jobs: JobQueue
    adapters: AdapterRegistry
    admission: Admission
    estimator: TokenEstimator
    cost: CostMeter
    files: FileService
    views: InputViewResolver
    telemetry: Telemetry
    redis: Redis  # the exact daily sampling cap (RedisKeys.labeling_daily)
    keys: RedisKeys
