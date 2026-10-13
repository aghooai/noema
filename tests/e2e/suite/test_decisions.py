"""/v1/decisions on the compose stack: native, Jev, escalation, hierarchical (32 options) and the shared L2 cache
(spec-06 through the real engine, adapters and the wire mock). Every body is validated with the decision invariants
paired with its request (CheckedClient)."""

from __future__ import annotations

from typing import Any

import orjson
import pytest
from tests.e2e.conftest import REPO, Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import Json, mock_calls, mock_seq, tag

pytestmark = [pytest.mark.e2e]

EXAMPLES = REPO / "src" / "noema" / "contracts" / "_vendor" / "examples"
DEPARTMENT = "Which team should handle"
RATE_HEADERS = ("x-ratelimit-limit-requests", "x-ratelimit-remaining-requests", "x-ratelimit-limit-tokens")


def example(name: str) -> Json:
    loaded: Json = orjson.loads((EXAMPLES / name).read_bytes())
    return loaded


def unique(body: Json) -> Json:
    """The example with a unique input text (no L2 hit from another test or run)."""
    return {**body, "input": f"{body['input']} ({tag()})"}


def answers(decision: Json) -> dict[str, Json]:
    return {a["name"]: a for a in decision["answers"]}


async def test_native_decision_on_the_ensemble(stack: Stack, mock: Mock) -> None:
    """router-default (DeepSeek logprobs over Mantle, Luna and Haiku verbalized over Converse): the default scenario's
    distributions pooled; the stored row is served by the other replica."""
    body = unique(example("decision-request.valid.json")) | {"escalation": "off"}
    before = await mock_seq(mock)
    response = await stack.client(0).request("POST", "/v1/decisions", json=body, expect=200)
    assert all(h in response.headers for h in RATE_HEADERS)
    decision = response.json()
    got = answers(decision)
    assert got["department"]["candidates"][0]["value"] == "billing"
    assert got["urgency"]["candidates"][0]["value"] == "high" and got["needs_human"]["candidates"][0]["value"] is False
    members = {m["model"]: m for m in got["department"]["members"]}
    assert set(members) == {sc.DEEPSEEK, sc.LUNA, sc.HAIKU}
    assert members[sc.DEEPSEEK]["provider"] == "bedrock-mantle-chat" and members[sc.DEEPSEEK]["status"] == "ok"
    assert decision["provenance"]["cache"] == {"hit": False}
    calls = await mock_calls(mock, before)  # (a sampled labeling job may add labeler calls: Sol, Sonnet)
    assert {c["model"] for c in calls} >= {sc.DEEPSEEK, sc.LUNA, sc.HAIKU}
    assert {c["op"] for c in calls if c["model"] == sc.DEEPSEEK} == {"chat"}
    stored = (await stack.client(1).request("GET", f"/v1/decisions/{decision['id']}", expect=200)).json()
    assert (
        stored["id"] == decision["id"]
        and answers(stored)["department"]["candidates"] == got["department"]["candidates"]
    )


async def test_jev_decision_and_its_native_row(stack: Stack) -> None:
    """lightweight-model-decide (Luna) in the Jev format; getDecision returns the stored native result."""
    body = unique(example("decision-request.valid-jev.json"))
    jev = (await stack.client(1).request("POST", "/v1/decisions", json=body, expect=200)).json()
    assert jev["answers"]["department"]["choice"] == "billing" and jev["answers"]["needs_human"]["type"] == "noul"
    stored = (await stack.client(0).request("GET", f"/v1/decisions/{jev['decision_id']}", expect=200)).json()
    assert stored["provenance"]["profile_id"] == "lightweight-model-decide"
    assert answers(stored)["department"]["candidates"][0]["value"] == "billing"


async def test_escalation_from_a_weak_single_model_answer(stack: Stack, mock: Mock) -> None:
    """Luna's first answer is a tie (margin 0 < 0.10): the question escalates to router-default, whose three
    members agree on billing; the stage is recorded in the provenance."""
    tie = {"billing": 0.48, "shipping": 0.48, "technical": 0.02, "other": 0.02}
    confident = {"billing": 0.8, "shipping": 0.1, "technical": 0.05, "other": 0.05}
    rules = [  # `nth: 1`: only Luna's first call (stage 0) ties; its stage-1 call answers like the others
        sc.answer({"model": sc.LUNA, "prompt_contains": DEPARTMENT, "nth": 1}, tie, 100),
        sc.answer({"prompt_contains": DEPARTMENT}, confident, 100),
    ]
    await mock.load(sc.document("e2e-escalation", rules))
    question: Any = example("decision-request.valid.json")["questions"][0]
    body: Json = {
        "profile": "lightweight-model-decide",
        "input": f"I was charged twice for my order. ({tag()})",
        "questions": [question],
        "include": ["members", "stages"],
    }
    before = await mock_seq(mock)
    decision = (await stack.client(0).request("POST", "/v1/decisions", json=body, expect=200)).json()
    department = answers(decision)["department"]
    assert department["candidates"][0]["value"] == "billing" and department["confidence"]["margin"] > 0.5
    (stage,) = decision["provenance"]["stages"]
    assert (stage["profile_id"], stage["questions"]) == ("router-default", ["department"])
    assert "margin_below" in stage["triggered_by"]
    models = [c["model"] for c in await mock_calls(mock, before)]
    assert models.count(sc.LUNA) == 2 and sc.DEEPSEEK in models and sc.HAIKU in models


async def test_hierarchical_decision_over_32_options(stack: Stack, mock: Mock) -> None:
    """32 grouped options exceed every template's max_choices: round 1 asks the four groups, round 2 the options
    of the expanded billing and shipping groups; the answer reproduces decision-result.valid-hierarchical.json."""
    request = example("decision-request.valid-hierarchical.json")
    values = [c["value"] for c in request["questions"][0]["choices"]]
    billing = dict(zip(values[0:8], [0.55, 0.08, 0.04, 0.15, 0.12, 0.03, 0.02, 0.01], strict=True))
    shipping = dict(zip(values[16:24], [0.20, 0.60, 0.03, 0.02, 0.05, 0.01, 0.08, 0.01], strict=True))
    groups = {"billing": 0.4799, "technical": 0.0201, "shipping": 0.4799, "other": 0.0201}
    rules = [
        sc.answer({"model": sc.LUNA, "prompt_contains": "billing_double_charge, billing_refund_status"}, groups, 300),
        sc.answer({"model": sc.LUNA}, {**billing, **shipping}, 300),
    ]
    await mock.load(sc.document("e2e-hierarchical", rules))
    before = await mock_seq(mock)
    decision = (await stack.client(1).request("POST", "/v1/decisions", json=unique(request), expect=200)).json()
    (issue,) = decision["answers"]
    expected = example("decision-result.valid-hierarchical.json")["answers"][0]
    assert issue["choice_method"] == "hierarchical" and issue["choice"] == expected["choice"]
    for key in ("candidates", "groups", "unresolved_mass", "confidence"):
        assert issue[key] == expected[key], key
    luna = [c for c in await mock_calls(mock, before) if c["model"] == sc.LUNA]
    assert len(luna) == 3  # round 1, then one round-2 call per expanded group


async def test_l2_cache_hit_across_replicas(stack: Stack, mock: Mock) -> None:
    """The same request again, on the other replica: answered from the shared L2 (Valkey) with no member call."""
    body = unique(example("decision-request.valid-default-profile.json")) | {"profile": "router-default"}
    first = (await stack.client(0).request("POST", "/v1/decisions", json=body, expect=200)).json()
    assert first["provenance"]["cache"] == {"hit": False}
    before = await mock_seq(mock)
    again = (await stack.client(1).request("POST", "/v1/decisions", json=body, expect=200)).json()
    assert again["provenance"]["cache"] == {"hit": True} and again["id"] != first["id"]
    strip = ("members",)
    assert [{k: v for k, v in a.items() if k not in strip} for a in again["answers"]] == [
        {k: v for k, v in a.items() if k not in strip} for a in first["answers"]
    ]
    members = {sc.DEEPSEEK, sc.LUNA, sc.HAIKU}
    assert [c for c in await mock_calls(mock, before) if c["model"] in members] == []
    stored = await stack.client(0).request("GET", f"/v1/decisions/{again['id']}", expect=200)
    assert stored.json()["provenance"]["cache"] == {"hit": True}
