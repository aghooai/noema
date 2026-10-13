"""F1 (spec-10 §4, 06 §7): `ThrottlingException` on Opus for tenant A only, under pinned `opus-deep`.

Pass: >= 99% of A's turns commit on the next member (Sol) with no backoff sleep (spec-04 §6: a throttle with a next
member falls back at once); tenant B (same profile, never throttled) keeps its TTFT p95 within 10% of a clean run
with the same load; S1 on every turn (zero duplicated text, fallback success >= 99%, P-SSE-1, no 500).
"""

from __future__ import annotations

import re

import anyio
import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite.kit import mock_calls, mock_seq
from tests.fault import kit

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]
TAG = re.compile(r"f01-(t_acme|t_beta)-[0-9a-f]{10}")


def throttled_scenario(*, throttle: bool) -> kit.Json:
    rules = [kit.gen_fault(kit.OPUS, "throttling", 100)] if throttle else []
    if throttle:
        rules[0]["match"]["prompt_contains"] = "f01-t_acme"  # tenant A's turns only (their text carries the tag)
    return kit.scenario("fault-F1", rules, [kit.gen(kit.OPUS), kit.gen(kit.SOL)], kit.members())


async def heal_quota(stack: Stack, *, timeout_s: float = 90.0) -> None:
    """Tenant A's (account, Opus) quota breaker (no gauge) closed again: opus-deep turns of A until three in a row
    were answered by Opus (while it is open Opus is skipped; half-open, 1 admit in 20 is the probe that closes it)."""
    client, streak = stack.client(0, tenant="t_acme"), 0
    with anyio.fail_after(timeout_s):
        while streak < 3:
            probe = kit.body("f01-heal", "opus-deep", stream=False)
            turn = (await client.request("POST", "/v1/turns", json=probe, expect=200)).json()
            streak = streak + 1 if turn["attempts"][0]["model"] == kit.OPUS else 0
            await anyio.sleep(0.2)


def make_body(i: int, tenant: str) -> kit.Json:
    return kit.body(f"f01-{tenant}", "opus-deep")


def fallback_gaps_ms(calls: list[kit.Json]) -> list[float]:
    """Per tenant-A turn that reached Opus: ms from the mock's 429 (the throttled response sent) to the arrival of
    the same turn's Sol request, i.e. Noema's reaction time (a backoff sleep would sit in it)."""
    done: dict[str, float] = {}
    sol: dict[str, float] = {}
    for call in calls:
        if call["op"] != "converse-stream" or (found := TAG.search(str(call["body"]))) is None:
            continue
        if call["model"] == kit.OPUS and call.get("t_done_ns"):
            done[found.group(0)] = call["t_done_ns"] / 1e6
        elif call["model"] == kit.SOL:
            sol[found.group(0)] = call["t_req_recv_ns"] / 1e6
    return [sol[k] - t for k, t in done.items() if "t_acme" in k and k in sol]


async def test_f01_throttling_falls_back_at_once_for_the_throttled_tenant_only(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    await mock.load(throttled_scenario(throttle=False))
    await kit.run_turns(stack, 20, make_body)  # warm-up (pools, credentials), not measured
    clean = await kit.run_turns(stack, kit.TURNS, make_body)
    kit.assert_s1(clean)
    clean_b = [r.ttft_ms for r in clean if r.tenant == "t_beta" and r.ttft_ms is not None]
    # the throttled phase starts with an empty quota-breaker window: the clean run's `ok` outcomes on (A, Opus) would
    # otherwise dilute the > 50% rule by a timing-dependent amount (run 2 of the first report: no opening at all)
    await anyio.sleep(kit.BREAKER_WINDOW_S)

    await mock.load(throttled_scenario(throttle=True))
    await kit.run_turns(stack, 20, make_body, tenants=("t_beta",))  # B-only warm-up after the pause, not measured
    before = await mock_seq(mock)
    try:
        records = await kit.run_turns(stack, kit.TURNS, make_body)
        calls = await mock_calls(mock, before)
    finally:
        kit.mark_budget_used()
        await mock.load(throttled_scenario(throttle=False))
        await heal_quota(stack)
    a = [r for r in records if r.tenant == "t_acme"]
    b = [r for r in records if r.tenant == "t_beta"]
    on_sol = [r for r in a if r.completed and r.model_of(r.committed) == kit.SOL]
    gaps = fallback_gaps_ms(calls)
    throttled = [c for c in calls if c["model"] == kit.OPUS and c.get("fault")]
    served, failed_first = kit.fallback_success(records)
    b_ttft = [r.ttft_ms for r in b if r.ttft_ms is not None]
    kit.record_result(
        "F1",
        turns=len(records),
        a_turns=len(a),
        a_committed_on_sol=len(on_sol),
        a_outcomes=kit.counts(kit.outcome(r) for r in a),
        a_first_attempt_model=kit.counts(r.model_of(r.started[0]["attempt_id"]) if r.started else None for r in a),
        opus_throttled_requests=len(throttled),
        throttled_akids=kit.counts(c.get("akid") for c in throttled),
        fallback=kit.fallback_numbers(records),
        fallback_gap_ms=kit.summary(gaps),
        b_outcomes=kit.counts(kit.outcome(r) for r in b),
        b_ttft_clean=kit.summary(clean_b),
        b_ttft_fault=kit.summary(b_ttft),
    )
    kit.assert_s1(records)
    assert all(r.completed and r.model_of(r.committed) == kit.OPUS for r in b), kit.counts(kit.outcome(r) for r in b)
    assert gaps and kit.pct(gaps, 95) < 200, kit.summary(gaps)  # a backoff (jitter up to 0.5 s) would sit here
    assert kit.pct(b_ttft, 95) <= 1.10 * kit.pct(clean_b, 95), (kit.summary(b_ttft), kit.summary(clean_b))
    assert served >= 0.99 * failed_first, kit.fallback_numbers(records)
    assert len(on_sol) >= 0.99 * len(a), kit.counts(kit.outcome(r) for r in a)
