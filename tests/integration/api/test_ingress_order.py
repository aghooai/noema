"""T-ING-1 (spec-04 §13): each ingress step's error appears only when every earlier step passed (one request per
step), and the idempotency record is completed with None on every exit before a model call."""

from __future__ import annotations

from typing import Any

import orjson
import pytest
from fastapi import APIRouter, Request
from starlette.responses import Response
from tests.unit.api.apifakes import TURN_BODY, FakeServices, asgi_client, auth, make_app, services_with, snapshot_with

from noema.api import deps
from noema.core.errors import ErrorCode, NoemaError

pytestmark = pytest.mark.integration
CAP = 8 * 1_048_576  # above the 5 MB inline image below, below the 50 MiB default
GOOD = orjson.dumps(TURN_BODY)
DUP_QUESTIONS = {
    **TURN_BODY,
    "decisions": {"questions": [{"type": "predicate", "name": "q", "instructions": "?"}] * 2},
}
BIG_IMAGE = {
    "input": [
        {
            "role": "user",
            "content": [{"type": "input_image", "image_url": "data:image/png;base64," + "A" * 5_000_004}],
        }
    ]
}


def _route(services: FakeServices, outcome: dict[str, Any]) -> APIRouter:
    """createTurn as the orchestrator will write it: authorize (6), begin (7), then fail before any model call."""
    router = APIRouter()

    @router.post("/v1/turns", name="createTurn")
    async def create_turn(request: Request) -> Response:
        if outcome.get("authz"):
            raise NoemaError(ErrorCode.PERMISSION_DENIED, "Not allowed.")  # step 6
        replay = await deps.begin_idempotency(request, resource_id="turn_1", stream=True)  # step 7
        if replay is not None:
            return replay
        raise outcome["pre_call"]  # steps 8-11 (plan, credentials, session lock, admission)

    return router


def _req(**overrides: Any) -> dict[str, Any]:
    """A request that passes every step; each override breaks exactly one."""
    headers = auth(**{"content-type": "application/json", "idempotency-key": "k1"})
    req: dict[str, Any] = {"path": "/v1/turns", "body": GOOD, "headers": headers}
    for key, value in overrides.items():
        if key == "drop_header":
            req["headers"] = {k: v for k, v in req["headers"].items() if k != value}
        elif key == "headers":
            req["headers"] = {**req["headers"], **value}
        else:
            req[key] = value
    return req


# Each row breaks one step and also every later step, so only ordering can explain the observed status.
STEPS = [
    ("route", _req(path="/v1/turnz", body=b"x" * (CAP + 1), drop_header="authorization"), 404, "not_found"),
    (
        "body cap",
        _req(body=b"x" * (CAP + 1), drop_header="authorization", headers={"content-type": "text/x"}),
        413,
        "request_too_large",
    ),
    (
        "content type",
        _req(body=b"{", drop_header="authorization", headers={"content-type": "text/x"}),
        415,
        "unsupported_media_type",
    ),
    (
        "authn",
        _req(body=b"{", drop_header="authorization", headers={"aghoo-version": "x", "x-aghoo-end-user": "u"}),
        401,
        "invalid_api_key",
    ),
    ("version", _req(body=b"{", headers={"aghoo-version": "x", "x-aghoo-end-user": "u"}), 400, "schema_violation"),
    (
        "end user",
        _req(body=b"{", headers={"x-aghoo-end-user": "u", "aghoo-deadline-ms": "1"}),
        422,
        "feature_not_available",
    ),
    ("params", _req(body=b"{", headers={"aghoo-deadline-ms": "1"}), 400, "schema_violation"),
    ("schema", _req(body=orjson.dumps({**DUP_QUESTIONS, "extra": 1})), 400, "schema_violation"),
    ("semantic rules", _req(body=orjson.dumps(DUP_QUESTIONS)), 400, "schema_violation"),
    ("inline image", _req(body=orjson.dumps(BIG_IMAGE)), 413, "image_too_large"),
]


@pytest.mark.parametrize(("step", "req", "status", "code"), STEPS, ids=[s[0] for s in STEPS])
async def test_each_step_fails_only_after_the_earlier_ones_passed(
    step: str, req: dict[str, Any], status: int, code: str
) -> None:
    services = services_with(snapshot_with(max_json_body_bytes=CAP))
    app, _ = make_app(services, routers=[_route(services, {})])
    async with asgi_client(app) as client:
        resp = await client.post(req["path"], content=req["body"], headers=req["headers"])
    assert (resp.status_code, resp.json()["error"]["code"]) == (status, code), resp.text
    assert services.idempotency.begins == []  # steps 0-5 never touch the idempotency store
    assert (services.authn.calls != []) is (step not in {"route", "body cap", "content type"})


async def test_authorization_runs_before_the_idempotency_hook() -> None:
    services = FakeServices()
    app, _ = make_app(services, routers=[_route(services, {"authz": True})])
    async with asgi_client(app) as client:
        resp = await client.post("/v1/turns", content=GOOD, headers=_req()["headers"])
    assert resp.status_code == 403
    assert services.idempotency.begins == []


@pytest.mark.parametrize(
    "error",
    [
        NoemaError(ErrorCode.UNSUPPORTED_PARAMETER, "No."),  # step 8 (turn plan)
        NoemaError(ErrorCode.TENANT_ACCESS_MISCONFIGURED, "No."),  # step 9 (credentials)
        NoemaError(ErrorCode.SESSION_BUSY, "Busy."),  # step 10 (session lock)
        NoemaError(ErrorCode.RPM_EXCEEDED, "Slow down.", retry_after_s=3),  # step 11 (admission)
        RuntimeError("bug"),
    ],
    ids=["plan", "credentials", "session", "admission", "bug"],
)
async def test_every_pre_call_exit_completes_the_record_with_none(error: Exception) -> None:
    services = FakeServices()
    app, _ = make_app(services, routers=[_route(services, {"pre_call": error})])
    async with asgi_client(app) as client:
        first = await client.post("/v1/turns", content=GOOD, headers=_req()["headers"])
        retry = await client.post("/v1/turns", content=GOOD, headers=_req()["headers"])
    assert first.status_code == retry.status_code  # not stored: the retry re-executes (IMPL-077)
    assert len(services.idempotency.begins) == 2
    assert [resp for _, resp in services.idempotency.completes] == [None, None]
