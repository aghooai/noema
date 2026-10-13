"""F10 (spec-10 §4, 06 §7): Redis (valkey) unavailable mid-run, through toxiproxy: `down` (the proxy disabled: every
connection closed, new ones refused) and `hung` (a timeout toxic: connections open, no byte passes).

Paced sessionless turns on both replicas and both tenants for RUN_S; the outage lasts from OUTAGE[0] to OUTAGE[1].
Pass (spec-10 F10): >= 99.9% of the turns sent during the outage complete; admission falls back to local GCRA
buckets (`aghoo_admission_degraded` 1); L2 is bypassed (`aghoo_cache_misses_total{reason=error|bypass}` grows);
same-pod SSE unaffected; a cancel served by the process running the turn is unaffected (202), one served by any
other process answers 500: another pod, or a sibling process of the same pod (WEB_CONCURRENCY = 2, IMPL-087: the
running-turn registry is per process); every turn after recovery completes;
an idempotent duplicate on the other pod waits for the original to finish, then 410 (IMPL-021).
"""

from __future__ import annotations

import time
import uuid

import anyio
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite.kit import live_turn
from tests.fault import kit
from tests.fault.stackctl import Toxiproxy, metric_total

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]
RUN_S, PACE_S = 45.0, 0.2
OUTAGE = (10.0, 35.0)
SAME_POD_TRIES = 8  # P(8 fresh connections all reach the sibling process) = 0.5**8 if accepts split evenly


async def cancel_probe(stack: Stack, cancel_on: int, tries: int = 1) -> int | str:
    """A slow turn on api-1, cancelled from replica `cancel_on` once its first delta arrived: the cancel's status.
    `tries` > 1: a 500 is retried on a fresh connection (a new client), which may reach the pod's other process;
    the statuses of every try, comma-joined (e.g. "500,202")."""
    statuses: list[int] = []
    try:
        async with live_turn(stack.client(0), kit.body("f10-slow")) as turn:
            await turn.until(lambda e: e.event == "generation.delta", timeout_s=15)
            while len(statuses) < tries and (not statuses or statuses[-1] == 500):
                cancel = await stack.client(cancel_on).request("POST", f"/v1/turns/{turn.turn_id}/cancel")
                statuses.append(cancel.status_code)
            await turn.rest()
    except AssertionError as exc:
        return f"createTurn failed: {str(exc)[:120]}"
    return statuses[0] if tries == 1 else ",".join(map(str, statuses))


async def attach_probe(stack: Stack) -> kit.Json:
    """The idempotent duplicate of a running slow turn of api-1 sent to api-2: its status and how it ended."""
    key = {"Idempotency-Key": f"f10-{uuid.uuid4()}"}
    request = kit.body("f10-slow")
    out: kit.Json = {}
    try:
        async with live_turn(stack.client(0), request, headers=key) as turn:
            await turn.until(lambda e: e.event == "generation.delta", timeout_s=15)
            dup = kit.TurnRecord(-1, "t_acme", 1)
            with anyio.fail_after(60):
                await kit.one_turn(stack.client(1), dup, request, key)
            out = {"status": dup.status, "envelope": dup.envelope, "events": len(dup.events), "ms": dup.e2e_ms}
            await turn.rest()
    except (AssertionError, TimeoutError) as exc:
        out["failed"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return out


@pytest.mark.parametrize("mode", ["down", "hung"])
async def test_f10_redis_outage_keeps_turns_flowing(stack: Stack, mock: Mock, toxi: Toxiproxy, mode: str) -> None:
    await kit.fresh_retry_budget()
    slow = kit.gen(kit.SOL, step_ms=400, marker="f10-slow")
    await mock.load(kit.scenario("fault-F10", [slow, kit.gen(kit.SOL)], kit.members()))
    misses0 = {r: await metric_total(stack, "aghoo_cache_misses_total", reason=r) for r in ("error", "bypass")}
    probes: kit.Json = {}
    times: dict[str, float] = {}

    async def outage(t0: float) -> None:
        await anyio.sleep(OUTAGE[0])
        times["down"] = await (toxi.enable("valkey", False) if mode == "down" else toxi.blackhole("valkey"))
        await anyio.sleep(3.0)
        probes["admission_degraded"] = await metric_total(stack, "aghoo_admission_degraded")
        probes["same_pod_cancel"] = await cancel_probe(stack, 0, tries=SAME_POD_TRIES)
        probes["cross_pod_cancel"] = await cancel_probe(stack, 1)
        probes["cross_pod_attach"] = await attach_probe(stack)
        await anyio.sleep(max(0.0, t0 + OUTAGE[1] - time.monotonic()))
        await toxi.reset()
        times["up"] = time.monotonic()

    async with anyio.create_task_group() as tg:
        tg.start_soon(outage, time.monotonic())
        records = await kit.run_turns(
            stack, int(RUN_S / PACE_S), lambda i, t: kit.body(f"f10-{mode}-{t}"), pace_s=PACE_S, concurrency=60
        )
    kit.mark_budget_used()
    misses = {r: await metric_total(stack, "aghoo_cache_misses_total", reason=r) - misses0[r] for r in misses0}
    during = [r for r in records if times["down"] <= r.t_send < times["up"]]
    after = [r for r in records if r.t_send >= times["up"] + 2.0]
    kit.record_result(
        f"F10-{mode}",
        turns=len(records),
        outage_s=round(times["up"] - times["down"], 1),
        during=len(during),
        during_outcomes=kit.counts(kit.outcome(r) for r in during),
        during_e2e_ms=kit.summary([r.e2e_ms for r in during]),
        after_outcomes=kit.counts(kit.outcome(r) for r in after),
        before_outcomes=kit.counts(kit.outcome(r) for r in records if r.t_send < times["down"]),
        l2_misses=misses,
        **probes,
    )
    kit.assert_s1(records)
    assert sum(r.completed for r in during) >= 0.999 * len(during), kit.counts(kit.outcome(r) for r in during)
    assert all(r.completed for r in after), kit.counts(kit.outcome(r) for r in after)
    assert probes["admission_degraded"] >= 1 and misses["error"] + misses["bypass"] > 0, (probes, misses)
    same_pod = str(probes["same_pod_cancel"]).split(",")  # sibling-process 500s, then the owning process's 202
    assert same_pod[-1] == "202" and set(same_pod[:-1]) <= {"500"} and probes["cross_pod_cancel"] == 500, probes
    assert probes["cross_pod_attach"].get("status") == 410, probes["cross_pod_attach"]  # IMPL-021: waits, then 410
