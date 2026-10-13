"""F6 (spec-10 §4, 06 §7): an exception event inside ConverseStream (`exception_event{type, after}`: `after` text
chunks, then an exception frame; the stream ends).

- Pre-commit (`after: 0`, modelStreamErrorException on Opus under pinned opus-deep): the attempt fails before any
  text and the turn falls back to Sol.
- Post-commit (`after: 5` on Sol under sol-fast, one row per stream exception type of spec-10 §2.1a): the 5 committed
  chunks stay the only text and the turn ends with a typed error: `all_routes_unavailable`, `retryable: true` for
  the provider-side types (G10b); validationException is reported as classified.
Pass: 100% classified (no 500, no untyped end), fallback success >= 99% (S1).
"""

from __future__ import annotations

import pytest
from tests.e2e.conftest import Mock, Stack
from tests.fault import kit

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]
PROVIDER_SIDE = (
    "modelStreamErrorException",
    "internalServerException",
    "serviceUnavailableException",
    "throttlingException",
)
POST_TYPES = (*PROVIDER_SIDE, "validationException")


async def test_f06_pre_commit_exception_falls_back(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    broken = kit.gen(kit.OPUS, fault={"kind": "exception_event", "type": "modelStreamErrorException", "after": 0})
    await mock.load(kit.scenario("fault-F6-pre", [broken, kit.gen(kit.SOL)], kit.members()))
    records = await kit.run_turns(stack, kit.TURNS // 2, lambda i, t: kit.body(f"f06a-{t}", "opus-deep"))
    kit.mark_budget_used()
    kit.record_result(
        "F6-pre",
        turns=len(records),
        outcomes=kit.counts(kit.outcome(r) for r in records),
        committed_model=kit.counts(r.model_of(r.committed) for r in records),
        fallback=kit.fallback_numbers(records),
    )
    kit.assert_s1(records)
    served, of = kit.fallback_success(records)
    assert served >= 0.99 * of, kit.fallback_numbers(records)
    assert all(r.completed for r in records), kit.counts(kit.outcome(r) for r in records)


async def test_f06_post_commit_exception_is_a_typed_error_after_the_committed_text(stack: Stack, mock: Mock) -> None:
    """One run, the turns spread over the exception types by their text tag; turns refused once the Sol breaker
    opened on these failures (no attempt, `all_routes_unavailable`) are counted apart."""
    await kit.fresh_retry_budget()
    rules = []
    for kind in POST_TYPES:
        rule = kit.gen(kit.SOL, fault={"kind": "exception_event", "type": kind, "after": 5})
        rule["match"]["prompt_contains"] = f"f06b-{kind}-"
        rules.append(rule)
    await mock.load(kit.scenario("fault-F6-post", rules, kit.members()))
    records = await kit.run_turns(
        stack, kit.TURNS // 2, lambda i, t: kit.body(f"f06b-{POST_TYPES[i % len(POST_TYPES)]}-{t}"), concurrency=100
    )
    kit.mark_budget_used()
    rows: dict[str, kit.Json] = {}
    for n, kind in enumerate(POST_TYPES):
        mine = [r for r in records if r.index % len(POST_TYPES) == n]
        committed = [r for r in mine if r.committed]
        rows[kind] = {
            "turns": len(mine),
            "committed": len(committed),
            "outcomes": kit.counts(kit.outcome(r) for r in mine),
            "errors": kit.counts(((r.error or {}).get("code"), (r.error or {}).get("retryable")) for r in committed),
            "rendered_5_chunks": kit.counts(r.text == "".join(kit.CHUNKS[:5]) for r in committed),
        }
    kit.record_result("F6-post", turns=len(records), **rows)
    kit.assert_s1(records)
    assert all(r.committed or kit.outcome(r) == "error:all_routes_unavailable" for r in records)
    for kind in PROVIDER_SIDE:
        assert rows[kind]["committed"] >= 5, rows[kind]
        assert set(rows[kind]["errors"]) == {str(("all_routes_unavailable", True))}, rows[kind]
        assert set(rows[kind]["rendered_5_chunks"]) == {"True"}, rows[kind]
