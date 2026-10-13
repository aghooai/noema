"""Wave E edge fixes in the ingress: the raw JSON body is not kept once the gate parsed it (fix 4), a body over
`OFFLOAD_BYTES` is checked and fingerprinted off the event loop (fix 3), and an admission 429 carries the six
x-ratelimit-* headers (fix 17, spec-08 §4)."""

from __future__ import annotations

import threading
from typing import Any

import orjson
import pytest
from fastapi import APIRouter, Request
from starlette.responses import Response
from tests.unit.api.apifakes import DEV_KEY, asgi_client, auth, make_app, principal, stub_routers

from noema.api import deps
from noema.api.middleware import gate as gate_step
from noema.api.middleware import ingress
from noema.api.middleware.context import CTX_SCOPE_KEY
from noema.api.middleware.idempotency import principal_fingerprint, request_fingerprint
from noema.core.errors import ErrorCode
from noema.ops.admission import AdmissionRejected

pytestmark = pytest.mark.unit
JSON = {"content-type": "application/json"}
RATE = {
    "x-ratelimit-limit-requests": "4",
    "x-ratelimit-remaining-requests": "0",
    "x-ratelimit-reset-requests": "15",
    "x-ratelimit-limit-tokens": "1000",
    "x-ratelimit-remaining-tokens": "900",
    "x-ratelimit-reset-tokens": "1",
}


def _sessions_router(seen: dict[str, Any]) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/sessions", name="createSession")
    async def create(request: Request) -> Response:
        seen["raw_in_ctx"] = getattr(request.scope[CTX_SCOPE_KEY], "body", b"")
        seen["replayed"] = await request.body()
        seen["json"] = deps.get_context(request).json
        await deps.begin_idempotency(request, resource_id="ses_1")
        return Response(status_code=204)

    return router


async def test_the_raw_body_is_not_kept_after_the_gate() -> None:
    seen: dict[str, Any] = {}
    app, _ = make_app(routers=[_sessions_router(seen), *stub_routers()], validate=False)
    body = {"metadata": {"pad": "x" * 4096}}
    async with asgi_client(app, check_contract=False) as client:
        resp = await client.post("/v1/sessions", content=orjson.dumps(body), headers=auth(**JSON))
    assert resp.status_code == 204
    assert seen["json"] == body  # what routes read
    assert seen["raw_in_ctx"] == b"" and seen["replayed"] == b""  # no second copy of the bytes for the request


async def test_a_large_body_is_checked_and_fingerprinted_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[tuple[str, bool]] = []
    check, fingerprint = gate_step.check_body, request_fingerprint

    def checked(*args: Any) -> Any:
        threads.append(("check", threading.current_thread() is threading.main_thread()))
        return check(*args)

    def fingerprinted(*args: Any) -> str:
        threads.append(("fingerprint", threading.current_thread() is threading.main_thread()))
        return fingerprint(*args)

    monkeypatch.setattr(gate_step, "check_body", checked)
    monkeypatch.setattr(ingress, "request_fingerprint", fingerprinted, raising=False)
    seen: dict[str, Any] = {}
    app, services = make_app(routers=[_sessions_router(seen), *stub_routers()], validate=False)
    big = orjson.dumps({"metadata": {"pad": "x" * (2 << 20)}})  # over OFFLOAD_BYTES (1 MiB)
    keyed = auth(**JSON, **{"idempotency-key": "k-big"})
    async with asgi_client(app, check_contract=False) as client:
        small = await client.post("/v1/sessions", content=b"{}", headers=auth(**JSON))
        large = await client.post("/v1/sessions", content=big, headers=keyed)
    assert (small.status_code, large.status_code) == (204, 204)
    assert threads == [("check", True), ("check", False), ("fingerprint", False)]
    (begin,) = services.idempotency.begins
    canonical = fingerprint(orjson.loads(big), "2026-10-01")  # the canonical fingerprint, bound to the caller
    assert begin["fp"] == principal_fingerprint(canonical, principal(DEV_KEY).principal_id)


async def test_an_admission_429_carries_the_rate_limit_headers() -> None:
    router = APIRouter()

    @router.get("/v1/turns/{turn_id}", name="getTurn")
    async def rejected(request: Request) -> Response:
        raise AdmissionRejected(ErrorCode.RPM_EXCEEDED, retry_after_s=15, headers=RATE)

    app, _ = make_app(routers=[router, *stub_routers()])  # the response validator is on
    async with asgi_client(app) as client:
        resp = await client.get("/v1/turns/turn_1", headers=auth())
    assert resp.status_code == 429 and resp.headers["retry-after"] == "15"
    assert {k: resp.headers.get(k) for k in RATE} == RATE


async def test_the_json_access_line_carries_the_request_fields(caplog: pytest.LogCaptureFixture) -> None:
    """Wave E edge fix 30 (spec-09 §2): the JSON log line of a request carries route, status, latency, request id,
    tenant and key id. The JSON formatter keeps only its 08 §7 keys, so they are in the message and `tenant`."""
    import logging

    from noema.telemetry.logs import JsonFormatter

    app, _ = make_app()
    with caplog.at_level(logging.INFO, logger="noema.api.access"):
        async with asgi_client(app) as client:
            await client.get("/v1/turns/turn_1", headers=auth(**{"x-request-id": "req-access-1"}))
    (record,) = [r for r in caplog.records if r.name == "noema.api.access"]
    line = orjson.loads(JsonFormatter().format(record))
    assert line["tenant"] == "t_acme" and line["event"] == "http.access"
    message = line["message"]
    assert message.startswith("GET /v1/turns/{turn_id} 501 ")
    assert "request_id=req-access-1" in message and f"key_id={principal(DEV_KEY).key_id}" in message
    assert "ms" in message.split()[3]
