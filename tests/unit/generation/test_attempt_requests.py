"""Per-attempt requests (spec-04 §5.1, §6; IMPL-015, IMPL-084): one PromptBuilder.build per attempt (media
renegotiated per model), degradations stamped with the attempt and model, the effort override and its
param_unsupported degradation, the max_tokens formula, per-(model, effort) stream timeouts capped by ctx.deadline,
the in-adapter connect retry flag, the pinned snapshot, and one Reservation.consume per attempt."""

from __future__ import annotations

from collections.abc import AsyncIterator

from tests.contract_internal.kits import principal
from tests.fakes.config import patched_example, snapshot_from
from tests.fakes.generation import StaticPromptBuilder

from noema.config.models import route_timeouts_v1
from noema.core.errors import ErrorKind
from noema.core.protocols.generation import AssembledPrompt
from noema.core.protocols.media import ContentPart, MediaPlan, PlannedPart
from noema.core.values import Degradation, DocumentBlock, Message

from .rig import SOL, Rig, drive, make_rig
from .scripted import Call, failure, text_stream

SOL_A, OPUS_A, GLM_A = "sol-6.1", "opus-5.5", "glm-5.3"


class PerModelBuilder:
    """A PromptBuilder that renegotiates media per model: a document becomes markdown for Sol."""

    def __init__(self) -> None:
        self.builds: list[str] = []
        self.base = StaticPromptBuilder("Summarize doc-1.").prompt

    async def build(self, alias: str) -> AssembledPrompt:
        self.builds.append(alias)
        degradations: tuple[Degradation, ...] = ()
        if alias == SOL_A:
            degradations = (Degradation("document", "markdown", "model_capability", file_id="file_1", part_index=0),)
        return AssembledPrompt(self.base.system, self.base.messages, None, degradations)


async def test_media_is_renegotiated_for_every_attempt_and_degradations_name_the_attempt() -> None:
    rig = make_rig({OPUS_A: [Call(open_error=failure(ErrorKind.RETRYABLE, "503"))], SOL_A: [Call(text_stream("ok"))]})
    builder = PerModelBuilder()
    out = await drive(rig, "opus-deep", effort="high", hold=False, builder=builder)
    assert builder.builds == [OPUS_A, SOL_A]
    first, second = (ev for _, ev in out.of("generation.started"))
    assert first["degradations"] == []
    assert second["degradations"] == [
        {
            "from": "document",
            "to": "markdown",
            "reason": "model_capability",
            "file_id": "file_1",
            "part_index": 0,
            "attempt_id": second["attempt_id"],
            "model": SOL,
        }
    ]
    assert rig.telemetry.counter("aghoo_degradations_total", reason="model_capability", model=SOL) == 1


async def test_an_effort_the_member_does_not_accept_keeps_its_own_and_degrades() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("a")), Call(text_stream("b"))]})
    out = await drive(rig, "sol-fast", effort="none", hold=False)  # sol accepts low..max only
    [(_, started)] = out.of("generation.started")
    assert started["degradations"] == [
        {
            "from": "effort:none",
            "to": "effort:low",
            "reason": "param_unsupported",
            "attempt_id": started["attempt_id"],
            "model": SOL,
        }
    ]
    assert rig.adapter.calls[0].request.effort == "low"
    out = await drive(rig, "sol-fast", effort="high", hold=False)  # accepted: the override wins
    assert rig.adapter.calls[1].request.effort == "high" and out.of("generation.started")[0][1]["degradations"] == []


async def test_max_tokens_formula() -> None:
    snap = snapshot_from(patched_example({"models": {SOL_A: {"reasoning_reserve_tokens": 500}}}))
    cases = [
        ("sol-fast", None, SOL_A, 4096 + 500),  # no spec, member or cap value: GEN_DEFAULT_MAX_TOKENS + reserve
        ("sol-fast", 2000, SOL_A, 2000 + 500),  # the spec's max_output_tokens
        ("glm-reason", None, GLM_A, 4096),  # the member's 4096, within glm's cap
        ("glm-reason", 1000, GLM_A, 1000),
    ]
    for profile, spec_max, alias, expected in cases:
        rig = make_rig({alias: [Call(text_stream("x"))]})
        await drive(rig, profile, max_output_tokens=spec_max, hold=False, ctx=rig.ctx(snapshot=snap))
        assert rig.adapter.calls[0].request.max_tokens == expected, (profile, spec_max)


async def test_requests_use_the_pinned_snapshot_retry_default_and_per_model_timeouts() -> None:
    rig = make_rig({OPUS_A: [Call(open_error=failure(ErrorKind.RETRYABLE, "503"))], SOL_A: [Call(text_stream("ok"))]})
    ctx = rig.ctx(deadline_s=60)
    await drive(rig, "opus-deep", effort="high", hold=False, ctx=ctx)
    opus, sol = rig.adapter.calls
    for call in (opus, sol):
        assert call.request.call_kind == "generation" and call.request.retry == "default"
        assert call.request.snapshot is ctx.snapshot and call.deadline is ctx.deadline
        assert call.request.temperature is None and not call.request.logprobs
    assert opus.timeouts == route_timeouts_v1(OPUS_A, "high")
    assert sol.timeouts == route_timeouts_v1(SOL_A, "high")  # the permissive row, not sol-fast's 8 s
    assert rig.reservation.consumed == [("generation", OPUS_A), ("generation", SOL_A)]


async def test_sol_fast_gets_its_fast_route_row() -> None:
    rig = make_rig({SOL_A: [Call(text_stream("ok"))]})
    await drive(rig, "sol-fast", hold=False)
    timeouts = rig.adapter.calls[0].timeouts
    assert (timeouts.first_event_s, timeouts.first_answer_s) == (5, 8)
    assert rig.adapter.calls[0].request.model_alias == SOL_A


class NegotiatingPrompt:
    """A PromptBuilder over the fake MediaNegotiator: the turn's parts negotiated for each attempt's model."""

    def __init__(self, rig: Rig, parts: tuple[ContentPart, ...], plan: MediaPlan) -> None:
        self.rig, self.parts, self.plan = rig, parts, plan

    async def build(self, alias: str) -> AssembledPrompt:
        snap = self.rig.snap
        blocks, degradations = await self.rig.world.negotiator.negotiate(
            alias, snap.model(alias), self.parts, self.plan, principal=principal()
        )
        return AssembledPrompt(("system",), (Message("user", blocks),), None, degradations)


async def test_media_negotiator_runs_per_attempt_on_a_fallback_from_opus_to_sol() -> None:
    rig = make_rig({OPUS_A: [Call(open_error=failure(ErrorKind.RETRYABLE, "503"))], SOL_A: [Call(text_stream("ok"))]})

    async def body() -> AsyncIterator[bytes]:
        yield b"%PDF-1.7 test"

    pdf = await rig.world.files.upload(
        principal(), body(), declared_type="application/pdf", filename="r.pdf", session_id=None
    )
    parts = (ContentPart(0, "text", None, text="Summarize doc-1."), ContentPart(1, "file", "doc-1", file=pdf))
    plan = MediaPlan((PlannedPart(1, "doc-1", "current", "pdf", True, 0, None, ()),), ())
    out = await drive(rig, "opus-deep", effort="high", hold=False, builder=NegotiatingPrompt(rig, parts, plan))
    opus_req, sol_req = (c.request for c in rig.adapter.calls)
    assert any(isinstance(b, DocumentBlock) for m in opus_req.messages for b in m.content)
    assert not any(isinstance(b, DocumentBlock) for m in sol_req.messages for b in m.content)
    first, second = (ev for _, ev in out.of("generation.started"))
    assert first["degradations"] == []
    [deg] = second["degradations"]
    assert (deg["reason"], deg["attempt_id"], deg["model"]) == ("model_capability", second["attempt_id"], SOL)
