"""Turn routes (owner: orchestrator; spec-04 §1 steps 6-12, §11; spec-08 §3, §7).

The ingress middleware has run steps 0-5 before these bodies start (see `noema.api.deps`). createTurn authorizes
(step 6), claims the Idempotency-Key with the pre-generated `turn_` id (step 7), runs steps 8-11 in
`TurnService.prepare` and starts the turn task (step 12): SSE streams the turn's event log through the SSE writer;
`stream: false` waits and answers the `TurnObject`, or the failure's HTTP status (`upstream_stalled` -> 503
`all_routes_unavailable`).
"""

from __future__ import annotations

import re
from typing import Final

from fastapi import APIRouter, Request
from starlette.responses import Response

from noema.api import deps
from noema.api.routes import capacity
from noema.api.sse_frame import SendBytes, SseResponse, WriteOutcome
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.turns.request import parse_turn_request
from noema.turns.service import TurnService

router = APIRouter()

_TURN_ID: Final = re.compile(r"^turn_[0-9A-Za-z]{1,64}$")
_SSE_ONLY: Final = frozenset({ErrorCode.UPSTREAM_STALLED, ErrorCode.CLIENT_TOO_SLOW})


def _resolve(request: Request) -> tuple[deps.RequestContextView, deps.ApiServices, Principal]:
    """The context, services and principal, read directly with the `deps` getters (the same values as
    `ContextDep` / `ServicesDep` / `PrincipalDep`, without FastAPI dependency resolution on the turn hot path)."""
    return deps.get_context(request), deps.get_services(request), deps.get_principal(request)


def _turn_id(ctx: deps.RequestContextView) -> str:
    turn_id = ctx.path_params.get("turn_id", "")
    if not _TURN_ID.match(turn_id):
        raise NoemaError(ErrorCode.NOT_FOUND, "Turn not found.")
    return turn_id


@router.post("/v1/turns", name="createTurn")
async def create_turn(request: Request) -> Response:
    """createTurn: the per-process turn cap, then steps 6-12 (authorize, idempotency, plan, credentials, session
    lock, admission, stream). A keyed request found at the cap waits for step 7: a duplicate attaches or replays
    without a slot, a new turn is shed then (Wave E)."""
    ctx, svc, who = _resolve(request)
    service = TurnService.of(svc)
    slot = capacity.Slot(request, svc, who, keyed=capacity.keyed(ctx), turns=service.running)
    try:
        body = parse_turn_request(ctx.json)
        turn_id = svc.ids.new("turn")
        deadline = ctx.deadline
        assert deadline is not None  # createTurn always carries Aghoo-Deadline-Ms (default 120 s)

        async def begin() -> Response | None:  # step 7
            replay = await deps.begin_idempotency(request, resource_id=turn_id, stream=body.stream)
            if replay is None:
                slot.claimed()
            return replay

        try:
            created = await service.create(  # steps 6-11, one unit of work for steps 6-10 where it can (E sprint A)
                snapshot=ctx.snapshot,
                principal=who,
                request=body,
                turn_id=turn_id,
                t_in=ctx.t_in,
                deadline=deadline,
                trace_id=deps.trace_id(ctx),
                begin=begin,
                keyed=capacity.keyed(ctx),
            )
        except Exception as exc:
            slot.failed(exc)  # an admission queue_timeout saturates the tenant (noema.api.overload, Wave E3)
            raise
        if isinstance(created, Response):  # step 7: an idempotent replay or attach
            return created
        prepared = created
        if not deps.content_may_be_kept(ctx.snapshot, prepared.data_class, store=body.store):
            deps.withhold_response(request)  # the idempotency record never keeps what the turn may not (Wave E)
        headers = dict(prepared.reservation.headers())
        runner = await service.start(prepared, on_dispatch=lambda: deps.mark_model_dispatched(request))
        if body.stream:  # the turn outlives the request: a retry attaches to its event log (IMPL-077, Wave E)
            deps.mark_model_dispatched(request)
    finally:
        slot.release()  # the turn is in service.running now, or it failed before it started
    if body.stream:

        async def write(send: SendBytes) -> WriteOutcome:
            return await svc.sse.run(svc.events.subscribe(turn_id), send)

        async def on_close(outcome: WriteOutcome) -> None:
            if outcome == "client_too_slow":  # cancelled as T18 (client_abort), stored as failed (spec-04 §12)
                runner.request_cancel("failed")

        return SseResponse(write, headers=headers, on_close=on_close)
    await service.wait(runner)
    result = runner.result
    assert result is not None
    if result.error is not None and result.status == "failed":
        error = result.error
        if error.code in _SSE_ONLY:
            raise NoemaError(ErrorCode.ALL_ROUTES_UNAVAILABLE, "No route could complete the answer.")
        raise NoemaError(error.code, error.message)
    return deps.json_response(result.turn, headers=headers)


@router.get("/v1/turns/{turn_id}", name="getTurn")
async def get_turn(request: Request) -> Response:
    """getTurn: the stored turn (404 until it is persisted; spec-04 §11)."""
    ctx, svc, who = _resolve(request)
    turn_id = _turn_id(ctx)
    return deps.json_response(await TurnService.of(svc).get(who, turn_id))


@router.post("/v1/turns/{turn_id}/cancel", name="cancelTurn")
async def cancel_turn(request: Request) -> Response:
    """cancelTurn (spec-04 §11): 202 `{turn_id, status}`; idempotent."""
    ctx, svc, who = _resolve(request)
    svc.authz.check(who, "turn.cancel")
    turn_id = _turn_id(ctx)
    replay = await deps.begin_idempotency(request, resource_id=turn_id)
    if replay is not None:
        return replay
    answer = await TurnService.of(svc).cancel(who, turn_id)
    return deps.json_response(answer, status_code=202)


@router.get("/v1/turns/{turn_id}/events", name="resumeTurnEvents")
async def resume_turn_events(request: Request) -> Response:
    """resumeTurnEvents: 501 `not_implemented` in Phase 1 (IMPL-036, spec-08 §7) for a turn in V(turn); 404
    `not_found` outside it, as getTurn (spec-09 §3; Wave E2)."""
    ctx, svc, who = _resolve(request)
    svc.authz.check(who, "turn.read")
    await TurnService.of(svc).get(who, _turn_id(ctx))
    raise NoemaError(ErrorCode.NOT_IMPLEMENTED, "Resuming a turn's event stream is not available in this phase.")
