"""GET /v1/usage per group_by on the stack: the usage lines of a turn and a decision (written behind through the
outbox) show up under every dimension (project, session, model, day, line), every dimension adds up to the same
totals, the turn's generation line is priced by the stack's dev catalog (not estimated), and the report pages with
a cursor (IMPL-040). Runs as t_beta's `second` project, which only this test writes to."""

from __future__ import annotations

from datetime import UTC, datetime

import anyio
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import Json, report, tag
from tests.e2e.suite.turns import new_session, run_turn

pytestmark = [pytest.mark.e2e]

GROUPS = ("project", "session", "model", "day", "line")
FIELDS = ("input_tokens", "output_tokens")


def totals(rows: list[Json]) -> tuple[int, ...]:
    return tuple(sum(int(r["usage"][f]) for r in rows) for f in FIELDS)


async def test_usage_lines_per_group_by(stack: Stack, mock: Mock) -> None:
    client = stack.client(0, tenant="t_beta", project="second")
    await mock.load(sc.document("e2e-usage", [sc.stream(sc.SOL, first_text_ms=200, step_ms=20)], sc.router(ms=100)))
    session_id = await new_session(client)
    await run_turn(client, f"What is the capital of France? ({tag()})", session_id=session_id)
    decision: Json = {
        "profile": "lightweight-model-decide",
        "escalation": "off",
        "session_id": session_id,
        "input": f"Please call me back. ({tag()})",
        "questions": [{"type": "predicate", "name": "callback", "instructions": "The user asks for a call back."}],
    }
    await stack.client(1, tenant="t_beta", project="second").request("POST", "/v1/decisions", json=decision, expect=200)

    reader = stack.client(1, tenant="t_beta", project="second")
    with anyio.fail_after(30):
        while True:
            by_session = await report(reader, "session")
            lines = {r["line"] for r in by_session if r["key"] == session_id}
            if {"generation", "decision"} <= lines:
                break
            await anyio.sleep(0.5)
    with anyio.fail_after(30):  # every dimension sums the same lines (retried while a late line lands between reads)
        while True:
            reports = {group: await report(reader, group) for group in GROUPS}
            if len({totals(rows) for group, rows in reports.items() if group != "model"}) == 1:
                break
            await anyio.sleep(0.5)

    project_id = stack.project_id("t_beta", "second")
    assert {r["key"] for r in reports["project"]} == {project_id}
    assert {r["key"] for r in reports["day"]} >= {datetime.now(UTC).date().isoformat()}
    assert {r["key"] for r in reports["line"]} >= {"generation", "decision"}
    assert all(r["key"] == r["line"] for r in reports["line"])
    models = {r["key"] for r in reports["model"]}
    assert {sc.SOL, sc.LUNA} <= models, models
    assert totals(reports["model"])[0] > 0
    # the stack's dev price catalog (bootstrap: prices import, prices_ref db:<n>): the completed turn's generation
    # line is priced from reported tokens, so it costs more than 0 and is not estimated
    generation = next(r for r in by_session if r["key"] == session_id and r["line"] == "generation")
    assert generation["usage"]["cost_usd"] > 0 and generation["usage"]["cost_estimated"] is False, generation

    page1 = (await reader.request("GET", "/v1/usage", params={"group_by": "line", "limit": 1}, expect=200)).json()
    assert len(page1["data"]) == 1 and page1["has_more"] is True
    query = {"group_by": "line", "limit": 1, "after": page1["next_cursor"]}
    page2 = (await reader.request("GET", "/v1/usage", params=query, expect=200)).json()
    assert page2["data"] and page2["data"][0] != page1["data"][0]
    today = datetime.now(UTC).date().isoformat()
    window = await report(reader, "line", **{"from": today, "to": today})
    assert {r["line"] for r in window} >= {"generation", "decision"}
    too_long = {"group_by": "day", "from": "2024-01-01", "to": today}
    rejected = await reader.request("GET", "/v1/usage", params=too_long, expect=400)
    assert rejected.json()["error"]["code"] == "schema_violation"
