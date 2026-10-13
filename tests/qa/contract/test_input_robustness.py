"""Deterministic reproducers of the schemathesis findings, plus per-operation header rules (gate Q1, 06 §7 "no
500s"). Every response goes through the e2e `CheckedClient` (ContractValidator: declared status, schema, error
envelope agreeing with ERROR_TABLE and x-noema-error-codes).

- A U+0000 in an id or a stored string is input Postgres cannot hold. It must be answered like any other id that
  cannot exist (404 not_found / session_not_found) or a schema error (400), never 500 internal_error.
- `x-aghoo-end-user` on a project without end-user verification: 422 feature_not_available, `param` the header
  (the EndUser parameter of the bundle).
- No credentials: 401 invalid_api_key with `WWW-Authenticate` on every authenticated operation (with a body of the
  declared media type: the 413/415 pre-checks come first).
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from tests.e2e.conftest import Stack
from tests.qa.contract.fuzzkit import OPS
from tests.unit.media_pipeline.media_fixtures import png

pytestmark = [pytest.mark.e2e]
NUL = "\u0000"
Q = [{"type": "predicate", "name": "p", "instructions": "Is it?"}]


def _msg(*parts: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"role": "user", "content": list(parts)}]


TEXT = {"type": "input_text", "text": "hi"}
LIGHT = {"profile": "lightweight-model-decide", "escalation": "off", "questions": Q}
NUL_CASES: list[tuple[str, str, dict[str, Any] | None]] = [  # ids that cannot exist; strings Postgres cannot hold
    ("POST", "/v1/turns", {"stream": False, "input": _msg({"type": "input_file", "file_id": f"file_{NUL}"})}),
    ("POST", "/v1/turns", {"stream": False, "session_id": f"ses_{NUL}", "input": _msg(TEXT)}),
    ("POST", "/v1/turns", {"stream": False, "retry_of": f"turn_{NUL}", "input": _msg(TEXT)}),
    ("POST", "/v1/decisions", {**LIGHT, "input": _msg({"type": "input_file", "file_id": f"file_{NUL}"})}),
    ("POST", "/v1/decisions", {**LIGHT, "session_id": f"ses_{NUL}", "input": "x"}),
    ("POST", "/v1/sessions", {"metadata": {"k": f"v{NUL}"}}),
    ("POST", "/v1/sessions", {"metadata": {f"k{NUL}": "v"}}),
    ("GET", "/v1/files/file_%00", None),
    ("GET", "/v1/files/file_%00/events", None),
    ("GET", "/v1/files/file_%00/derivatives/raster", None),
    ("DELETE", "/v1/files/file_%00", None),
    ("GET", "/v1/sessions/ses_%00", None),
    ("PATCH", "/v1/sessions/ses_%00", {"metadata": {}}),
    ("DELETE", "/v1/sessions/ses_%00", None),
    ("GET", "/v1/sessions/ses_%00/turns", None),
    ("GET", "/v1/decisions/dec_%00", None),
    ("GET", "/v1/turns/turn_%00", None),
    ("POST", "/v1/turns/turn_%00/cancel", None),
]


@pytest.mark.parametrize(
    ("method", "path", "body"), NUL_CASES, ids=[f"{m} {p} {i}" for i, (m, p, _) in enumerate(NUL_CASES)]
)
async def test_nul_in_ids_and_strings_is_a_client_error(
    stack: Stack, method: str, path: str, body: dict[str, Any] | None
) -> None:
    client = stack.client(0)
    response = await client.request(method, path, json=body)
    assert response.status_code in (400, 404), (response.status_code, response.text[:300])


AUTHENTICATED = sorted(op for op, raw in OPS.items() if raw.authenticated)


def _concrete(path: str) -> str:
    ids = {"{turn_id}": "turn_x", "{decision_id}": "dec_x", "{session_id}": "ses_x", "{file_id}": "file_x"}
    for name, value in {**ids, "{kind}": "raster"}.items():
        path = path.replace(name, value)
    return path


def _body(operation_id: str) -> dict[str, Any]:
    """A body of the declared media type (the 413/415 pre-checks run before authentication, spec-04 §1)."""
    media = OPS[operation_id].body_ptrs
    if "multipart/form-data" in media:
        return {"files": {"file": ("qa.png", png(8, 8), "image/png")}, "data": {"purpose": "input"}}
    return {"json": {}} if "application/json" in media else {}


@pytest.mark.parametrize("operation_id", AUTHENTICATED)
async def test_end_user_header_is_phase_2(stack: Stack, operation_id: str) -> None:
    op = OPS[operation_id]
    headers = {"x-aghoo-end-user": "eyJhbGciOiJub25lIn0.e30."}
    response = await stack.client(0).request(op.method, _concrete(op.path), headers=headers, **_body(operation_id))
    error = response.json()["error"]
    assert (response.status_code, error["code"], error["param"]) == (422, "feature_not_available", "x-aghoo-end-user")


@pytest.mark.parametrize("operation_id", AUTHENTICATED)
async def test_no_credentials_is_401_with_www_authenticate(stack: Stack, operation_id: str) -> None:
    op = OPS[operation_id]
    response = await stack.client(0, role=None).request(op.method, _concrete(op.path), **_body(operation_id))
    assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_api_key"
    assert response.headers.get("www-authenticate", "").lower().startswith("bearer")
    await anyio.sleep(0.15)  # the per-IP auth-failure bucket refills at 10/s


TEXT_NUL = {"type": "input_text", "text": f"hi {NUL} there"}
STORED_CASES: list[tuple[str, dict[str, Any], str]] = [  # (create path, body, the read path of its id)
    ("/v1/turns", {"stream": False, "input": _msg(TEXT_NUL)}, "/v1/turns/{turn_id}"),
    ("/v1/turns", {"stream": False, "input": _msg({"type": "input_json", "json": {"a": NUL}})}, "/v1/turns/{turn_id}"),
    ("/v1/decisions", {**LIGHT, "input": f"a {NUL} b"}, "/v1/decisions/{id}"),
]


@pytest.mark.parametrize(("path", "body", "read"), STORED_CASES, ids=["turn text", "turn input_json", "decision input"])
async def test_nul_in_stored_text_is_rejected_or_stored(
    stack: Stack, default_scenario: None, path: str, body: dict[str, Any], read: str
) -> None:
    """A create that answers 2xx returns an id that can be read back (spec-04 §8: a stored turn or decision row);
    the alternative is a 400. Answering 200 and then losing the row (its outbox entry can never be applied) is
    the failure this guards against."""
    created = await stack.client(0).request("POST", path, json=body)
    if created.status_code == 400:
        return
    assert created.status_code == 200, created.text[:300]
    key = read.split("{")[1].rstrip("}")
    url = read.replace("{" + key + "}", str(created.json()[key]))
    reader = stack.client(1)
    for _ in range(20):  # 10 s: the outbox applies a queued row within its drain interval
        got = await reader.request("GET", url)
        if got.status_code != 404:
            break
        await anyio.sleep(0.5)
    assert got.status_code == 200, f"{path} answered 200 but {url} is {got.status_code} 10 s later"
