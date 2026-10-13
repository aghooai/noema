"""createTurn ingress, the orchestrator's steps (spec-04 §1 steps 6 and 8-11; spec-09 §3; IMPL-022, IMPL-023,
IMPL-024, IMPL-036, IMPL-045, IMPL-069, IMPL-084).

Step 6 (`authorize`) checks the role and scope first, then loads the session and the file parts and applies the
visibility and write rules. Steps 8-11 (`prepare`) plan the turn (HTTP errors), get the tenant credentials, take
the session lease and select the history, then reserve the work graph. An error after the lease releases it (no
`seq` consumed).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final, cast

import anyio

from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal, TenantRef
from noema.core.protocols.auth import ResourceRef
from noema.core.protocols.decision import CompiledQuestionSet
from noema.core.protocols.media import FileRef
from noema.core.protocols.ops import DecisionWork, GenerationWork, QuestionWork, WorkGraph
from noema.core.protocols.services import ServiceSet
from noema.core.protocols.storage import Busy, SessionLease, UnitOfWork
from noema.core.values import DataClass, JsonValue
from noema.turns import session_order
from noema.turns.context.budget import candidate_profiles, context_budget, member_aliases, profile_usable
from noema.turns.context.history import (
    DECISION_CONTEXT_TURNS,
    HISTORY_SCAN_LIMIT,
    HistoryTurn,
    committed_turns,
    select_history,
)
from noema.turns.coordination import Coordinator
from noema.turns.model import PreparedTurn, SessionInfo
from noema.turns.preflight import estimate, history_blocks, ingress_blocks, modalities_of
from noema.turns.request import TurnRequest
from noema.turns.spec import compute_spec0

if TYPE_CHECKING:
    from noema.config.models import DecisionProfileCfg
    from noema.core.protocols.config import ConfigSnapshot

log = logging.getLogger("noema.turns.ingress")

TENANT_WIDE: Final = frozenset({"tenant_admin", "auditor"})
_DATA_CLASSES: Final = frozenset({"public", "internal", "confidential", "restricted"})


@dataclass(frozen=True, slots=True)
class Authorized:
    """Step 6 results: the session row facts and the file parts' refs (by file id)."""

    session: SessionInfo | None
    refs: Mapping[str, FileRef] = field(default_factory=dict)


def _session_info(row: Mapping[str, object]) -> SessionInfo:
    data_class = str(row.get("data_class", "internal"))
    sticky = row.get("sticky_spec")
    last_seq = row.get("last_seq", 0)
    return SessionInfo(
        session_id=str(row["id"]),
        owner_principal_id=str(row.get("owner_principal_id", "")),
        project_id=str(row.get("project_id", "")),
        data_class=cast("DataClass", data_class if data_class in _DATA_CLASSES else "internal"),
        last_seq=last_seq if isinstance(last_seq, int) else 0,
        sticky_spec=cast("Mapping[str, JsonValue]", sticky) if isinstance(sticky, Mapping) else None,
    )


def visible_to(principal: Principal, owner: str | None, session_owner: str | None) -> bool:
    """spec-09 §3 visibility V of a turn or file (within the caller's RLS scope): tenant-wide roles see all;
    developers and end users their own and their sessions'."""
    if principal.roles & TENANT_WIDE:
        return True
    if not principal.roles & {"developer", "end_user"}:
        return False
    me = principal.principal_id
    return owner == me or session_owner == me


def _checked_session(services: ServiceSet, principal: Principal, row: Mapping[str, object] | None) -> SessionInfo:
    """Step 6's session rules: 404 `session_not_found`, then the session's turn.create authorization."""
    if row is None:
        raise NoemaError(ErrorCode.SESSION_NOT_FOUND, "Session not found.", param="session_id")
    session = _session_info(row)
    ref = ResourceRef("session", session.session_id, session.project_id, session.owner_principal_id)
    services.authz.check(principal, "turn.create", ref)
    return session


def foldable(request: TurnRequest) -> bool:
    """Steps 6-10 can share one unit of work (E sprint A; `authorize_in`, `prepare(held=...)`): a stored turn of
    a session without file parts or `retry_of` (those read through other units of work; the caller also checks
    that step 7 has no Idempotency-Key, whose store uses Postgres)."""
    return request.store and request.session_id is not None and not request.file_parts and request.retry_of is None


@dataclass(frozen=True, slots=True)
class Held:
    """The unit of work steps 6-10 share (`foldable`). `close()` commits it (step 10 calls it before admission);
    `credentials_error` is step 9's broker failure, from the `broker.get` made before the unit opened (so the
    open transaction never waits on STS), raised at step 9's place."""

    uow: UnitOfWork
    close: Callable[[], Awaitable[None]]
    credentials_error: Exception | None


async def early_credentials(services: ServiceSet, principal: Principal) -> Exception | None:
    """Step 9's `broker.get`, made before step 6 for a folded ingress; its failure is kept for step 9."""
    try:
        await services.broker.get(principal.tenant_id)
    except Exception as exc:
        return exc
    return None


async def authorize_in(services: ServiceSet, principal: Principal, request: TurnRequest, uow: UnitOfWork) -> Authorized:
    """Step 6 of a `foldable` request in the shared unit of work (the errors of `authorize`)."""
    services.authz.check(principal, "turn.create")
    assert request.session_id is not None
    return Authorized(session=_checked_session(services, principal, await uow.sessions.get(request.session_id)))


async def authorize(services: ServiceSet, principal: Principal, request: TurnRequest) -> Authorized:
    """Step 6 (see the module docstring). Raises 403 `permission_denied` / `session_forbidden`, 404
    `session_not_found` / `not_found`, and 500 `internal_error` when Postgres is down."""
    services.authz.check(principal, "turn.create")
    session: SessionInfo | None = None
    if request.session_id is not None:
        async with services.db.uow(principal) as uow:
            row = await uow.sessions.get(request.session_id)
        session = _checked_session(services, principal, row)
    refs: dict[str, FileRef] = {}
    for part in request.file_parts:
        file_id = part.file_id or ""
        if file_id in refs:
            continue
        try:
            file = await services.files.get(principal, file_id)
        except NoemaError as exc:
            raise NoemaError(ErrorCode.NOT_FOUND, "File not found.", param=part.param) from exc
        owner_of_session = session.owner_principal_id if session and file.session_id == session.session_id else None
        if not visible_to(principal, file.principal_id, owner_of_session):
            raise NoemaError(ErrorCode.NOT_FOUND, "File not found.", param=part.param)
        refs[file_id] = file
    if request.retry_of is not None:
        await _check_retry_of(services, principal, request.retry_of)
    return Authorized(session=session, refs=refs)


async def _check_retry_of(services: ServiceSet, principal: Principal, turn_id: str) -> None:
    """`retry_of`: turn.read visibility, else 404 `not_found` (stored only in Phase 1)."""
    async with services.db.uow(principal) as uow:
        row = await uow.turns.get(turn_id)
        session_owner = None
        if row is not None and isinstance(row.get("session_id"), str):
            session_row = await uow.sessions.get(str(row["session_id"]))
            session_owner = str(session_row.get("owner_principal_id")) if session_row is not None else None
    if row is None or not visible_to(principal, str(row.get("principal_id", "")), session_owner):
        raise NoemaError(ErrorCode.NOT_FOUND, "Turn not found.", param="retry_of")


def _allowed_decision_profiles(snapshot: ConfigSnapshot, tenant_id: str) -> list[str]:
    tenant = snapshot.tenant(tenant_id)
    overrides = tenant.request_overrides
    return sorted({snapshot.routing.router_profile, *(overrides.decision_profiles if overrides else ())})


def _allowed_generation_profiles(snapshot: ConfigSnapshot, tenant_id: str) -> list[str]:
    overrides = snapshot.tenant(tenant_id).request_overrides
    return sorted(overrides.generation_profiles if overrides else ())


def _profile_not_allowed(param: str, allowed: Sequence[str]) -> NoemaError:
    details: dict[str, JsonValue] = {"allowed": list(allowed)}
    return NoemaError(
        ErrorCode.PROFILE_NOT_ALLOWED, "The profile is not allowed for this tenant.", param=param, details=details
    )


def _quorum_max(profile: DecisionProfileCfg) -> int:
    return len({m.model for m in profile.members if not m.optional and m.weight > 0})


@dataclass(frozen=True, slots=True)
class Plan:
    """Step 8 results (the decision side; the generation pins are validated in place)."""

    profile: DecisionProfileCfg
    questions: CompiledQuestionSet
    deadline_ms: int
    min_quorum: int
    verify_questions: CompiledQuestionSet | None


def plan_turn(
    services: ServiceSet,
    snapshot: ConfigSnapshot,
    principal: Principal,
    request: TurnRequest,
    refs: Mapping[str, FileRef],
) -> Plan:
    """Step 8 (spec-04 §1): strategy, decision profile and questions (IMPL-069), the generation pins (IMPL-015,
    IMPL-036), `verify` (IMPL-024) and the file status check (409 unless `wait_for_files`)."""
    if request.strategy_requested in ("speculative", "hedge"):
        raise NoemaError(
            ErrorCode.FEATURE_NOT_AVAILABLE,
            f"strategy {request.strategy_requested} is not available in this phase.",
            param="strategy",
        )
    routing = snapshot.routing
    router = snapshot.decision_profile(routing.router_profile)
    profile_id = request.decisions.profile or routing.router_profile
    allowed = _allowed_decision_profiles(snapshot, principal.tenant_id)
    if profile_id not in allowed:
        raise _profile_not_allowed("decisions.profile", allowed)
    try:
        profile = snapshot.decision_profile(profile_id)
    except NoemaError as exc:
        raise _profile_not_allowed("decisions.profile", allowed) from exc
    questions = services.compiler.compile(
        profile,
        request.decisions.questions,
        origin="turn",
        output_format="native",
        blocking=request.decisions.blocking,
        platform_from=router,
    )
    for q in questions.questions:
        if q.source == "platform" and q.method == "hierarchical":
            details: dict[str, JsonValue] = {"max_choices": questions.flat_limit, "question": q.name}
            raise NoemaError(
                ErrorCode.UNSUPPORTED_PARAMETER,
                "A platform question has more options than this decision profile can answer.",
                param="decisions.profile",
                details=details,
            )
    min_quorum = request.decisions.min_quorum or profile.min_quorum
    q_max = _quorum_max(profile)
    if not 1 <= min_quorum <= q_max:
        raise NoemaError(
            ErrorCode.UNSUPPORTED_PARAMETER,
            "min_quorum exceeds the profile's distinct members.",
            param="decisions.min_quorum",
            details={"max": q_max},
        )
    _check_generation_pins(snapshot, principal, request)
    verify_questions = _verification_questions(services, snapshot, request)
    _check_files(request, refs)
    return Plan(profile, questions, request.decisions.deadline_ms or profile.deadline_ms, min_quorum, verify_questions)


def _check_generation_pins(snapshot: ConfigSnapshot, principal: Principal, request: TurnRequest) -> None:
    pins = request.pins
    if pins.generation_profile is None:
        return
    allowed = _allowed_generation_profiles(snapshot, principal.tenant_id)
    if pins.generation_profile not in allowed:
        raise _profile_not_allowed("generation.profile", allowed)
    try:
        profile = snapshot.generation_profile(pins.generation_profile)
    except NoemaError as exc:
        raise _profile_not_allowed("generation.profile", allowed) from exc
    if profile.mode in ("best_of_n", "fuse"):
        raise NoemaError(
            ErrorCode.FEATURE_NOT_AVAILABLE,
            f"Generation profiles of mode {profile.mode} are not available in this phase.",
            param="generation.profile",
        )
    if pins.effort is not None and profile.members:
        primary = snapshot.model(profile.members[0].model)
        if pins.effort not in primary.controls.effort:
            raise NoemaError(
                ErrorCode.UNSUPPORTED_PARAMETER,
                "The pinned profile's primary model does not accept this effort.",
                param="generation.effort",
            )


def verify_possible(snapshot: ConfigSnapshot, request: TurnRequest) -> bool:
    """spec-11 §1.1: `verify: true` or any binding that sets `verify_answer` (static; `verify: false` never
    removes it)."""
    return request.pins.verify is True or any(b.then.verify_answer for b in snapshot.routing.bindings)


def _verification_questions(
    services: ServiceSet, snapshot: ConfigSnapshot, request: TurnRequest
) -> CompiledQuestionSet | None:
    verification = snapshot.routing.verification
    configured = verification is not None and verification.decision_profile is not None
    if request.pins.verify is True and not configured:
        raise NoemaError(
            ErrorCode.UNSUPPORTED_PARAMETER, "Verification is not configured for this platform.", param="verify"
        )
    if not configured or not verify_possible(snapshot, request):
        return None
    assert verification is not None and verification.decision_profile is not None  # noqa: PT018
    profile = snapshot.decision_profile(verification.decision_profile)
    return services.compiler.compile(
        profile, (), origin="verification", output_format="native", select=verification.questions
    )


def _check_files(request: TurnRequest, refs: Mapping[str, FileRef]) -> None:
    """Every `file_id` part is ready/partial, else 409 `file_not_ready` (`file_rejected` for rejected/failed),
    unless `wait_for_files` (the wait is in preflight) or `allow_partial_files` (a rejected part is dropped)."""
    for part in request.file_parts:
        ref = refs.get(part.file_id or "")
        if ref is None or ref.status in ("ready", "partial"):
            continue
        if ref.status in ("rejected", "failed", "deleted"):
            if not request.allow_partial_files:
                raise NoemaError(ErrorCode.FILE_REJECTED, "A file of this turn was rejected.", param=part.param)
        elif not request.wait_for_files:
            raise NoemaError(ErrorCode.FILE_NOT_READY, "A file of this turn is not ready.", param=part.param)


async def _data_class(
    services: ServiceSet, principal: Principal, session: SessionInfo | None, *, store: bool
) -> DataClass:
    """The session's data class, then the project default, then "internal" (IMPL-004). Postgres down: a
    `store: false` turn continues as "internal" (nothing is stored, IMPL-022); a `store: true` turn fails with
    500 `internal_error` (retryable, spec-04 §12), never with a laxer class than the project's."""
    if session is not None:
        return session.data_class
    try:
        async with services.db.uow(principal) as uow:
            project = await uow.projects.get(principal.project_id)
    except Exception as exc:
        if store:
            raise NoemaError(ErrorCode.INTERNAL_ERROR, "The project could not be read; retry.") from exc
        return "internal"
    value = project.get("default_data_class") if project is not None else None
    return cast("DataClass", value) if value in _DATA_CLASSES else "internal"


def _history_limit(request: TurnRequest) -> int:
    """How many stored turns step 10 reads (the selection's scan, at least the decision context's turns)."""
    last_n = request.history.last_n if request.history.mode == "last_n" else None
    scan = min(last_n, HISTORY_SCAN_LIMIT) if last_n else HISTORY_SCAN_LIMIT  # the contract has no maximum
    return max(scan, DECISION_CONTEXT_TURNS)


async def _history_rows(
    services: ServiceSet, principal: Principal, session_id: str, last_seq: int, limit: int, *, required: bool
) -> tuple[Mapping[str, object], ...]:
    with services.telemetry.span("history.fetch"):
        try:
            async with services.db.uow(principal) as uow:
                return await uow.turns.recent(session_id, max_seq=last_seq, limit=limit)
        except NoemaError:
            if required:
                raise
            log.warning("history not read (Postgres down): session %s", session_id)
            return ()


async def prepare(
    services: ServiceSet,
    coordinator: Coordinator,
    *,
    snapshot: ConfigSnapshot,
    principal: Principal,
    request: TurnRequest,
    authorized: Authorized,
    turn_id: str,
    t_in: float,
    deadline: Deadline,
    trace_id: str,
    held: Held | None = None,
) -> PreparedTurn:
    """Steps 8-11 (see the module docstring). `held`: the unit of work of a folded ingress (`foldable`), in which
    step 10 takes the lease and reads the history; it is committed before step 11."""
    plan = plan_turn(services, snapshot, principal, request, authorized.refs)
    tenant = snapshot.tenant_ref(principal.tenant_id)
    if held is None:
        await services.broker.get(principal.tenant_id)  # step 9: 503 tenant_access_misconfigured
    elif held.credentials_error is not None:
        raise held.credentials_error  # step 9 (the broker was asked before the unit of work opened)
    session = authorized.session
    lease: SessionLease | None = None
    rows: tuple[Mapping[str, object], ...] | None = None
    limit = _history_limit(request)

    async def history(uow: UnitOfWork, locked: SessionLease) -> tuple[Mapping[str, object], ...]:
        with services.telemetry.span("history.fetch"):
            return await uow.turns.recent(locked.session_id, max_seq=locked.last_seq, limit=limit)

    if held is not None:  # step 10 in the shared unit of work: one try, then commit (before any wait)
        if session is not None and not await coordinator.session_pending(session.session_id):
            result = await held.uow.sessions.acquire(
                session.session_id, turn_id, expected_seq=request.expected_seq, lease_s=session_order.LEASE_S
            )
            if not isinstance(result, Busy):
                lease, rows = result, await history(held.uow, result)
        await held.close()
    if request.store and session is not None and lease is None:  # step 10 (busy: the FIFO wait of spec-04 §2)
        lease, read = await session_order.acquire_then(
            services.db,
            coordinator,
            services.clock,
            principal,
            session_id=session.session_id,
            turn_id=turn_id,
            expected_seq=request.expected_seq,
            queue_timeout_ms=snapshot.platform.sessions.queue_timeout_ms,
            then=history,
        )
        rows = cast("tuple[Mapping[str, object], ...]", read)
    try:
        return await _prepare_locked(
            services,
            snapshot,
            principal,
            request,
            authorized,
            plan,
            tenant,
            (lease, rows),
            turn_id,
            t_in,
            deadline,
            trace_id,
        )
    except BaseException:
        if lease is not None:
            with anyio.CancelScope(shield=True):
                await session_order.release(services.db, coordinator, principal, lease)
        raise


async def _prepare_locked(
    services: ServiceSet,
    snapshot: ConfigSnapshot,
    principal: Principal,
    request: TurnRequest,
    authorized: Authorized,
    plan: Plan,
    tenant: TenantRef,
    locked: tuple[SessionLease | None, tuple[Mapping[str, object], ...] | None],
    turn_id: str,
    t_in: float,
    deadline: Deadline,
    trace_id: str,
) -> PreparedTurn:
    session = authorized.session
    lease, rows = locked
    sticky: Mapping[str, JsonValue] | None = None
    if session is not None and lease is not None:
        sticky = lease.sticky_spec  # the history `rows` were read with the lease (prepare)
    elif session is not None:
        if request.expected_seq is not None and request.expected_seq != session.last_seq:
            details: dict[str, JsonValue] = {"last_seq": session.last_seq}
            raise NoemaError(ErrorCode.SESSION_CONFLICT, "The session has moved on.", details=details)
        sticky = session.sticky_spec
        rows = await _history_rows(
            services, principal, session.session_id, session.last_seq, _history_limit(request), required=False
        )
    recent = tuple(committed_turns(rows or ()))
    tenant_cfg = snapshot.tenant(principal.tenant_id)
    modalities = modalities_of(request.parts, authorized.refs)
    blocks = ingress_blocks(request.parts, authorized.refs)
    pins = request.pins

    def usable_with(est: int) -> Callable[[str], bool]:
        return lambda profile: profile_usable(
            snapshot, profile, allowed_models=tenant_cfg.allowed_models, est_input=est, spec_max=pins.max_output_tokens
        )

    system = (request.instructions,) if request.instructions else ()
    probe_aliases = member_aliases(snapshot, (snapshot.routing.default_profile,))
    est_cur = estimate(services.estimator, probe_aliases, system, blocks)
    spec0 = compute_spec0(snapshot, sticky, modalities, pins, usable_with(est_cur))
    candidates = candidate_profiles(
        snapshot, spec0_profile=spec0.spec.generation_profile, pinned=pins.generation_profile
    )
    aliases = member_aliases(snapshot, candidates)
    est_cur = estimate(services.estimator, aliases, system, blocks)
    budget = context_budget(snapshot, candidates, pins.max_output_tokens)
    history = select_history(recent, request.history, budget.available(est_cur))
    est_input = estimate(services.estimator, aliases, system, [*history_blocks(history), *blocks])
    if not usable_with(est_input)(spec0.spec.generation_profile):
        spec0 = compute_spec0(
            snapshot, None, modalities, replace(pins, generation_profile=None), usable_with(est_input)
        )
    if not usable_with(est_input)(snapshot.routing.default_profile):
        raise NoemaError(
            ErrorCode.NO_ROUTE_SATISFIES_CONSTRAINTS, "No generation profile fits this request.", param="input"
        )
    data_class = await _data_class(services, principal, session, store=request.store)
    store_content = getattr(snapshot.platform.data_classes, data_class).store_content
    graph = _work_graph(
        services, snapshot, principal, request, plan, candidates, est_input, budget.max_output, recent, authorized.refs
    )
    reservation = await services.admission.reserve(
        principal,
        graph,
        queue_timeout_s=snapshot.platform.limits.admission_queue_timeout_ms / 1000,
        deadline=deadline,
    )
    return PreparedTurn(
        turn_id=turn_id,
        principal=principal,
        tenant=tenant,
        snapshot=snapshot,
        deadline=deadline,
        t_in=t_in,
        trace_id=trace_id,
        request=request,
        refs=authorized.refs,
        session=session,
        lease=lease,
        recent=tuple(recent),
        history=history,
        spec0=spec0,
        candidates=candidates,
        aliases=aliases,
        budget=budget,
        est_input_tokens=est_input,
        profile=plan.profile,
        questions=plan.questions,
        deadline_ms=plan.deadline_ms,
        min_quorum=plan.min_quorum,
        verify_questions=plan.verify_questions,
        reservation=reservation,
        data_class=data_class,
        store_content=store_content,
        created_at=services.clock.now(),
        modalities=modalities,
    )


def _work_graph(
    services: ServiceSet,
    snapshot: ConfigSnapshot,
    principal: Principal,
    request: TurnRequest,
    plan: Plan,
    candidates: tuple[str, ...],
    est_input: int,
    max_output: int,
    recent: Sequence[HistoryTurn],
    refs: Mapping[str, FileRef],
) -> WorkGraph:
    """Step 11 (spec-11 §1.1): the router decision, verification when possible, and the generation."""
    slack = snapshot.routing.speculation.slack_ms or 0
    decision_aliases = sorted({m.model for m in plan.profile.members})
    decision_input = estimate(services.estimator, decision_aliases, (), ingress_blocks(request.parts, refs))
    decision_input += sum(t.tokens for t in recent[:DECISION_CONTEXT_TURNS])
    works = [
        DecisionWork(
            profile_id=plan.profile.id,
            questions=tuple(QuestionWork(q.name, len(q.options), q.source) for q in plan.questions.questions),
            stage0_deadline_ms=plan.deadline_ms,
            escalation="profile_default",
            escalate_budget_ms=plan.deadline_ms + slack,
            kind="decision_member",
            input_tokens=decision_input,
        )
    ]
    verification = snapshot.routing.verification
    if plan.verify_questions is not None and verification is not None and verification.decision_profile:
        verifier = snapshot.decision_profile(verification.decision_profile)
        works.append(
            DecisionWork(
                profile_id=verifier.id,
                questions=tuple(
                    QuestionWork(q.name, len(q.options), q.source) for q in plan.verify_questions.questions
                ),
                stage0_deadline_ms=verifier.deadline_ms,
                escalation="off",
                escalate_budget_ms=None,
                kind="verification_member",
                input_tokens=decision_input + max_output,
            )
        )
    generation = GenerationWork(candidates, est_input, request.pins.max_output_tokens)
    return services.planner.plan(snapshot, principal, decisions=works, generation=generation)
