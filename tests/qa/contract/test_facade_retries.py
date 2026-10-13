"""Gate Q2, retries (06 §2.2 "retry behavior on 429/503"): the official `openai` Python client's own retry loop
against the facade, counted at Noema (the recorded responses) and on the mock (the member calls of each attempt).

- 503: every member fails (facadekit.ALL_DOWN) -> every answer unavailable -> 503 all_routes_unavailable with
  `retry-after: 1`; the SDK (max_retries=2) makes exactly 3 attempts, each reaching the members, then raises
  InternalServerError with the contract code. With the members healed after the first 503: [503, 200].
- 429: t_beta at `rpm_per_key: 30` (noema-admin, restored afterwards): once the key's bucket is empty the facade
  answers 429 rpm_exceeded with `retry-after`, and the SDK's retry after that delay succeeds: [429, 200].
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import httpx
import openai
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite.kit import admin, mock_calls, mock_seq
from tests.e2e.suite.multipod import mint_key, patched_config
from tests.qa.contract.facadekit import ALL_DOWN, facade, scenario, unique

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(180)]
Q: Any = [{"type": "predicate", "name": "flag", "instructions": "Is this a regression of the export?"}]


def down_body() -> Any:
    return unique({"model": "aghoo/router-default", "input": f"{ALL_DOWN}: export fails.", "questions": Q})


async def test_503_is_retried_by_the_sdk_until_its_budget(stack: Stack, default_scenario: None, mock: Mock) -> None:
    await mock.load(scenario())
    sdk = facade(stack, max_retries=2)
    before = await mock_seq(mock)
    with pytest.raises(openai.InternalServerError) as raised:
        await sdk.client.decisions.create(**down_body())
    assert (raised.value.status_code, raised.value.code) == (503, "all_routes_unavailable")
    assert sdk.seen == [(503, "all_routes_unavailable")] * 3
    outcomes = [c["outcome"] for c in await mock_calls(mock, before) if ALL_DOWN in str(c.get("body"))]
    # every attempt reached the members (a call the engine gives up on is `disconnected` in the mock log)
    assert outcomes.count("fault") >= 3 and set(outcomes) <= {"fault", "disconnected"}, outcomes


async def test_503_then_healed_members_succeeds_on_retry(stack: Stack, default_scenario: None, mock: Mock) -> None:
    await mock.load(scenario())
    sdk = facade(stack, max_retries=2)

    async def heal_after_first_503(response: httpx.Response) -> None:
        if response.status_code == 503 and len(sdk.seen) == 1:
            await mock.load(scenario(all_down=False))  # before the SDK's retry (retry-after: 1 s)

    sdk.on_response = heal_after_first_503
    decision = await sdk.client.decisions.create(**down_body())
    assert sdk.seen == [(503, "all_routes_unavailable"), (200, None)]
    assert decision.answers[0].type == "predicate"


async def test_429_is_retried_after_retry_after(stack: Stack, default_scenario: None, tmp_path: Path) -> None:
    minted = await mint_key(stack, "t_beta", "main", "developer2")

    def limit(doc: dict[str, Any]) -> None:
        doc["tenants"]["t_beta"]["limits"] = {"rpm_per_key": 30}

    async with patched_config(stack, tmp_path, limit):
        drain = facade(stack, key=str(minted["key"]), max_retries=0)
        with anyio.fail_after(60):
            while not drain.seen or drain.seen[-1][0] != 429:
                body = unique({"model": "aghoo/router-default", "input": "Export fails.", "questions": Q})
                try:
                    await drain.client.decisions.create(**body)
                except openai.RateLimitError as exc:
                    assert exc.code == "rpm_exceeded" and int(exc.response.headers["retry-after"]) >= 1
        sdk = facade(stack, key=str(minted["key"]), max_retries=2)
        body = unique({"model": "aghoo/router-default", "input": "Export fails.", "questions": Q})
        with anyio.fail_after(90):
            decision = await sdk.client.decisions.create(**body)
        assert sdk.seen == [(429, "rpm_exceeded"), (200, None)] and decision.answers[0].type == "predicate"
    await admin(stack, ["keys", "revoke", str(minted["key_id"])])
