"""GenerationEngine conformance (spec-02 §10, spec-04 §4.1, §6): hold/auto-commit, cancel reasons, the failure
walk with the shared AttemptBudget, race, and one `PromptBuilder.build` per attempt."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import anyio
from tests.fakes.generation import StaticPromptBuilder

from noema.core.clock import Deadline
from noema.core.protocols.generation import AttemptBudget, AttemptRecord, AttemptSpec, ProfileRun, TurnContext
from noema.core.values import JsonValue

from .kits import GenerationKit, NullReservation, principal
from .registry import kit_fixture

kit = kit_fixture("generation_engine")
SOL, OPUS, GLM = "us.openai.gpt-6.1-sol", "us.anthropic.claude-opus-5-5", "us.zai.glm-5.3"
SOL_STREAM = {"first_event_ms": 200, "first_text_ms": 720, "chunks": ["Sure, ", "here it is."]}
Ev = tuple[int, str, str, JsonValue]


def scenario(*rules: dict[str, Any]) -> dict[str, Any]:
    return {"scenario": "gen-suite", "seed": 5, "defaults": {"latency": {"fixed_ms": 0}}, "rules": list(rules)}


def ctx(kit: GenerationKit, budget_s: float = 120) -> TurnContext:
    return TurnContext(
        principal(),
        kit.snapshot.tenant_ref("t_acme"),
        kit.snapshot,
        Deadline(kit.clock.monotonic() + budget_s, kit.clock),
        NullReservation(),
        "turn_1",
        "ses_1",
        "0" * 32,
    )


async def drive(
    kit: GenerationKit,
    profile: str,
    *,
    hold: bool = True,
    commit_at_ms: float | None = None,
    cancel: tuple[float, Literal["spec_changed", "client_abort", "deadline"]] | None = None,
    auto: Literal["spec_agrees", "gate"] = "spec_agrees",
    budget: AttemptBudget | None = None,
    arm: bool = False,
    builder: StaticPromptBuilder | None = None,
    budget_s: float = 120,
    probe_ms: tuple[float, ...] = (),
    probes: list[bool] | None = None,
) -> tuple[list[Ev], tuple[AttemptRecord, ...], float | None]:
    """Run one profile; returns (ms, type, attempt id, reason|text|finish|code) events, the records and the
    first-text offset in ms. `probes` collects `run.upstream_started` at each `probe_ms`."""
    events: list[Ev] = []
    t0 = kit.clock.monotonic()
    first: list[float | None] = []

    def ms() -> int:
        return round((kit.clock.monotonic() - t0) * 1000)

    async def read(run: ProfileRun) -> None:
        async for ev in run.events():
            detail = next((ev[k] for k in ("reason", "text", "finish_reason", "code") if k in ev), None)
            events.append((ms(), str(ev["type"]), str(ev.get("attempt_id", "")), detail))

    async def control(run: ProfileRun) -> None:
        ft = await run.first_text()
        first.append(None if ft is None else round((ft - t0) * 1000))

    async def probe(run: ProfileRun) -> None:
        for at in probe_ms:
            await kit.clock.sleep(max(0.0, t0 + at / 1000 - kit.clock.monotonic()))
            assert probes is not None
            probes.append(run.upstream_started)

    async with kit.clock.autojump():
        cm = kit.engine.run(
            ctx(kit, budget_s),
            AttemptSpec(profile, None, None, "none"),
            builder or StaticPromptBuilder(),
            hold=hold,
            speculative=True,
            auto_commit=auto,
            failure_budget=budget or AttemptBudget(),
        )
        async with cm as run, anyio.create_task_group() as tg:
            tg.start_soon(read, run)
            tg.start_soon(control, run)
            tg.start_soon(probe, run)
            if arm:
                await run.commit("spec_agrees")
            if commit_at_ms is not None:
                await kit.clock.sleep(commit_at_ms / 1000)
                await run.commit("spec_agrees")
            if cancel is not None:
                await kit.clock.sleep(cancel[0] / 1000)
                await run.cancel(cancel[1])
            records = await run.result()
    return events, records, first[0] if first else None


def types(events: list[Ev]) -> list[str]:
    return [e[1] for e in events]


async def test_hold_buffers_until_commit(kit: GenerationKit) -> None:
    kit.use_scenario(scenario({"match": {"model": SOL}, "stream": SOL_STREAM}))
    events, records, first = await drive(kit, "sol-fast", commit_at_ms=930)
    assert first == 720
    assert types(events) == [
        "generation.started",
        "generation.committed",
        "generation.delta",
        "generation.delta",
        "generation.completed",
    ]
    assert events[1][0] == 930 and events[1][3] == "spec_agrees"
    assert "".join(str(e[3]) for e in events if e[1] == "generation.delta") == "Sure, here it is."
    assert events[-1][3] == "stop"
    [rec] = records
    assert (rec.outcome, rec.finish_reason, rec.cancel_reason, rec.model) == ("committed", "stop", None, SOL)
    assert rec.spec_signature == AttemptSpec("sol-fast", None, None, "none").signature()
    assert rec.first_text_at is not None  # the attempt's own first text, on the clock of started_at (C2b)
    assert round((rec.first_text_at - rec.started_at).total_seconds() * 1000) == 720


async def test_auto_commit_at_first_text(kit: GenerationKit) -> None:
    kit.use_scenario(scenario({"match": {"model": SOL}, "stream": SOL_STREAM}))
    events, _, _ = await drive(kit, "sol-fast", hold=False, auto="gate")
    committed = next(e for e in events if e[1] == "generation.committed")
    assert committed[0] == 720 and committed[3] == "gate"


async def test_armed_run_commits_at_first_text(kit: GenerationKit) -> None:
    kit.use_scenario(scenario({"match": {"model": SOL}, "stream": SOL_STREAM}))
    events, _, _ = await drive(kit, "sol-fast", arm=True)
    assert next(e for e in events if e[1] == "generation.committed")[0] == 720


async def test_spec_changed_cancel(kit: GenerationKit) -> None:
    kit.use_scenario(scenario({"match": {"model": SOL}, "stream": SOL_STREAM}))
    events, [rec], _ = await drive(kit, "sol-fast", cancel=(930, "spec_changed"))
    assert types(events) == ["generation.started", "generation.cancelled"] and events[1][3] == "spec_changed"
    assert (rec.outcome, rec.cancel_reason) == ("cancelled", "spec_changed")
    assert rec.usage.tokens_estimated or rec.usage.output_tokens >= 0


async def test_client_abort_after_commit_keeps_the_text(kit: GenerationKit) -> None:
    long = {**SOL_STREAM, "chunks": ["a"] * 40, "tokens_per_s": 10}
    kit.use_scenario(scenario({"match": {"model": SOL}, "stream": long}))
    events, [rec], _ = await drive(kit, "sol-fast", hold=False, cancel=(2000, "client_abort"))
    assert types(events)[-1] == "generation.cancelled" and events[-1][3] == "client_abort"
    assert "generation.completed" not in types(events)
    assert (rec.outcome, rec.finish_reason) == ("committed", "cancelled")


async def test_upstream_started_flips_at_the_first_upstream_event(kit: GenerationKit) -> None:
    # spec-04 §3 `arrived`: MessageStart @200, first text @720 (G04: during_holdback once it is True)
    kit.use_scenario(scenario({"match": {"model": SOL}, "stream": SOL_STREAM}))
    probes: list[bool] = []
    await drive(kit, "sol-fast", commit_at_ms=930, probe_ms=(0, 150, 250, 800), probes=probes)
    assert probes == [False, False, True, True]


async def test_an_adapter_deadline_after_commit_is_t19(kit: GenerationKit) -> None:
    # the adapter's timeout:deadline on a committed attempt: no frame, finish_reason cancelled (spec-04 §4.1 T19)
    kit.use_scenario(
        scenario({"match": {"model": SOL}, "stream": SOL_STREAM, "fault": {"kind": "stall_after", "n": 1}})
    )
    events, [rec], _ = await drive(kit, "sol-fast", hold=False, budget_s=10)
    assert types(events) == ["generation.started", "generation.committed", "generation.delta"]
    assert (rec.outcome, rec.finish_reason, rec.error_code) == ("committed", "cancelled", None)


async def test_deadline_cancel_emits_no_frame(kit: GenerationKit) -> None:
    kit.use_scenario(scenario({"match": {"model": SOL}, "stream": SOL_STREAM}))
    events, [rec], _ = await drive(kit, "sol-fast", cancel=(300, "deadline"))
    assert types(events) == ["generation.started"]
    assert (rec.outcome, rec.cancel_reason) == ("cancelled", "deadline")


async def test_fallback_walk_spends_the_budget(kit: GenerationKit) -> None:
    kit.use_scenario(
        scenario(
            {"match": {"model": OPUS}, "fault": {"kind": "http_5xx", "status": 503}, "latency_ms": 150},
            {"match": {"model": SOL}, "stream": SOL_STREAM},
        )
    )
    budget = AttemptBudget()
    builder = StaticPromptBuilder()
    events, records, _ = await drive(kit, "opus-deep", hold=False, budget=budget, builder=builder)
    assert types(events)[:3] == ["generation.started", "generation.cancelled", "generation.started"]
    assert events[1][3] == "failed" and events[1][0] == 150
    assert [r.outcome for r in records] == ["failed", "committed"] and budget.remaining == 1
    assert [r.route_id for r in records] == ["opus-deep", "opus-deep"]
    assert builder.builds == ["opus-5.5", "sol-6.1"]  # one PromptBuilder.build per attempt
    assert records[0].first_text_at is None and records[1].first_text_at is not None


async def test_exhausted_budget_ends_with_an_error(kit: GenerationKit) -> None:
    kit.use_scenario(scenario({"match": {"model": OPUS}, "fault": {"kind": "http_5xx", "status": 503}}))
    events, records, first = await drive(kit, "opus-deep", hold=False, budget=AttemptBudget(0))
    assert types(events) == ["generation.started", "generation.cancelled", "error"]
    assert events[-1][3] == "all_routes_unavailable" and first is None
    assert [r.outcome for r in records] == ["failed"]


async def test_default_profile_is_the_last_resort(kit: GenerationKit) -> None:
    kit.use_scenario(
        scenario(
            {"match": {"model": OPUS}, "fault": {"kind": "http_5xx", "status": 503}},
            {"match": {"model": SOL, "nth": 1}, "fault": {"kind": "http_5xx", "status": 500}},
            {"match": {"model": SOL}, "stream": SOL_STREAM},
        )
    )
    _, records, _ = await drive(kit, "opus-deep", hold=False)
    assert [(r.route_id, r.outcome) for r in records] == [
        ("opus-deep", "failed"),
        ("opus-deep", "failed"),
        ("sol-fast", "committed"),
    ]


async def test_tenant_access_never_falls_back(kit: GenerationKit) -> None:
    kit.use_scenario(
        scenario(
            {"match": {"model": OPUS}, "fault": {"kind": "access_denied"}},
            {"match": {"model": SOL}, "stream": SOL_STREAM},
        )
    )
    events, records, _ = await drive(kit, "opus-deep", hold=False)
    assert types(events)[-1] == "error" and events[-1][3] == "tenant_access_misconfigured"
    assert len(records) == 1


async def test_reasoning_only_is_a_pre_commit_failure(kit: GenerationKit) -> None:
    kit.use_scenario(
        scenario(
            {"match": {"model": GLM}, "fault": {"kind": "reasoning_only"}, "stream": {"reasoning": {"deltas": 5}}},
            {"match": {"model": SOL}, "stream": SOL_STREAM},
        )
    )
    _, records, _ = await drive(kit, "glm-reason", hold=False)
    assert [r.outcome for r in records] == ["failed", "committed"]


async def test_content_filter_is_an_empty_committable_answer(kit: GenerationKit) -> None:
    kit.use_scenario(scenario({"match": {"model": SOL}, "fault": {"kind": "refusal"}, "stream": SOL_STREAM}))
    events, [rec], first = await drive(kit, "sol-fast", hold=False)
    assert first is not None
    assert types(events) == ["generation.started", "generation.committed", "generation.completed"]
    assert events[-1][3] == "content_filter" and rec.finish_reason == "content_filter"


async def test_post_commit_stall(kit: GenerationKit) -> None:
    kit.use_scenario(
        scenario({"match": {"model": SOL}, "stream": SOL_STREAM, "fault": {"kind": "stall_after", "n": 1}})
    )
    events, [rec], _ = await drive(kit, "sol-fast", hold=False)
    assert types(events)[-1] == "error" and events[-1][3] == "upstream_stalled"
    assert events[-1][0] == 720 + 30_000
    assert (rec.outcome, rec.error_code) == ("committed", "upstream_stalled")


async def test_race_winner_and_lost_race(kit: GenerationKit) -> None:
    kit.use_scenario(
        scenario(
            {"match": {"model": SOL}, "stream": SOL_STREAM},
            {"match": {"model": GLM}, "stream": {"first_event_ms": 100, "first_text_ms": 1200, "chunks": ["glm"]}},
        )
    )
    events, records, first = await drive(kit, "race-sol-glm", commit_at_ms=930)
    starts = [e for e in events if e[1] == "generation.started"]
    assert len(starts) == 2 and first == 720
    committed = next(i for i, e in enumerate(events) if e[1] == "generation.committed")
    assert events[committed + 1][1] == "generation.cancelled" and events[committed + 1][3] == "lost_race"
    by_model: Mapping[str, AttemptRecord] = {r.model: r for r in records}
    assert by_model[SOL].outcome == "committed" and by_model[GLM].cancel_reason == "lost_race"
