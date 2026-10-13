"""`GenerationEngine.run` lifecycle (spec-02 §10, spec-11 §6): fail-fast credentials, Phase-2 modes, the attempt
watchdog at `__aexit__` (stuck attempts), exceptions passing through unchanged, records in start order, and a
deadline that has already passed."""

from __future__ import annotations

import anyio
import pytest
from tests.fakes.generation import StaticPromptBuilder

from noema.core.errors import ErrorCode, ErrorKind, NoemaError, TenantAccessMisconfigured
from noema.core.protocols.generation import AttemptBudget, AttemptSpec

from .rig import SOL, drive, make_rig
from .scripted import Call, failure, text_stream

SOL_A = "sol-6.1"


async def test_credentials_fail_fast_before_any_attempt() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("never"))]})
    rig.world.broker.fail("t_acme", "access_denied")
    with pytest.raises(TenantAccessMisconfigured) as caught:
        async with rig.engine.run(
            rig.ctx(),
            AttemptSpec("sol-fast", None, None, "none"),
            StaticPromptBuilder(),
            hold=True,
            speculative=True,
            failure_budget=AttemptBudget(),
        ):
            pytest.fail("the body must not run")
    assert caught.value.code is ErrorCode.TENANT_ACCESS_MISCONFIGURED and rig.adapter.calls == []


async def test_best_of_n_is_not_available_in_phase_1() -> None:
    rig = make_rig({})
    with pytest.raises(NoemaError) as caught:
        async with rig.engine.run(
            rig.ctx(),
            AttemptSpec("best-of-sol-opus", None, None, "none"),
            StaticPromptBuilder(),
            hold=False,
            speculative=False,
            failure_budget=AttemptBudget(),
        ):
            pytest.fail("the body must not run")
    assert caught.value.code is ErrorCode.FEATURE_NOT_AVAILABLE


async def test_leaving_the_run_with_live_attempts_cancels_them_as_stuck() -> None:
    rig = make_rig({SOL_A: [Call(text_stream(*"abcdefgh", first_s=0.1, every_s=1.0), hang=True)]})
    async with rig.clock.autojump():
        async with rig.engine.run(
            rig.ctx(),
            AttemptSpec("sol-fast", None, None, "none"),
            StaticPromptBuilder(),
            hold=False,
            speculative=True,
            failure_budget=AttemptBudget(),
        ) as run:
            await rig.clock.sleep(2.5)
        records = await run.result()
    [rec] = records
    assert (rec.outcome, rec.finish_reason) == ("committed", "cancelled")
    assert rig.telemetry.counter("aghoo_stuck_attempts_total") == 1
    assert rig.adapter.calls[0].closed


async def test_an_uncommitted_stuck_attempt_ends_cancelled_deadline() -> None:
    rig = make_rig({SOL_A: [Call(hang=True)]})
    async with rig.clock.autojump():
        async with rig.engine.run(
            rig.ctx(),
            AttemptSpec("sol-fast", None, None, "none"),
            StaticPromptBuilder(),
            hold=True,
            speculative=True,
            failure_budget=AttemptBudget(),
        ) as run:
            await rig.clock.sleep(1)
        [rec] = await run.result()
        events = [ev async for ev in run.events()]
    assert (rec.outcome, rec.cancel_reason, rec.usage.tokens_estimated) == ("cancelled", "deadline", True)
    assert [ev["type"] for ev in events] == ["generation.started"]  # no frame for the watchdog's cancel


async def test_an_exception_in_the_body_passes_through_unchanged() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("a"), hang=True)]})

    class Boom(Exception):
        pass

    async def body() -> None:
        async with (
            rig.clock.autojump(),
            rig.engine.run(
                rig.ctx(),
                AttemptSpec("sol-fast", None, None, "none"),
                StaticPromptBuilder(),
                hold=True,
                speculative=True,
                failure_budget=AttemptBudget(),
            ),
        ):
            await rig.clock.sleep(0.5)
            raise Boom

    with pytest.raises(Boom):
        await body()


async def test_records_are_in_start_order_with_the_spec_signature_and_times() -> None:
    rig = make_rig(
        {
            "opus-5.5": [Call(open_error=failure(ErrorKind.RETRYABLE, "503"), open_s=0.15)],
            SOL_A: [Call(text_stream("ok"))],
        }
    )
    out = await drive(rig, "opus-deep", effort="high", hold=False)
    spec = AttemptSpec("opus-deep", "high", None, "none")
    assert [r.model for r in out.records] == ["us.anthropic.claude-opus-5-5", SOL]
    assert all(r.spec_signature == spec.signature() for r in out.records)
    first, second = out.records
    assert first.started_at <= first.ended_at <= second.started_at <= second.ended_at
    assert (first.ended_at - first.started_at).total_seconds() == pytest.approx(0.15)


async def test_first_text_is_none_when_nothing_answered() -> None:
    rig = make_rig({SOL_A: [Call(open_error=failure(ErrorKind.RETRYABLE, "503"))]})
    out = await drive(rig, budget=AttemptBudget(0))
    assert out.first_ms is None and out.types()[-1] == "error"


async def test_a_passed_deadline_starts_nothing() -> None:
    rig = make_rig({})
    out = await drive(rig, ctx=rig.ctx(deadline_s=0))
    assert out.events == [] and out.records == () and out.first_ms is None


async def test_commit_and_cancel_after_the_run_ended_are_no_ops() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("a"))]})
    out = await drive(rig, hold=False)
    await out.run.commit("spec_agrees")
    await out.run.cancel("client_abort")
    assert await out.run.result() == out.records
    with anyio.fail_after(1):
        assert [ev async for ev in out.run.events()] == []  # already drained and closed
