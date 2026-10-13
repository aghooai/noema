"""`ServiceSet`: the process services as the domains and routes see them (spec-01 §3), and the job-handler
convention of the `jobs` and `media-worker` processes (spec-01 §1, spec-11 §4).

`composition.Services` (a frozen dataclass) satisfies `ServiceSet` structurally; domain modules type their
dependencies with it (they never import `noema.composition`). Job handlers: each owner exports

    def make_handlers(services: ServiceSet) -> JobHandlers: ...

from its jobs module (`noema.identity.jobs`, `noema.labeling.jobs`, `noema.ops.retention`, `noema.media.worker`),
mapping each exact job kind it handles to a `JobHandler`. The entrypoint merges the maps (a kind claimed by two
owners is a startup error), expands its `--kinds` globs against the merged keys (`expand_kinds`) and runs
`services.jobs.worker(kinds, dispatch, concurrency=…)`.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

from noema.core.clock import Clock
from noema.core.ids import IdGen
from noema.core.protocols.adapters import AdapterRegistry
from noema.core.protocols.auth import Authenticator, Authorizer
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.credentials import CredentialBroker, SecretsProvider
from noema.core.protocols.decision import DecisionCache, DecisionEngine, QuestionCompiler
from noema.core.protocols.events import SseWriter, TurnEventLog
from noema.core.protocols.generation import GenerationEngine
from noema.core.protocols.labeling import CalibratorRegistry, LabelingSampler
from noema.core.protocols.media import FileService, InputViewResolver, MediaBudgetPlanner, MediaNegotiator
from noema.core.protocols.ops import (
    Admission,
    CostMeter,
    HealthRegistry,
    RetryBudget,
    TokenEstimator,
    WorkGraphPlanner,
)
from noema.core.protocols.runtime import ProcessRuntime
from noema.core.protocols.storage import Database, IdempotencyStore, Job, JobQueue, Outbox
from noema.core.protocols.telemetry import Telemetry
from noema.core.redis_keys import RedisKeys
from noema.core.settings import ProcessSettings

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from types_aiobotocore_s3 import S3Client

type JobHandler = Callable[[Job], Awaitable[None]]
type JobHandlers = Mapping[str, JobHandler]


class ServiceSet(Protocol):
    """Every process service, read-only: exactly the `composition.Services` fields (spec-01 §3) plus `runtime` and
    `settings`. `redis` is a bytes client (decode_responses=False) whose every key goes through `keys`; `s3` is the
    aiobotocore client with platform credentials (bucket `settings.s3_bucket`)."""

    @property
    def clock(self) -> Clock: ...
    @property
    def ids(self) -> IdGen: ...
    @property
    def config(self) -> ConfigRegistry: ...
    @property
    def secrets(self) -> SecretsProvider: ...
    @property
    def broker(self) -> CredentialBroker: ...
    @property
    def adapters(self) -> AdapterRegistry: ...
    @property
    def health(self) -> HealthRegistry: ...
    @property
    def retry_budget(self) -> RetryBudget: ...
    @property
    def planner(self) -> WorkGraphPlanner: ...
    @property
    def admission(self) -> Admission: ...
    @property
    def estimator(self) -> TokenEstimator: ...
    @property
    def cost(self) -> CostMeter: ...
    @property
    def outbox(self) -> Outbox: ...
    @property
    def db(self) -> Database: ...
    @property
    def jobs(self) -> JobQueue: ...
    @property
    def idempotency(self) -> IdempotencyStore: ...
    @property
    def events(self) -> TurnEventLog: ...
    @property
    def sse(self) -> SseWriter: ...
    @property
    def files(self) -> FileService: ...
    @property
    def views(self) -> InputViewResolver: ...
    @property
    def negotiator(self) -> MediaNegotiator: ...
    @property
    def media_budget(self) -> MediaBudgetPlanner: ...
    @property
    def calibrators(self) -> CalibratorRegistry: ...
    @property
    def sampler(self) -> LabelingSampler: ...
    @property
    def compiler(self) -> QuestionCompiler: ...
    @property
    def decision_cache(self) -> DecisionCache: ...
    @property
    def decisions(self) -> DecisionEngine: ...
    @property
    def generation(self) -> GenerationEngine: ...
    @property
    def authn(self) -> Authenticator: ...
    @property
    def authz(self) -> Authorizer: ...
    @property
    def telemetry(self) -> Telemetry: ...
    @property
    def redis(self) -> Redis: ...
    @property
    def keys(self) -> RedisKeys: ...
    @property
    def s3(self) -> S3Client: ...
    @property
    def runtime(self) -> ProcessRuntime: ...
    @property
    def settings(self) -> ProcessSettings: ...


def merge_handlers(*maps: JobHandlers) -> dict[str, JobHandler]:
    """The union of the owners' handler maps. ValueError when two owners claim the same kind."""
    merged: dict[str, JobHandler] = {}
    for handlers in maps:
        if clash := set(merged) & set(handlers):
            raise ValueError(f"job kinds claimed twice: {sorted(clash)}")
        merged.update(handlers)
    return merged


def expand_kinds(patterns: Sequence[str], available: Iterable[str]) -> tuple[str, ...]:
    """The exact job kinds for `--kinds` patterns (shell globs, e.g. "labeling.*"), sorted; `jobs.claim` matches
    kinds exactly. ValueError when a pattern matches no handler kind (a typo would otherwise idle the worker)."""
    kinds = sorted(set(available))
    out: set[str] = set()
    for pattern in patterns:
        matched = [kind for kind in kinds if fnmatch.fnmatchcase(kind, pattern)]
        if not matched:
            raise ValueError(f"--kinds {pattern!r} matches no job handler")
        out.update(matched)
    return tuple(sorted(out))
