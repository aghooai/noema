"""Route registration and the 501 stubs (IMPL-066: the app's routes equal the openapi routes)."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from tests.unit.api.apifakes import asgi_client, auth, make_app, request_for

from noema.api.app import API_ROUTERS
from noema.contracts.bundle import default_bundle

pytestmark = pytest.mark.unit
OPERATIONS = default_bundle().operations()


def test_registered_routes_equal_the_openapi_operations() -> None:
    """create_app mounts API_ROUTERS without prefixes; each route answers (501 test below), never Starlette's 404."""
    registered = {
        (method, r.path, r.name)
        for router in API_ROUTERS
        for r in router.routes
        if isinstance(r, APIRoute)
        for method in r.methods or ()
    }
    expected = {(op.method, op.path, op.operation_id) for op in OPERATIONS}
    assert registered == expected
    assert len(expected) == 25


@pytest.mark.parametrize("op", [o for o in OPERATIONS if o.authenticated], ids=lambda o: o.operation_id)
async def test_every_authenticated_stub_answers_a_typed_501(op: object) -> None:
    app, _ = make_app()
    method, url, headers, body = request_for(op)  # type: ignore[arg-type]
    async with asgi_client(app) as client:
        resp = await client.request(method, url, headers=headers, content=body)
    assert resp.status_code == 501, resp.text
    error = resp.json()["error"]
    assert error == {
        "type": "invalid_request_error",
        "code": "not_implemented",
        "message": "This operation is not implemented yet.",
        "param": None,
        "request_id": resp.headers["x-request-id"],
        "retryable": False,
    }


async def test_health_stubs() -> None:
    app, _ = make_app()
    async with asgi_client(app) as client:
        live = await client.get("/healthz")
        ready = await client.get("/readyz")
    assert (live.status_code, live.content) == (200, b"")
    assert (ready.status_code, ready.content) == (503, b"")
    assert live.headers["x-request-id"].startswith("req_")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/metrics"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("PUT", "/v1/turns"),
        ("GET", "/v1/turns/"),
        ("DELETE", "/v1/turns/turn_1"),
        ("GET", "/v2/turns"),
    ],
)
async def test_unknown_routes_and_methods_are_404_not_found(method: str, path: str) -> None:
    app, _ = make_app()
    async with asgi_client(app) as client:
        resp = await client.request(method, path, headers=auth())
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert resp.json()["error"]["request_id"] == resp.headers["x-request-id"]
