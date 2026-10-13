"""Q9 (supporting; spec-10 §3, spec-06 §19.5): the hierarchical method's `deadline_exceeded` share at `deadline_ms`
1,000 vs 3,000 on the mock's probe latency profile, `make qa-hier-deadline`.

The 32-option, 4-group question of `decision-request.valid-hierarchical.json` (it exceeds every template's
max_choices, so round 1 asks the groups and round 2 the options of the expanded groups), `escalation: off`, on
`lightweight-model-decide` (Luna, the example's profile) and on `router-default` (DeepSeek, Luna, Haiku), every
member on its spec-10 §2.2 probe distribution. n decisions per (profile, deadline) at concurrency 20, every response
contract-validated (decision invariants paired with the request).

Pass: every answer is either scored by the hierarchical method or `unavailable / deadline_exceeded` (no other
outcome: §19.5 "no silent truncation"); and on both profiles the share is >= 95% at 1,000 ms (round 1 gets 500 ms,
below Luna's p5 of 640 ms and Haiku's 890 ms, so quorum cannot be met) and <= 5% at 3,000 ms (both rounds fit:
round 1 1,500 ms, ~0.75 s per Luna call). The shares and the per-member statuses are reported.
"""

from __future__ import annotations

import os
from typing import Final

import anyio
import orjson
import pytest
from tests.e2e.conftest import REPO, Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import tag
from tests.fault import kit
from tests.load import gatekit

pytestmark = [pytest.mark.e2e, pytest.mark.perf, pytest.mark.timeout(1800)]

N: Final = int(os.environ.get("NOEMA_HIER_N", "200"))
CONC: Final = 20
EXAMPLE: Final = REPO / "src/noema/contracts/_vendor/examples/decision-request.valid-hierarchical.json"
REQUEST: Final[gatekit.Json] = orjson.loads(EXAMPLE.read_bytes())
VALUES: Final = [str(c["value"]) for c in REQUEST["questions"][0]["choices"]]
ROUND1: Final = "billing_double_charge, billing_refund_status"  # the group listing of the round-1 prompt
GROUPS: Final = {"billing": 0.4799, "technical": 0.0201, "shipping": 0.4799, "other": 0.0201}
OPTIONS: Final = {
    **dict(zip(VALUES[0:8], [0.55, 0.08, 0.04, 0.15, 0.12, 0.03, 0.02, 0.01], strict=True)),
    **dict(zip(VALUES[16:24], [0.20, 0.60, 0.03, 0.02, 0.05, 0.01, 0.08, 0.01], strict=True)),
}
PROBE: Final = {sc.DEEPSEEK: gatekit.DEEPSEEK_PROBE, sc.LUNA: gatekit.LUNA_PROBE, sc.HAIKU: gatekit.HAIKU_PROBE}
CASES: Final = [(p, d) for p in ("lightweight-model-decide", "router-default") for d in (1000, 3000)]


def rules() -> list[gatekit.Rule]:
    """Per member: the group distribution for round 1, the option distribution for round 2, at its probe latency."""
    out = [gatekit.labeler()]
    for model, latency in PROBE.items():
        out.append({"match": {"model": model, "prompt_contains": ROUND1}, "answer": {"dist": GROUPS}})
        out.append({"match": {"model": model}, "answer": {"dist": OPTIONS}})
        out[-2]["latency"] = out[-1]["latency"] = dict(latency)
    return out


def body(profile: str, deadline_ms: int, i: int) -> gatekit.Json:
    return {
        **REQUEST,
        "profile": profile,
        "deadline_ms": deadline_ms,
        "escalation": "off",
        "input": f"{REQUEST['input']} ({tag(f'Q9-{i}')})",
    }


def outcome(answer: gatekit.Json) -> str:
    if answer["type"] == "unavailable":
        return f"unavailable:{answer['reason']}"
    return f"{answer['type']}:{answer.get('choice_method')}"


@pytest.mark.parametrize(("profile", "deadline_ms"), CASES, ids=[f"{p}-{d}" for p, d in CASES])
async def test_deadline_exceeded_share(
    stack: Stack, mock: Mock, gate_config: int, profile: str, deadline_ms: int
) -> None:
    await gatekit.fresh_mock(mock, kit.scenario(f"Q9-{profile}-{deadline_ms}", rules()))
    clients = [stack.client(r, tenant=t) for t in kit.TENANTS for r in (0, 1)]
    answers: list[gatekit.Json] = []
    limiter = anyio.Semaphore(CONC)

    async def go(i: int) -> None:
        async with limiter:
            client = clients[i % len(clients)]
            response = await client.request("POST", "/v1/decisions", json=body(profile, deadline_ms, i), expect=200)
            answers.extend(response.json()["answers"])

    async with anyio.create_task_group() as tg:
        for i in range(N):
            tg.start_soon(go, i)
    outcomes = kit.counts(outcome(a) for a in answers)
    exceeded = outcomes.get("unavailable:deadline_exceeded", 0)
    members = kit.counts(f"{m['model']}:{m['status']}" for a in answers for m in a.get("members") or [])
    exceeded_share = gatekit.share(exceeded, N)
    numbers = {"n": len(answers), "outcomes": outcomes, "deadline_exceeded_share": exceeded_share, "members": members}
    gatekit.record(f"Q9 {profile} deadline_ms={deadline_ms}", **numbers)
    assert set(outcomes) <= {"choice:hierarchical", "unavailable:deadline_exceeded"}, outcomes
    if deadline_ms == 1000:
        assert exceeded_share >= 0.95, numbers
    else:
        assert exceeded_share <= 0.05, numbers
