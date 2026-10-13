"""Read-only admin views W4 (owner: labeling; IMPL-042, ADR-003, spec-09 §3).

`listCalibrators` and `listLabelingRuns` (tenant_admin, reviewer, auditor) and `listLabelingDropped` (tenant_admin,
reviewer): the Authorizer decides the role and the `admin.read` scope (403 `permission_denied`); the rows are
tenant-wide (no project), newest first, keyset-paginated (`limit`, `after`). The middleware has already validated
the query parameters, so the bodies only map them.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from noema.api import deps
from noema.api.deps import ContextDep, PrincipalDep, ServicesDep
from noema.labeling import views

router = APIRouter()


def _limit(ctx: ContextDep) -> int:
    value = ctx.params.get("limit")
    return value if isinstance(value, int) and not isinstance(value, bool) else 50


def _after(ctx: ContextDep) -> str | None:
    value = ctx.params.get("after")
    return value if isinstance(value, str) else None


@router.get("/v1/admin/calibrators", name="listCalibrators")
async def list_calibrators(request: Request, ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """listCalibrators: the tenant's artifacts plus the platform ones that can serve it."""
    svc.authz.check(who, "admin.calibrators.read")
    q = views.params_of(ctx.params, "profile_id", "question_name", "status")
    page = await views.list_calibrators(
        svc.db,
        who,
        profile_id=q["profile_id"],
        question_name=q["question_name"],
        status=q["status"],
        after=_after(ctx),
        limit=_limit(ctx),
    )
    return deps.json_response(page)


@router.get("/v1/admin/labeling/runs", name="listLabelingRuns")
async def list_labeling_runs(request: Request, ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """listLabelingRuns: LLM-consensus runs with their counts and consensus rates per class."""
    svc.authz.check(who, "admin.labeling.read")
    q = views.params_of(ctx.params, "profile_id", "question_name", "status")
    page = await views.list_runs(
        svc.db,
        who,
        profile_id=q["profile_id"],
        question_name=q["question_name"],
        status=q["status"],
        after=_after(ctx),
        limit=_limit(ctx),
    )
    return deps.json_response(page)


@router.get("/v1/admin/labeling/dropped", name="listLabelingDropped")
async def list_labeling_dropped(request: Request, ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """listLabelingDropped: sampled items the labelers did not agree on, with both answers."""
    svc.authz.check(who, "admin.labeling.dropped.read")
    q = views.params_of(ctx.params, "run_id", "question_name", "reason")
    page = await views.list_dropped(
        svc.db,
        who,
        run_id=q["run_id"],
        question_name=q["question_name"],
        reason=q["reason"],
        after=_after(ctx),
        limit=_limit(ctx),
    )
    return deps.json_response(page)
