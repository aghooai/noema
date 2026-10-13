"""`FakeInputViewResolver`, `FakeMediaNegotiator`, `FakeMediaBudgetPlanner` and `FakeSandboxRunner`
(spec-02 §6, spec-07 §4-§5, ADR-002 §7).

Views (spec-06 §4): text/json parts are native text; an image is `native` for an image-capable model, else
`ocr_text` under `derive_for_text_members` or `skipped` under `skip_incapable_members`; a PDF is `native` for a
document-capable model, else `derived_text` (markdown) or `skipped`. An unreadable or not-ready file gives
`metadata_only` plus a `not_ready` degradation (never raises). Beyond the view budget (spec-07 §5, ruling R3) a cut
image falls back to its `ocr_text` when that is ready and non-empty, else it is omitted (`metadata_only`); a cut
document is omitted (the real resolver's `derived_text` row is not modelled); degradation `media_budget` to
`ocr_text` / `omitted`. `by_modality` keeps a cut part's pre-cut view while another part of the modality shows
something; a modality none of whose parts shows anything is `metadata_only` (IMPL-058).
The negotiator returns blocks for media parts only (the prompt builder renders text parts itself, spec-09 §5) and
applies the same capability rule per attempt (degradation `model_capability`; `attempt_id` is left None, the
generation engine stamps it). The budget planner keeps the first `max_images` images and `max_documents` documents
native (media_budget beyond), labels them `image-N` / `doc-N` (a well-formed unused given label is kept, spec-07
§4.1) and sets `file_id`; it does not model the §4.5 text allowance (`page_texts`, `text_layer_pages`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final, Literal

import anyio.lowlevel

from noema.config.models import DecisionMemberCfg, ModelCfg
from noema.core.errors import NoemaError
from noema.core.principal import Principal
from noema.core.protocols.media import (
    ContentPart,
    DecisionViewBudget,
    DerivativeKind,
    FileRef,
    InputView,
    MediaCaps,
    MediaPlan,
    PlannedPart,
    ViewKind,
    ViewPart,
)
from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxLimits
from noema.core.values import ContentBlock, Degradation, DocumentBlock, ImageBlock, Modality, TextBlock

from .media import FakeFileService

_ADEQUACY: Final[tuple[ViewKind, ...]] = ("native", "thumbnail", "ocr_text", "derived_text", "metadata_only", "skipped")


def _modality(part: ContentPart) -> Modality:
    if part.kind == "image":
        return "image"
    if part.kind == "file" and part.file is not None and (part.file.media_type or "").startswith("image/"):
        return "image"
    return "document" if part.kind == "file" else "text"


def _least(kinds: Sequence[ViewKind]) -> ViewKind:
    return max(kinds, key=_ADEQUACY.index) if kinds else "native"


class FakeInputViewResolver:
    """Implements `InputViewResolver` (see the module docstring)."""

    def __init__(self, files: FakeFileService) -> None:
        self._files = files

    async def resolve(
        self,
        member: DecisionMemberCfg,
        parts: Sequence[ContentPart],
        *,
        policy: str,
        budget: DecisionViewBudget,
        model: ModelCfg,
        principal: Principal,
    ) -> InputView:
        await anyio.lowlevel.checkpoint()
        views: list[ViewPart] = []
        degradations: list[Degradation] = []
        by_modality: dict[Modality, list[ViewKind]] = {}
        shown: set[Modality] = {"text"}
        images = documents = 0
        for part in parts:
            modality = _modality(part)
            if modality == "text":
                text = part.text if part.text is not None else str(part.json)
                views.append(ViewPart(part.index, part.label, "native", (TextBlock(text),)))
                by_modality.setdefault("text", []).append("native")
                continue
            over = (images >= budget.max_images) if modality == "image" else (documents >= budget.max_documents)
            images += modality == "image"
            documents += modality == "document"
            view, adequacy = await self._media_view(part, modality, policy, model, principal, over, degradations)
            views.append(view)
            by_modality.setdefault(modality, []).append(adequacy)
            if view.kind != "metadata_only":
                shown.add(modality)
        merged = {m: _least(kinds) if m in shown else "metadata_only" for m, kinds in by_modality.items()}
        return InputView(_least(list(merged.values())), merged, tuple(views), tuple(degradations))

    async def _media_view(
        self,
        part: ContentPart,
        modality: Modality,
        policy: str,
        model: ModelCfg,
        principal: Principal,
        over: bool,
        degradations: list[Degradation],
    ) -> tuple[ViewPart, ViewKind]:
        """The part's view and its adequacy ignoring a budget cut (`by_modality`)."""
        file = part.file
        fid = file.file_id if file is not None else None
        capable = modality in model.modalities
        if not capable and policy == "skip_incapable_members":
            return ViewPart(part.index, part.label, "skipped", ()), "skipped"
        kind: DerivativeKind
        view: ViewKind
        if modality == "image":
            kind, view = ("raster_1568", "native") if capable else ("ocr_text", "ocr_text")
        else:
            kind, view = ("pdf_sanitized", "native") if capable else ("markdown", "derived_text")
        if over:
            pre_cut: ViewKind = view if file is not None and file.status in ("ready", "partial") else "metadata_only"
            return await self._cut(part, modality, principal, degradations), pre_cut
        data = await self._read(principal, file, kind)
        if file is None or data is None:
            degradations.append(Degradation(modality, "metadata_only", "not_ready", fid, part.index))
            return ViewPart(part.index, part.label, "metadata_only", ()), "metadata_only"
        block: ContentBlock
        if view == "native" and modality == "image":
            block = ImageBlock("png", data, 1568, 1176)
        elif view == "native":
            block = DocumentBlock("pdf", part.label or f"doc-{part.index + 1}", data, file.pages or 1)
        else:
            block = TextBlock(data.decode("utf-8", "replace"))
        return ViewPart(part.index, part.label, view, (block,)), view

    async def _read(self, principal: Principal, file: FileRef | None, kind: DerivativeKind) -> bytes | None:
        if file is None or file.status not in ("ready", "partial"):
            return None
        try:
            return await self._files.read_derivative(principal, file.file_id, kind)
        except NoemaError:
            return None

    async def _cut(
        self, part: ContentPart, modality: Modality, principal: Principal, degradations: list[Degradation]
    ) -> ViewPart:
        """A budget-cut part (spec-07 §5 step 1, ruling R3): an image keeps its non-empty `ocr_text`, else omitted."""
        file = part.file
        fid = file.file_id if file is not None else None
        source = (file.media_type or file.declared_type) if file is not None else modality
        ocr = await self._read(principal, file, "ocr_text") if modality == "image" else None
        text = ocr.decode("utf-8", "replace") if ocr is not None else ""
        if text.strip():
            degradations.append(Degradation(source, "ocr_text", "media_budget", fid, part.index))
            return ViewPart(part.index, part.label, "ocr_text", (TextBlock(text),))
        degradations.append(Degradation(source, "omitted", "media_budget", fid, part.index))
        return ViewPart(part.index, part.label, "metadata_only", ())


class _Labeler:
    """`image-N` / `doc-N`, numbered per class in call order; a well-formed unused given label is kept (spec-07
    §4.1), anything else (a filename, a clash) gets the next free number."""

    def __init__(self) -> None:
        self._used: set[str] = set()
        self._next = {"image": 1, "doc": 1}

    def __call__(self, prefix: Literal["image", "doc"], given: str | None) -> str:
        if given is not None and re.fullmatch(rf"{prefix}-[1-9][0-9]{{0,5}}", given) and given not in self._used:
            self._used.add(given)
            return given
        n = self._next[prefix]
        while f"{prefix}-{n}" in self._used:
            n += 1
        self._next[prefix] = n + 1
        self._used.add(f"{prefix}-{n}")
        return f"{prefix}-{n}"


class FakeMediaBudgetPlanner:
    """Implements `MediaBudgetPlanner` (see the module docstring); pure."""

    def plan(
        self, turn_parts: Sequence[ContentPart], history_parts: Sequence[ContentPart], caps: MediaCaps
    ) -> MediaPlan:
        planned: list[PlannedPart] = []
        degradations: list[Degradation] = []
        images = documents = 0
        labels = _Labeler()
        for origin, parts in (("current", turn_parts), ("previous", history_parts)):
            for part in parts:
                modality = _modality(part)
                if modality == "text" or part.file is None:
                    continue
                is_image = modality == "image"
                native = images < caps.max_images if is_image else documents < caps.max_documents
                images += is_image
                documents += not is_image
                pages = part.file.pages or 1
                kind: Literal["image", "pdf", "markdown"] = (
                    "image" if is_image else "pdf" if part.file.media_type == "application/pdf" else "markdown"
                )
                planned.append(
                    PlannedPart(
                        part_index=part.index,
                        label=labels("image" if is_image else "doc", part.label),
                        origin="current" if origin == "current" else "previous",
                        kind=kind,
                        native=native,
                        text_pages=0 if is_image else pages,
                        text_cut=None,
                        page_images=tuple(range(1, pages + 1)) if kind == "pdf" else (),
                        file_id=part.file.file_id,
                    )
                )
                if not native:
                    degradations.append(Degradation(modality, "dropped", "media_budget", part.file.file_id, part.index))
        return MediaPlan(tuple(planned), tuple(degradations))


class FakeMediaNegotiator:
    """Implements `MediaNegotiator` (see the module docstring)."""

    def __init__(self, files: FakeFileService) -> None:
        self._files = files

    async def negotiate(
        self, alias: str, model: ModelCfg, parts: Sequence[ContentPart], plan: MediaPlan, *, principal: Principal
    ) -> tuple[tuple[ContentBlock, ...], tuple[Degradation, ...]]:
        await anyio.lowlevel.checkpoint()
        blocks: list[ContentBlock] = []
        degradations: list[Degradation] = []
        for part in parts:
            modality = _modality(part)
            file = part.file
            if modality == "text" or file is None:
                continue  # text parts are the prompt builder's (spec-09 §5)
            p = next((q for q in plan.parts if q.part_index == part.index and q.file_id in ("", file.file_id)), None)
            if p is None or not p.native:
                continue
            capable = modality in model.modalities
            kind: DerivativeKind = (
                ("raster_1568" if capable else "ocr_text")
                if modality == "image"
                else ("pdf_sanitized" if capable else "markdown")
            )
            try:
                data = await self._files.read_derivative(principal, file.file_id, kind)
            except NoemaError:
                degradations.append(
                    Degradation(modality, "dropped", "not_ready", file.file_id, part.index, model=model.id)
                )
                continue
            label = p.label
            if capable and modality == "image":
                blocks.append(ImageBlock("png", data, 1568, 1176))
            elif capable:
                blocks += [TextBlock(f"[{label}]"), DocumentBlock("pdf", label, data, file.pages or 1)]
            else:
                to = "ocr_text" if modality == "image" else "markdown"
                degradations.append(
                    Degradation(
                        file.media_type or modality, to, "model_capability", file.file_id, part.index, model=model.id
                    )
                )
                blocks.append(TextBlock(data.decode("utf-8", "replace")))
        return tuple(blocks), tuple(degradations)


class FakeSandboxRunner:
    """Implements `SandboxRunner`: answers each job from `script[kind]` (default `ok`, no outputs); `jobs` logs
    every (job, limits). Never raises for job failures."""

    def __init__(self, script: Mapping[str, ExtractResult] | None = None) -> None:
        self.script = dict(script or {})
        self.jobs: list[tuple[SandboxJob, SandboxLimits]] = []

    async def run(self, job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        await anyio.lowlevel.checkpoint()
        self.jobs.append((job, limits))
        return self.script.get(job.kind) or ExtractResult("ok", (), {}, None, 0, f"fake:{job.kind}")
