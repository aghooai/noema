"""Run-state records shared by the coordinator, the member runner and the answer builder (spec-06 §6-§7).

Plain mutable slotted dataclasses: the coordinator owns them (one event loop, no locks); nothing here is
validated per event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from noema.config.models import DecisionMemberCfg, DecisionProfileCfg, ModelCfg
from noema.core.protocols.media import InputView, ViewKind
from noema.core.values import ContentBlock, Cost, Modality, UsageRecord

from .elicitation.templates import Template

MemberStatus = Literal["ok", "refused", "timeout", "error", "late", "skipped"]
CallOutcome = Literal["pending", "completed", "failed", "cancelled"]
ZERO_USAGE = UsageRecord(0, 0)


@dataclass(frozen=True, slots=True)
class StageMember:
    """One member of a stage profile: `member_id` is `m{i+1}` in `members[]` order; `view` is its resolved input
    view (None for text-only input); `view_blocks` its rendered input blocks, shared by every question."""

    member_id: str
    cfg: DecisionMemberCfg
    model: ModelCfg
    template: Template
    max_choices: int
    view: InputView | None
    view_kind: ViewKind
    by_modality: dict[Modality, ViewKind]
    skipped: bool
    view_blocks: tuple[ContentBlock, ...]

    @property
    def counts(self) -> bool:
        """Counts toward quorum: non-optional with weight > 0."""
        return not self.cfg.optional and self.cfg.weight > 0


@dataclass(slots=True)
class CallRec:
    """One provider call. `completed`: provider usage; `cancelled` (lost race, close, deadline, cancel): estimated
    usage on the waste line; `failed` (an adapter error before any answer): no usage."""

    started_at: float
    model_alias: str
    model_id: str
    max_tokens: int
    input_estimate: int
    backup: bool
    round: int = 0
    group: str | None = None
    ended_at: float | None = None
    outcome: CallOutcome = "pending"
    usage: UsageRecord = ZERO_USAGE
    cost: Cost | None = None
    region: str | None = None

    @property
    def in_flight(self) -> bool:
        return self.outcome == "pending"


@dataclass(slots=True)
class Slot:
    """One member's state for one question of one stage. `status` None = still running. `dist` is keyed by option
    key over the measured options (hierarchical: the joint over the expanded options); `round1`/`round2` hold the
    hierarchical per-round reads (group id -> p, and group id -> option key -> p)."""

    member: StageMember
    status: MemberStatus | None = None
    dist: dict[str, float] | None = None
    unresolved: float = 0.0
    censored: tuple[str, ...] = ()
    error: tuple[str, str] | None = None
    skip_reason: Literal["input_view", "too_many_choices"] | None = None
    calls: list[CallRec] = field(default_factory=list)
    ended_at: float | None = None
    region: str | None = None
    model_revision: str | None = None
    round1: dict[str, float] | None = None
    round2: dict[str, dict[str, float]] = field(default_factory=dict)
    pending_groups: set[str] = field(default_factory=set)

    @property
    def started_at(self) -> float | None:
        return self.calls[0].started_at if self.calls else None


@dataclass(slots=True)
class StageInfo:
    """One stage (0 = the requested profile, then escalation stages)."""

    index: int
    profile: DecisionProfileCfg
    members: tuple[StageMember, ...]
    t0: float
    deadline: float
    deadline_ms: int
    min_quorum: int
    view_signature: str
    capped: bool = False
    questions: tuple[str, ...] = ()
    triggered_by: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        return f"{self.profile.id}@{self.profile.version}"
