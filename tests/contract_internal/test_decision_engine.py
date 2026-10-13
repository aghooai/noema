"""DecisionEngine conformance (spec-02 §5, spec-06 §7, §10): the snapshot contract (S1 at commit-critical
closure, at most one final update), cancel, idempotent finalize, `row` only for stored turns, the dispatch check,
and contract-valid results (schema + `result_invariants`)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import anyio
import pytest

from noema.contracts.validate import ContractValidator
from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.decision import (
    CompiledQuestionSet,
    DecisionContext,
    DecisionRequest,
    DecisionSnapshot,
    ProfileRef,
    Purpose,
)

from .kits import DecisionKit, NullReservation, principal
from .registry import kit_fixture

kit = kit_fixture("decision_engine")
HAIKU = "us.anthropic.claude-haiku-5-5"


def d_std(*, haiku_misses: str = "sensitive personal data") -> dict[str, Any]:
    """spec-04 §4.2 D-std: DeepSeek and Luna answer every question by 900 ms, Haiku all but one by 930 ms."""
    intent = {"factual_qa": 0.9, "chitchat": 0.05, "coding": 0.05}
    level = {"trivial": 0.1, "moderate": 0.8, "hard": 0.1}
    low = {"true": 0.05, "false": 0.95}
    rules: list[dict[str, Any]] = [
        {"match": {"model": HAIKU, "prompt_contains": haiku_misses}, "fault": {"kind": "hang_before_first_byte"}}
    ]
    for text, dist in (("primary intent", intent), ("How hard", level), ("", low)):
        match = {"prompt_contains": text} if text else {}
        rules.append({"match": {"model": HAIKU, **match}, "answer": {"dist": dist}, "latency_ms": 930})
        rules.append({"match": match, "answer": {"dist": dist}, "latency_ms": 900})
    return {"scenario": "d-std", "seed": 7, "defaults": {"latency": {"fixed_ms": 0}}, "rules": rules}


def turn_request(kit: DecisionKit) -> DecisionRequest:
    profile = kit.snapshot.decision_profile("router-default")
    qs = kit.compiler.compile(profile, [], origin="turn", output_format="native", platform_from=profile)
    return DecisionRequest(
        ProfileRef("router-default", 1),
        qs,
        "What is the capital of France?",
        1000,
        2,
        "profile_default",
        frozenset({"members"}),
        "turn",
        "native",
    )


def context(
    kit: DecisionKit,
    *,
    turn_id: str | None = "turn_1",
    store: bool = True,
    budget_s: float = 120,
    purpose: Purpose = "router",
) -> DecisionContext:
    return DecisionContext(
        principal=principal(),
        tenant=kit.snapshot.tenant_ref("t_acme"),
        snapshot=kit.snapshot,
        deadline=Deadline(kit.clock.monotonic() + budget_s, kit.clock),
        escalate_until=None,
        reservation=NullReservation(),
        purpose=purpose,
        store=store,
        data_class="internal",
        content_policy="full",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        turn_id=turn_id,
        session_id="ses_1" if turn_id else None,
    )


def wire_request(qs: CompiledQuestionSet) -> dict[str, Any]:
    return {"questions": [dict(q.wire) for q in qs.questions], "input": "x", "include": ["members"]}


def check(snapshot: DecisionSnapshot, qs: CompiledQuestionSet) -> None:
    validator = ContractValidator()
    validator.validate_schema("getDecision", 200, "application/json", dict(snapshot.result))
    validator.check_decision_result(snapshot.result, wire_request(qs))


async def test_s1_then_one_final_update(kit: DecisionKit) -> None:
    kit.use_scenario(d_std())
    req = turn_request(kit)
    t0 = kit.clock.monotonic()
    async with kit.clock.autojump(), kit.engine.run(context(kit), req) as run:
        s1 = await run.commit_critical()
        s1_ms = round((kit.clock.monotonic() - t0) * 1000)
        assert await run.commit_critical() is s1  # idempotent
        updates = [u async for u in run.updates()]
        final_ms = round((kit.clock.monotonic() - t0) * 1000)
        final = await run.finalize()
    assert (s1_ms, s1.status, s1.final) == (930, "quorum", False)
    assert {"intent", "complexity"} <= s1.final_names and "sensitive_data" not in s1.final_names
    assert s1.answers["intent"].value == "factual_qa" and s1.answers["intent"].final
    assert not s1.answers["sensitive_data"].final
    assert len(updates) == 1 and updates[0] is final and final_ms == 1000
    assert final.status == "completed" and final.final and final.final_names - s1.final_names == {"sensitive_data"}
    assert final.decision_id == run.decision_id == s1.decision_id
    check(s1, req.questions)
    check(final, req.questions)


async def test_row_only_for_stored_turns(kit: DecisionKit) -> None:
    kit.use_scenario(d_std())
    async with kit.clock.autojump():
        stored = await kit.engine.decide(context(kit), turn_request(kit))
        unstored = await kit.engine.decide(context(kit, store=False), turn_request(kit))
        native = await kit.engine.decide(context(kit, turn_id=None), turn_request(kit))
    assert stored.row is not None and stored.row["id"] == stored.decision_id and stored.row["turn_id"] == "turn_1"
    assert stored.row["status"] in ("completed", "partial") and stored.row["result"] == dict(stored.result)
    assert unstored.row is None and native.row is None


async def test_finalize_is_idempotent(kit: DecisionKit) -> None:
    kit.use_scenario(d_std())
    async with kit.clock.autojump(), kit.engine.run(context(kit), turn_request(kit)) as run:
        first = await run.finalize()
        assert await run.finalize() is first


async def test_cancel_closes_pending_members_as_late(kit: DecisionKit) -> None:
    kit.use_scenario(d_std())
    async with kit.engine.run(context(kit), turn_request(kit)) as run:
        await kit.clock.advance(0.5)
        await run.cancel()
        await run.cancel()  # idempotent
        final = await run.finalize()
    assert final.final and final.status == "partial"
    result: Any = final.result
    assert {m["status"] for m in result["answers"][0]["members"]} == {"late"}


async def test_all_members_down_gives_a_final_partial_s1(kit: DecisionKit) -> None:
    kit.use_scenario(
        {
            "scenario": "down",
            "defaults": {"latency": {"fixed_ms": 0}},
            "rules": [{"match": {}, "fault": {"kind": "http_5xx", "status": 503}, "latency_ms": 50}],
        }
    )
    req = turn_request(kit)
    async with kit.clock.autojump(), kit.engine.run(context(kit), req) as run:
        s1 = await run.commit_critical()
        assert [u async for u in run.updates()] == []
    assert s1.final and s1.status == "partial"
    assert {a.type for a in s1.answers.values()} == {"unavailable"}
    assert {a.reason for a in s1.answers.values()} == {"all_members_failed"}
    check(s1, req.questions)


async def test_dispatch_check_rejects_an_exhausted_deadline(kit: DecisionKit) -> None:
    kit.use_scenario(d_std())
    with pytest.raises(NoemaError) as err:
        async with kit.engine.run(context(kit, budget_s=0.01), turn_request(kit)):
            pass
    assert err.value.code is ErrorCode.DEADLINE_EXCEEDED


async def test_cancelling_the_waiter_does_not_cancel_the_run(kit: DecisionKit) -> None:
    kit.use_scenario(d_std())
    async with kit.clock.autojump(), kit.engine.run(context(kit), turn_request(kit)) as run:
        with anyio.move_on_after(0):
            await run.commit_critical()
        s1 = await run.commit_critical()
        assert s1.status == "quorum"


async def test_a_pregenerated_decision_id_is_the_run_id(kit: DecisionKit) -> None:
    """createDecision pre-generates the dec_ id before the idempotency begin() (spec-04 §1 step 7)."""
    kit.use_scenario(d_std())
    ctx = replace(context(kit, turn_id=None), decision_id="dec_01J9ZQ7ABCDEFGHJKMNPQRSTVW")
    async with kit.clock.autojump():
        snap = await kit.engine.decide(ctx, turn_request(kit))
    assert snap.decision_id == "dec_01J9ZQ7ABCDEFGHJKMNPQRSTVW"


async def test_row_for_applies_a_stricter_content_policy(kit: DecisionKit) -> None:
    """IMPL-071 / spec-04 §10: the turn's final policy may be stricter than the run's; `row_for` re-renders
    `input` (redacted text or NULL) and never relaxes below ctx.content_policy."""
    kit.use_scenario(d_std())
    req = replace(turn_request(kit), input="Mail jane.doe@example.com about the capital of France.")
    async with kit.clock.autojump(), kit.engine.run(context(kit), req) as run:
        assert run.row_for("metadata_only") is None  # no final snapshot yet
        final = await run.finalize()
    assert final.row is not None
    full, redacted, bare = (run.row_for(p) for p in ("full", "redacted", "metadata_only"))
    assert full is not None and redacted is not None and bare is not None
    assert full["input"] == final.row["input"] and "jane.doe@example.com" in str(full["input"])
    assert "jane.doe@example.com" not in str(redacted["input"]) and "[REDACTED:pii_email]" in str(redacted["input"])
    assert bare["input"] is None and bare["result"] == final.row["result"]
    strict_ctx = replace(context(kit), content_policy="metadata_only")
    async with kit.clock.autojump(), kit.engine.run(strict_ctx, req) as strict:
        await strict.finalize()
    clamped = strict.row_for("full")
    assert clamped is not None and clamped["input"] is None


async def test_a_failed_row_insert_goes_to_the_outbox(kit: DecisionKit) -> None:
    """spec-02 §5: finalize() of a /v1/decisions run never raises for a DB failure; the row is an outbox record."""
    if kit.db_available is None or kit.outbox_records is None:
        pytest.skip("the kit has no Postgres-down switch")
    kit.use_scenario(d_std())
    kit.db_available(False)
    try:
        async with kit.clock.autojump():
            snap = await kit.engine.decide(context(kit, turn_id=None), turn_request(kit))
    finally:
        kit.db_available(True)
    (record,) = [r for r in kit.outbox_records("decision_row") if r.id == snap.decision_id]
    assert record.tenant_id == "t_acme" and record.payload["status"] in ("completed", "partial")
