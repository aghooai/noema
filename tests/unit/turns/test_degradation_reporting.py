"""M2 (06 §10, spec-10 M2; spec-07 §4.3, IMPL-015): every degradation reason Phase 1 produces is reported on the
right event with the right `attempt_id`, in ONE turn through the orchestrator with the real generation engine,
MediaNegotiator and MediaBudgetPlanner (on the fake file service). opus-deep at effort xhigh, Opus failing before any
byte, so Sol serves as attempt 2:
- `preflight.completed` (no `attempt_id`, no `model`): `media_budget` (21 usable images for 20 slots),
  `active_content_removed` (a sanitized PDF) and `rejected` (a rejected file under `allow_partial_files`);
- each attempt's `generation.started` (its own `attempt_id`, its Bedrock id as `model`): `param_unsupported`
  (xhigh is not an Opus or Sol effort), `not_ready` and `extraction_failed` (an image whose raster is pending /
  failed), `size_limit` (Opus: a PDF over 4.5 MB) and `model_capability` (Sol: no document input);
- `route.selected` carries attempt 1's list, `turn.degradations` the preflight's.
`policy` is in the contract but has no Phase 1 producer (no code path emits it)."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from typing import Any, get_args

import pytest
from tests.golden.sse_scenarios import document, router_rules, stream

from noema.core.values import DegradationReason, JsonValue
from noema.generation.engine import StreamingGenerationEngine
from noema.media.budget import MediaBudgetPlannerImpl
from noema.media.negotiation import MediaNegotiatorImpl

from .harness import STICKY_SOL, USER_BODY, TurnHarness, make_harness, principal, run_turn, running

pytestmark = pytest.mark.unit

OPUS, SOL = "us.anthropic.claude-opus-5-5", "us.openai.gpt-6.1-sol"
PRODUCED = frozenset(get_args(DegradationReason)) - {"policy"}
PREFLIGHT = {"media_budget", "active_content_removed", "rejected"}


def _harness() -> TurnHarness:
    opus_down = stream(OPUS, first_event_ms=50, first_text_ms=200, chunks=["x"], fault={"kind": "http_5xx"})
    sol = stream(SOL, first_event_ms=100, first_text_ms=300, chunks=["The ", "answer."])
    h = make_harness(scenario=document("m2", [opus_down, sol], router_rules()), hold_decisions_until_cancel=True)
    sticky: dict[str, JsonValue] = {
        **STICKY_SOL,
        "generation_profile": "opus-deep",
        "effort": "xhigh",
        "max_output_tokens": 4000,
        "modalities": ["text", "image", "document"],  # IMPL-014: a sticky spec applies to its own modality set
    }
    h.seed_session("ses_1", last_seq=1, sticky_spec=sticky)
    w = h.world
    h.override("negotiator", MediaNegotiatorImpl(w.files))
    h.override("media_budget", MediaBudgetPlannerImpl())
    h.override(
        "generation",
        StreamingGenerationEngine(
            clock=w.clock,
            ids=w.ids,
            adapters=w.adapters,
            broker=w.broker,
            health=w.health,
            retry_budget=w.retry_budget,
            estimator=w.estimator,
            cost=w.cost,
            telemetry=w.telemetry,
            rng=random.Random(7),
        ),
    )
    return h


async def _upload(h: TurnHarness, declared: str, name: str) -> str:
    async def body() -> AsyncIterator[bytes]:
        yield b"%PDF-1.7 fake" if declared == "application/pdf" else b"\x89PNG\r\n\x1a\n" + name.encode()

    ref = await h.world.files.upload(principal(), body(), declared_type=declared, filename=name, session_id=None)
    return ref.file_id


async def _media(h: TurnHarness) -> list[dict[str, Any]]:
    files = h.world.files
    big_pdf = await _upload(h, "application/pdf", "big.pdf")
    files.set_derivative(big_pdf, "pdf_sanitized", bytes=5_000_000)  # > MAX_DOCUMENT_BYTES (4.5 MB)
    sanitized = await _upload(h, "application/pdf", "macro.pdf")
    files.set_active_content(sanitized, ("javascript",))
    rejected = await _upload(h, "application/pdf", "bad.pdf")
    files.set_status(rejected, "rejected", rejection={"code": "type_mismatch"})
    pending = await _upload(h, "image/png", "pending.png")
    files.set_derivative(pending, "raster_1568", status="pending")
    failed = await _upload(h, "image/png", "failed.png")
    files.set_derivative(failed, "raster_1568", status="failed")
    images = [await _upload(h, "image/png", f"img{i}.png") for i in range(21)]  # 21 usable for 20 slots: one cut
    parts = [{"type": "input_file", "file_id": f} for f in (big_pdf, sanitized, rejected)]
    return parts + [{"type": "input_image", "file_id": f} for f in (pending, failed, *images)]


def _of(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == kind]


async def test_every_degradation_reason_is_on_the_right_event_with_its_attempt() -> None:
    h = _harness()
    async with running(h):
        content = [{"type": "input_text", "text": "Compare doc-1 with the images."}, *await _media(h)]
        body = {
            **USER_BODY,
            "session_id": "ses_1",
            "allow_partial_files": True,
            "input": [{"role": "user", "content": content}],
        }
        runner = await run_turn(h, body)
    assert runner.result is not None and runner.result.status == "completed"
    events = h.events(runner.turn_id)
    [preflight] = _of(events, "preflight.completed")
    a1, a2 = _of(events, "generation.started")
    [selected] = _of(events, "route.selected")
    assert (a1["model"], a2["model"]) == (OPUS, SOL)

    assert {d["reason"] for d in preflight["degradations"]} == PREFLIGHT
    assert all("attempt_id" not in d and "model" not in d for d in preflight["degradations"])
    per_attempt = {
        a1["attempt_id"]: {"param_unsupported", "not_ready", "extraction_failed", "size_limit"},
        a2["attempt_id"]: {"param_unsupported", "not_ready", "extraction_failed", "model_capability"},
    }
    for started in (a1, a2):
        assert {d["reason"] for d in started["degradations"]} == per_attempt[started["attempt_id"]], started
        assert all(d["attempt_id"] == started["attempt_id"] for d in started["degradations"])
        assert all(d["model"] == started["model"] for d in started["degradations"])
    assert selected["attempt_id"] == a1["attempt_id"] and selected["degradations"] == a1["degradations"]
    assert runner.result.turn["degradations"] == preflight["degradations"]

    seen = {d["reason"] for e in (preflight, a1, a2) for d in e["degradations"]}
    assert seen == PRODUCED  # every reason a Phase 1 code path emits, each on its event
    effort = next(d for d in a1["degradations"] if d["reason"] == "param_unsupported")
    assert (effort["from"], effort["to"]) == ("effort:xhigh", "effort:high")
