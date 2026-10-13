"""Preflight (spec-04 §9; IMPL-074; spec-07 §3.2, §4.1): file waits, rejected files, inline images, labels and
page texts, and the ingress file check (step 8, HTTP 409)."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any

import pytest
from tests.golden.sse_scenarios import document, router_rules, stream

from noema.core.errors import ErrorCode, NoemaError

from .harness import STICKY_SOL, USER_BODY, TurnHarness, first, make_harness, principal, run_turn, running, types_of

pytestmark = pytest.mark.unit

SOL = "us.openai.gpt-6.1-sol"


def _h() -> TurnHarness:
    sol = stream(SOL, first_event_ms=100, first_text_ms=300, chunks=["ok"])
    h = make_harness(scenario=document("p", [sol], router_rules()))
    h.seed_session("ses_1", last_seq=0, sticky_spec=STICKY_SOL)
    return h


async def _upload(
    h: TurnHarness, declared: str = "application/pdf", name: str = "Report.pdf", *, ready: bool = True
) -> str:
    async def body() -> AsyncIterator[bytes]:
        yield b"%PDF-1.7 fake" if declared == "application/pdf" else b"\x89PNG\r\n\x1a\n"

    h.world.files.auto_ready = ready
    ref = await h.world.files.upload(principal(), body(), declared_type=declared, filename=name, session_id=None)
    h.world.files.auto_ready = True
    return ref.file_id


def _body(*parts: dict[str, Any], **extra: Any) -> dict[str, Any]:
    content = [{"type": "input_text", "text": "Summarize"}, *parts]
    return {**USER_BODY, "session_id": "ses_1", "input": [{"role": "user", "content": content}], **extra}


async def test_file_not_ready_is_409_at_ingress_without_wait() -> None:
    h = _h()
    async with running(h):
        fid = await _upload(h, ready=False)
        with pytest.raises(NoemaError) as err:
            await h.start(_body({"type": "input_file", "file_id": fid}))
    assert err.value.code is ErrorCode.FILE_NOT_READY and err.value.param == "input[0].content[1]"
    assert h.world.db.store.rows("session")[0]["active_turn_id"] is None


async def test_wait_for_files_waits_then_runs_with_labels_and_page_texts() -> None:
    h = _h()

    async def ready_later(runner: Any) -> None:
        await h.clock.sleep(0.5)
        h.world.files.set_status(fid, "ready")

    async with running(h):
        fid = await _upload(h, ready=False)
        img = await _upload(h, "image/png", "Photo.png")
        body = _body(
            {"type": "input_file", "file_id": fid}, {"type": "input_image", "file_id": img}, wait_for_files=True
        )
        runner = await run_turn(h, body, during=ready_later)
    _, pf = first(h, runner, "preflight.completed")
    assert pf["modalities"] == ["text", "image", "document"]
    assert runner.preflight is not None
    labels = {p.index: p.label for p in runner.preflight.parts}
    assert labels == {0: None, 1: "doc-1", 2: "image-1"}
    doc = runner.preflight.parts[1]
    assert doc.page_texts is not None and len(doc.page_texts) == 2  # the fake PDF has 2 pages
    created_at = first(h, runner, "turn.created")[0]
    assert round(-created_at) >= 500  # preflight.completed (t_d0) came after the 0.5 s wait
    rows = h.world.db.store.rows("turn_file")
    assert sorted((r["part_index"], r["file_id"]) for r in rows) == [(1, fid), (2, img)]
    stored = h.world.db.store.rows("turn")[0]["input_parts"]
    assert stored[1] == {"type": "file", "file_id": fid} and "Report" not in str(stored)


async def test_rejected_file_fails_or_is_dropped_with_allow_partial_files() -> None:
    h = _h()

    async def reject(runner: Any) -> None:
        await h.clock.sleep(0.2)
        h.world.files.set_status(fid, "rejected", rejection={"code": "type_mismatch"})

    async with running(h):
        fid = await _upload(h, ready=False)
        runner = await run_turn(h, _body({"type": "input_file", "file_id": fid}, wait_for_files=True), during=reject)
    # P-SSE-1: exactly one route.selected (spec0's, no attempt) even when preflight fails; no decision.completed
    assert types_of(h, runner) == ["turn.created", "route.selected", "error", "turn.completed"]
    assert first(h, runner, "route.selected")[1]["speculative"] is True
    assert first(h, runner, "error")[1]["code"] == "file_rejected"
    h2 = _h()

    async def reject2(runner: Any) -> None:
        await h2.clock.sleep(0.2)
        h2.world.files.set_status(fid2, "rejected", rejection={"code": "type_mismatch"})

    async with running(h2):
        fid2 = await _upload(h2, ready=False)
        body = _body({"type": "input_file", "file_id": fid2}, wait_for_files=True, allow_partial_files=True)
        runner2 = await run_turn(h2, body, during=reject2)
    _, pf = first(h2, runner2, "preflight.completed")
    assert [d["reason"] for d in pf["degradations"]] == ["rejected"] and pf["degradations"][0]["file_id"] == fid2
    assert runner2.result is not None and runner2.result.status == "completed"


async def test_a_pdf_whose_active_content_was_removed_is_one_preflight_degradation() -> None:
    """spec-09 §6.3 step 5: a non-empty `pdf_sanitized.active_content` gives one `active_content_removed` (from the
    file's media type to pdf_sanitized) on `preflight.completed`; a clean PDF gives none."""
    h = _h()
    async with running(h):
        fid = await _upload(h)
        clean = await _upload(h, name="Clean.pdf")
        h.world.files.set_active_content(fid, ("open_action", "javascript"))
        body = _body({"type": "input_file", "file_id": clean}, {"type": "input_file", "file_id": fid})
        runner = await run_turn(h, body)
    _, pf = first(h, runner, "preflight.completed")
    removed = [d for d in pf["degradations"] if d["reason"] == "active_content_removed"]
    expected = {"from": "application/pdf", "to": "pdf_sanitized", "reason": "active_content_removed"}
    assert removed == [{**expected, "file_id": fid, "part_index": 2}]
    assert runner.result is not None and runner.result.status == "completed"


async def test_wait_for_files_ends_at_the_deadline() -> None:
    h = _h()
    async with running(h):
        fid = await _upload(h, ready=False)
        runner = await run_turn(h, _body({"type": "input_file", "file_id": fid}, wait_for_files=True), deadline_s=1.0)
    assert first(h, runner, "error")[1]["code"] == "deadline_exceeded"
    assert runner.result is not None and runner.result.status == "failed"


async def test_inline_images_are_ingested_and_oversized_ones_fail_in_preflight() -> None:
    h = _h()
    small = base64.b64encode(b"\x89PNG\r\n\x1a\nimg").decode()
    big = "A" * 5_100_000  # > 3,750,000 decoded bytes (the gate's own check runs at ingress in the API path)
    async with running(h):
        ok = await run_turn(h, _body({"type": "input_image", "image_url": f"data:image/png;base64,{small}"}))
        bad = await run_turn(h, _body({"type": "input_image", "image_url": f"data:image/png;base64,{big}"}))
    assert ok.preflight is not None and ok.preflight.parts[1].label == "image-1"
    assert ok.preflight.parts[1].file is not None and ok.preflight.parts[1].file.origin == "inline"
    assert first(h, bad, "error")[1]["code"] == "image_too_large"
