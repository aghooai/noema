"""Calibration and labeling fakes (spec-02 §9, ADR-003, spec-06 §8.3): transforms, `FakeCalibratorRegistry`,
`FakeLabelingSampler`.

Transforms follow spec-06 §8.3 exactly (temperature with the epsilon floor; Platt on the predicate logit), so a
calibrated fake decision is numerically what the engine computes. The registry is a per-pod snapshot: `lookup` is
tenant first, then platform, active artifacts only; `epoch(t)` is `f"{platform_n}.{tenant_n}"` for a known tenant
and "" otherwise (L2 bypass); `refresh()` reloads from an optional async `source` and never raises. The sampler
decides in memory from `labeling.sample` (a deterministic draw per decision id) and puts `labeling_offer` records.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

import anyio.lowlevel

from noema.core.clock import Clock
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.decision import DecisionSnapshot
from noema.core.protocols.labeling import Calibrator, CalibratorBindingKey, ContentRef
from noema.core.protocols.storage import Outbox, OutboxRecord

DEFAULT_EPSILON: Final = 1e-4
_log: Final = logging.getLogger("tests.fakes.labeling")


@dataclass(frozen=True, slots=True)
class IdentityTransform:
    """Leaves the distribution unchanged."""

    def apply(self, dist: Mapping[str, float], unresolved: float) -> dict[str, float]:
        return dict(dist)


@dataclass(frozen=True, slots=True)
class TemperatureTransform:
    """q_o = (1-u)·exp(ln max(p̂_o, ε)/T) / Σ exp(ln max(p̂_o', ε)/T), with p̂ = p/(1-u) (spec-06 §8.3)."""

    t: float
    epsilon: float = DEFAULT_EPSILON

    def apply(self, dist: Mapping[str, float], unresolved: float) -> dict[str, float]:
        scale = 1.0 - unresolved
        if scale <= 0 or not dist:
            return dict(dist)
        logits = {k: math.log(max(v / scale, self.epsilon)) / self.t for k, v in dist.items()}
        top = max(logits.values())
        weights = {k: math.exp(v - top) for k, v in logits.items()}
        total = sum(weights.values())
        return {k: scale * w / total for k, w in weights.items()}


@dataclass(frozen=True, slots=True)
class PlattTransform:
    """P_cal(true) = σ(a·logit(clamp(P(true), ε, 1-ε)) + b); P_cal(false) = 1 - P_cal(true); both scaled by (1-u)."""

    a: float
    b: float
    epsilon: float = DEFAULT_EPSILON

    def apply(self, dist: Mapping[str, float], unresolved: float) -> dict[str, float]:
        scale = 1.0 - unresolved
        p = min(max(dist.get("true", 0.0) / scale if scale > 0 else 0.0, self.epsilon), 1 - self.epsilon)
        cal = 1.0 / (1.0 + math.exp(-(self.a * math.log(p / (1 - p)) + self.b)))
        return {"true": cal * scale, "false": (1.0 - cal) * scale}


Source = Callable[[], Awaitable[tuple[Mapping[str, int], list[tuple[str | None, Calibrator]]]]]


class FakeCalibratorRegistry:
    """Implements `CalibratorRegistry` (see the module docstring)."""

    def __init__(self, config: ConfigRegistry | None = None, *, source: Source | None = None) -> None:
        self._config = config
        self._source = source
        self._tenant: dict[tuple[str, CalibratorBindingKey], Calibrator] = {}
        self._platform: dict[CalibratorBindingKey, Calibrator] = {}
        self._epochs: dict[str, int] = {"platform": 0}
        self.refreshes = 0
        self.refresh_errors = 0

    def add(self, calibrator: Calibrator, *, tenant_id: str | None = None) -> None:
        """Activate an artifact (tenant scope when `tenant_id` is given) and bump that scope's epoch."""
        if calibrator.scope == "tenant":
            if tenant_id is None:
                raise ValueError("a tenant calibrator needs tenant_id")
            self._tenant[(tenant_id, calibrator.key)] = calibrator
            self._epochs[tenant_id] = self._epochs.get(tenant_id, 0) + 1
        else:
            self._platform[calibrator.key] = calibrator
            self._epochs["platform"] += 1

    def lookup(self, tenant_id: str, key: CalibratorBindingKey) -> Calibrator | None:
        return self._tenant.get((tenant_id, key)) or self._platform.get(key)

    def _known(self, tenant_id: str) -> bool:
        if tenant_id in self._epochs:
            return True
        if self._config is None:
            return False
        try:
            self._config.current().tenant(tenant_id)
        except Exception:
            return False
        return True

    def epoch(self, tenant_id: str) -> str:
        if not self._known(tenant_id):
            return ""
        return f"{self._epochs['platform']}.{self._epochs.get(tenant_id, 0)}"

    async def refresh(self) -> None:
        await anyio.lowlevel.checkpoint()
        self.refreshes += 1
        if self._source is None:
            return
        try:
            epochs, calibrators = await self._source()
        except Exception:  # keep the last snapshot (never raises)
            self.refresh_errors += 1
            _log.warning("calibrator refresh failed; keeping the last snapshot")
            return
        tenant: dict[tuple[str, CalibratorBindingKey], Calibrator] = {}
        platform: dict[CalibratorBindingKey, Calibrator] = {}
        for tenant_id, cal in calibrators:
            if tenant_id is None:
                platform[cal.key] = cal
            else:
                tenant[(tenant_id, cal.key)] = cal
        self._tenant, self._platform = tenant, platform  # atomic swap
        self._epochs = {"platform": 0, **epochs}


def _draw(decision_id: str, salt: str) -> float:
    h = hashlib.sha256(f"{salt}:{decision_id}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


class FakeLabelingSampler:
    """Implements `LabelingSampler`: uniform draw < uniform_rate -> "uniform"; else an answer in the low band and
    a draw < uncertainty_rate -> "uncertainty"; at most max_per_day_per_tenant offers per UTC day. Never raises."""

    def __init__(self, clock: Clock, config: ConfigRegistry, outbox: Outbox) -> None:
        self._clock = clock
        self._config = config
        self._outbox = outbox
        self._per_day: Counter[tuple[str, date]] = Counter()
        self.offers: list[OutboxRecord] = []

    def offer(self, tenant_id: str, snapshot: DecisionSnapshot, ref: ContentRef) -> None:
        try:
            sample = self._config.current().labeling.sample
            day = self._clock.now().date()
            cap = sample.max_per_day_per_tenant
            if cap is not None and self._per_day[(tenant_id, day)] >= cap:
                return
            scored = [a for a in snapshot.answers.values() if a.type not in ("refusal", "unavailable")]
            if not scored:
                return
            reason = None
            if _draw(ref.decision_id, "uniform") < sample.uniform_rate:
                reason = "uniform"
            elif (
                any(a.band == "low" for a in scored) and _draw(ref.decision_id, "uncertainty") < sample.uncertainty_rate
            ):
                reason = "uncertainty"
            if reason is None:
                return
            self._per_day[(tenant_id, day)] += 1
            record = OutboxRecord(
                kind="labeling_offer",
                id=ref.decision_id,
                tenant_id=tenant_id,
                payload={
                    "decision_id": ref.decision_id,
                    "turn_id": ref.turn_id,
                    "question_names": [a.name for a in scored],
                    "sample_reason": reason,
                },
            )
            self.offers.append(record)
            self._outbox.put(record)
        except Exception:  # never raises (spec-02 §9)
            _log.exception("labeling offer failed")
