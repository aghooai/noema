"""F8 (spec-10 §4, 06 §7): malformed provider output.

- Generation attempt: Opus under pinned opus-deep sends one text chunk, then a ConverseStream frame with a flipped
  message CRC (`malformed{bad_crc}`) or valid CRCs around a truncated JSON payload (`truncated_json`). The router
  members answer at 900 ms, so the chunk is still held (holdback, S1 pending, hold cap first text + 400 ms) when the
  frame breaks: the attempt fails pre-commit, its held text is dropped and the turn falls back to Sol.
- Decision members: Luna's verbalized answer without a letter / with probabilities out of range / not JSON
  (`invalid_verbalized`), Haiku's not JSON, DeepSeek's (Mantle) truncated body (`truncated_json`) or broken JSON
  (`malformed`): that member's vote is `error` with its parse code, the others decide, the turn completes.
Pass: 0 HTTP 500 and 0 `internal_error` events; fallback success >= 99% (S1).
"""

from __future__ import annotations

import pytest
from tests.e2e.conftest import Mock, Stack
from tests.fault import kit

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(900)]
ATTEMPT = {"crc": {"kind": "malformed", "variant": "bad_crc"}, "trunc": {"kind": "truncated_json"}}
MEMBERS = {
    "luna-missing": (kit.LUNA, {"kind": "invalid_verbalized", "variant": "missing_letter"}),
    "luna-sum": (kit.LUNA, {"kind": "invalid_verbalized", "variant": "sum_out_of_range"}),
    "luna-notjson": (kit.LUNA, {"kind": "invalid_verbalized", "variant": "not_json"}),
    "haiku-notjson": (kit.HAIKU, {"kind": "invalid_verbalized", "variant": "not_json"}),
    "deepseek-trunc": (kit.DEEPSEEK, {"kind": "truncated_json"}),
    "deepseek-malformed": (kit.DEEPSEEK, {"kind": "malformed", "variant": "not_json"}),
}


async def test_f08_a_malformed_stream_fails_the_attempt_and_falls_back(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    names = list(ATTEMPT)
    rules = []
    for name, fault in ATTEMPT.items():
        rule = kit.gen(kit.OPUS, first_text_ms=300, fault=fault)
        rule["match"]["prompt_contains"] = f"f08a-{name}-"
        rules.append(rule)
    await mock.load(kit.scenario("fault-F8a", rules, [kit.gen(kit.SOL)], kit.members(ms=900)))
    records = await kit.run_turns(stack, kit.TURNS // 2, lambda i, t: kit.body(f"f08a-{names[i % 2]}-{t}", "opus-deep"))
    kit.mark_budget_used()
    kit.record_result(
        "F8-attempt",
        turns=len(records),
        **{
            name: {
                "outcomes": kit.counts(kit.outcome(r) for r in records if r.index % 2 == n),
                "fallback": kit.fallback_numbers([r for r in records if r.index % 2 == n]),
                "status": kit.counts(r.status for r in records if r.index % 2 == n),
            }
            for n, name in enumerate(names)
        },
    )
    kit.assert_s1(records)  # includes: no 500, no internal_error
    served, of = kit.fallback_success(records)
    assert served >= 0.99 * of, kit.fallback_numbers(records)


async def test_f08_invalid_decision_output_is_a_member_error(stack: Stack, mock: Mock) -> None:
    await kit.fresh_retry_budget()
    names = list(MEMBERS)
    rules = []
    for name, (model, fault) in MEMBERS.items():
        match = {"model": model, "prompt_contains": f"f08b-{name}-"}
        rules.append({"match": match, "answer": {"dist": kit.sc_low()}, "fault": fault, "latency_ms": kit.MEMBER_MS})
    await mock.load(kit.scenario("fault-F8b", [kit.gen(kit.SOL)], [kit.members()[0], *rules], kit.members()[1:]))
    records = await kit.run_turns(stack, kit.TURNS // 2, lambda i, t: kit.body(f"f08b-{names[i % len(names)]}-{t}"))
    kit.mark_budget_used()
    rows: dict[str, kit.Json] = {}
    for n, (name, (model, _)) in enumerate(MEMBERS.items()):
        mine = [r for r in records if r.index % len(names) == n]
        votes = [v for r in mine for v in kit.member_votes(r, model)]
        rows[name] = {
            "turns": len(mine),
            "outcomes": kit.counts(kit.outcome(r) for r in mine),
            "votes": kit.counts((v["status"], (v.get("error") or {}).get("code")) for v in votes),
        }
    kit.record_result("F8-members", turns=len(records), **rows)
    kit.assert_s1(records)
    assert all(r.completed for r in records), kit.counts(kit.outcome(r) for r in records)
    for name, row in rows.items():  # the faulted member never counts as `ok`; its failures carry a code
        assert not any(key.startswith("('ok'") for key in row["votes"]), (name, row)
        assert any(key.startswith("('error', '") for key in row["votes"]), (name, row)
