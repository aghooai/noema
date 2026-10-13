"""SEC-AZ-01 on the stack (gate SE1, 06 §9 "AuthZ"): every authenticated OpenAPI operation x the five roles x
{own, peer, other project, other tenant} against the independent oracle (tests/security/authz_oracle.py); exact
status and error code per cell, zero leaks (no 2xx and no non-empty list where the oracle says 404, 403 or an
empty page). List reads are checked by content (which known sessions appear; which projects usage reports), and
every authenticated operation without a key answers 401 `invalid_api_key`. Every response is contract-validated."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from tests.e2e.conftest import CheckedClient, Stack
from tests.e2e.suite.kit import Json, eventually
from tests.security.artifacts import write_jsonl
from tests.security.authz_oracle import RELATIONS, ROLES, RULES, Expected, Relation, Role, expect, visible
from tests.security.stack.cells import DESTRUCTIVE, Cell, generate, path_of, request_kwargs
from tests.security.stack.world import World, new_file, new_session
from tests.unit.media_pipeline import media_fixtures as fx

from noema.core.values import JsonValue

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(900)]


def outcome(response: Any) -> tuple[int, str | None, Json | None]:
    body: Json | None = None
    if response.headers.get("content-type", "").startswith("application/json"):
        body = response.json()
    code = body.get("error", {}).get("code") if body is not None and response.status_code >= 400 else None
    return response.status_code, code, body


async def all_sessions(client: CheckedClient) -> tuple[int, set[str]]:
    """listSessions paged to the end: (status of the first page, every id)."""
    ids: set[str] = set()
    params: dict[str, Any] = {"limit": 100}
    while True:
        response = await client.request("GET", "/v1/sessions", params=params)
        if response.status_code != 200:
            return response.status_code, ids
        page = response.json()
        ids |= {str(s["id"]) for s in page["data"]}
        if not page["has_more"]:
            return 200, ids
        params["after"] = page["next_cursor"]


async def run_cell(world: World, cell: Cell) -> tuple[Expected, tuple[int, str | None, Json | None]]:
    expected = expect(cell.operation, cell.role, cell.relation, cell.kind, cell.variant)
    res = None
    if cell.kind is not None:
        res = await world.owned(cell.relation, cell.role)
        if cell.operation in DESTRUCTIVE and expected.allowed:  # a fresh victim, so later cells keep theirs
            owner = world.owner_client(cell.relation, cell.role)
            if cell.operation == "deleteSession":
                res = replace(res, session=await new_session(owner))
            else:
                res = replace(res, file=await new_file(owner, ready=False))
    client = world.caller(cell.role)
    response = await client.request(cell.method, path_of(cell, res), **request_kwargs(cell, res))
    return expected, outcome(response)


def judge(cell: Cell, expected: Expected, got: tuple[int, str | None, Json | None]) -> tuple[str | None, bool]:
    """(mismatch text or None, leak?)."""
    status, code, body = got
    if expected.empty:
        data = body.get("data") if body else None
        if status == 200 and data == []:
            return None, False
        return f"{cell.name}: {status} {code} data={data!r:.80} (expected an empty page)", bool(data)
    if (status, code) == (expected.status, expected.code):
        return None, False
    leak = not expected.allowed and 200 <= status < 300
    return f"{cell.name}: {status} {code} (expected {expected.status} {expected.code})", leak


async def test_authz_matrix(stack: Stack) -> None:
    world = World(stack)
    cells = generate()
    mismatches: list[str] = []
    leaks: list[str] = []
    rows: list[JsonValue] = []
    for cell in cells:
        if cell.operation == "listSessions":
            continue  # by content below
        expected, got = await run_cell(world, cell)
        problem, leak = judge(cell, expected, got)
        rows.append({"cell": cell.name, "expected": [expected.status, expected.code], "got": [got[0], got[1]],
                     "ok": problem is None, "leak": leak})  # fmt: skip
        if problem:
            mismatches.append(problem)
        if leak:
            leaks.append(problem or cell.name)
    path = write_jsonl("authz-matrix.jsonl", rows)
    print(f"SEC-AZ-01: {len(rows)} cells, {len(mismatches)} mismatches, {len(leaks)} leaks ({path})")
    assert (leaks, mismatches) == ([], [])


def relation_of(owner: tuple[str, str, str], caller: Role) -> Relation:
    tenant, project, role = owner
    if tenant != "t_acme":
        return "other_tenant"
    if project != "main":
        return "other_project"
    return "own" if role == caller else "peer"


async def test_list_sessions_shows_exactly_the_visible_sessions(stack: Stack) -> None:
    world = World(stack)
    for role in ROLES:
        for relation in RELATIONS:
            if not (relation == "own" and role in ("reviewer", "auditor")):
                await world.owned(relation, role)
    problems: list[str] = []
    for role in ROLES:
        status, listed = await all_sessions(world.caller(role))
        assert status == 200, (role, status)
        for owner, owned in world.known().items():
            should = role != "reviewer" and visible("session", role, relation_of(owner, role))
            if (owned.session in listed) != should:
                problems.append(f"{role} lists the session of {owner}: {owned.session in listed} != {should}")
    assert problems == [], problems


async def test_usage_is_scoped_to_the_callers_project_or_tenant(stack: Stack) -> None:
    acme = {stack.project_id("t_acme", p) for p in ("main", "second")}
    beta = {stack.project_id("t_beta", p) for p in ("main", "second")}
    scopes = {"developer": {stack.project_id("t_acme", "main")}, "tenant_admin": acme, "auditor": acme}
    for role, allowed in scopes.items():
        client = stack.client(1, role=role)
        page = await client.request("GET", "/v1/usage", params={"group_by": "project"}, expect=200)
        keys = {str(row["key"]) for row in page.json()["data"]}
        assert keys and keys <= allowed and not keys & beta, (role, keys)


async def test_no_key_is_401_on_every_authenticated_operation(stack: Stack) -> None:
    world = World(stack)
    owned = await world.owned("own", "developer")
    anonymous = stack.client(1, role=None)
    seen: set[str] = set()
    for cell in generate():
        if cell.operation in seen or cell.role != "developer" or cell.relation not in ("own", "none"):
            continue
        seen.add(cell.operation)
        response = await anonymous.request(cell.method, path_of(cell, owned), **request_kwargs(cell, owned))
        assert outcome(response)[:2] == (401, "invalid_api_key"), cell.name
    assert seen == set(RULES)


async def test_a_file_the_tenant_admin_put_in_my_session(stack: Stack) -> None:
    """V(file) = uploader or owner of its session (spec-09 §3): the session owner reads a file the tenant_admin
    uploaded into it, a peer does not, and deleteFile by the visible non-uploader is 403 `permission_denied`."""
    world = World(stack)
    dev, admin = world.caller("developer"), world.caller("tenant_admin")
    session = await new_session(dev)
    files = {"file": ("admin.png", fx.png(20, 20), "image/png")}
    form = {"purpose": "input", "session_id": session}
    file_id = str((await admin.request("POST", "/v1/files", data=form, files=files, expect=202)).json()["id"])
    await eventually(admin, f"/v1/files/{file_id}", lambda b: b["status"] == "ready", timeout_s=90)
    rows = [
        (dev, "GET", f"/v1/files/{file_id}", 200, None),
        (dev, "GET", f"/v1/files/{file_id}/derivatives/raster", 200, None),
        (stack.client(1, role="developer2"), "GET", f"/v1/files/{file_id}", 404, "not_found"),
        (world.caller("end_user"), "GET", f"/v1/files/{file_id}", 404, "not_found"),
        (dev, "DELETE", f"/v1/files/{file_id}", 403, "permission_denied"),
        (admin, "DELETE", f"/v1/files/{file_id}", 200, None),
    ]
    got = [outcome(await client.request(method, path))[:2] for client, method, path, _, _ in rows]
    assert got == [(status, code) for *_, status, code in rows]
