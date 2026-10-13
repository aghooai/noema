"""F5 (spec-10 §4, 06 §7): a stream that stalls mid-way (`stall_after{n}`: n text chunks, then silence).

- Pre-commit: GLM under pinned glm-reason streams reasoning and stalls before any text (`n: 0`): the attempt fails at
  its first-answer timeout (30 s) and the turn falls back to Sol (`Aghoo-Deadline-Ms: 45000`).
- Post-commit: Sol under sol-fast commits at its first text and stalls after 3 chunks: the generation idle timer
  (30 s) ends the turn with `error{upstream_stalled, retryable: true}`; the 3 chunks stay the client's only text and
  the stored turn keeps them (spec-04 G10).
"""

from __future__ import annotations

import pytest
from tests.e2e.conftest import Mock, Stack
from tests.e2e.suite.kit import eventually
from tests.fault import kit

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]
DEADLINE_MS = 45_000
PARTIAL = "".join(kit.CHUNKS[:3])


async def test_f05_pre_commit_stall_falls_back(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    stalled = kit.gen(kit.GLM, reasoning=10, fault={"kind": "stall_after", "n": 0})
    await mock.load(kit.scenario("fault-F5-pre", [stalled, kit.gen(kit.SOL)], kit.members()))
    records = await kit.run_turns(
        stack, kit.TURNS // 2, lambda i, t: kit.body(f"f05a-{t}", "glm-reason"), deadline_ms=DEADLINE_MS, concurrency=50
    )
    kit.mark_budget_used()
    kit.record_result(
        "F5-pre",
        turns=len(records),
        outcomes=kit.counts(kit.outcome(r) for r in records),
        committed_model=kit.counts(r.model_of(r.committed) for r in records),
        e2e_ms=kit.summary([r.e2e_ms for r in records]),
        fallback=kit.fallback_numbers(records),
    )
    kit.assert_s1(records)
    assert max(r.e2e_ms for r in records) <= DEADLINE_MS + 1_000
    served, of = kit.fallback_success(records)
    assert served >= 0.99 * of, kit.fallback_numbers(records)
    assert all(r.completed for r in records), kit.counts(kit.outcome(r) for r in records)


async def test_f05_post_commit_stall_is_upstream_stalled_and_keeps_the_partial_text(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    stalled = kit.gen(kit.SOL, fault={"kind": "stall_after", "n": 3})
    await mock.load(kit.scenario("fault-F5-post", [stalled], kit.members()))
    records = await kit.run_turns(
        stack, kit.TURNS // 2, lambda i, t: kit.body(f"f05b-{t}"), deadline_ms=DEADLINE_MS, concurrency=100
    )
    kit.mark_budget_used()
    attempted = [r for r in records if r.committed]
    errors = [r.error or {} for r in attempted]
    error_at = [r.at_ms[r.kinds.index("error")] for r in attempted if "error" in r.kinds]
    stored = [
        await eventually(stack.client(1, tenant=r.tenant), f"/v1/turns/{r.turn_id}", lambda b: True)
        for r in attempted[:20]
    ]
    kit.record_result(
        "F5-post",
        turns=len(records),
        outcomes=kit.counts(kit.outcome(r) for r in records),
        committed=len(attempted),
        error_retryable=kit.counts(e.get("retryable") for e in errors),
        error_at_ms=kit.summary(error_at),
        rendered_partial=kit.counts(r.text == PARTIAL for r in attempted),
        stored_checked=len(stored),
        stored_text_is_partial=kit.counts(s["text"] == PARTIAL for s in stored),
        stored_attempt_outcomes=kit.counts(tuple(a["outcome"] for a in s["attempts"]) for s in stored),
    )
    kit.assert_s1(records)
    assert attempted and all(e.get("code") == "upstream_stalled" and e.get("retryable") is True for e in errors)
    assert all(r.text == PARTIAL for r in attempted)
    assert all(s["text"] == PARTIAL for s in stored)
    assert min(error_at) >= 30_000 and max(r.e2e_ms for r in records) <= DEADLINE_MS + 1_000
