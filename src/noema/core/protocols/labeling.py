"""Calibration and labeling (spec-02 §9, ADR-003, IMPL-072)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from noema.core.protocols.decision import DecisionSnapshot
from noema.core.values import JsonValue


@dataclass(frozen=True, slots=True)
class CalibratorBindingKey:
    """One key type everywhere; the scope is not part of the key. `member_subset`: sorted ok member ids of the
    answering stage."""

    profile_id: str
    profile_version: str
    question_signature: str
    member_subset: tuple[str, ...]
    input_view_signature: str
    choice_method: Literal["flat", "hierarchical"] = "flat"


class Transform(Protocol):
    def apply(self, dist: Mapping[str, float], unresolved: float) -> dict[str, float]:
        """Sync; preserves the unresolved mass."""
        ...


@dataclass(frozen=True, slots=True)
class Calibrator:
    """An active calibrator artifact: per-member temperatures and the pooled transform, applied together."""

    id: str
    scope: Literal["tenant", "platform"]
    method: Literal["temperature", "platt"]
    epsilon: float
    member_T: Mapping[str, float]  # noqa: N815 (ADR-003 field name)
    pooled: Transform
    member: Callable[[str], Transform]
    holdout: Mapping[str, JsonValue]
    key: CalibratorBindingKey


class CalibratorRegistry(Protocol):
    """A per-pod in-memory snapshot (IMPL-072)."""

    def lookup(self, tenant_id: str, key: CalibratorBindingKey) -> Calibrator | None:
        """Sync; active artifacts only; tenant scope first, then platform."""
        ...

    def epoch(self, tenant_id: str) -> str:
        """Sync; f"{platform_n}.{tenant_n}"; "" if unknown (L2 bypass)."""
        ...

    async def refresh(self) -> None:
        """On calibrators_changed and every 5 s: SystemCalls.calibrator_epochs, then active_calibrators(changed
        scope keys); swaps the snapshot atomically; on error keeps the last snapshot. Never raises."""
        ...


@dataclass(frozen=True, slots=True)
class ContentRef:
    """Where a labeling job finds the decision's content (IMPL-071)."""

    decision_id: str
    turn_id: str | None


class LabelingSampler(Protocol):
    def offer(self, tenant_id: str, snapshot: DecisionSnapshot, ref: ContentRef) -> None:
        """Sync, never raises; samples in memory (labeling.sample rates). A sampled offer goes to
        Outbox.put(kind="labeling_offer"), applied as a labeling.item job (spec-11 §3-§4)."""
        ...
