"""`noema.api.deps`: the FastAPI dependencies run on the event loop, never in the threadpool."""

from __future__ import annotations

import inspect
from typing import Any, get_args

import fastapi.dependencies.utils as fastapi_deps
import pytest
from fastapi import APIRouter, Request
from starlette.responses import Response
from tests.unit.api.apifakes import asgi_client, auth, make_app

from noema.api import deps

pytestmark = pytest.mark.unit
DEPS = (deps.ContextDep, deps.ServicesDep, deps.PrincipalDep, deps.SnapshotDep)


@pytest.mark.parametrize("annotated", DEPS)
def test_every_dependency_is_a_coroutine_function(annotated: Any) -> None:
    (depends,) = get_args(annotated)[1:]
    assert inspect.iscoroutinefunction(depends.dependency), depends.dependency


async def test_a_route_with_every_dependency_never_hops_to_the_threadpool(monkeypatch: pytest.MonkeyPatch) -> None:
    hops: list[str] = []
    real = fastapi_deps.run_in_threadpool  # type: ignore[attr-defined]  # what solve_dependencies calls

    async def counting(func: Any, *args: Any, **kwargs: Any) -> Any:
        hops.append(getattr(func, "__name__", repr(func)))
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(fastapi_deps, "run_in_threadpool", counting)
    router = APIRouter()
    seen: dict[str, object] = {}

    @router.get("/v1/turns/{turn_id}", name="getTurn")
    async def probe(
        request: Request,
        ctx: deps.ContextDep,
        svc: deps.ServicesDep,
        who: deps.PrincipalDep,
        snapshot: deps.SnapshotDep,
    ) -> Response:
        seen.update(ctx=ctx is deps.get_context(request), svc=svc is deps.get_services(request))
        seen.update(who=who is ctx.principal, snapshot=snapshot is ctx.snapshot)
        return deps.json_response({"ok": True})

    app, _ = make_app(routers=[router], validate=False)
    async with asgi_client(app) as client:
        resp = await client.get("/v1/turns/turn_1", headers=auth())
    assert resp.status_code == 200, resp.text
    assert seen == {"ctx": True, "svc": True, "who": True, "snapshot": True}
    assert hops == []
