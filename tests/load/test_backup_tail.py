"""L5 (06 §4 "DeepSeek p99 with backup requests <= 1.5 s"; spec-10 §3): `make qa-load-backup-tail`.

Uncancelled `/v1/decisions` (`escalation: off`, `deadline_ms: 30000`) on the DeepSeek-only test profile (gatekit:
router-default's DeepSeek member, `backup_after_ms: 400`), the mock answering DeepSeek on its probe distribution
(spec-10 §2.2: log-normal [470, 590] ms with a 2% uniform(4,000, 12,000) ms tail; the backup repeats the body, so its
draw is independent). n >= 5,000 decisions at concurrency 50 over both replicas and two tenants, after a 50-request
warm-up; every response is contract-validated (decision invariants paired with the request) after the run.

Pass: client-observed decision latency p99 <= 1,500 ms, every decision 200 with its answer scored by an `ok`
DeepSeek member. Reported: the member latency, the backups the mock received, how many primaries ended in the tail
(cancelled when their backup won) and the driver's loop lag.
"""

from __future__ import annotations

import os
import time
from typing import Final

import anyio
import orjson
import pytest
from tests.e2e.conftest import CheckedClient, Mock, Stack
from tests.e2e.suite import scenarios as sc
from tests.e2e.suite.kit import mock_calls, tag
from tests.fault import kit
from tests.load import gatekit

pytestmark = [pytest.mark.e2e, pytest.mark.perf, pytest.mark.timeout(1800)]

N: Final = int(os.environ.get("NOEMA_BACKUP_TAIL_N", "5000"))
WARMUP: Final = 50
CONC: Final = 50
BOUND_MS: Final = 1500.0
QUESTION: Final = {"type": "predicate", "name": "callback", "instructions": "The user asks for a call back."}
RULES: Final = [
    gatekit.labeler(),
    {"match": {"model": sc.DEEPSEEK}, "answer": {"dist": dict(sc.LOW)}, "latency": dict(gatekit.DEEPSEEK_PROBE)},
]


def request(i: int) -> gatekit.Json:
    return {
        "profile": gatekit.DEEPSEEK_ONLY,
        "escalation": "off",
        "deadline_ms": 30000,
        "include": ["members"],
        "input": f"Please call me back. ({tag(f'L5-{i}')})",
        "questions": [QUESTION],
    }


async def timed(client: CheckedClient, body: gatekit.Json) -> tuple[int, dict[str, str], bytes, float, float]:
    """One createDecision, timed around the HTTP exchange only (validated later); (status, headers, body, ms, t0)."""
    t0 = time.monotonic()
    response = await client.http.post(
        "/v1/decisions", content=orjson.dumps(body), headers={"content-type": "application/json"}
    )
    return response.status_code, dict(response.headers), response.content, (time.monotonic() - t0) * 1000, t0


async def test_deepseek_p99_with_backups(stack: Stack, mock: Mock, gate_config: int) -> None:
    seq = await gatekit.fresh_mock(mock, kit.scenario("L5-backup-tail", RULES))
    total = WARMUP + N
    tenants = kit.TENANTS
    clients = {(t, r): gatekit.gate_client(stack, r, t) for t in tenants for r in (0, 1)}
    who = [(tenants[i % 2], (i // 2) % 2) for i in range(total)]
    bodies = [request(i) for i in range(total)]
    results: list[tuple[int, dict[str, str], bytes, float, float]] = [(0, {}, b"", 0.0, 0.0)] * total
    limiter = anyio.Semaphore(CONC)

    async def go(i: int) -> None:
        async with limiter:
            results[i] = await timed(clients[who[i]], bodies[i])

    lag = gatekit.LagProbe()
    async with lag, anyio.create_task_group() as tg:
        for i in range(total):
            tg.start_soon(go, i)
            if i < CONC:
                await anyio.sleep(gatekit.RAMP_S / CONC)

    member_ms: list[float] = []
    statuses: list[str] = []
    for i, (status, headers, content, _ms, _t0) in enumerate(results):
        client = clients[who[i]]
        client.validator.check_response("createDecision", status, headers, content, bodies[i])
        assert status == 200, (status, content[:300])
        (answer,) = orjson.loads(content)["answers"]
        (member,) = answer["members"]
        statuses.append(f"{answer['type']}:{member['status']}")
        member_ms.append(float(member["latency_ms"]))
    steady = [ms for ms, t0 in sorted(((r[3], r[4]) for r in results), key=lambda x: x[1])[WARMUP:]]
    calls = [c for c in await mock_calls(mock, seq) if c["model"] == sc.DEEPSEEK]
    numbers: gatekit.Json = {
        "n": len(steady),
        "warmup": WARMUP,
        "client_ms": gatekit.dist(steady),
        "member_latency_ms": gatekit.dist(member_ms[WARMUP:]),
        "bound_ms": BOUND_MS,
        "answers": kit.counts(statuses),
        "mock_deepseek_calls": len(calls),
        "mock_calls_by_k_outcome": kit.counts(f"k{c['k']}:{c['outcome']}" for c in calls),
        "backup_share": gatekit.share(sum(1 for c in calls if c["k"] >= 1), total),
        **gatekit.validity(lag.p99_ms, []),
    }
    gatekit.record("L5 DeepSeek p99 with backups", **numbers)
    assert numbers["answers"] == {"predicate:ok": total}, numbers["answers"]
    gatekit.assert_within(numbers, numbers["client_ms"]["p99"], BOUND_MS)
