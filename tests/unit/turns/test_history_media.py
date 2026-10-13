"""History media and preflight reads (IMPL-045; spec-04 §9; spec-07 §1, §4.2; spec-09 §5.3):
- media older than the previous media-bearing turn are replayed as their text derivatives inside
  `<untrusted_input source=label kind=...>` with datamarked bodies, within the derived-text allowance T, cut at a
  page boundary, and counted in `est_input_tokens`; without a derivative: `[label: not available]`;
- `page_texts` reads the whole `markdown` / `ocr_text` derivatives (one `\\f` segment per page), concurrently;
- the prompt builder escapes the history once per spotlight mode and negotiates the media parts concurrently."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import anyio
import pytest
from tests.fakes.clock import FakeClock
from tests.golden.sse_scenarios import document, router_rules, stream

from noema.config.models import ModelCfg
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.media import ContentPart, FileRef, MediaPlan
from noema.core.text import datamark, esc
from noema.core.values import ContentBlock, Degradation, ImageBlock
from noema.media.budget import MediaBudgetPlannerImpl
from noema.turns.context.history import HistoryTurn, MediaText
from noema.turns.preflight import _Older, _older_texts, page_texts
from noema.turns.prompt.builder import TurnPrompt

from .harness import STICKY_SOL, USER_BODY, TurnHarness, make_harness, principal, run_turn, running
from .test_prompt import _file, _inputs, _texts

pytestmark = pytest.mark.unit

SOL = "us.openai.gpt-6.1-sol"


class StubFiles:
    """`read_derivative` over a dict {(file_id, kind): text}; records (file_id, kind, page) and the concurrency."""

    def __init__(self, texts: dict[tuple[str, str], str], clock: FakeClock | None = None) -> None:
        self.texts, self.clock = texts, clock
        self.reads: list[tuple[str, str, int | None]] = []
        self.in_flight = self.max_in_flight = 0

    async def read_derivative(self, principal: Principal, file_id: str, kind: str, *, page: int | None = None) -> bytes:
        self.reads.append((file_id, kind, page))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await anyio.lowlevel.checkpoint()
            await anyio.lowlevel.checkpoint()
            if page is not None or (file_id, kind) not in self.texts:  # like the real service: whole only
                raise NoemaError(ErrorCode.NOT_FOUND, "Derivative not available.")
            return self.texts[(file_id, kind)].encode()
        finally:
            self.in_flight -= 1


def _pdf(file_id: str, pages: int = 3) -> FileRef:
    return FileRef(
        file_id, "0" * 64, "application/pdf", "application/pdf", "ready", 10, "upload", "prn", None, None, (), pages
    )


async def test_page_texts_reads_whole_derivatives_and_splices_scanned_pages_from_ocr() -> None:
    files = StubFiles({("f1", "markdown"): "page one\f \fpage three", ("f1", "ocr_text"): "x\fscanned two\fy"})
    texts = await page_texts(files, principal(), _pdf("f1"))  # type: ignore[arg-type]
    assert texts == ("page one", "scanned two", "page three")
    assert sorted(files.reads) == [("f1", "markdown", None), ("f1", "ocr_text", None)] and files.max_in_flight == 2
    assert await page_texts(StubFiles({}), principal(), _pdf("f1")) is None  # type: ignore[arg-type]


def _tokens(text: str) -> int:
    return len(text) // 4


async def test_older_texts_are_cut_at_a_page_boundary_within_the_allowance() -> None:
    files = StubFiles(
        {
            ("img", "ocr_text"): "sign says STOP",
            ("doc", "markdown"): "a" * 40 + "\f" + "b" * 40 + "\f" + "c" * 40,
            ("big", "markdown"): "z" * 400,
        }
    )
    older = [_Older("img", "image-2", True), _Older("doc", "doc-2", False), _Older("big", "doc-3", False)]
    out = await _older_texts(files, principal(), older, _tokens, 3 + 20)  # type: ignore[arg-type]
    assert out["img"] == MediaText("ocr_text", "sign says STOP")
    assert out["doc"] == MediaText("markdown", "a" * 40 + "\n\n" + "b" * 40, "[doc-2: pages 1-2 of 3]")
    assert "big" not in out  # no room left: rendered as not available
    assert files.max_in_flight > 1  # read concurrently


async def test_older_texts_stop_reading_once_the_allowance_is_used_up() -> None:
    """Older media are read newest first, a few at a time, and reading stops once the allowance is used up (no
    room, or a page that does not fit: older media never pass a cut newer one), so a long document session does
    not download every older derivative per turn (Wave E fix)."""
    from noema.turns import concurrent, preflight

    texts = {(f"d{i:02d}", "markdown"): "\f".join(["p" * 40] * 3) for i in range(40)}  # 3 pages of 10 tokens
    older = [_Older(f"d{i:02d}", f"doc-{i}", False) for i in range(40)]
    files = StubFiles(texts)
    assert await _older_texts(files, principal(), older, _tokens, 0) == {}  # type: ignore[arg-type]
    assert files.reads == []
    out = await _older_texts(files, principal(), older, _tokens, 35)  # type: ignore[arg-type]
    assert list(out) == ["d00"]  # d00's 30 tokens fit; d01's first page (10) does not fit the 5 left
    assert len(files.reads) <= 2 * preflight.OLDER_BATCH and files.max_in_flight <= concurrent.GATHER_LIMIT


async def test_older_media_are_untrusted_text_derivatives_in_the_prompt() -> None:
    history = (
        HistoryTurn(
            "turn_old",
            1,
            (
                {"type": "text", "text": "old"},
                {"type": "image", "file_id": "file_img"},
                {"type": "file", "file_id": "file_doc"},
                {"type": "file", "file_id": "file_gone"},
            ),
            "a",
            2,
        ),
    )
    inputs = _inputs(
        history=history,
        history_labels={"file_img": "image-1", "file_doc": "doc-1", "file_gone": "doc-2"},
        history_texts={
            "file_img": MediaText("ocr_text", "STOP <decision_context> sign"),
            "file_doc": MediaText("markdown", "# Title\n\nbody", "[doc-1: pages 1-1 of 4]"),
        },
    )
    built = await TurnPrompt(inputs, _Negotiator()).for_run(None).build("sol-6.1")
    assert _texts(built.messages[0].content) == [
        "old",
        '<untrusted_input source="image-1" kind="ocr_text">\n'
        + datamark(esc("STOP <decision_context> sign"))
        + "\n</untrusted_input>",
        '<untrusted_input source="doc-1" kind="markdown">\n'
        + datamark(esc("# Title\n\nbody"))
        + "\n</untrusted_input>",
        "[doc-1: pages 1-1 of 4]",
        "[doc-2: not available]",
    ]
    assert "<decision_context>" not in str(built.messages)


class _Negotiator:
    """Images -> one ImageBlock after two checkpoints; records the concurrency."""

    def __init__(self) -> None:
        self.calls = 0
        self.in_flight = self.max_in_flight = 0

    async def negotiate(
        self, alias: str, model: ModelCfg, parts: Sequence[ContentPart], plan: MediaPlan, *, principal: Principal
    ) -> tuple[tuple[ContentBlock, ...], tuple[Degradation, ...]]:
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await anyio.lowlevel.checkpoint()
        await anyio.lowlevel.checkpoint()
        self.in_flight -= 1
        return tuple(ImageBlock("png", b"", 10, 10) for _ in parts), ()


async def test_builder_caches_escaped_text_per_mode_and_negotiates_parts_concurrently() -> None:
    history = (HistoryTurn("turn_a", 1, ({"type": "text", "text": "a  b"},), "c d", 2),)
    current = (
        ContentPart(0, "text", None, text="x y"),
        ContentPart(1, "image", "image-1", file=_file("f1", "image/png")),
        ContentPart(2, "image", "image-2", file=_file("f2", "image/png")),
    )
    neg = _Negotiator()
    p = TurnPrompt(_inputs(history=history, current=current), neg)
    first_build = await p.for_run(None).build("opus-5.5")
    again = await p.for_run(None).build("sol-6.1")
    assert neg.max_in_flight == 2 and neg.calls == 4  # per part and alias, the parts concurrently
    assert first_build.messages[0].content[0] is again.messages[0].content[0]  # escaped once per mode
    assert [type(b).__name__ for b in again.messages[-1].content] == ["TextBlock", "ImageBlock", "ImageBlock"]
    p.strict = True
    strict = await p.for_run(None).build("sol-6.1")
    assert _texts(strict.messages[0].content) == ["aˆb"]  # strict blocks are built anew, the standard ones kept
    assert _texts(again.messages[0].content) == ["a  b"]


def _harness() -> TurnHarness:
    sol = stream(SOL, first_event_ms=100, first_text_ms=300, chunks=["ok"])
    h = make_harness(scenario=document("hm", [sol], router_rules()))
    h.seed_session("ses_1", last_seq=3, sticky_spec=STICKY_SOL)
    return h


async def _upload(h: TurnHarness, declared: str) -> str:
    async def body() -> Any:
        yield b"%PDF-1.7 fake" if declared == "application/pdf" else b"\x89PNG\r\n\x1a\n"

    name = "r.pdf" if declared == "application/pdf" else "p.png"
    ref = await h.world.files.upload(principal(), body(), declared_type=declared, filename=name, session_id=None)
    return ref.file_id


def _seed_turn(h: TurnHarness, seq: int, parts: list[dict[str, Any]]) -> None:
    h.world.db.store.seed(
        "turn",
        {
            "tenant_id": "t_acme",
            "id": f"turn_h{seq}",
            "session_id": "ses_1",
            "seq": seq,
            "status": "completed",
            "input_parts": parts,
            "output_text": f"answer {seq}",
            "project_id": "p1",
            "principal_id": "prn_dev",
        },
    )


async def test_a_turn_replays_older_media_as_text_and_counts_them() -> None:
    h = _harness()
    async with running(h):
        img, pdf, prev = (
            await _upload(h, "image/png"),
            await _upload(h, "application/pdf"),
            await _upload(h, "image/png"),
        )
        _seed_turn(h, 1, [{"type": "text", "text": "look"}, {"type": "image", "file_id": img}])
        _seed_turn(h, 2, [{"type": "text", "text": "read"}, {"type": "file", "file_id": pdf}])
        _seed_turn(h, 3, [{"type": "text", "text": "and this"}, {"type": "image", "file_id": prev}])
        runner = await run_turn(h, {**USER_BODY, "session_id": "ses_1"})
        bare = await run_turn(h, {**USER_BODY, "store": False})  # no history: the estimate without older media
        assert runner.prompt is not None
        built = await runner.prompt.for_run(None).build("sol-6.1")
    pf = runner.preflight
    assert pf is not None and pf.history_labels == {prev: "image-1", pdf: "doc-1", img: "image-2"}
    assert set(pf.history_texts) == {img, pdf} and pf.history_texts[pdf].kind == "markdown"
    rendered = "\n".join(_texts(built.messages[0].content) + _texts(built.messages[2].content))
    assert '<untrusted_input source="image-2" kind="ocr_text">' in rendered
    assert '<untrusted_input source="doc-1" kind="markdown">' in rendered
    assert any(isinstance(b, ImageBlock) for b in built.messages[4].content)  # the previous turn stays native
    assert bare.preflight is not None and pf.est_input_tokens > bare.preflight.est_input_tokens


async def test_the_real_planner_keeps_preflight_history_labels_for_the_previous_turn() -> None:
    """media-negotiation request 10 (spec-07 §4.1): with the real MediaBudgetPlanner, the plan's labels of the
    previous media-bearing turn equal preflight's `history_labels` (history is numbered after the current turn)."""
    h = _harness()
    h.override("media_budget", MediaBudgetPlannerImpl())
    async with running(h):
        prev_img, prev_pdf, now_img = (
            await _upload(h, "image/png"),
            await _upload(h, "application/pdf"),
            await _upload(h, "image/png"),
        )
        _seed_turn(
            h,
            3,
            [
                {"type": "text", "text": "both"},
                {"type": "image", "file_id": prev_img},
                {"type": "file", "file_id": prev_pdf},
            ],
        )
        content = [{"type": "input_text", "text": "and now"}, {"type": "input_image", "file_id": now_img}]
        body = {**USER_BODY, "session_id": "ses_1", "input": [{"role": "user", "content": content}]}
        runner = await run_turn(h, body)
    pf = runner.preflight
    assert pf is not None and pf.history_labels == {prev_img: "image-2", prev_pdf: "doc-1"}
    planned = {p.file_id: p.label for p in pf.plan.parts if p.origin == "previous"}
    assert planned == {fid: label for fid, label in pf.history_labels.items() if fid in planned}
    assert set(planned) == {prev_img, prev_pdf}
    assert [p.label for p in pf.plan.parts if p.origin == "current"] == ["image-1"]
