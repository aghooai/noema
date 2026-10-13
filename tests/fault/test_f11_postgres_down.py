"""F11 (spec-10 §4, 06 §7): Postgres unavailable for 60 s (toxiproxy `postgres` disabled: the pools' connections
closed, new ones refused) while paced sessionless turns run on both replicas, half `store: true`, half `store: false`,
and SLOW in-flight `store: true` turns finalize inside the outage.

Pass: `store: true` turns fail fast at ingress with 500 `internal_error` (retryable); `store: false` turns are
unaffected; the in-flight turns finish streaming and their finalize goes to the outbox; after recovery every
finalized turn exists exactly once with one generation usage line per committed attempt (by id, in the database).
"""

from __future__ import annotations

import time

import anyio
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite.kit import eventually
from tests.fault import kit
from tests.fault.stackctl import Toxiproxy, sql

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]
RUN_S, PACE_S = 95.0, 0.25
OUTAGE = (10.0, 70.0)
SLOW = 10


def by_phase(records: list[kit.TurnRecord], t_down: float, store: bool) -> dict[str, dict[str, int]]:
    """Outcomes per 10 s bucket of the send time relative to the outage start (negative: before it)."""
    out: dict[str, list[str]] = {}
    for r in records:
        if (r.index % 2 == 0) is store:
            out.setdefault(f"{int((r.t_send - t_down) // 10) * 10:+d}s", []).append(kit.outcome(r))
    return {k: kit.counts(v) for k, v in sorted(out.items(), key=lambda kv: int(kv[0][:-1]))}


async def test_f11_postgres_outage(stack: Stack, mock: Mock, toxi: Toxiproxy) -> None:
    await kit.fresh_retry_budget()
    slow_rule = kit.gen(kit.SOL, step_ms=1000, marker="f11-slow")
    await mock.load(kit.scenario("fault-F11", [slow_rule, kit.gen(kit.SOL)], kit.members()))
    times: dict[str, float] = {}
    slow = [kit.TurnRecord(i, kit.TENANTS[i % 2], i % 2) for i in range(SLOW)]

    async def in_flight(record: kit.TurnRecord) -> None:
        client = stack.client(record.replica, tenant=record.tenant)
        await kit.one_turn(client, record, kit.body("f11-slow"), {"aghoo-deadline-ms": "30000"})

    async def outage(t0: float) -> None:
        await anyio.sleep(OUTAGE[0] - 2.0)
        async with anyio.create_task_group() as tg:
            for record in slow:
                tg.start_soon(in_flight, record)
            await anyio.sleep(2.0)
            times["down"] = await toxi.enable("postgres", False)
        await anyio.sleep(max(0.0, t0 + OUTAGE[1] - time.monotonic()))
        times["up"] = await toxi.enable("postgres", True)

    def make_body(i: int, tenant: str) -> kit.Json:
        return kit.body(f"f11-{tenant}", store=(i % 2 == 0))

    async with anyio.create_task_group() as tg:
        tg.start_soon(outage, time.monotonic())
        records = await kit.run_turns(stack, int(RUN_S / PACE_S), make_body, pace_s=PACE_S, concurrency=60)
    kit.mark_budget_used()
    during = [r for r in records if times["down"] <= r.t_send < times["up"]]
    stored_during = [r for r in during if r.index % 2 == 0]
    ephemeral_during = [r for r in during if r.index % 2 == 1]
    finalized = [r for r in slow if r.completed]
    for r in finalized:  # the outbox applies the finalize once Postgres is back
        await eventually(stack.client(1, tenant=r.tenant), f"/v1/turns/{r.turn_id}", lambda b: True, timeout_s=60)
    ids = [r.turn_id for r in finalized + [r for r in records if r.index % 2 == 0 and r.completed]]
    listed = ",".join(f"'{i}'" for i in ids)
    turn_rows = await sql(stack, f"select id, count(*) from noema.turn where id in ({listed}) group by id")
    lines = await sql(
        stack,
        f"select turn_id, count(*) from noema.usage_line where turn_id in ({listed}) and line = 'generation' "
        "group by turn_id",
    )
    kit.record_result(
        "F11",
        turns=len(records),
        outage_s=round(times["up"] - times["down"], 1),
        store_true_by_10s=by_phase(records, times["down"], True),
        store_false_by_10s=by_phase(records, times["down"], False),
        store_true_during_ingress_ms=kit.summary([r.e2e_ms for r in stored_during if r.status != 200]),
        in_flight=kit.counts(kit.outcome(r) for r in slow),
        in_flight_finished_s_after_down=kit.summary([r.t_send + r.e2e_ms / 1000 - times["down"] for r in slow]),
        checked_turns=len(ids),
        turn_rows=kit.counts(int(n) for _, n in turn_rows),
        missing_turns=len(set(ids) - {i for i, _ in turn_rows}),
        generation_lines_per_turn=kit.counts(int(n) for _, n in lines),
        turns_without_generation_line=len(set(ids) - {i for i, _ in lines}),
        store_false_failures=kit.counts(
            (r.envelope or {}).get("error", {}).get("message") for r in ephemeral_during if not r.completed
        ),
    )
    kit.assert_s1(records + slow, allow_500=True)
    assert all(r.completed for r in slow), kit.counts(kit.outcome(r) for r in slow)
    assert {i for i, _ in turn_rows} == set(ids) and all(n == "1" for _, n in turn_rows)
    assert {i for i, _ in lines} == set(ids) and all(n == "1" for _, n in lines)
    assert all(kit.outcome(r) in ("completed", "http:500:internal_error") for r in stored_during)
    assert all((r.envelope or {}).get("error", {}).get("retryable", True) for r in stored_during)
    assert all(r.completed for r in ephemeral_during), kit.counts(kit.outcome(r) for r in ephemeral_during)
