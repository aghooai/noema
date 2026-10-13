"""The request flow shared by `POST /v1/decisions` and the facade (spec-04 §1 steps 6-12 for decisions; spec-06
§2, §4.1, §15; IMPL-022, IMPL-032, IMPL-052, IMPL-057, IMPL-074).

Order: authorize (and the session's access) -> idempotency (the route) -> profile, questions and files (HTTP
errors, no reservation) -> admission -> inline-image ingest -> the run (credentials, views, dispatch check) ->
reconcile and release. A response is stored for replay once a model call was dispatched.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, cast

from noema.config.models import DecisionProfileCfg
from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.auth import ResourceRef
from noema.core.protocols.config import ConfigSnapshot
from noema.core.protocols.decision import (
    CompiledQuestionSet,
    DecisionContext,
    DecisionRequest,
    DecisionSnapshot,
    Origin,
    OutputFormat,
    ProfileRef,
)
from noema.core.protocols.media import FileRef
from noema.core.protocols.services import ServiceSet
from noema.core.values import DataClass, JsonValue

from .compiler import QuestionCompilerImpl
from .inputs import NativeInput
from .profiles import check_family, effective_quorum
from .run import DecisionRunImpl
from .work import decision_work

_log: Final = logging.getLogger("noema.decision.service")
_DATA_CLASSES: Final = frozenset({"public", "internal", "confidential", "restricted"})
IncludeKey = Literal["members", "member_distributions", "stages"]


@dataclass(frozen=True, slots=True)
class DecisionCall:
    """A validated request before admission: the resolved profile, the compiled request (its `input` is filled
    after the inline ingest) and the input in progress."""

    profile: DecisionProfileCfg
    request: DecisionRequest
    input: NativeInput
    session_id: str | None


def compile_questions(
    svc: ServiceSet,
    snapshot: ConfigSnapshot,
    profile: DecisionProfileCfg,
    questions: Sequence[Mapping[str, JsonValue]],
    *,
    origin: Origin,
    output_format: OutputFormat,
) -> CompiledQuestionSet:
    """Compile against the request's pinned snapshot when the compiler supports it."""
    compiler = svc.compiler
    if isinstance(compiler, QuestionCompilerImpl):
        return compiler.compile_with(snapshot, profile, questions, origin=origin, output_format=output_format)
    return compiler.compile(profile, questions, origin=origin, output_format=output_format)


def trace_id_of(trace_id: str | None, request_id: str) -> str:
    """The request's W3C trace id, else one derived from the request id (32 lower-case hex digits)."""
    if trace_id and len(trace_id) == 32 and all(c in "0123456789abcdef" for c in trace_id):
        return trace_id
    return hashlib.sha256(request_id.encode()).hexdigest()[:32]


async def data_class_of(svc: ServiceSet, who: Principal, session_id: str | None) -> DataClass:
    """The session's data class (after its access check: 404 `session_not_found`, 403 `permission_denied`), else
    the project's `default_data_class`, else `internal`. When the project can't be read (Postgres down, a pool or
    statement timeout) the decision continues (IMPL-022) with the strictest class, `restricted`: never a laxer
    class than the project's (no L2, metadata_only, no labeling offer; Wave E)."""
    if session_id is not None:
        async with svc.db.uow(who) as uow:
            row = await uow.sessions.get(session_id)
            if row is None:
                raise NoemaError(ErrorCode.SESSION_NOT_FOUND, "Session not found.", param="session_id")
            ref = ResourceRef("session", session_id, str(row.get("project_id")), str(row.get("owner_principal_id")))
            svc.authz.check(who, "decision.create", ref)
            value = row.get("data_class")
            return cast(DataClass, value) if value in _DATA_CLASSES else "internal"
    try:
        async with svc.db.uow(who) as uow:
            project = await uow.projects.get(who.project_id)
    except Exception as exc:  # the decision does not depend on Postgres (IMPL-022)
        _log.warning("project lookup failed (%s); data class restricted", type(exc).__name__)
        return "restricted"
    value = project.get("default_data_class") if project is not None else None
    return cast(DataClass, value) if value in _DATA_CLASSES else "internal"


async def visible_file(svc: ServiceSet, who: Principal, ref: FileRef) -> None:
    """V(file) (spec-09 §3): the uploader or the owner of its session (tenant-wide roles: every file); a file
    outside it is 404 `not_found`, like a missing one (Wave E)."""
    session_owner: str | None = None
    if ref.session_id is not None and ref.principal_id != who.principal_id:
        async with svc.db.uow(who) as uow:
            row = await uow.sessions.get(ref.session_id)
        owner = row.get("owner_principal_id") if row is not None else None
        session_owner = owner if isinstance(owner, str) else None
    resource = ResourceRef("file", ref.file_id, None, ref.principal_id, session_owner)
    try:
        svc.authz.check(who, "decision.create", resource)
    except NoemaError as exc:
        raise NoemaError(ErrorCode.NOT_FOUND, "File not found.") from exc


async def prepare_native(
    svc: ServiceSet, snapshot: ConfigSnapshot, who: Principal, body: Mapping[str, Any], profile: DecisionProfileCfg
) -> DecisionCall:
    """Questions, quorum, the template family and the uploaded files of a `/v1/decisions` body."""
    output_format: OutputFormat = "jev" if body.get("output_format") == "jev" else "native"
    qs = compile_questions(svc, snapshot, profile, body["questions"], origin="native", output_format=output_format)
    min_quorum = effective_quorum(profile, body.get("min_quorum"))
    native = NativeInput(body["input"])
    check_family(profile, "")  # a /v1/decisions input is always a standard input: verify-dist profiles are 422
    await native.check_files(svc.files, who, lambda ref: visible_file(svc, who, ref))
    include: frozenset[IncludeKey] = frozenset(
        cast(IncludeKey, v) for v in body.get("include", ()) if v in ("members", "member_distributions", "stages")
    )
    request = DecisionRequest(
        profile=ProfileRef(profile.id, profile.version),
        questions=qs,
        input="",
        deadline_ms=int(body.get("deadline_ms") or profile.deadline_ms),
        min_quorum=min_quorum,
        escalation="off" if body.get("escalation") == "off" else "profile_default",
        include=include,
        origin="native",
        output_format=output_format,
        safety_identifier=body.get("safety_identifier"),
    )
    return DecisionCall(profile, request, native, body.get("session_id"))


async def execute(
    svc: ServiceSet,
    snapshot: ConfigSnapshot,
    who: Principal,
    call: DecisionCall,
    *,
    deadline: Deadline,
    trace_id: str,
    decision_id: str,
    data_class: DataClass,
    on_dispatch: Callable[[], None],
) -> tuple[DecisionSnapshot, Mapping[str, JsonValue], Mapping[str, str]]:
    """Admission, inline ingest and the run: `(final snapshot, full result, x-ratelimit-* headers)`; the headers
    are `Reservation.headers()` right after the debit, for the 200 response (IMPL-033). Raises the HTTP errors of
    admission (429), ingest (413/409) and `run().__aenter__` (503 credentials, 504 dispatch)."""
    req = call.request
    tokens = call.input.estimate(svc.estimator, call.profile.members[0].model)
    work = decision_work(req, escalate_budget_ms=deadline.remaining_ms(), input_tokens=tokens)
    graph = svc.planner.plan(snapshot, who, decisions=[work], generation=None)
    timeout_s = snapshot.platform.limits.admission_queue_timeout_ms / 1000
    reservation = await svc.admission.reserve(who, graph, queue_timeout_s=timeout_s, deadline=deadline)
    rate_headers = dict(reservation.headers())
    try:
        await call.input.ingest(svc.files, who, deadline=deadline, session_id=call.session_id)
        req = replace(req, input=call.input.value())
        ctx = DecisionContext(
            principal=who,
            tenant=snapshot.tenant_ref(who.tenant_id),
            snapshot=snapshot,
            deadline=deadline,
            escalate_until=deadline,
            reservation=reservation,
            purpose="decision",
            store=True,
            data_class=data_class,
            content_policy=snapshot.platform.data_classes.for_class(data_class).store_content,
            trace_id=trace_id,
            session_id=call.session_id,
            decision_id=decision_id,
        )
        async with svc.decisions.run(ctx, req) as run:
            if not isinstance(run, DecisionRunImpl) or run.dispatched:
                on_dispatch()
            final = await run.finalize()
        full: Mapping[str, JsonValue] = final.result
        usage = {}
        if isinstance(run, DecisionRunImpl):
            full = run.full_result or final.result
            usage = run.usage_by_alias()
        await reservation.reconcile(usage)
    finally:
        await reservation.release()
    return final, full, rate_headers


def all_unavailable(final: DecisionSnapshot) -> bool:
    """Every answer is `unavailable` (any reason)."""
    return bool(final.answers) and all(a.type == "unavailable" for a in final.answers.values())


def raise_tenant_access(final: DecisionSnapshot, full: Mapping[str, JsonValue]) -> None:
    """503 `tenant_access_misconfigured` (row stored, `details.decision_id`) when every answer is unavailable and
    every member that ran failed with `tenant_access_misconfigured` (IMPL-057)."""
    if not all_unavailable(final):
        return
    answers = full.get("answers")
    codes: list[object] = []
    for a in answers if isinstance(answers, list) else []:
        members = a.get("members") if isinstance(a, Mapping) else None
        for m in members if isinstance(members, list) else []:
            if isinstance(m, Mapping) and m.get("status") != "skipped":
                error = m.get("error")
                codes.append(error.get("code") if isinstance(error, Mapping) and m.get("status") == "error" else None)
    if codes and all(c == "tenant_access_misconfigured" for c in codes):
        raise NoemaError(
            ErrorCode.TENANT_ACCESS_MISCONFIGURED,
            "Tenant access is misconfigured for every decision member.",
            details={"decision_id": final.decision_id},
        )
