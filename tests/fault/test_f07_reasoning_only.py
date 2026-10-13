"""F7 (spec-10 §4, 06 §7): reasoning-only output (`reasoning_only`: reasoning deltas, then `max_tokens` with no
text) and `empty_text` (`end_turn`, no text) on GLM under pinned glm-reason, split by the turns' text tags.

Pass: nothing from the failed attempt is committed or rendered, the turn falls back to Sol (S1 >= 99%), and no turn
delivers an empty answer (0 `generation.completed` with empty text).
"""

from __future__ import annotations

import pytest
from tests.e2e.conftest import Mock, Stack
from tests.fault import kit

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]
KINDS = ("reasoning_only", "empty_text")


async def test_f07_text_less_answers_fall_back_and_are_never_delivered(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    rules = []
    for kind in KINDS:
        rule = kit.gen(kit.GLM, reasoning=10, fault={"kind": kind})
        rule["match"]["prompt_contains"] = f"f07-{kind}-"
        rules.append(rule)
    await mock.load(kit.scenario("fault-F7", rules, [kit.gen(kit.SOL)], kit.members()))
    records = await kit.run_turns(
        stack, kit.TURNS // 2, lambda i, t: kit.body(f"f07-{KINDS[i % 2]}-{t}", "glm-reason"), concurrency=50
    )
    kit.mark_budget_used()
    empty = [r for r in records if r.of("generation.completed") and not r.text]
    from_glm = [r for r in records if r.committed and r.model_of(r.committed) == kit.GLM]
    kit.record_result(
        "F7",
        turns=len(records),
        **{
            kind: {
                "outcomes": kit.counts(kit.outcome(r) for r in records if r.index % 2 == n),
                "fallback": kit.fallback_numbers([r for r in records if r.index % 2 == n]),
            }
            for n, kind in enumerate(KINDS)
        },
        completed_with_empty_text=len(empty),
        committed_from_glm=len(from_glm),
    )
    kit.assert_s1(records)
    assert not empty and not from_glm
    served, of = kit.fallback_success(records)
    assert served >= 0.99 * of, kit.fallback_numbers(records)
