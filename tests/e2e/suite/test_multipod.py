"""Multi-pod behavior on the stack's two api replicas: the circuit breaker opened by one pod's evidence reaches the
other through Redis pub/sub (IMPL-078), the GCRA admission buckets and the per-IP auth-failure bucket are shared
(Valkey), and a key revocation reaches every pod's key cache within a second (keys_revoked)."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

import anyio
import pytest
from tests.e2e.conftest import DEFAULT_SCENARIO, Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import Json, admin, mock_calls, mock_seq, tag
from tests.e2e.suite.multipod import OPEN, circuit, heal_circuits, keyed, mint_key, patched_config, wait_for

pytestmark = [pytest.mark.e2e]


def predicates(n: int) -> Json:
    """A router-default decision of `n` predicates (every member asked n times), escalation off."""
    marker = tag("breaker")
    questions = [
        {"type": "predicate", "name": f"p{i}", "instructions": f"Fact {i} is stated ({marker})."} for i in range(n)
    ]
    return {
        "profile": "router-default",
        "escalation": "off",
        "include": ["members"],
        "input": f"Some text ({marker})",
        "questions": questions,
    }


def member_errors(decision: Json, model: str) -> set[str]:
    members = [m for a in decision["answers"] for m in a["members"] if m["model"] == model]
    return {m.get("error", {}).get("code", m["status"]) for m in members}


@pytest.mark.slow
async def test_breaker_opened_on_one_replica_opens_on_the_other(stack: Stack, mock: Mock) -> None:
    haiku_down = sc.fault({"model": sc.HAIKU}, "http_5xx", 20, status=503)
    await mock.load(sc.document("e2e-breaker", [haiku_down, sc.answer({}, sc.LOW, 50)]))
    try:
        for _ in range(8):  # a 503 burst seen by api-1 only: >= 20 failed calls in the 30 s window
            await stack.client(0).request("POST", "/v1/decisions", json=predicates(10), expect=200)
            if await circuit(stack, 0, sc.HAIKU) == OPEN:
                break
        assert await circuit(stack, 0, sc.HAIKU) == OPEN

        async def remote_open() -> bool:
            return await circuit(stack, 1, sc.HAIKU) == OPEN

        await wait_for(remote_open, timeout_s=3)  # published on breaker_events, applied by api-2
        before = await mock_seq(mock)
        decision = (await stack.client(1).request("POST", "/v1/decisions", json=predicates(3), expect=200)).json()
        assert member_errors(decision, sc.HAIKU) == {"circuit_open"}
        assert all(a["type"] == "predicate" for a in decision["answers"])  # DeepSeek + Luna still make quorum
        assert sc.HAIKU not in {c["model"] for c in await mock_calls(mock, before)}  # api-2 never called Haiku
    finally:  # heal: after open_s (15 s) the half-open probes succeed and close the breaker on both pods
        await mock.load(DEFAULT_SCENARIO.read_text())
        await heal_circuits(stack)


async def test_admission_gcra_bucket_is_shared_by_both_replicas(stack: Stack, tmp_path: Path) -> None:
    """t_beta at rpm_per_key 4 (a config version activated through noema-admin): a fresh key's 4 calls spread over
    both replicas drain one bucket (remaining 3, 2, 1, 0), and the fifth is 429 rpm_exceeded on either pod."""
    minted = await mint_key(stack, "t_beta", "main", "developer2")

    def limit(doc: dict[str, Any]) -> None:
        doc["tenants"]["t_beta"]["limits"] = {"rpm_per_key": 4}

    one_call: Json = {
        "profile": "lightweight-model-decide",
        "escalation": "off",
        "input": f"Please call me back. ({tag()})",
        "questions": [{"type": "predicate", "name": "callback", "instructions": "The user asks for a call back."}],
    }

    async def limit_seen(replica: int, value: str) -> bool:
        other = {**one_call, "input": f"Probe ({tag()})"}
        response = await stack.client(replica, tenant="t_beta").request("POST", "/v1/decisions", json=other)
        seen: str | None = response.headers.get("x-ratelimit-limit-requests")
        return seen == value

    async with patched_config(stack, tmp_path, limit):
        for replica in (0, 1):  # both pods run the new version (config_changed, or the 5 s poll)
            await wait_for(partial(limit_seen, replica, "4"), timeout_s=10, every_s=0.5)
        remaining = []
        for i in range(4):
            body = {**one_call, "input": f"Call {i} ({tag()})"}  # a unique input: an L2 hit would refund the call
            response = await keyed(stack, i % 2, minted["key"]).request("POST", "/v1/decisions", json=body)
            assert response.status_code == 200, response.text
            remaining.append(response.headers["x-ratelimit-remaining-requests"])
        assert remaining == ["3", "2", "1", "0"]
        for replica in (0, 1):
            body = {**one_call, "input": f"Over the limit ({tag()})"}
            refused = await keyed(stack, replica, minted["key"]).request("POST", "/v1/decisions", json=body)
            assert refused.status_code == 429 and refused.json()["error"]["code"] == "rpm_exceeded"
            assert int(refused.headers["retry-after"]) >= 1
    for replica in (0, 1):
        await wait_for(partial(limit_seen, replica, "6000"), timeout_s=10, every_s=0.5)
    await admin(stack, ["keys", "revoke", str(minted["key_id"])])


async def test_auth_failure_bucket_is_shared_by_both_replicas(stack: Stack) -> None:
    """The per-IP auth-failure GCRA bucket (burst 50, 10/s) lives in Valkey: 70 bad keys spread over both replicas
    exhaust it (each replica alone saw 35, under the burst), then it refills."""
    bad = [keyed(stack, replica, "ak_test_" + "x" * 40) for replica in (0, 1)]
    statuses: list[int] = []
    for i in range(70):
        response = await bad[i % 2].request("GET", "/v1/profiles")
        statuses.append(response.status_code)
    assert set(statuses) <= {401, 429} and statuses.count(429) >= 5, statuses
    assert {s for i, s in enumerate(statuses) if i % 2 == 1 and s == 429} == {429}  # api-2 refused too
    await anyio.sleep(6.0)  # refill (10/s) before any later test authenticates from this address
    await stack.client(1).request("GET", "/v1/profiles", expect=200)


async def test_key_revocation_reaches_both_replicas(stack: Stack) -> None:
    minted = await mint_key(stack, "t_acme", "main", "developer")
    clients = [keyed(stack, replica, minted["key"]) for replica in (0, 1)]
    for client in clients:  # both pods have the key row in their LRU now (60 s TTL)
        await client.request("GET", "/v1/profiles", expect=200)
    await admin(stack, ["keys", "revoke", str(minted["key_id"])])

    async def refused_everywhere() -> bool:
        codes = [(await client.request("GET", "/v1/profiles")).status_code for client in clients]
        return codes == [401, 401]

    await wait_for(refused_everywhere, timeout_s=5)  # far below the cache TTL: the keys_revoked pub/sub path
    denied = await clients[1].request("GET", "/v1/profiles", expect=401)
    assert denied.json()["error"]["code"] == "invalid_api_key"
