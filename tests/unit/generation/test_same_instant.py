"""Same-instant interleavings (spec-04 §4.1): another task ends an attempt (lost_race at a commit, ctx.deadline,
an orchestrator cancel) while that attempt's next adapter event is already scheduled. anyio does not deliver the
cancel to a task whose wake-up is pending, so the attempt's task still receives the event; the engine must drop it
instead of driving a terminal attempt (IllegalTransition would crash the run's task group). Also the cancel order
after a commit: spec_changed is a no-op there (T9 is P/S/B only), so a later client_abort still ends the answer."""

from __future__ import annotations

from noema.generation.fsm import St

from .rig import cancel, commit, drive, make_rig
from .scripted import Call, text_stream

SOL_A, GLM_A = "sol-6.1", "glm-5.3"


async def test_race_members_with_the_same_first_text_instant_commit_one_attempt() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("sol", first_s=0.3))], GLM_A: [Call(text_stream("glm", first_s=0.3))]})
    out = await drive(rig, "race-sol-glm", hold=False)
    [(ms, committed)] = out.of("generation.committed")
    assert ms == 300 and committed["reason"] == "spec_agrees"
    winner = committed["attempt_id"]
    [(_, lost)] = out.of("generation.cancelled")
    assert lost["reason"] == "lost_race" and lost["attempt_id"] != winner
    assert {ev["attempt_id"] for _, ev in out.of("generation.delta")} == {winner}
    assert out.text() in ("sol", "glm")
    assert sorted(r.outcome for r in out.records) == ["cancelled", "committed"]


async def test_race_commit_at_the_instant_of_a_held_loser_text() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("s1", first_s=0.3))], GLM_A: [Call(text_stream("g1", "g2", first_s=0.5))]})
    out = await drive(rig, "race-sol-glm", actions=[(0.6, commit())])
    assert out.text() == "s1"
    assert [ev["reason"] for _, ev in out.of("generation.cancelled")] == ["lost_race"]


async def test_deadline_at_the_instant_of_a_committed_delta() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("a", "b", "c", first_s=0.5, every_s=0.5), hang=True)]})
    out = await drive(rig, hold=False, ctx=rig.ctx(deadline_s=1.0))
    [rec] = out.records
    assert (rec.outcome, rec.finish_reason, rec.cancel_reason) == ("committed", "cancelled", None)
    assert out.run.attempts[0].state is St.ERR  # T19 on a committed attempt: no frame
    assert out.of("generation.cancelled") == [] and out.of("generation.completed") == []
    assert out.text() in ("a", "ab") and all(ms <= 1000 for ms, _ in out.events)


async def test_deadline_at_the_instant_of_the_first_held_text() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("a", first_s=1.0), hang=True)]})
    out = await drive(rig, ctx=rig.ctx(deadline_s=1.0))
    [rec] = out.records
    assert (rec.outcome, rec.cancel_reason) == ("cancelled", "deadline")
    assert out.types() == ["generation.started"]


async def test_cancel_at_the_instant_of_a_delta() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("a", "b", first_s=0.3, every_s=0.2), hang=True)]})
    out = await drive(rig, actions=[(0.5, cancel("spec_changed"))])
    assert out.types() == ["generation.started", "generation.cancelled"]
    assert out.records[0].cancel_reason == "spec_changed"


async def test_spec_changed_after_commit_keeps_the_answer_and_a_later_client_abort_still_applies() -> None:
    rig = make_rig({SOL_A: [Call(text_stream(*"abcdefghij", first_s=0.1, every_s=0.5), hang=True)]})
    out = await drive(
        rig,
        hold=False,
        actions=[(1.0, cancel("spec_changed")), (1.2, cancel("client_abort"))],
        ctx=rig.ctx(deadline_s=20),
    )
    assert out.text() == "abc"  # T9 covers P/S/B only: the committed attempt streamed on until the abort
    assert [(ms, ev["reason"]) for ms, ev in out.of("generation.cancelled")] == [(1200, "client_abort")]
    [rec] = out.records
    assert (rec.outcome, rec.finish_reason, rec.cancel_reason) == ("committed", "cancelled", None)
    assert out.events[-1][0] == 1200
