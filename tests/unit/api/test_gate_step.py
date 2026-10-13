"""Step 5: the request gate (IMPL-041, IMPL-064, IMPL-037): parameters, schema, semantic rules, inline images."""

from __future__ import annotations

import copy
from typing import Any

import orjson
import pytest
from fastapi import APIRouter, Request
from starlette.responses import Response
from tests.unit.api.apifakes import TURN_BODY, asgi_client, auth, make_app

from noema.api import deps
from noema.api.middleware.gate import INLINE_IMAGE_MAX_BYTES, decoded_size
from noema.contracts.bundle import VENDOR_DIR

pytestmark = pytest.mark.unit
JSON = {"content-type": "application/json"}
EXAMPLES = VENDOR_DIR / "examples"
DECISION = orjson.loads((EXAMPLES / "decision-request.valid.json").read_bytes())


async def _post(path: str, body: object, **headers: str) -> Any:
    app, _ = make_app()
    async with asgi_client(app) as client:
        resp = await client.post(path, content=orjson.dumps(body), headers=auth(**JSON, **headers))
    return resp.status_code, resp.json()["error"]


@pytest.mark.parametrize(
    ("url", "status", "code", "param"),
    [
        ("/v1/sessions?foo=1", 400, "schema_violation", "foo"),
        ("/v1/sessions?limit=0", 400, "schema_violation", "limit"),
        ("/v1/sessions?limit=ten", 400, "schema_violation", "limit"),
        ("/v1/sessions?limit=1&limit=2", 400, "schema_violation", "limit"),
        ("/v1/turns/turn_1?verbose=1", 400, "schema_violation", "verbose"),
        ("/v1/decisions/not-a-decision", 404, "not_found", None),
        ("/v1/sessions/not-a-session", 404, "session_not_found", None),
        ("/v1/files/not-a-file", 404, "not_found", None),
        ("/v1/sessions/not-a-session/turns", 404, "session_not_found", None),  # malformed id = unknown id
        ("/v1/files/file_1/derivatives/raster_1568", 400, "schema_violation", "kind"),  # IMPL-065
        ("/v1/usage?from=2026-13-01", 400, "schema_violation", "from"),
    ],
)
async def test_parameter_failures(url: str, status: int, code: str, param: str | None) -> None:
    app, _ = make_app()
    async with asgi_client(app) as client:
        resp = await client.get(url, headers=auth())
    assert resp.status_code == status
    error = resp.json()["error"]
    assert (error["code"], error["param"]) == (code, param)


async def test_coerced_parameters_defaults_and_the_deadline_reach_the_route() -> None:
    seen: dict[str, Any] = {}
    router = APIRouter()

    @router.get("/v1/sessions", name="listSessions")
    async def sessions(request: Request, ctx: deps.ContextDep) -> Response:
        seen["sessions"] = (dict(ctx.params), ctx.deadline, ctx.request_id)
        return deps.json_response({})

    @router.post("/v1/turns", name="createTurn")
    async def turns(request: Request, ctx: deps.ContextDep) -> Response:
        assert ctx.deadline is not None
        seen["turns"] = (ctx.params["Aghoo-Deadline-Ms"], ctx.deadline.at - ctx.t_in, ctx.json)
        return deps.json_response({})

    app, _ = make_app(routers=[router], validate=False)
    async with asgi_client(app) as client:
        await client.get("/v1/sessions?limit=10", headers=auth())
        await client.post("/v1/turns", content=orjson.dumps(TURN_BODY), headers=auth(**JSON))
        default_deadline = seen["turns"]
        await client.post(
            "/v1/turns", content=orjson.dumps(TURN_BODY), headers=auth(**JSON, **{"aghoo-deadline-ms": "2500"})
        )
    params, deadline, request_id = seen["sessions"]
    assert params == {"limit": 10, "Aghoo-Version": "2026-10-01", "x-request-id": request_id}
    assert deadline is None
    assert default_deadline == (120_000, pytest.approx(120.0), TURN_BODY)
    assert seen["turns"][:2] == (2500, pytest.approx(2.5))


@pytest.mark.parametrize("value", ["99", "600001", "1.5", "soon"])
async def test_an_invalid_deadline_is_400(value: str) -> None:
    status, error = await _post("/v1/turns", TURN_BODY, **{"aghoo-deadline-ms": value})
    assert (status, error["code"], error["param"]) == (400, "schema_violation", "Aghoo-Deadline-Ms")


async def test_body_schema_and_json_failures() -> None:
    status, error = await _post("/v1/turns", {"input": [{"role": "assistant", "content": []}]})
    assert (status, error["code"]) == (400, "schema_violation")
    assert error["param"].startswith("input[0]")
    app, _ = make_app()
    async with asgi_client(app) as client:
        resp = await client.post("/v1/turns", content=b"{nope", headers=auth(**JSON))
        empty = await client.post("/v1/turns", content=b"", headers=auth(**JSON))
    assert (resp.status_code, resp.json()["error"]["param"]) == (400, None)
    assert empty.status_code == 400


async def test_semantic_rules_run_after_the_schema() -> None:
    dup = orjson.loads((EXAMPLES / "decision-request.invalid-duplicate-choices.json").read_bytes())
    status, error = await _post("/v1/decisions", dup)
    assert (status, error["code"], error["param"]) == (400, "schema_violation", "questions[0].choices")
    turn = {**TURN_BODY, "decisions": {"questions": [DECISION["questions"][0], DECISION["questions"][0]]}}
    status, error = await _post("/v1/turns", turn)
    assert (status, error["param"]) == (400, "decisions.questions")
    images = orjson.loads((EXAMPLES / "openai-request.invalid-21-images.json").read_bytes())
    status, error = await _post("/openai/v1/decisions", images)
    assert (status, error["param"]) == (400, "input")


def test_decoded_size_is_arithmetic() -> None:
    assert decoded_size("data:image/png;base64,AAAA") == 3
    assert decoded_size("data:image/png;base64,AAA=") == 2
    assert decoded_size("data:image/png;base64,AA==") == 1
    assert decoded_size("data:image/png;base64,") == 0


def _with_image(body: dict[str, Any], b64_len: int, *, facade: bool = False) -> dict[str, Any]:
    out = copy.deepcopy(body)
    image = {"type": "input_image", "image_url": "data:image/png;base64," + "A" * b64_len}
    text = {"type": "input_text", "text": "look"}
    out["input"] = [{"role": "user", **({"type": "message"} if facade else {}), "content": [text, image]}]
    return out


@pytest.mark.parametrize(
    ("path", "body", "facade"),
    [("/v1/turns", TURN_BODY, False), ("/v1/decisions", DECISION, False), ("/openai/v1/decisions", None, True)],
)
async def test_inline_images_over_3_75_mb_are_413_image_too_large(
    path: str, body: dict[str, Any] | None, facade: bool
) -> None:
    base = body or orjson.loads((EXAMPLES / "openai-request.valid.json").read_bytes())
    at_limit = INLINE_IMAGE_MAX_BYTES * 4 // 3
    status, error = await _post(path, _with_image(base, at_limit + 4, facade=facade))
    assert (status, error["code"], error["param"]) == (413, "image_too_large", "input[0].content[1].image_url")
    app, _ = make_app()
    async with asgi_client(app) as client:
        ok = await client.post(
            path, content=orjson.dumps(_with_image(base, at_limit, facade=facade)), headers=auth(**JSON)
        )
    assert ok.status_code == 501  # exactly 3,750,000 decoded bytes passes the gate


@pytest.mark.parametrize(
    ("url", "status", "code", "param"),
    [
        ("/v1/sessions/ses_%00", 404, "session_not_found", None),
        ("/v1/sessions/ses_a%0Ab", 404, "session_not_found", None),
        ("/v1/files/file_%00/events", 404, "not_found", None),
        ("/v1/files/file_%C2%85", 404, "not_found", None),  # U+0085, a C1 control character
        ("/v1/sessions?after=a%00b", 400, "schema_violation", "after"),
    ],
)
async def test_control_characters_in_ids_and_query_strings_never_reach_the_route(
    url: str, status: int, code: str, param: str | None
) -> None:
    """Wave E2 (finding 17, ruling R7): Postgres text cannot hold U+0000 (the lookup answered 500). A path id with
    a control character is that operation's 404 like any malformed id, a query string parameter with one is 400
    `schema_violation`; neither reaches the route."""
    reached: list[str] = []
    router = APIRouter()

    @router.get("/v1/sessions/{session_id}", name="getSession")
    @router.get("/v1/files/{file_id}", name="getFile")
    @router.get("/v1/files/{file_id}/events", name="watchFile")
    @router.get("/v1/sessions", name="listSessions")
    async def probe(request: Request) -> Response:
        reached.append(request.url.path)
        return Response(status_code=204)

    app, _ = make_app(routers=[router])
    async with asgi_client(app) as client:
        resp = await client.get(url, headers=auth())
    assert resp.status_code == status, resp.text
    error = resp.json()["error"]
    assert (error["code"], error["param"]) == (code, param)
    assert reached == []


@pytest.mark.parametrize(
    ("body", "param"),
    [({"metadata": {"k": "v\u0000"}}, "metadata.k"), ({"metadata": {"k\u0000": "v"}}, "metadata")],
)
@pytest.mark.parametrize("method", ["POST", "PATCH"])
async def test_session_metadata_holding_nul_is_400(method: str, body: dict[str, Any], param: str) -> None:
    """Wave E2 (finding 17): Postgres text cannot hold U+0000, so session metadata holding it is a 400
    `schema_violation` at the gate (createSession and updateSession answered 500)."""
    app, _ = make_app()
    path = "/v1/sessions" if method == "POST" else "/v1/sessions/ses_1"
    async with asgi_client(app) as client:
        resp = await client.request(method, path, content=orjson.dumps(body), headers=auth(**JSON))
    assert resp.status_code == 400, resp.text
    assert (resp.json()["error"]["code"], resp.json()["error"]["param"]) == ("schema_violation", param)
