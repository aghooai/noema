"""getUsage, healthz and readyz through the real ingress middleware with the response validator on (Q1):
group_by keys, RLS scope per role (developer: its project; tenant_admin/auditor: tenant), 403 for end_user,
400s, keyset pages; readyz 200 / 503 (draining, no config snapshot), without a body."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from tests.fakes.auth import FakeAuthorizer
from tests.fakes.db import FakeDatabase
from tests.unit.api.apifakes import DEV_KEY, FakeServices, asgi_client, auth, make_app, principal

from noema.api.routes.health import router as health_router
from noema.api.routes.usage import router as usage_router
from noema.core.errors import ErrorCode, NoemaError

pytestmark = pytest.mark.unit

ADMIN = "ak_test_01J000000000000000000ADM00." + "a" * 43
AUDITOR = "ak_test_01J000000000000000000AUD00." + "b" * 43
END_USER = "ak_test_01J000000000000000000END00." + "c" * 43
OTHER_DEV = "ak_test_01J000000000000000000DV200." + "e" * 43


@dataclass
class Runtime:
    draining: bool = False


class BrokenConfig:
    def current(self) -> Any:
        raise NoemaError(ErrorCode.INTERNAL_ERROR, "no config yet")


def services() -> FakeServices:
    svc = FakeServices()
    svc.authn.principals.update(
        {
            ADMIN: principal(ADMIN, roles=frozenset({"tenant_admin"})),
            AUDITOR: principal(AUDITOR, roles=frozenset({"auditor"}), project="prj_2"),
            END_USER: principal(END_USER, roles=frozenset({"end_user"})),
            OTHER_DEV: principal(OTHER_DEV, project="prj_2"),
        }
    )
    db = FakeDatabase(svc.clock)
    lines = [
        ("prj_1", "l1", "decision", "deepseek.v3.2", "ses_1", datetime(2026, 10, 9, 23, 59, tzinfo=UTC), "0.000010"),
        ("prj_1", "l2", "decision", "deepseek.v3.2", None, datetime(2026, 10, 10, 0, 1, tzinfo=UTC), "0.000020"),
        ("prj_1", "l3", "generation", "us.openai.gpt-6.1-sol", "ses_1", datetime(2026, 10, 10, tzinfo=UTC), "1.5"),
        ("prj_2", "l4", "cache_savings", None, None, datetime(2026, 10, 10, tzinfo=UTC), "0.5"),
        ("prj_1", "l5", "waste", "deepseek.v3.2", None, datetime(2026, 8, 1, tzinfo=UTC), "9"),  # outside the range
    ]
    for project, lid, line, model, session, at, cost in lines:
        db.store.seed(
            "usage_line",
            {
                "tenant_id": "t_acme", "project_id": project, "id": lid, "line": line, "model_id": model,
                "region": None if model is None else "us-east-1", "session_id": session, "turn_id": None,
                "decision_id": None, "attempt_id": None, "labeling_run_id": None, "input_tokens": 10,
                "output_tokens": 2, "cached_input_tokens": 1, "cache_write_input_tokens": 0, "reasoning_tokens": 0,
                "tokens_estimated": False, "cost_usd": Decimal(cost), "cost_estimated": line == "decision", "at": at,
            },
        )  # fmt: skip
    svc.authz = FakeAuthorizer()  # type: ignore[attr-defined]
    svc.db = db  # type: ignore[attr-defined]
    svc.runtime = Runtime()  # type: ignore[attr-defined]
    return svc


async def get(svc: FakeServices, path: str, key: str = DEV_KEY) -> tuple[int, Any]:
    app, _ = make_app(svc, routers=[usage_router, health_router])
    async with asgi_client(app) as c:
        r = await c.get(path, headers=auth(key))
    return r.status_code, (r.json() if r.content else None)


async def test_default_group_by_line_for_the_developer_project() -> None:
    status, doc = await get(services(), "/v1/usage")
    assert status == 200
    assert (doc["group_by"], doc["from"], doc["to"]) == ("line", "2026-09-11", "2026-10-10")
    assert [(r["key"], r["line"]) for r in doc["data"]] == [("decision", "decision"), ("generation", "generation")]
    decision = doc["data"][0]["usage"]
    assert decision["input_tokens"] == 20 and decision["cost_usd"] == 0.00003 and decision["cost_estimated"] is True
    assert doc["has_more"] is False and doc["next_cursor"] is None


@pytest.mark.parametrize(
    ("group_by", "keys"),
    [
        ("project", [("prj_1", "decision"), ("prj_1", "generation")]),
        ("session", [("ses_1", "decision"), ("ses_1", "generation"), (None, "decision")]),
        ("model", [("deepseek.v3.2", "decision"), ("us.openai.gpt-6.1-sol", "generation")]),
        ("day", [("2026-10-09", "decision"), ("2026-10-10", "decision"), ("2026-10-10", "generation")]),
    ],
)
async def test_every_group_by(group_by: str, keys: list[tuple[str | None, str]]) -> None:
    status, doc = await get(services(), f"/v1/usage?group_by={group_by}")
    assert status == 200 and [(r["key"], r["line"]) for r in doc["data"]] == keys


async def test_tenant_wide_roles_see_every_project() -> None:
    for key in (ADMIN, AUDITOR):
        status, doc = await get(services(), "/v1/usage?group_by=project", key)
        assert status == 200
        assert [(r["key"], r["line"]) for r in doc["data"]] == [
            ("prj_1", "decision"),
            ("prj_1", "generation"),
            ("prj_2", "cache_savings"),
        ]
    status, doc = await get(services(), "/v1/usage?group_by=project", OTHER_DEV)
    assert [(r["key"], r["line"]) for r in doc["data"]] == [("prj_2", "cache_savings")]


async def test_end_user_is_forbidden() -> None:
    status, doc = await get(services(), "/v1/usage", END_USER)
    assert status == 403 and doc["error"]["code"] == "permission_denied"


async def test_range_and_pages() -> None:
    status, doc = await get(services(), "/v1/usage?from=2026-08-01&to=2026-08-01")
    assert status == 200 and [r["line"] for r in doc["data"]] == ["waste"]
    seen: list[tuple[str | None, str]] = []
    after = ""
    while True:
        status, doc = await get(services(), f"/v1/usage?group_by=session&limit=1{after}")
        assert status == 200
        seen += [(r["key"], r["line"]) for r in doc["data"]]
        if not doc["has_more"]:
            break
        after = f"&after={doc['next_cursor']}"
    assert seen == [("ses_1", "decision"), ("ses_1", "generation"), (None, "decision")]


@pytest.mark.parametrize(
    ("query", "param"),
    [
        ("from=2026-10-11&to=2026-10-10", "from"),
        ("from=2025-01-01&to=2026-10-10", "from"),
        ("after=bm9wZQ", "after"),
        ("group_by=tenant", "group_by"),
        ("limit=500", "limit"),
    ],
)
async def test_bad_queries_are_400(query: str, param: str) -> None:
    status, doc = await get(services(), f"/v1/usage?{query}")
    assert status == 400 and doc["error"]["code"] == "schema_violation" and doc["error"]["param"] == param


async def test_healthz_and_readyz() -> None:
    svc = services()
    assert (await get(svc, "/healthz"))[0] == 200
    assert await get(svc, "/readyz") == (200, None)  # no body (spec-11 §7: no dependency states disclosed)
    svc.runtime.draining = True  # type: ignore[attr-defined]
    assert await get(svc, "/readyz") == (503, None)
    assert (await get(svc, "/healthz"))[0] == 200


async def test_readyz_without_a_config_snapshot_is_503() -> None:
    from noema.api.routes.health import ready

    svc = services()
    svc.config = BrokenConfig()  # type: ignore[assignment]
    assert ready(svc) is False  # type: ignore[arg-type]
