"""T-FSM-1 (spec-04 §13): the real engine driven through every row T1-T19 of spec-04 §4.1 with scripted adapters
on a FakeClock, plus "no transition out of a terminal state". The pure table is in test_fsm_table.py."""

from __future__ import annotations

from noema.core.errors import ErrorKind
from noema.core.protocols.adapters import MessageStart, Stop, StopReason, StreamEvent, TextDelta, UsageEvent
from noema.core.protocols.generation import AttemptBudget
from noema.core.values import UsageRecord
from noema.generation import StreamingProfileRun
from noema.generation.fsm import St

from .rig import GLM, OPUS, SOL, cancel, commit, drive, make_rig
from .scripted import Call, error_at, failure, reasoning, text_stream

SOL_A = "sol-6.1"
USAGE = UsageRecord(input_tokens=40, output_tokens=12, reasoning_tokens=2)


async def test_t1_a1_starts_on_the_first_member_the_breaker_admits() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("hi"))]})
    rig.world.health.force(OPUS, "us-east-1", "open")
    out = await drive(rig, "opus-deep", effort="high", actions=[(1.0, commit())])
    [(ms, started)] = out.of("generation.started")
    assert (ms, started["model"], started["route_id"], started["speculative"]) == (0, SOL, "opus-deep", True)
    assert [c.request.model_alias for c in rig.adapter.calls] == [SOL_A]


async def test_t2_fallback_attempt_carries_the_run_flag() -> None:
    rig = make_rig(
        {
            "opus-5.5": [Call(open_error=failure(ErrorKind.RETRYABLE, "503"), open_s=0.15)],
            SOL_A: [Call(text_stream("ok"))],
        }
    )
    out = await drive(rig, "opus-deep", speculative=False, hold=False)
    starts = out.of("generation.started")
    assert [(ms, ev["model"], ev["speculative"]) for ms, ev in starts] == [(0, OPUS, False), (150, SOL, False)]
    assert out.types()[:3] == ["generation.started", "generation.cancelled", "generation.started"]


async def test_t3_upstream_events_move_pending_to_streaming() -> None:
    rig = make_rig({SOL_A: [Call([(0.1, MessageStart(at=0.0)), reasoning(0.2, 40)], hang=True)]})
    seen: list[tuple[St, bool]] = []

    async def look(run: StreamingProfileRun) -> None:
        seen.append((run.attempts[0].state, run.upstream_started))

    out = await drive(rig, actions=[(0.05, look), (0.15, look), (0.25, look), (0.3, cancel("spec_changed"))])
    assert seen == [(St.P, False), (St.S, True), (St.S, True)]
    assert out.records[0].usage.reasoning_tokens == 9  # ceil(40 chars / 4.61, Sol's), counted but never forwarded
    assert out.of("generation.delta") == []


async def test_upstream_started_ignores_usage_and_errors_and_counts_a_textless_stop() -> None:
    # ProfileRun.upstream_started (spec-02 §10): MessageStart, reasoning, text or a stop; usage and error do not count
    opus = Call([(0.1, UsageEvent(USAGE)), error_at(0.2, ErrorKind.RETRYABLE, "500")])
    rig = make_rig({"opus-5.5": [opus], SOL_A: [Call([(0.2, Stop("end_turn"))], hang=True)]})
    seen: list[bool] = []

    async def look(run: StreamingProfileRun) -> None:
        seen.append(run.upstream_started)

    actions = [(0.15, look), (0.3, look), (0.5, look), (0.6, cancel("spec_changed"))]
    out = await drive(rig, "opus-deep", effort="high", actions=actions)
    assert seen == [False, False, True]  # Opus usage @100, Opus error @200, Sol (opened @200) stop @400
    assert [r.model for r in out.records] == [OPUS, SOL]


async def test_t4_first_text_buffers_and_resolves_first_text() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("a", "b", first_s=0.72), hang=True)]})
    states: list[St] = []

    async def look(run: StreamingProfileRun) -> None:
        states.append(run.attempts[0].state)

    out = await drive(rig, actions=[(0.8, look), (0.9, cancel("spec_changed"))])
    assert out.first_ms == 720 and states == [St.B]
    assert out.types() == ["generation.started", "generation.cancelled"]  # nothing shown before commit


async def test_t4_textless_content_filter_stop_is_an_empty_committable_answer() -> None:
    rig = make_rig({SOL_A: [Call([(0.1, MessageStart(at=0)), (0.3, Stop("content_filter"))])]})
    out = await drive(rig, actions=[(0.5, commit())])
    assert out.first_ms == 300
    assert out.types() == ["generation.started", "generation.committed", "generation.completed"]
    assert out.of("generation.completed")[0][1]["finish_reason"] == "content_filter"
    assert out.records[0].finish_reason == "content_filter"


async def test_t5_more_text_stop_and_usage_stay_buffered() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("a", "b", "c", usage=USAGE))]})
    out = await drive(rig, actions=[(2.0, commit())])
    assert [ms for ms, _ in out.events][1:] == [2000] * 5  # committed, 3 deltas, completed: all at the commit
    assert out.text() == "abc"
    assert out.of("generation.completed")[0][1]["usage"]["output_tokens"] == 12  # the held provider usage


async def test_t6_commit_flushes_the_buffer_then_streams_live() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("a", "b", "c", "d", first_s=0.72, every_s=0.2))]})
    out = await drive(rig, actions=[(0.93, commit())])
    deltas = out.of("generation.delta")
    assert [(ms, ev["seq"], ev["text"]) for ms, ev in deltas] == [
        (930, 0, "a"),
        (930, 1, "b"),
        (1120, 2, "c"),
        (1320, 3, "d"),
    ]
    assert out.of("generation.committed")[0][1]["reason"] == "spec_agrees"


async def test_t7_an_armed_run_commits_at_the_first_text() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("x", first_s=0.72))]})
    out = await drive(rig, actions=[(0.1, commit())])
    assert out.of("generation.committed")[0][0] == 720


async def test_t7_hold_false_commits_with_the_auto_commit_reason() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("x", first_s=0.72))]})
    out = await drive(rig, hold=False, auto="gate")
    [(ms, ev)] = out.of("generation.committed")
    assert (ms, ev["reason"]) == (720, "gate")


async def test_t8_holdback_deadline_commit() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("x", "y", first_s=0.3))]})
    out = await drive(rig, actions=[(0.7, commit("holdback_deadline"))])
    [(ms, ev)] = out.of("generation.committed")
    assert (ms, ev["reason"]) == (700, "holdback_deadline")


async def test_t8_hold_cap_with_no_held_text_arms_the_run() -> None:
    # first_text() is the run's: Opus's text at 300 ms resolves it, Opus then fails before any commit. The
    # orchestrator's hold cap (first_text + max_hold) is one-shot and ignores a later S1 (decision (b), spec-04 §3),
    # so the cap arms the run and the fallback commits at its own first text, never later.
    opus = Call([(0.05, MessageStart(at=0)), (0.3, TextDelta("doomed")), error_at(0.5, ErrorKind.RETRYABLE, "500")])
    rig = make_rig({"opus-5.5": [opus], SOL_A: [Call(text_stream("real", first_s=1.2))]})
    out = await drive(rig, "opus-deep", effort="high", actions=[(0.7, commit("holdback_deadline"))])
    assert out.first_ms == 300
    [(ms, ev)] = out.of("generation.committed")
    assert (ms, ev["reason"], ev["attempt_id"]) == (1700, "holdback_deadline", out.records[1].attempt_id)
    assert out.text() == "real"


async def test_t9_spec_changed_cancels_every_live_attempt_of_the_run() -> None:
    rig = make_rig(
        {SOL_A: [Call(text_stream("s", first_s=0.72), hang=True)], "glm-5.3": [Call(text_stream("g"), hang=True)]}
    )
    out = await drive(rig, "race-sol-glm", actions=[(0.5, cancel("spec_changed"))])
    cancelled = out.of("generation.cancelled")
    assert [(ms, ev["reason"]) for ms, ev in cancelled] == [(500, "spec_changed"), (500, "spec_changed")]
    assert [(r.outcome, r.cancel_reason) for r in out.records] == [("cancelled", "spec_changed")] * 2
    assert all(c.closed for c in rig.adapter.calls)  # the provider streams were closed, never drained


async def test_t10_pre_commit_failures_fail_the_attempt() -> None:
    cases = [
        Call([error_at(0.3, ErrorKind.RETRYABLE, "ModelStreamErrorException")]),
        Call(open_error=failure(ErrorKind.RETRYABLE, "503")),
        Call([error_at(5.0, ErrorKind.RETRYABLE, "timeout:first_event")]),
        Call([(0.1, MessageStart(at=0)), error_at(8.0, ErrorKind.RETRYABLE, "timeout:first_answer")]),
        Call([(0.1, TextDelta("partial")), error_at(30.1, ErrorKind.RETRYABLE, "timeout:idle")]),
        Call([(0.1, MessageStart(at=0)), (0.2, Stop("end_turn"))]),  # empty answer
        Call([reasoning(0.1, 400), (0.9, Stop("max_tokens"))]),  # reasoning-only
        Call([(0.1, TextDelta("no stop"))]),  # the stream ended without a Stop
    ]
    for call in cases:
        rig = make_rig({SOL_A: [call]})
        out = await drive(rig, budget=AttemptBudget(0))
        assert out.types() == ["generation.started", "generation.cancelled", "error"], call
        assert out.of("generation.cancelled")[0][1]["reason"] == "failed"
        assert out.of("error")[0][1]["code"] == "all_routes_unavailable"
        [rec] = out.records
        assert (rec.outcome, rec.cancel_reason, rec.error_code) == ("failed", "failed", "all_routes_unavailable")
        assert out.text() == ""  # nothing of a failed attempt is ever shown


async def test_t11_the_walk_starts_the_next_member() -> None:
    rig = make_rig({"opus-5.5": [Call([error_at(0.2, ErrorKind.RETRYABLE, "500")])], SOL_A: [Call(text_stream("ok"))]})
    budget = AttemptBudget()
    out = await drive(rig, "opus-deep", effort="high", hold=False, budget=budget)
    assert [r.outcome for r in out.records] == ["failed", "committed"] and budget.remaining == 1
    assert rig.world.retry_budget.spent == 0  # a fallback to another model is not a retry (R6)


async def test_t12_the_walk_ends_with_the_code_of_the_last_failure() -> None:
    expected = {
        ErrorKind.RETRYABLE: ("all_routes_unavailable", True),
        ErrorKind.THROTTLED: ("all_routes_unavailable", True),
        ErrorKind.FATAL: ("internal_error", True),
        ErrorKind.INVALID_REQUEST: ("internal_error", True),  # ERROR_TABLE: retryable
        ErrorKind.TENANT_ACCESS: ("tenant_access_misconfigured", False),
    }
    for kind, (code, retryable) in expected.items():
        rig = make_rig({SOL_A: [Call(open_error=failure(kind, "x"))] * 3})
        out = await drive(rig, "sol-fast", hold=False)
        [(_, err)] = out.of("error")
        assert (err["code"], err["retryable"]) == (code, retryable), kind
        assert out.types()[-1] == "error"


async def test_t13_race_losers_end_lost_race_at_the_commit() -> None:
    rig = make_rig(
        {
            SOL_A: [Call(text_stream("sol", first_s=0.72))],
            "glm-5.3": [Call(text_stream("glm", first_s=1.2), hang=True)],
        }
    )
    out = await drive(rig, "race-sol-glm", actions=[(0.93, commit())])
    starts = out.of("generation.started")
    assert [ev["model"] for _, ev in starts] == [SOL, GLM]
    i = out.types().index("generation.committed")
    assert out.types()[i + 1] == "generation.cancelled" and out.events[i + 1][1]["reason"] == "lost_race"
    assert {r.model: (r.outcome, r.cancel_reason) for r in out.records} == {
        SOL: ("committed", None),
        GLM: ("cancelled", "lost_race"),
    }


async def test_t14_deltas_after_commit_are_live_with_increasing_seq() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("a", "b", "c", first_s=0.1, every_s=0.5))]})
    out = await drive(rig, hold=False)
    assert [(ms, ev["seq"]) for ms, ev in out.of("generation.delta")] == [(100, 0), (600, 1), (1100, 2)]


async def test_t15_completion_maps_the_stop_reason_and_carries_usage() -> None:
    stops: list[tuple[StopReason, str]] = [("end_turn", "stop"), ("stop_sequence", "stop"), ("max_tokens", "length")]
    for stop, finish in stops:
        rig = make_rig({SOL_A: [Call(text_stream("a", stop=stop, usage=USAGE))]})
        out = await drive(rig, hold=False)
        [(_, done)] = out.of("generation.completed")
        assert done["finish_reason"] == finish and out.types()[-1] == "generation.completed"
        assert done["usage"]["tokens_estimated"] is False and done["usage"]["input_tokens"] == 40
        [rec] = out.records
        assert (rec.outcome, rec.finish_reason, rec.usage) == ("committed", finish, USAGE)
    # usage before the stop (Mantle order) is fine too
    mantle: list[tuple[float, StreamEvent]] = [(0.1, TextDelta("a")), (0.2, UsageEvent(USAGE)), (0.3, Stop("end_turn"))]
    rig = make_rig({SOL_A: [Call(mantle)]})
    out = await drive(rig, hold=False)
    assert out.records[0].usage == USAGE


async def test_t16_idle_timeout_after_commit_is_upstream_stalled() -> None:
    events: list[tuple[float, StreamEvent]] = [
        (0.72, TextDelta("a")),
        (0.82, TextDelta("b")),
        error_at(30.82, ErrorKind.RETRYABLE, "timeout:idle"),
    ]
    rig = make_rig({SOL_A: [Call(events)]})
    out = await drive(rig, actions=[(0.93, commit())])
    assert out.types()[-1] == "error"
    [(ms, err)] = out.of("error")
    assert (ms, err["code"], err["retryable"]) == (30820, "upstream_stalled", True)
    [rec] = out.records
    assert (rec.outcome, rec.error_code, rec.finish_reason) == ("committed", "upstream_stalled", None)
    assert out.text() == "ab"  # the partial text is kept, no fallback after commit
    assert len(rig.adapter.calls) == 1


async def test_t17_other_errors_after_commit_map_by_kind() -> None:
    expected = {
        ErrorKind.RETRYABLE: "all_routes_unavailable",
        ErrorKind.FATAL: "internal_error",
        ErrorKind.TENANT_ACCESS: "tenant_access_misconfigured",
    }
    for kind, code in expected.items():
        rig = make_rig({SOL_A: [Call([(0.1, TextDelta("a")), error_at(0.2, kind, "ModelStreamErrorException")])]})
        out = await drive(rig, hold=False)
        assert out.of("error")[0][1]["code"] == code
        assert (out.records[0].outcome, out.records[0].error_code) == ("committed", code)
        assert len(rig.adapter.calls) == 1


async def test_t18_client_abort_before_and_after_commit() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("a", first_s=0.72), hang=True)]})
    out = await drive(rig, actions=[(0.5, cancel("client_abort"))])
    assert out.of("generation.cancelled")[0][1]["reason"] == "client_abort"
    assert (out.records[0].outcome, out.records[0].cancel_reason) == ("cancelled", "client_abort")

    rig = make_rig({SOL_A: [Call(text_stream(*"abcdef", first_s=0.1, every_s=0.5), hang=True)]})
    out = await drive(rig, hold=False, actions=[(1.2, cancel("client_abort"))])
    assert out.types()[-1] == "generation.cancelled" and "generation.completed" not in out.types()
    assert out.text() == "abc"
    [rec] = out.records
    assert (rec.outcome, rec.finish_reason, rec.cancel_reason) == ("committed", "cancelled", None)


async def test_t19_deadline_ends_attempts_without_a_frame() -> None:
    # explicit cancel("deadline"), uncommitted
    rig = make_rig({SOL_A: [Call(text_stream("a", first_s=0.72), hang=True)]})
    out = await drive(rig, actions=[(0.3, cancel("deadline"))])
    assert out.types() == ["generation.started"]
    assert (out.records[0].outcome, out.records[0].cancel_reason) == ("cancelled", "deadline")
    # ctx.deadline passes while committed and streaming: the engine's own watcher, no frame
    rig = make_rig({SOL_A: [Call(text_stream(*"abcdef", first_s=0.1, every_s=0.5), hang=True)]})
    out = await drive(rig, hold=False, ctx=rig.ctx(deadline_s=1.2))
    assert out.types()[-1] == "generation.delta"
    [rec] = out.records
    assert (rec.outcome, rec.finish_reason) == ("committed", "cancelled")
    # an adapter timeout:deadline is T19 too: no walk, no frame
    rig = make_rig({"opus-5.5": [Call([error_at(2.0, ErrorKind.RETRYABLE, "timeout:deadline")])]})
    out = await drive(rig, "opus-deep", effort="high", ctx=rig.ctx(deadline_s=2.0))
    assert out.types() == ["generation.started"] and len(rig.adapter.calls) == 1
    assert out.records[0].cancel_reason == "deadline"


async def test_no_transition_out_of_a_terminal_state() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("a", first_s=0.1))]})
    out = await drive(
        rig,
        hold=False,
        actions=[(1.0, cancel("client_abort")), (1.1, commit()), (1.2, cancel("spec_changed"))],
    )
    assert out.types() == ["generation.started", "generation.committed", "generation.delta", "generation.completed"]
    [rec] = out.records
    assert (rec.outcome, rec.finish_reason) == ("committed", "stop")
    assert out.run.attempts[0].state is St.DONE


async def test_an_error_after_the_stop_completes_the_answer() -> None:
    events = [*text_stream("a", first_s=0.1), error_at(0.5, ErrorKind.RETRYABLE, "ModelStreamErrorException")]
    rig = make_rig({SOL_A: [Call(events)]})
    out = await drive(rig, hold=False)
    assert out.types()[-1] == "generation.completed" and "error" not in out.types()
    held = make_rig({SOL_A: [Call(events)]})
    out = await drive(held, actions=[(1.0, commit())])
    assert out.types()[-1] == "generation.completed" and out.records[0].finish_reason == "stop"
