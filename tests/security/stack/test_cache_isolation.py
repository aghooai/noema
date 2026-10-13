"""SE3 (06 §9 "Cache isolation", §6 "Isolation"; SEC-ISO-01 / spec-06 T-L2-2 on the stack): one decision request,
byte-identical, sent by principals of the same project, another project and another tenant. Only the same project
may hit L2 (the positive control: the cache works, so the misses are not vacuous); another project and another
tenant always miss, their members are called again, and Valkey holds a separate tenant-prefixed key for each.
Bedrock-side prompt-cache isolation (`cacheReadInputTokens`, timing) is a Phase 0 gate (not run)."""

from __future__ import annotations

from typing import Any, Final

import anyio
import orjson
import pytest
import redis.asyncio as aioredis
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import Json, data, live_turn, metric_samples, mock_calls, mock_seq, tag, turn_body

pytestmark = [pytest.mark.e2e]
MEMBERS: Final = {sc.DEEPSEEK, sc.LUNA, sc.HAIKU}
QUESTION: Final[Json] = {"type": "predicate", "name": "refund", "instructions": "The user asks for a refund."}


async def decide(stack: Stack, mock: Mock, body: Json, *, tenant: str, project: str, role: str) -> tuple[Json, int]:
    """(the decision, the number of member calls it made)."""
    before = await mock_seq(mock)
    client = stack.client(1, tenant=tenant, project=project, role=role)
    decision: Json = (await client.request("POST", "/v1/decisions", json=body, expect=200)).json()
    calls = [c for c in await mock_calls(mock, before) if c["model"] in MEMBERS]
    return decision, len(calls)


async def l2_keys(stack: Stack) -> list[str]:
    client: Any = aioredis.from_url(str(stack.state["endpoints"]["redis"]))
    try:
        return sorted([k.decode() async for k in client.scan_iter(match="*dec:l2:*", count=1000)])
    finally:
        await client.aclose()


async def test_l2_never_hits_across_projects_or_tenants(stack: Stack, mock: Mock) -> None:
    body: Json = {
        "profile": "router-default",
        "escalation": "off",
        "questions": [QUESTION],
        "input": f"I want my money back for order 4471. ({tag('iso')})",
    }
    keys_before = set(await l2_keys(stack))
    first, calls = await decide(stack, mock, body, tenant="t_acme", project="main", role="developer")
    assert first["provenance"]["cache"] == {"hit": False} and calls > 0
    peer, calls = await decide(stack, mock, body, tenant="t_acme", project="main", role="developer2")
    assert peer["provenance"]["cache"] == {"hit": True} and calls == 0  # positive control: same project
    rows = {}
    for tenant, project in (("t_acme", "second"), ("t_beta", "main"), ("t_beta", "second")):
        other, calls = await decide(stack, mock, body, tenant=tenant, project=project, role="developer")
        rows[(tenant, project)] = (other["provenance"]["cache"], calls)
    assert rows == {k: ({"hit": False}, rows[k][1]) for k in rows} and all(n > 0 for _, n in rows.values()), rows
    again, calls = await decide(stack, mock, body, tenant="t_beta", project="main", role="developer")
    assert again["provenance"]["cache"] == {"hit": True} and calls == 0  # t_beta's own entry
    new = sorted(set(await l2_keys(stack)) - keys_before)
    tenants = [k.split("dec:l2:")[1].split(":")[0] for k in new]
    assert sorted(tenants) == ["t_acme", "t_acme", "t_beta", "t_beta"], new  # one key per (tenant, project)
    digests = [k.rsplit(":", 1)[1] for k in new]
    assert len(set(digests)) == 4, new  # per-tenant HMAC input digests and the project part differ


async def forged_cancel_turn(stack: Stack, mock: Mock) -> list[str]:
    """A streamed t_acme turn on api-1; after its first delta a `turn_cancel` naming it but tenant t_beta is published
    straight on the shared channel. Returns the stream's event types."""
    chunks = [f"word{i} " for i in range(30)]
    await mock.load(
        sc.document("sec-forged-cancel", [sc.stream(sc.SOL, first_text_ms=300, chunks=chunks)], sc.router())
    )
    client: Any = aioredis.from_url(str(stack.state["endpoints"]["redis"]))
    try:
        async with live_turn(stack.client(0), turn_body(f"Tell me a story. ({tag('iso')})")) as turn:
            await turn.until(lambda e: e.event == "generation.delta")
            payload = orjson.dumps({"tenant_id": "t_beta", "turn_id": turn.turn_id})
            assert await client.publish("noema:ch:turn_cancel", payload) >= 1  # both api pods listen
            events = await turn.rest()
    finally:
        await client.aclose()
    completed = [data(e) for e in events if e.event == "generation.completed"]
    assert completed and completed[-1]["finish_reason"] == "stop", [e.event for e in events]
    return [e.event for e in events]


async def test_forged_cross_tenant_cancel_is_dropped(stack: Stack, mock: Mock) -> None:
    """SEC-ISO-02 (spec-09 §7), the isolation property: the forged cross-tenant cancel does not cancel the turn."""
    assert "generation.cancelled" not in await forged_cancel_turn(stack, mock)


async def test_forged_cross_tenant_cancel_emits_a_security_event(stack: Stack, mock: Mock) -> None:
    """SEC-ISO-02, the signal: the receiver "drops ... and emits `pubsub_tenant_mismatch`" (spec-09 §7)."""
    before = await mismatches(stack)
    await forged_cancel_turn(stack, mock)
    await anyio.sleep(1.0)
    assert await mismatches(stack) > before


async def mismatches(stack: Stack) -> float:
    total = 0.0
    for replica in (0, 1):
        for labels, value in await metric_samples(stack, replica, "aghoo_security_violations_total"):
            if labels.get("kind") == "pubsub_tenant_mismatch":
                total += value
    return total
