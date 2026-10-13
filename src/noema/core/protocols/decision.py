"""The decision engine (spec-02 §5; compiled types spec-06 §3.2, input spec-06 §4.1; IMPL-070).

The hierarchical answer reaches callers only through the wire `DecisionResult` (`choice_method`, `groups[]`,
`unresolved_mass`); `AnswerSummary` carries the flat summary bindings and conditioning read.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from noema.core.clock import Deadline
from noema.core.principal import Principal, TenantRef
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.media import ContentPart
from noema.core.protocols.ops import Reservation
from noema.core.values import ContentPolicy, DataClass, JsonValue

if TYPE_CHECKING:
    from noema.config.models import DecisionProfileCfg

Purpose = Literal["decision", "router", "verification"]
Origin = Literal["native", "facade", "turn", "verification"]
OutputFormat = Literal["native", "jev", "openai"]


# --- compiled questions (spec-06 §3.2) -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OptionSpec:
    """One answer option. `key`: the member/distribution key ("true"/"false", choice value or level label);
    `value`: the wire value of the candidate (bool for predicates); `index`: declared position (score: level
    index)."""

    key: str
    value: str | bool
    description: str | None
    index: int


@dataclass(frozen=True, slots=True)
class GroupSpec:
    """One stage-1 option of a hierarchical question (spec-06 §19.2). `id` is "g1", "g2", ... in group order;
    `label` is the request-supplied group name or "options a-b" (1-based) for a server split; `description` is
    `", ".join(o.key for o in options)`."""

    id: str
    label: str
    options: tuple[OptionSpec, ...]
    description: str


@dataclass(frozen=True, slots=True)
class CompiledQuestion:
    """A validated question. `type` is echoed on the answer; `options` are in declared order; `criteria` is the
    predicate's (true, false) text; `wire` is the canonical question dict (spec-06 §3.3). `method` is
    "hierarchical" iff type == "choice" and K > L(profile) (spec-06 §19.1); `groups` is non-empty iff hierarchical."""

    name: str
    type: Literal["predicate", "noul", "choice", "score"]
    instructions: str
    options: tuple[OptionSpec, ...]
    criteria: tuple[str | None, str | None] | None
    wire: Mapping[str, object]
    signature: str
    commit_critical: bool
    source: Literal["platform", "request"]
    method: Literal["flat", "hierarchical"] = "flat"
    groups: tuple[GroupSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class CompiledQuestionSet:
    """The questions in answer order, the set signature (spec-06 §3.3) and L(profile)."""

    questions: tuple[CompiledQuestion, ...]
    signature: str
    commit_critical: frozenset[str]
    flat_limit: int


# --- input (spec-06 §4.1) --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Msg:
    """One conversation message of resolved parts."""

    role: Literal["user", "assistant"]
    parts: tuple[ContentPart, ...]


type StandardInput = str | Mapping[str, object] | tuple[Msg, ...]  # text | JSON state | messages


@dataclass(frozen=True, slots=True)
class VerificationInput:
    """Verifier input: the question plus `(id, text)` answers (Phase 1: one ("A", answer))."""

    question: str
    answers: tuple[tuple[str, str], ...]


type DecisionInput = StandardInput | VerificationInput


# --- context, request, snapshots -------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProfileRef:
    """A decision profile at one version."""

    id: str
    version: int


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """Per-run context. `deadline` is end-to-end (IMPL-023); `escalate_until` None disables escalation (IMPL-025,
    IMPL-070); the run calls `reservation.consume("decision_member" | "verification_member", model)` per call;
    `store` = persist (L2 allowed); `data_class`: session, then project default, then "internal" (IMPL-004);
    `content_policy` decides whether the decision row stores `input` (IMPL-071). `decision_id`: the `dec_` id
    pre-generated before the idempotency `begin()` (createDecision, spec-04 §1 step 7), used as the run's id when
    set; None = the engine mints one (`IdGen.new("dec")`). `escalate_slack_ms` (turns, C2b; read only when
    `escalate_until` is set): the engine replaces `escalate_until` by `min(t0 + deadline_ms + slack_ms,
    deadline)` from its own decision start t0 (stage-0 views resolved, IMPL-074) and the request's effective
    stage-0 `deadline_ms`, which is the turn's W (spec-04 §3, IMPL-023); None = `escalate_until` as given."""

    principal: Principal
    tenant: TenantRef
    snapshot: ConfigSnapshot
    deadline: Deadline
    escalate_until: Deadline | None
    reservation: Reservation
    purpose: Purpose
    store: bool
    data_class: DataClass
    content_policy: ContentPolicy
    trace_id: str
    turn_id: str | None = None
    session_id: str | None = None
    decision_id: str | None = None
    escalate_slack_ms: int | None = None


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """A compiled decision request. `deadline_ms`/`min_quorum` are the effective stage-0 values. `output_format`
    is persisted; jev/openai reject a question that needs the hierarchical method (422 unsupported_parameter,
    IMPL-036), native answers it (IMPL-027)."""

    profile: ProfileRef
    questions: CompiledQuestionSet
    input: DecisionInput
    deadline_ms: int
    min_quorum: int
    escalation: Literal["profile_default", "off"]
    include: frozenset[Literal["members", "member_distributions", "stages"]]
    origin: Origin
    output_format: OutputFormat
    safety_identifier: str | None = None


@dataclass(frozen=True, slots=True)
class AnswerSummary:
    """What bindings and conditioning read (no Pydantic). `value`: the choice value, P(true) or level label (None
    for refusal/unavailable); `top`: (value, emitted probability), best first, at most 2 entries; `probability`:
    p_max (choice/score) or P(true); `reason`: the unavailable reason."""

    name: str
    type: Literal["predicate", "noul", "choice", "score", "refusal", "unavailable"]
    value: str | bool | float | None
    top: tuple[tuple[str | bool, float], ...]
    probability: float | None
    margin: float | None
    band: Literal["high", "medium", "low"] | None
    calibrated: bool
    final: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    """A point-in-time view of a run. `result` is the wire-ready native DecisionResult dict (orjson), validated only
    under NOEMA_CONTRACT_VALIDATE=1. `row` is set on the final snapshot only, iff ctx.store and ctx.turn_id is not
    None: the noema.decision row (spec-03 column names) the orchestrator inserts in `SessionRepo.finalize_turn`. The
    engine inserts the row itself only when ctx.turn_id is None (/v1/decisions, facade), inside finalize()."""

    decision_id: str
    status: Literal["quorum", "completed", "partial"]
    final: bool
    final_names: frozenset[str]
    answers: Mapping[str, AnswerSummary]
    result: Mapping[str, JsonValue]
    row: Mapping[str, object] | None = None


class DecisionRun(Protocol):
    """A running decision. Never raises for member failures once `__aenter__` returned."""

    @property
    def decision_id(self) -> str: ...

    async def commit_critical(self) -> DecisionSnapshot:
        """S1: every commit-critical answer final (IMPL-002). Idempotent; cancelling the waiter does not cancel the
        run."""
        ...

    def updates(self) -> AsyncIterator[DecisionSnapshot]:
        """Yields at most one snapshot (the final), then ends."""
        ...

    async def finalize(self) -> DecisionSnapshot:
        """The final snapshot. Idempotent; shielded (2 s): usage lines and the L2 put always; the row insert (if
        ctx.store) and the labeling offer only when ctx.turn_id is None. A DB failure goes to the outbox
        (IMPL-022), never raised."""
        ...

    async def cancel(self) -> None:
        """Idempotent; pending members become late and the result becomes final."""
        ...

    def row_for(self, content_policy: ContentPolicy) -> Mapping[str, object] | None:
        """Sync: the final snapshot's `row` with `input` stored under `content_policy`, for a turn whose final
        policy (decided after commit, the most restrictive of both, spec-04 §10, IMPL-018) is stricter than
        `ctx.content_policy`: "full" = the input, "redacted" = its redacted text (`Telemetry.redact`),
        "metadata_only" = NULL. A policy laxer than `ctx.content_policy` is clamped to it. None before the final
        snapshot or when it has no row."""
        ...


class DecisionEngine(Protocol):
    def run(self, ctx: DecisionContext, req: DecisionRequest) -> AbstractAsyncContextManager[DecisionRun]:
        """`__aenter__` may raise `NoemaError` (request problems, deadline_exceeded before dispatch,
        tenant_access_misconfigured); `__aexit__` cancels in-flight calls and awaits the terminal state (IMPL-070)."""
        ...

    async def decide(self, ctx: DecisionContext, req: DecisionRequest) -> DecisionSnapshot:
        """`run` + `finalize`."""
        ...


class QuestionCompiler(Protocol):
    def compile(
        self,
        profile: DecisionProfileCfg,
        app_questions: Sequence[Mapping[str, JsonValue]],
        *,
        origin: Origin,
        output_format: OutputFormat,
        blocking: Sequence[str] = (),
        select: Sequence[str] | None = None,
        platform_from: DecisionProfileCfg | None = None,
    ) -> CompiledQuestionSet:
        """Sync; raises `NoemaError` (spec-06 §3.1). `select`: platform question names to keep, in config order
        (verification); `platform_from`: turns take the platform questions from routing.router_profile
        (IMPL-069)."""
        ...


class DecisionCache(Protocol):
    """The L2 decision cache (IMPL-004); fail-open."""

    async def get(self, key: str, *, timeout_s: float = 0.02) -> bytes | None:
        """Timeout or error gives None."""
        ...

    async def put(self, key: str, artifact: bytes, ttl_s: int, *, timeout_s: float = 0.02) -> None:
        """Errors are swallowed (and counted)."""
        ...

    async def purge_tenant(self, tenant_id: str) -> int:
        """SCAN `RedisKeys.l2_pattern(tenant)` + UNLINK; returns the count."""
        ...
