"""The pre-commit failure walk (spec-04 §6, IMPL-016): fallback order, the default profile as the last resort, the
turn-wide failure budget, the retry budget, fail-closed tenant access, the throttling rules (an immediate fallback,
else one jittered same-member retry inside the deadline), breaker / allowlist / context-window skips and race."""

from __future__ import annotations

import random

from tests.fakes.config import patched_example, snapshot_from
from tests.fakes.generation import StaticPromptBuilder

from noema.core.errors import ErrorKind
from noema.core.protocols.generation import AttemptBudget
from noema.generation.policy import throttle_backoff_s

from .rig import GLM, OPUS, SOL, commit, drive, make_rig
from .scripted import Call, failure, text_stream

SOL_A, OPUS_A, GLM_A = "sol-6.1", "opus-5.5", "glm-5.3"
ALL_BUT_OPUS = ["haiku-5.5", "luna-6", "deepseek-3.2", SOL_A, GLM_A, "sonnet-5.5"]


def down(code: str = "503", kind: ErrorKind = ErrorKind.RETRYABLE, *, after: float = 0.1) -> Call:
    return Call(open_error=failure(kind, code), open_s=after)


async def test_throttle_with_a_next_member_falls_back_at_once() -> None:
    rig = make_rig({OPUS_A: [down("ThrottlingException", ErrorKind.THROTTLED)], SOL_A: [Call(text_stream("ok"))]})
    out = await drive(rig, "opus-deep", effort="high", hold=False)
    starts = [(ms, ev["model"]) for ms, ev in out.of("generation.started")]
    assert starts == [(0, OPUS), (100, SOL)]  # no backoff sleep (F1)


async def test_throttle_without_a_next_member_retries_the_same_member_after_retry_after() -> None:
    throttled = down("ThrottlingException", ErrorKind.THROTTLED, after=0.05)
    throttled.open_error = failure(ErrorKind.THROTTLED, "ThrottlingException", retry_after_s=1.5)
    rig = make_rig({SOL_A: [throttled, Call(text_stream("ok"))]})
    budget = AttemptBudget()
    out = await drive(rig, "sol-fast", hold=False, budget=budget)
    assert [(ms, ev["model"]) for ms, ev in out.of("generation.started")] == [(0, SOL), (1550, SOL)]
    assert [r.outcome for r in out.records] == ["failed", "committed"] and budget.remaining == 1


async def test_throttle_retry_uses_full_jitter_when_no_retry_after() -> None:
    rig = make_rig({SOL_A: [down("ThrottlingException", ErrorKind.THROTTLED, after=0.0), Call(text_stream("ok"))]})
    out = await drive(rig, "sol-fast", hold=False)
    wait_ms = out.of("generation.started")[1][0]
    expected = throttle_backoff_s(None, 0, random.Random(7))  # the rig's seeded rng, k = 0
    assert wait_ms == round(expected * 1000) and 0 <= wait_ms < 500


def test_jitter_window_doubles_with_the_failure_attempts() -> None:
    rng = random.Random(1)
    for k in range(4):
        waits = [throttle_backoff_s(None, k, rng) for _ in range(200)]
        assert min(waits) >= 0 and max(waits) < 0.5 * 2**k
    assert throttle_backoff_s(2.5, 3, rng) == 2.5


async def test_throttle_retry_needs_the_first_answer_timeout_before_the_deadline() -> None:
    throttled = Call(open_error=failure(ErrorKind.THROTTLED, "ThrottlingException", retry_after_s=1.0))
    rig = make_rig({SOL_A: [throttled]})
    out = await drive(rig, "sol-fast", hold=False, ctx=rig.ctx(deadline_s=8.5))  # 1 s + sol's 8 s > 8.5 s
    assert len(out.of("generation.started")) == 1
    assert out.of("error")[0][1]["code"] == "all_routes_unavailable"


async def test_throttle_with_the_attempt_budget_spent_ends_at_once_without_the_backoff() -> None:
    throttled = Call(open_error=failure(ErrorKind.THROTTLED, "ThrottlingException", retry_after_s=20.0), open_s=0.1)
    rig = make_rig({SOL_A: [throttled]})
    out = await drive(rig, "sol-fast", hold=False, budget=AttemptBudget(0))
    assert [(ms, ev["type"]) for ms, ev in out.events] == [
        (0, "generation.started"),
        (100, "generation.cancelled"),
        (100, "error"),  # not after the 20 s retry_after: the guard fails before the sleep (IMPL-016)
    ]
    assert rig.world.retry_budget.spent == 0


async def test_throttle_with_the_retry_budget_spent_ends_at_once_without_the_backoff() -> None:
    throttled = Call(open_error=failure(ErrorKind.THROTTLED, "ThrottlingException", retry_after_s=20.0), open_s=0.1)
    rig = make_rig({SOL_A: [throttled, Call(text_stream("never"))]})
    rig.world.retry_budget.exhaust(rig.snap.tenant_ref("t_acme").aws_account_id, SOL)
    budget = AttemptBudget()
    out = await drive(rig, "sol-fast", hold=False, budget=budget)
    assert [(ms, ev["type"]) for ms, ev in out.events][1:] == [(100, "generation.cancelled"), (100, "error")]
    assert budget.remaining == 2  # a refused retry spends no turn budget


async def test_throttle_retry_spends_the_retry_budget_once() -> None:
    throttled = Call(open_error=failure(ErrorKind.THROTTLED, "ThrottlingException", retry_after_s=1.0))
    rig = make_rig({SOL_A: [throttled, Call(text_stream("ok"))]})
    out = await drive(rig, "sol-fast", hold=False)
    assert [r.outcome for r in out.records] == ["failed", "committed"]
    assert rig.world.retry_budget.spent == 1  # taken before the sleep, not again at the start


async def test_the_default_profile_is_the_last_resort() -> None:
    rig = make_rig({OPUS_A: [down()], SOL_A: [down("500"), Call(text_stream("ok"))]})
    out = await drive(rig, "opus-deep", effort="high", hold=False)
    assert [(r.route_id, r.model, r.outcome) for r in out.records] == [
        ("opus-deep", OPUS, "failed"),
        ("opus-deep", SOL, "failed"),
        ("sol-fast", SOL, "committed"),
    ]


async def test_budget_exhaustion_ends_the_walk_and_is_shared_across_runs() -> None:
    budget = AttemptBudget()
    rig = make_rig({OPUS_A: [down(), down()], SOL_A: [down(), down(), down()]})
    first = await drive(rig, "opus-deep", effort="high", hold=False, budget=budget)
    assert len(first.records) == 3 and budget.remaining == 0  # A1 + 2 failure-driven attempts
    second = await drive(rig, "opus-deep", effort="high", hold=False, budget=budget, speculative=False)
    assert len(second.records) == 1  # A2's own first attempt still runs; no fallback is left
    assert second.types()[-1] == "error"


async def test_a_fallback_to_another_model_is_not_a_retry() -> None:
    """R6: a fallback to a different model never spends the retry budget of either model (only the turn's
    AttemptBudget, admission bounds the rest): it is served even with both models' budgets spent (F1, F4-F8)."""
    for kind, code in ((ErrorKind.RETRYABLE, "503"), (ErrorKind.THROTTLED, "ThrottlingException")):
        rig = make_rig({OPUS_A: [down(code, kind)], SOL_A: [Call(text_stream("ok"))]})
        account = rig.snap.tenant_ref("t_acme").aws_account_id
        rig.world.retry_budget.exhaust(account, OPUS)
        rig.world.retry_budget.exhaust(account, SOL)
        budget = AttemptBudget()
        out = await drive(rig, "opus-deep", effort="high", hold=False, budget=budget)
        assert [(r.model, r.outcome) for r in out.records] == [(OPUS, "failed"), (SOL, "committed")], code
        assert rig.world.retry_budget.spent == 0 and budget.remaining == 1


async def test_a_re_send_to_a_model_that_already_failed_is_a_retry() -> None:
    """R6: the retry budget guards same-model re-sends: opus-deep's Sol failed, so the default profile's Sol is a
    retry of Sol (one unit spent); with Sol's budget spent it is refused and the walk ends."""
    rig = make_rig({OPUS_A: [down()], SOL_A: [down("500"), Call(text_stream("ok"))]})
    out = await drive(rig, "opus-deep", effort="high", hold=False)
    assert [r.outcome for r in out.records] == ["failed", "failed", "committed"]
    assert rig.world.retry_budget.spent == 1

    rig = make_rig({OPUS_A: [down()], SOL_A: [down("500"), Call(text_stream("never"))]})
    rig.world.retry_budget.exhaust(rig.snap.tenant_ref("t_acme").aws_account_id, SOL)
    budget = AttemptBudget()
    out = await drive(rig, "opus-deep", effort="high", hold=False, budget=budget)
    assert [(r.model, r.outcome) for r in out.records] == [(OPUS, "failed"), (SOL, "failed")]
    assert out.types()[-1] == "error" and budget.remaining == 1  # a refused re-send spends no turn budget


async def test_tenant_access_never_falls_back() -> None:
    rig = make_rig({OPUS_A: [down("AccessDeniedException", ErrorKind.TENANT_ACCESS)]})
    out = await drive(rig, "opus-deep", effort="high", hold=False)
    assert out.of("error")[0][1] == {
        "type": "error",
        "code": "tenant_access_misconfigured",
        "message": "No route could answer.",
        "retryable": False,
    }
    assert len(rig.adapter.calls) == 1


async def test_an_open_breaker_skips_the_member_without_an_attempt() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("ok"))]})
    rig.world.health.force(OPUS, "us-east-1", "open")
    budget = AttemptBudget()
    out = await drive(rig, "opus-deep", effort="high", hold=False, budget=budget)
    assert [r.model for r in out.records] == [SOL] and budget.remaining == 2


async def test_every_member_skipped_ends_with_all_routes_unavailable() -> None:
    rig = make_rig({})
    rig.world.health.force(SOL, "us-east-1", "open")
    out = await drive(rig, "sol-fast", hold=False)
    assert out.types() == ["error"] and out.of("error")[0][1]["code"] == "all_routes_unavailable"
    assert out.first_ms is None and out.records == ()


async def test_race_members_do_not_spend_the_budget_and_all_failing_continues_at_the_default() -> None:
    rig = make_rig({SOL_A: [down(), Call(text_stream("ok"))], GLM_A: [down(after=0.2)]})
    budget = AttemptBudget()
    out = await drive(rig, "race-sol-glm", hold=False, budget=budget)
    assert [(r.route_id, r.model, r.outcome) for r in out.records] == [
        ("race-sol-glm", SOL, "failed"),
        ("race-sol-glm", GLM, "failed"),
        ("sol-fast", SOL, "committed"),
    ]
    assert budget.remaining == 1  # only the default-profile attempt spent budget


async def test_race_commit_picks_the_earliest_first_text() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("slow", first_s=0.9))], GLM_A: [Call(text_stream("fast", first_s=0.4))]})
    out = await drive(rig, "race-sol-glm", actions=[(1.0, commit())])
    committed = out.of("generation.committed")[0][1]["attempt_id"]
    winner = next(r for r in out.records if r.attempt_id == committed)
    assert winner.model == GLM and out.text() == "fast"
    assert out.first_ms == 400


async def test_a_model_outside_the_tenant_allowlist_is_skipped() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("ok"))]})
    snap = snapshot_from(patched_example({"tenants": {"t_acme": {"allowed_models": ALL_BUT_OPUS}}}))
    out = await drive(rig, "opus-deep", effort="high", hold=False, ctx=rig.ctx(snapshot=snap))
    assert [r.model for r in out.records] == [SOL]


async def test_a_member_whose_prompt_does_not_fit_its_context_window_is_skipped() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("ok"))]})
    snap = snapshot_from(patched_example({"models": {OPUS_A: {"context_window": 4096}}}))
    big = StaticPromptBuilder("x" * 4000)  # ~1,000 tokens + opus' 4,000 max tokens > 4,096
    out = await drive(
        rig, "opus-deep", effort="high", max_output_tokens=4000, hold=False, builder=big, ctx=rig.ctx(snapshot=snap)
    )
    assert [r.model for r in out.records] == [SOL]


async def test_a_tenant_access_failure_in_a_race_never_falls_back_to_the_default() -> None:
    rig = make_rig(
        {
            SOL_A: [down("AccessDeniedException", ErrorKind.TENANT_ACCESS)],
            GLM_A: [down("503", after=0.3)],
        }
    )
    out = await drive(rig, "race-sol-glm", hold=False)
    assert [r.outcome for r in out.records] == ["failed", "failed"]  # no sol-fast attempt afterwards
    assert out.of("error")[0][1]["code"] == "tenant_access_misconfigured"
