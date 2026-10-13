"""Authorization spot checks on the stack (spec-09 §3; the full matrix is Wave E): roles within one project,
another project of the same tenant and another tenant, against resources a developer created (a session with one
turn, a decision, a file). Each row is one request; every response is contract-validated."""

from __future__ import annotations

from typing import Any, Final

import pytest
from tests.e2e.conftest import CheckedClient, Stack
from tests.e2e.suite.kit import Json, eventually, tag, turn_body
from tests.e2e.suite.turns import new_session, run_turn
from tests.unit.media_pipeline import media_fixtures as fx

pytestmark = [pytest.mark.e2e]

ONE_QUESTION: Final[Json] = {
    "profile": "router-default",
    "escalation": "off",
    "questions": [{"type": "predicate", "name": "callback", "instructions": "The user asks for a call back."}],
}


async def created_by_developer(stack: Stack) -> dict[str, str]:
    dev = stack.client(0)
    session = await new_session(dev)
    turn = await run_turn(dev, f"What is the capital of France? ({tag()})", session_id=session)
    decision = await dev.request(
        "POST", "/v1/decisions", json={**ONE_QUESTION, "input": f"Call me ({tag()})"}, expect=200
    )
    files = {"file": ("bar.png", fx.png(32, 32), "image/png")}
    file = (await dev.request("POST", "/v1/files", data={"purpose": "input"}, files=files, expect=202)).json()
    await eventually(dev, f"/v1/files/{file['id']}", lambda b: b["status"] in ("ready", "partial"), timeout_s=60)
    return {"session": session, "turn": turn.turn_id, "decision": str(decision.json()["id"]), "file": str(file["id"])}


async def test_authz_spot_checks(stack: Stack) -> None:
    ids = await created_by_developer(stack)
    s, t, d, f = ids["session"], ids["turn"], ids["decision"], ids["file"]

    def as_(role: str | None, tenant: str = "t_acme", project: str = "main", replica: int = 1) -> CheckedClient:
        return stack.client(replica, tenant=tenant, project=project, role=role)

    decide: Any = {**ONE_QUESTION, "input": f"Call me back ({tag()})"}
    rows: list[tuple[str, CheckedClient, str, str, dict[str, Any], int, str | None]] = [
        # same project, other roles
        ("developer2 reads the decision", as_("developer2"), "GET", f"/v1/decisions/{d}", {}, 200, None),
        ("developer2 reads the session", as_("developer2"), "GET", f"/v1/sessions/{s}", {}, 404, "session_not_found"),
        ("developer2 reads the turn", as_("developer2"), "GET", f"/v1/turns/{t}", {}, 404, "not_found"),
        (
            "developer2 turns in it",
            as_("developer2"),
            "POST",
            "/v1/turns",
            {"json": turn_body("x", session_id=s)},
            404,
            "session_not_found",
        ),
        ("developer2 cancels the turn", as_("developer2"), "POST", f"/v1/turns/{t}/cancel", {}, 404, "not_found"),
        ("tenant_admin reads the session", as_("tenant_admin"), "GET", f"/v1/sessions/{s}", {}, 200, None),
        (
            "tenant_admin updates it",
            as_("tenant_admin"),
            "PATCH",
            f"/v1/sessions/{s}",
            {"json": {"metadata": {"k": "v"}}},
            200,
            None,
        ),
        ("auditor reads the turn", as_("auditor"), "GET", f"/v1/turns/{t}", {}, 200, None),
        ("auditor reads usage", as_("auditor"), "GET", "/v1/usage", {}, 200, None),
        (
            "auditor creates a turn",
            as_("auditor"),
            "POST",
            "/v1/turns",
            {"json": turn_body("x")},
            403,
            "permission_denied",
        ),
        (
            "auditor downloads a derivative",
            as_("auditor"),
            "GET",
            f"/v1/files/{f}/derivatives/raster",
            {},
            404,
            "not_found",
        ),
        ("reviewer reads the decision", as_("reviewer"), "GET", f"/v1/decisions/{d}", {}, 200, None),
        ("reviewer decides", as_("reviewer"), "POST", "/v1/decisions", {"json": decide}, 403, "permission_denied"),
        ("reviewer lists calibrators", as_("reviewer"), "GET", "/v1/admin/calibrators", {}, 200, None),
        ("reviewer reads usage", as_("reviewer"), "GET", "/v1/usage", {}, 403, "permission_denied"),
        ("end_user reads the decision", as_("end_user"), "GET", f"/v1/decisions/{d}", {}, 404, "not_found"),
        ("end_user reads usage", as_("end_user"), "GET", "/v1/usage", {}, 403, "permission_denied"),
        (
            "developer lists labeling runs",
            as_("developer"),
            "GET",
            "/v1/admin/labeling/runs",
            {},
            403,
            "permission_denied",
        ),
        ("tenant_admin lists labeling runs", as_("tenant_admin"), "GET", "/v1/admin/labeling/runs", {}, 200, None),
        # another project of the tenant
        (
            "second-project developer reads the decision",
            as_("developer", project="second"),
            "GET",
            f"/v1/decisions/{d}",
            {},
            404,
            "not_found",
        ),
        (
            "second-project developer reads the file",
            as_("developer", project="second"),
            "GET",
            f"/v1/files/{f}",
            {},
            404,
            "not_found",
        ),
        # another tenant (RLS)
        (
            "t_beta developer reads the decision",
            as_("developer", tenant="t_beta"),
            "GET",
            f"/v1/decisions/{d}",
            {},
            404,
            "not_found",
        ),
        (
            "t_beta tenant_admin reads the session",
            as_("tenant_admin", tenant="t_beta"),
            "GET",
            f"/v1/sessions/{s}",
            {},
            404,
            "session_not_found",
        ),
        (
            "t_beta tenant_admin reads the turn",
            as_("tenant_admin", tenant="t_beta"),
            "GET",
            f"/v1/turns/{t}",
            {},
            404,
            "not_found",
        ),
        (
            "t_beta tenant_admin cancels the turn",
            as_("tenant_admin", tenant="t_beta"),
            "POST",
            f"/v1/turns/{t}/cancel",
            {},
            404,
            "not_found",
        ),
        (
            "t_beta tenant_admin deletes the file",
            as_("tenant_admin", tenant="t_beta"),
            "DELETE",
            f"/v1/files/{f}",
            {},
            404,
            "not_found",
        ),
        (
            "t_beta auditor reads the derivative",
            as_("auditor", tenant="t_beta"),
            "GET",
            f"/v1/files/{f}/derivatives/raster",
            {},
            404,
            "not_found",
        ),
        # no credentials
        ("anonymous reads the decision", as_(None), "GET", f"/v1/decisions/{d}", {}, 401, "invalid_api_key"),
    ]
    failures: list[str] = []
    for name, client, method, path, kwargs, status, code in rows:
        response = await client.request(method, path, **kwargs)
        body: Json = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        got = body.get("error", {}).get("code") if status >= 400 else None
        if (response.status_code, got) != (status, code):
            failures.append(f"{name}: {response.status_code} {got} (expected {status} {code})")
    assert failures == [], failures
    owner = (await stack.client(0).request("GET", f"/v1/sessions/{s}", expect=200)).json()
    assert owner["metadata"] == {"k": "v"}  # the tenant_admin's update
