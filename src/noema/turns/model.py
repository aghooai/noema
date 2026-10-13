"""Turn-internal values shared by ingress, the runner and the service (no I/O)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from noema.core.clock import Deadline
from noema.core.errors import NoemaError
from noema.core.principal import Principal, TenantRef
from noema.core.protocols.decision import CompiledQuestionSet
from noema.core.protocols.media import FileRef
from noema.core.protocols.ops import Reservation
from noema.core.protocols.storage import SessionLease
from noema.core.values import ContentPolicy, DataClass, JsonValue
from noema.turns.context.budget import ContextBudget
from noema.turns.context.history import HistoryTurn
from noema.turns.request import TurnRequest
from noema.turns.spec import Spec0

if TYPE_CHECKING:
    from noema.config.models import DecisionProfileCfg
    from noema.core.protocols.config import ConfigSnapshot

TurnStatus = Literal["completed", "failed", "cancelled", "abandoned"]
StopStatus = Literal["cancelled", "abandoned", "failed"]


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """The session row facts a turn needs (loaded at authorization, spec-04 §1 step 6)."""

    session_id: str
    owner_principal_id: str
    project_id: str
    data_class: DataClass
    last_seq: int
    sticky_spec: Mapping[str, JsonValue] | None


@dataclass(slots=True)
class PreparedTurn:
    """The outcome of ingress steps 8-11 (spec-04 §1): everything the turn task needs. `lease` is held for
    `store: true` turns with a session; `history` is the selection (chronological), `recent` every committed turn
    read (newest first, for the decision context)."""

    turn_id: str
    principal: Principal
    tenant: TenantRef
    snapshot: ConfigSnapshot
    deadline: Deadline
    t_in: float
    trace_id: str
    request: TurnRequest
    refs: Mapping[str, FileRef]
    session: SessionInfo | None
    lease: SessionLease | None
    recent: tuple[HistoryTurn, ...]
    history: tuple[HistoryTurn, ...]
    spec0: Spec0
    candidates: tuple[str, ...]
    aliases: tuple[str, ...]
    budget: ContextBudget
    est_input_tokens: int
    profile: DecisionProfileCfg
    questions: CompiledQuestionSet
    deadline_ms: int
    min_quorum: int
    verify_questions: CompiledQuestionSet | None
    reservation: Reservation
    data_class: DataClass
    store_content: ContentPolicy
    created_at: datetime
    modalities: tuple[str, ...] = ()

    @property
    def session_id(self) -> str | None:
        return self.session.session_id if self.session is not None else None


@dataclass(slots=True)
class TurnResult:
    """How a turn ended: its status, the terminal error (None when completed or cancelled), and the TurnObject
    payload (`stream: false`)."""

    status: TurnStatus
    error: NoemaError | None
    turn: dict[str, JsonValue] = field(default_factory=dict)
