"""Session routes (owner: identity; spec-08 §9, spec-09 §3). The middleware already authenticated, validated the
parameters and the body; `noema.identity.sessions.SessionService` authorizes and does the work."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Request
from starlette.responses import Response

from noema.api import deps
from noema.api.deps import ContextDep, PrincipalDep, ServicesDep
from noema.core.values import JsonValue
from noema.identity.sessions import SessionService

router = APIRouter()


def _service(svc: deps.ApiServices) -> SessionService:
    return SessionService(db=svc.db, jobs=svc.jobs, authz=svc.authz, ids=svc.ids, clock=svc.clock)


def _body(ctx: deps.RequestContextView) -> Mapping[str, JsonValue]:
    body = ctx.json
    return body if isinstance(body, Mapping) else {}


def _page(ctx: deps.RequestContextView) -> tuple[str | None, int]:
    after, limit = ctx.params.get("after"), ctx.params.get("limit")
    return (after if isinstance(after, str) else None), (limit if isinstance(limit, int) else 50)


@router.get("/v1/sessions", name="listSessions")
async def list_sessions(ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """listSessions (a role/scope failure is an empty page, spec-09 §3)."""
    after, limit = _page(ctx)
    return deps.json_response(await _service(svc).list(who, after=after, limit=limit))


@router.post("/v1/sessions", name="createSession")
async def create_session(request: Request, ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """createSession (201; an Idempotency-Key replays the stored 201)."""
    svc.authz.check(who, "session.create")
    session_id = svc.ids.new("ses")
    replay = await deps.begin_idempotency(request, resource_id=session_id)
    if replay is not None:
        return replay
    session = await _service(svc).create(who, _body(ctx), session_id=session_id, snapshot=ctx.snapshot)
    deps.mark_model_dispatched(request)  # the session exists now: a retry must replay, not create another
    return deps.json_response(session, status_code=201)


@router.get("/v1/sessions/{session_id}", name="getSession")
async def get_session(ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """getSession."""
    return deps.json_response(await _service(svc).get(who, ctx.path_params["session_id"]))


@router.delete("/v1/sessions/{session_id}", name="deleteSession")
async def delete_session(ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """deleteSession (202 with the deletion job id)."""
    result = await _service(svc).delete(who, ctx.path_params["session_id"])
    return deps.json_response(result, status_code=202)


@router.patch("/v1/sessions/{session_id}", name="updateSession")
async def update_session(ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """updateSession."""
    session_id = ctx.path_params["session_id"]
    return deps.json_response(await _service(svc).update(who, session_id, _body(ctx), snapshot=ctx.snapshot))


@router.get("/v1/sessions/{session_id}/turns", name="listSessionTurns")
async def list_session_turns(ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """listSessionTurns (oldest first; `limit` counts messages)."""
    after, limit = _page(ctx)
    page = await _service(svc).list_turns(who, ctx.path_params["session_id"], after=after, limit=limit)
    return deps.json_response(page)
