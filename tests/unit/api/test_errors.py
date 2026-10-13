"""The error envelope (IMPL-030, spec-08 §2-§3): every status, never a stack trace, never echoing secrets."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter, Request
from starlette.exceptions import HTTPException
from starlette.responses import Response, StreamingResponse
from tests.unit.api.apifakes import DEV_KEY, asgi_client, auth, make_app

from noema.api import deps
from noema.contracts.bundle import default_bundle
from noema.core.errors import ERROR_TABLE, ErrorCode, NoemaError, TenantAccessMisconfigured

pytestmark = pytest.mark.unit
ERROR_SCHEMA = default_bundle().validator("/components/schemas/Error")


def _raising_app() -> tuple[object, object]:
    router = APIRouter()

    @router.get("/v1/turns/{turn_id}", name="getTurn")
    async def raise_code(request: Request, turn_id: str) -> Response:
        if turn_id == "boom":
            raise ValueError(f"leaked {DEV_KEY} detail")
        if turn_id == "http404":
            raise HTTPException(404)
        if turn_id == "http418":
            raise HTTPException(418)
        if turn_id == "tenant":
            raise TenantAccessMisconfigured("access_denied")
        if turn_id == "limited":
            deps.add_error_headers(request, {"x-ratelimit-limit-requests": "60"})
            raise NoemaError(ErrorCode.RPM_EXCEEDED, "Rate limit exceeded.", retry_after_s=0)
        raise NoemaError(ErrorCode(turn_id), "Failure.", param="x", details={"k": 1})

    app, _ = make_app(routers=[router], validate=False)
    return app, router


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda c: c.value)
async def test_every_code_renders_its_error_table_row(code: ErrorCode) -> None:
    app, _ = _raising_app()
    async with asgi_client(app) as client:  # type: ignore[arg-type]
        resp = await client.get(f"/v1/turns/{code.value}", headers=auth())
    spec = ERROR_TABLE[code]
    if spec.status is None:  # SSE-only codes over HTTP (spec-04 §1)
        expected = ErrorCode.ALL_ROUTES_UNAVAILABLE if code is ErrorCode.UPSTREAM_STALLED else ErrorCode.INTERNAL_ERROR
        spec, code = ERROR_TABLE[expected], expected
    body = resp.json()
    assert ERROR_SCHEMA.is_valid(body)
    assert resp.status_code == spec.status
    assert resp.headers["content-type"] == "application/json"
    error = body["error"]
    assert (error["code"], error["type"], error["retryable"]) == (code.value, spec.type.value, spec.retryable)
    assert error["request_id"] == resp.headers["x-request-id"]
    assert ("www-authenticate" in resp.headers) is (spec.status == 401)
    assert "retry-after" not in resp.headers


async def test_unexpected_exceptions_are_500_without_trace_or_secret(caplog: pytest.LogCaptureFixture) -> None:
    app, _ = _raising_app()
    with caplog.at_level(logging.ERROR, logger="noema.api.errors"):
        async with asgi_client(app) as client:  # type: ignore[arg-type]
            resp = await client.get("/v1/turns/boom", headers=auth())
    assert resp.status_code == 500
    assert resp.json()["error"] == {
        "type": "server_error",
        "code": "internal_error",
        "message": "Internal server error.",
        "param": None,
        "request_id": resp.headers["x-request-id"],
        "retryable": True,
    }
    assert "Traceback" not in resp.text
    assert DEV_KEY not in resp.text
    assert any(r.exc_info for r in caplog.records)  # logged server-side (the telemetry handler redacts)


async def test_starlette_http_exceptions_become_envelopes() -> None:
    app, _ = _raising_app()
    async with asgi_client(app) as client:  # type: ignore[arg-type]
        not_found = await client.get("/v1/turns/http404", headers=auth())
        teapot = await client.get("/v1/turns/http418", headers=auth())
    assert (not_found.status_code, not_found.json()["error"]["code"]) == (404, "not_found")
    assert (teapot.status_code, teapot.json()["error"]["code"]) == (500, "internal_error")


async def test_retry_after_details_and_error_headers() -> None:
    app, _ = _raising_app()
    async with asgi_client(app) as client:  # type: ignore[arg-type]
        limited = await client.get("/v1/turns/limited", headers=auth())
        tenant = await client.get("/v1/turns/tenant", headers=auth())
        detailed = await client.get("/v1/turns/session_busy", headers=auth())
    assert limited.headers["retry-after"] == "1"  # at least 1 (spec-08 §4)
    assert limited.headers["x-ratelimit-limit-requests"] == "60"
    assert tenant.status_code == 503
    assert "retry-after" not in tenant.headers  # absent when a retry can't help
    assert tenant.json()["error"]["details"] == {"cause": "access_denied"}
    assert detailed.json()["error"]["details"] == {"k": 1}
    assert detailed.json()["error"]["param"] == "x"


async def test_an_error_after_the_response_started_closes_the_stream() -> None:
    router = APIRouter()

    async def body() -> AsyncIterator[bytes]:
        yield b"partial"
        raise NoemaError(ErrorCode.ALL_ROUTES_UNAVAILABLE, "gone")

    @router.get("/v1/turns/{turn_id}", name="getTurn")
    async def stream(request: Request) -> Response:
        return StreamingResponse(body(), media_type="text/plain")

    app, _ = make_app(routers=[router], validate=False)
    async with asgi_client(app) as client:
        resp = await client.get("/v1/turns/t", headers=auth())
    assert (resp.status_code, resp.content) == (200, b"partial")


async def test_the_access_log_has_no_header_values(caplog: pytest.LogCaptureFixture) -> None:
    app, _ = make_app()
    with caplog.at_level(logging.INFO, logger="noema.api.access"):
        async with asgi_client(app) as client:
            await client.get("/v1/turns/turn_1", headers=auth(**{"x-request-id": "rid-1", "cookie": "c=secret"}))
    (record,) = [r for r in caplog.records if r.name == "noema.api.access"]
    fields = {k: getattr(record, k) for k in ("http_method", "http_route", "http_status", "request_id", "tenant_id")}
    assert fields == {
        "http_method": "GET",
        "http_route": "/v1/turns/{turn_id}",
        "http_status": 501,
        "request_id": "rid-1",
        "tenant_id": "t_acme",
    }
    assert getattr(record, "key_id", None) is not None
    dumped = repr(record.__dict__)
    assert DEV_KEY not in dumped
    assert "secret" not in dumped


async def test_a_noema_error_inside_a_single_exception_group_keeps_its_code() -> None:
    router = APIRouter()

    @router.get("/v1/turns/{turn_id}", name="getTurn")
    async def grouped(request: Request, turn_id: str) -> Response:
        if turn_id == "one":
            raise ExceptionGroup("tg", [NoemaError(ErrorCode.NOT_FOUND, "Not found.")])
        raise ExceptionGroup("tg", [NoemaError(ErrorCode.NOT_FOUND, "a"), ValueError("b")])

    app, _ = make_app(routers=[router], validate=False)
    async with asgi_client(app) as client:
        single = await client.get("/v1/turns/one", headers=auth())
        several = await client.get("/v1/turns/two", headers=auth())
    assert (single.status_code, single.json()["error"]["code"]) == (404, "not_found")
    assert (several.status_code, several.json()["error"]["code"]) == (500, "internal_error")


async def test_missing_lifespan_state_is_a_typed_500() -> None:
    app, _ = make_app(validate=False)
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/healthz", "headers": [], "query_string": b"", "state": {}}
    await app(scope, receive, send)  # type: ignore[arg-type]
    assert sent[0]["status"] == 500
    assert b'"code":"internal_error"' in sent[1]["body"]  # type: ignore[operator]
