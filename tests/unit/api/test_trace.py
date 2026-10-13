"""Step 2: W3C trace context (IMPL-033): a valid traceparent is joined, anything else starts a new trace."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Request
from opentelemetry import trace as otel_trace
from starlette.responses import Response
from tests.unit.api.apifakes import asgi_client, auth, make_app

from noema.api import deps
from noema.api.middleware.trace import attached, parse_trace

pytestmark = pytest.mark.unit
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
VALID = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


def test_a_valid_traceparent_is_joined_with_its_tracestate() -> None:
    info, ctx = parse_trace({"traceparent": VALID, "tracestate": "vendor=abc,other=1"})
    assert info.joined
    assert (info.trace_id, info.parent_span_id, info.sampled) == (TRACE_ID, "00f067aa0ba902b7", True)
    assert info.tracestate == "vendor=abc,other=1"
    assert ctx is not None
    with attached(ctx):
        span_ctx = otel_trace.get_current_span().get_span_context()
        assert span_ctx.trace_id == int(TRACE_ID, 16)
        assert span_ctx.is_remote
    assert not otel_trace.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize(
    "value",
    [
        "",
        "garbage",
        f"ff-{TRACE_ID}-00f067aa0ba902b7-01",  # version ff is invalid
        "00-00000000000000000000000000000000-00f067aa0ba902b7-01",  # all-zero trace id
        f"00-{TRACE_ID}-0000000000000000-01",  # all-zero span id
        f"00-{TRACE_ID.upper()}-00f067aa0ba902b7-01",  # upper-case hex
        f"00-{TRACE_ID}-00f067aa0ba902b7-01-extra",  # version 00 has exactly four fields
    ],
)
def test_an_invalid_traceparent_starts_a_new_trace(value: str) -> None:
    info, ctx = parse_trace({"traceparent": value, "tracestate": "vendor=abc"})
    assert not info.joined
    assert ctx is None
    assert info.trace_id is None
    assert info.tracestate is None


def test_an_oversized_tracestate_is_dropped() -> None:
    info, _ = parse_trace({"traceparent": VALID, "tracestate": "a=" + "b" * 600})
    assert info.joined
    assert info.tracestate is None


async def test_the_route_runs_inside_the_joined_trace() -> None:
    seen: dict[str, object] = {}
    router = APIRouter()

    @router.get("/v1/turns/{turn_id}", name="getTurn")
    async def probe(request: Request) -> Response:
        seen["joined"] = deps.get_context(request).trace.joined
        seen["otel_trace_id"] = otel_trace.get_current_span().get_span_context().trace_id
        return deps.json_response({"ok": True})

    app, _ = make_app(routers=[router], validate=False)
    async with asgi_client(app) as client:
        resp = await client.get("/v1/turns/turn_1", headers=auth(traceparent=VALID))
        assert resp.status_code == 200
        assert seen == {"joined": True, "otel_trace_id": int(TRACE_ID, 16)}
        resp = await client.get("/v1/turns/turn_1", headers=auth(traceparent="00-bad"))
        assert resp.status_code == 200
        assert seen == {"joined": False, "otel_trace_id": 0}
