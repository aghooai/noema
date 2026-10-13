"""The OpenAI Decisions-compatible facade (owner: decision; spec-06 §12.3, IMPL-031, IMPL-047, IMPL-049).

Answers come from Bedrock decision profiles only (D1); Noema never calls the OpenAI API. Every answer
unavailable -> 503 `all_routes_unavailable` (the decision is stored, `details.decision_id`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from noema.api import deps
from noema.api.routes import capacity
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.decision import DecisionRequest, ProfileRef
from noema.decision import service
from noema.decision.facade import map_input, map_questions, oa_response
from noema.decision.inputs import NativeInput
from noema.decision.profiles import check_family, resolve_facade

router = APIRouter()


@router.post("/openai/v1/decisions", name="createDecisionOpenAICompat")
async def create_decision_openai_compat(
    request: Request, ctx: deps.ContextDep, svc: deps.ServicesDep, who: deps.PrincipalDep
) -> Response:
    """createDecisionOpenAICompat (under the per-process cap, `capacity.Slot`)."""
    slot = capacity.Slot(request, svc, who, keyed=capacity.keyed(ctx))
    try:
        return await _create(request, ctx, svc, who, slot)
    finally:
        slot.release()


async def _create(
    request: Request, ctx: deps.RequestContextView, svc: deps.ApiServices, who: Principal, slot: capacity.Slot
) -> Response:
    body = ctx.json
    if not isinstance(body, Mapping):  # the request gate guarantees an object
        raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "The body must be a JSON object.")
    raw: Mapping[str, Any] = body
    svc.authz.check(who, "decision.create")
    data_class = await service.data_class_of(svc, who, None)
    if not deps.content_may_be_kept(ctx.snapshot, data_class):
        deps.withhold_response(request)  # the idempotency record keeps no content the data class forbids (Wave E)
    decision_id = svc.ids.new("dec")
    replay = await deps.begin_idempotency(request, resource_id=decision_id)
    if replay is not None:
        return replay
    slot.claimed()
    snapshot = ctx.snapshot
    profile = resolve_facade(snapshot, who.tenant_id, str(raw.get("model", "")))
    questions = map_questions(raw["questions"])
    qs = service.compile_questions(
        svc, snapshot, profile, [q.wire for q in questions], origin="facade", output_format="openai"
    )
    check_family(profile, "")
    native = NativeInput(map_input(raw["input"]))
    call = service.DecisionCall(
        profile,
        DecisionRequest(
            profile=ProfileRef(profile.id, profile.version),
            questions=qs,
            input="",
            deadline_ms=profile.deadline_ms,
            min_quorum=profile.min_quorum,
            escalation="profile_default",
            include=frozenset(),
            origin="facade",
            output_format="openai",
            safety_identifier=raw.get("safety_identifier"),
        ),
        native,
        None,
    )
    assert ctx.deadline is not None  # the facade always has one (Aghoo-Deadline-Ms)
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
    if service.all_unavailable(final):
        raise NoemaError(
            ErrorCode.ALL_ROUTES_UNAVAILABLE,
            "No decision member could answer.",
            details={"decision_id": final.decision_id},
            retry_after_s=1,
        )
    ensemble = ctx.params.get("x-aghoo-include") == "ensemble"
    model = f"aghoo/{profile.id}@{profile.version}"
    return deps.json_response(
        oa_response(final.result, questions, model=model, ensemble=ensemble), headers=rate_headers
    )
