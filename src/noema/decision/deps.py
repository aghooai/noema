"""The engine's collaborators (all core Protocols; injected by the composition root)."""

from __future__ import annotations

from dataclasses import dataclass

from noema.core.clock import Clock
from noema.core.ids import IdGen
from noema.core.protocols.adapters import AdapterRegistry
from noema.core.protocols.credentials import CredentialBroker, SecretsProvider
from noema.core.protocols.decision import DecisionCache
from noema.core.protocols.labeling import CalibratorRegistry, LabelingSampler
from noema.core.protocols.media import InputViewResolver
from noema.core.protocols.ops import CostMeter, HealthRegistry, TokenEstimator
from noema.core.protocols.storage import Database, Outbox
from noema.core.protocols.telemetry import Telemetry
from noema.core.redis_keys import RedisKeys


@dataclass(frozen=True, slots=True)
class EngineDeps:
    """Everything a decision run calls. `secrets` yields the input-digest pepper; `cache` is the L2 decision cache;
    `db`/`outbox` persist `/v1/decisions` rows (the outbox on a DB failure); `sampler` takes labeling offers."""

    clock: Clock
    ids: IdGen
    adapters: AdapterRegistry
    broker: CredentialBroker
    secrets: SecretsProvider
    views: InputViewResolver
    health: HealthRegistry
    estimator: TokenEstimator
    cost: CostMeter
    calibrators: CalibratorRegistry
    sampler: LabelingSampler
    cache: DecisionCache
    keys: RedisKeys
    db: Database
    outbox: Outbox
    telemetry: Telemetry
