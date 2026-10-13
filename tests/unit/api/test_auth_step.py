"""Step 4: authentication (401 never echoes the key), Aghoo-Version (400) and x-aghoo-end-user (422)."""

from __future__ import annotations

import orjson
import pytest
from fastapi import APIRouter, Request
from starlette.responses import Response
from tests.unit.api.apifakes import DEV_KEY, TURN_BODY, asgi_client, auth, make_app

from noema.api import deps
from noema.core.errors import ErrorCode, NoemaError

pytestmark = pytest.mark.unit
JSON = {"content-type": "application/json"}
BAD_KEY = "ak_test_01J0000000000000000000BAD0." + "s" * 43


@pytest.mark.parametrize("authorization", [None, "", f"Bearer {BAD_KEY}", f"Basic {DEV_KEY}", "Bearer oidc.jwt.token"])
async def test_401_is_typed_fixed_and_never_echoes_the_credential(authorization: str | None) -> None:
    app, _ = make_app()
    headers = {**JSON} if authorization is None else {**JSON, "authorization": authorization}
    async with asgi_client(app) as client:
        resp = await client.post("/v1/turns", content=orjson.dumps(TURN_BODY), headers=headers)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    error = resp.json()["error"]
    assert (error["code"], error["type"], error["message"], error["retryable"]) == (
        "invalid_api_key",
        "authentication_error",
        "Invalid API key.",
        False,
    )
    assert "s" * 43 not in resp.text
    assert "d" * 43 not in resp.text


async def test_auth_failure_rate_limit_from_the_authenticator_keeps_retry_after() -> None:
    app, services = make_app()
    services.authn.error = NoemaError(ErrorCode.RPM_EXCEEDED, "Too many failed authentications.", retry_after_s=7)
    async with asgi_client(app) as client:
        resp = await client.get("/v1/turns/turn_1", headers=auth())
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "7"
    assert resp.json()["error"]["retryable"] is True


async def test_the_authenticator_gets_the_client_ip_and_the_principal_carries_the_request_id() -> None:
    seen: dict[str, object] = {}
    router = APIRouter()

    @router.get("/v1/turns/{turn_id}", name="getTurn")
    async def probe(request: Request, principal: deps.PrincipalDep, ctx: deps.ContextDep) -> Response:
        seen.update(request_id=principal.request_id, ctx_id=ctx.request_id, version=ctx.version, ip=ctx.client_ip)
        return deps.json_response({})

    app, services = make_app(routers=[router], validate=False)
    async with asgi_client(app, client=("198.51.100.4", 1234)) as client:
        await client.get("/v1/turns/turn_1", headers=auth(**{"x-request-id": "abc"}))
    assert seen == {"request_id": "abc", "ctx_id": "abc", "version": "2026-10-01", "ip": "198.51.100.4"}
    assert services.authn.calls[0][1] == "198.51.100.4"


@pytest.mark.parametrize("path", ["/v1/decisions", "/v1/turns/turn_1"])
async def test_an_unsupported_aghoo_version_is_400_after_authentication(path: str) -> None:
    app, _ = make_app()
    method = "POST" if path == "/v1/decisions" else "GET"
    async with asgi_client(app) as client:
        unauthenticated = await client.request(method, path, headers={"aghoo-version": "2027-01-01", **JSON})
        resp = await client.request(
            method, path, headers=auth(**{"aghoo-version": "2027-01-01", **JSON}), content=b"{not json"
        )
    assert unauthenticated.status_code == 401
    assert resp.status_code == 400  # before the gate: the invalid body is not reached
    error = resp.json()["error"]
    assert (error["code"], error["param"], error["message"]) == (
        "schema_violation",
        "Aghoo-Version",
        "Unsupported Aghoo-Version.",
    )
    assert "2027" not in resp.text


async def test_x_aghoo_end_user_is_422_feature_not_available_before_the_gate() -> None:
    app, _ = make_app()
    async with asgi_client(app) as client:
        resp = await client.post("/v1/turns", headers=auth(**{"x-aghoo-end-user": "eyJ...", **JSON}), content=b"[]")
        versioned = await client.post(
            "/v1/turns", headers=auth(**{"x-aghoo-end-user": "x", "aghoo-version": "bad", **JSON}), content=b"[]"
        )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert (error["code"], error["param"], error["type"]) == (
        "feature_not_available",
        "x-aghoo-end-user",
        "capability_error",
    )
    assert versioned.status_code == 400  # Aghoo-Version is checked before x-aghoo-end-user
