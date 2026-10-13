"""Native decision routes (owner: decision; spec-06 §2, §12, §15, §16; IMPL-031, IMPL-057).

`POST /v1/decisions` answers 200 with the native `DecisionResult` (or the Jev view for `output_format: jev`),
also when every answer is unavailable (`status: partial`), except the 503 `tenant_access_misconfigured` of
IMPL-057. `GET /v1/decisions/{id}` returns the stored full native result, or 404 `not_found` outside V(decision).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from noema.api import deps
from noema.api.routes import capacity
from noema.config.models import DecisionProfileCfg
from noema.core.clock import Deadline, deadline_scope
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.auth import ResourceRef
from noema.core.protocols.config import ConfigSnapshot
from noema.decision import service
from noema.decision.jev import jev_response
from noema.decision.profiles import resolve_native

router = APIRouter()


def _body(ctx: deps.RequestContextView) -> Mapping[str, Any]:
    body = ctx.json
    if not isinstance(body, Mapping):  # the request gate guarantees an object
        raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "The body must be a JSON object.")
    return body


@router.post("/v1/decisions", name="createDecision")
async def create_decision(
    request: Request, ctx: deps.ContextDep, svc: deps.ServicesDep, who: deps.PrincipalDep
) -> Response:
    """createDecision (under the per-process cap, `capacity.Slot`)."""
    slot = capacity.Slot(request, svc, who, keyed=capacity.keyed(ctx))
    try:
        return await _create_decision(request, ctx, svc, who, slot)
    finally:
        slot.release()


async def _create_decision(
    request: Request, ctx: deps.RequestContextView, svc: deps.ApiServices, who: Principal, slot: capacity.Slot
) -> Response:
    body = _body(ctx)
    svc.authz.check(who, "decision.create")
    data_class = await service.data_class_of(svc, who, body.get("session_id"))
    if not deps.content_may_be_kept(ctx.snapshot, data_class):
        deps.withhold_response(request)  # the idempotency record keeps no content the data class forbids (Wave E)
    decision_id = svc.ids.new("dec")
    replay = await deps.begin_idempotency(request, resource_id=decision_id)
    if replay is not None:
        return replay
    slot.claimed()
    snapshot = ctx.snapshot
    profile = resolve_native(snapshot, who.tenant_id, body.get("profile"))
    assert ctx.deadline is not None  # createDecision always has one (Aghoo-Deadline-Ms)
    call = await _prepare(svc, snapshot, who, body, profile, ctx.deadline)
    final, full, rate_headers = await service.execute(
        svc,
        snapshot,
        who,
        call,
        deadline=ctx.deadline,
        trace_id=service.trace_id_of(ctx.trace.trace_id, ctx.request_id),
        decision_id=decision_id,
        data_class=data_class,
        on_dispatch=lambda: deps.mark_model_dispatched(request),
    )
    service.raise_tenant_access(final, full)
    if call.request.output_format == "jev":
        model = f"aghoo/{profile.id}@{profile.version}"
        jev = jev_response(final.result, call.request.questions.questions, model=model)
        return deps.json_response(jev, headers=rate_headers)
    return deps.json_response(dict(final.result), headers=rate_headers)


async def _prepare(
    svc: deps.ApiServices,
    snapshot: ConfigSnapshot,
    who: Principal,
    body: Mapping[str, Any],
    profile: DecisionProfileCfg,
    deadline: Deadline,
) -> service.DecisionCall:
    """`prepare_native` (its file checks read Postgres) under the request deadline: 504 `deadline_exceeded` when it
    passes first (before any dispatch, so no row and no usage, IMPL-032; Wave E2)."""
    async with deadline_scope(svc.clock, deadline.at):
        return await service.prepare_native(svc, snapshot, who, body, profile)
    raise NoemaError(ErrorCode.DEADLINE_EXCEEDED, "The deadline passed before the decision could start.")


@router.get("/v1/decisions/{decision_id}", name="getDecision")
async def get_decision(
    request: Request, ctx: deps.ContextDep, svc: deps.ServicesDep, who: deps.PrincipalDep
) -> Response:
    """getDecision: the stored native result (always full)."""
    decision_id = ctx.path_params["decision_id"]
    svc.authz.check(who, "decision.read")
    async with svc.db.uow(who) as uow:
        row = await uow.decisions.get(decision_id)
    if row is None:
        raise NoemaError(ErrorCode.NOT_FOUND, "Decision not found.")
    owner = row.get("principal_id")
    project = row.get("project_id")
    ref = ResourceRef("decision", decision_id, str(project), str(owner) if owner is not None else None)
    svc.authz.check(who, "decision.read", ref)
    result = row.get("result")
    if not isinstance(result, dict):
        raise NoemaError(ErrorCode.NOT_FOUND, "Decision not found.")
    return deps.json_response(result)
