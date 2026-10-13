"""Usage report (owner: ops-ledger; spec-11 §2, IMPL-040, spec-09 §3).

`GET /v1/usage`: authorization `usage.read` (developer, tenant_admin, auditor; others 403 `permission_denied`),
then one keyset page of `UsageRepo.report` under `uow(principal)`: a developer sees its project, tenant_admin and
auditor the whole tenant (RLS). The query parameters arrive coerced (with defaults) from the RequestGate.
"""

from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Request
from starlette.responses import Response

from noema.api import deps
from noema.ops.usage import UsageReporter, parse_query

router = APIRouter()


@router.get("/v1/usage", name="getUsage")
async def get_usage(request: Request, ctx: deps.ContextDep, svc: deps.ServicesDep, who: deps.PrincipalDep) -> Response:
    """getUsage: one page of usage rows by (key, line) over the inclusive UTC day range."""
    svc.authz.check(who, "usage.read")
    query = parse_query(ctx.params, svc.clock.now().astimezone(UTC).date())
    body = await UsageReporter(svc.db).report(who, query)
    return Response(body, status_code=200, media_type="application/json")
