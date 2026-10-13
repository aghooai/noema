"""Profile listing (owner: decision; spec-06 §12.4, IMPL-031): the caller's allowlist only (no scope required)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from noema.api import deps
from noema.decision.profiles import list_profiles

router = APIRouter()


@router.get("/v1/profiles", name="listProfiles")
async def list_profiles_route(
    request: Request, ctx: deps.ContextDep, svc: deps.ServicesDep, who: deps.PrincipalDep
) -> Response:
    """listProfiles: the decision and generation profiles the caller may select (`?kind=` filters)."""
    svc.authz.check(who, "profile.list")
    kind = ctx.params.get("kind")
    data = list_profiles(ctx.snapshot, who.tenant_id, kind if isinstance(kind, str) else None)
    return deps.json_response({"data": data})
