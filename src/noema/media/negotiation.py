"""`MediaNegotiator`: one attempt's media blocks for one model (spec-07 §4.1-§4.3, 02 §8.3), applying the turn's
`MediaPlan` by `models.<alias>.modalities` (never by alias):

- text, image, document: `[image-N]` + ImageBlock(raster_1568); `[doc-N]` + DocumentBlock(pdf_sanitized) plus the
  planned page images of scanned pages;
- text, image: images the same; a PDF is its Markdown (the plan's cut) plus every planned page image
  (`model_capability`);
- text: an image is its `ocr_text`; a PDF is its Markdown with the OCR segments of scanned pages spliced in (`ocr_text`
  alone if the Markdown is empty); both `model_capability`.

Text derivatives are `<untrusted_input source="{label}" kind="{markdown|ocr_text}">` blocks with `datamark(esc(...))`
bodies (standard spotlighting, IMPL-086); a missing, failed or empty one gives `[{label}: not available]`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from noema.core.protocols.media import (
    CONVERSE_CAPS_V1,
    ContentPart,
    DerivativeKind,
    DerivativeRef,
    FileRef,
    FileService,
    MediaPlan,
    PlannedPart,
)
from noema.core.text import datamark, esc
from noema.core.values import ContentBlock, Degradation, DegradationReason, DocumentBlock, TextBlock

from .budget import (
    Labeler,
    MediaClass,
    classify,
    kept_text,
    media_type,
    page_image_b64,
    page_images_within,
    ready,
    scanned_pages,
)
from .views import (
    DerivativeReader,
    ReadKey,
    TextKind,
    derivative_problem,
    file_problem,
    has_content,
    image_block,
    splice,
)

if TYPE_CHECKING:
    from noema.config.models import ModelCfg
    from noema.core.principal import Principal

TextSource = Literal["ocr", "markdown", "spliced"]
MAX_IMAGE_BYTES: Final = CONVERSE_CAPS_V1.max_image_bytes
MAX_DOCUMENT_BYTES: Final = CONVERSE_CAPS_V1.max_document_bytes
_TEXT_KINDS: Final[dict[TextSource, tuple[DerivativeKind, ...]]] = {
    "ocr": ("ocr_text",),
    "markdown": ("markdown",),
    "spliced": ("markdown", "ocr_text"),
}


def untrusted_block(label: str, kind: TextKind, text: str) -> TextBlock:
    """A text derivative wrapped for the prompt (spec-09 §5 item 3); the tags themselves are not datamarked."""
    return TextBlock(f'<untrusted_input source="{label}" kind="{kind}">\n{datamark(esc(text))}\n</untrusted_input>')


@dataclass(slots=True)
class _Job:
    """One media part's representation for this attempt, decided before any read."""

    part: ContentPart
    label: str
    cls: MediaClass | None
    file: FileRef | None
    planned: PlannedPart | None
    reason: DegradationReason | None = None  # this attempt's degradation reason, if any
    native_ref: DerivativeRef | None = None
    text: TextSource | None = None
    page_ref: DerivativeRef | None = None
    page_images: tuple[int, ...] = ()
    dropped: int = 0  # planned page images not sent: over the per-image limit or the part's reservation


def _match(plan: MediaPlan, part: ContentPart, cls: MediaClass) -> PlannedPart | None:
    """The planned part of `part`: same index and class, then the same `file_id` (entries without one, "", match any
    file), then the same label; so history parts whose index repeats a current part's still find their own entry.
    When the label does not tell two entries apart (the same file at the same index in both turns), the one with the
    fewest binaries wins, so two parts never send more than the plan together."""
    found = [p for p in plan.parts if p.part_index == part.index and p.kind == cls]
    file_id = part.file.file_id if part.file is not None else None
    exact = [p for p in found if p.file_id and p.file_id == file_id]
    pool = exact or [p for p in found if not p.file_id]
    labelled = next((p for p in pool if p.label == part.label), None)
    if labelled is not None or len(pool) <= 1:
        return labelled or (pool[0] if pool else None)
    return min(reversed(pool), key=lambda p: (p.native, len(p.page_images)))


def _prepare(job: _Job, modalities: frozenset[str]) -> None:
    file, planned = job.file, job.planned
    problem = file_problem(file)
    if file is None or problem is not None:
        job.reason = problem or "not_ready"
        return
    if planned is None:  # a part the plan does not know: nothing is sent (the plan bounds every request)
        return
    if job.cls == "image":
        if "image" not in modalities:
            job.reason = "model_capability"
        else:
            job.reason = _native_problem(file, "raster_1568", MAX_IMAGE_BYTES)
            if job.reason is None and planned.native:
                job.native_ref = ready(file, "raster_1568")
                return
        job.text = "ocr"  # budget cut (reported by the plan), capability, size or readiness: the OCR text
        return
    if job.cls == "markdown":
        job.text = "markdown"
        return
    page_ref = ready(file, "page_images")
    if "document" not in modalities:
        job.reason = "model_capability"
    else:
        job.reason = _native_problem(file, "pdf_sanitized", MAX_DOCUMENT_BYTES)
        if job.reason is None and planned.native:
            job.native_ref, job.page_ref = ready(file, "pdf_sanitized"), page_ref
            job.page_images = tuple(p for p in planned.page_images if p in scanned_pages(page_ref))
            return
    if "image" in modalities:  # the image-only row (also a document model's oversized or budget-cut PDF)
        job.text, job.page_ref = "markdown", page_ref
        job.page_images = planned.page_images if page_ref is not None else ()
    else:
        job.text = "spliced"


def _native_problem(file: FileRef, kind: DerivativeKind, max_bytes: int) -> DegradationReason | None:
    problem = derivative_problem(file, kind)
    ref = ready(file, kind)
    if problem is None and ref is not None and (ref.bytes or 0) > max_bytes:
        return "size_limit"
    return problem


def _keys(job: _Job) -> list[ReadKey]:
    file = job.file
    if file is None:
        return []
    fid = file.file_id
    keys: list[ReadKey] = [] if job.native_ref is None else [(fid, job.native_ref.kind, None)]
    texts = _TEXT_KINDS.get(job.text) if job.text is not None else None
    keys += [(fid, kind, None) for kind in texts or () if ready(file, kind) is not None]
    keys += [(fid, "page_images", page) for page in job.page_images]
    return keys


def _text(job: _Job, reader: DerivativeReader) -> tuple[TextKind, str] | None:
    """The kept text of the part (the plan's cut applied; image OCR and unplanned parts whole), or None if the
    needed derivative could not be read."""
    assert job.file is not None
    fid = job.file.file_id
    kind: TextKind = "markdown"
    if job.text == "ocr":
        pages, kind = reader.segments(fid, "ocr_text", split=False), "ocr_text"
    else:
        pages = reader.segments(fid, "markdown", split=job.cls == "pdf")
        if job.text == "spliced" and pages is not None:
            ocr = reader.segments(fid, "ocr_text")
            if has_content(pages):
                pages = splice(pages, ocr)
            elif has_content(ocr):
                pages, kind = ocr, "ocr_text"
    if pages is None:
        return None
    planned = job.planned
    if planned is None or job.cls == "image":
        return kind, kept_text(pages, text_cut=None, text_pages=0, text_chars=None)
    return kind, kept_text(
        pages, text_cut=planned.text_cut, text_pages=planned.text_pages, text_chars=planned.text_chars
    )


def _render(job: _Job, reader: DerivativeReader) -> tuple[list[ContentBlock], str]:
    """The part's blocks and the §4.3 `to` of what they carry."""
    blocks: list[ContentBlock] = []
    file, label, ref = job.file, job.label, job.native_ref
    if file is None or (job.reason is not None and job.text is None and ref is None):
        return [TextBlock(f"[{label}: not available]")], "omitted"
    fid = file.file_id
    if ref is not None:
        data = reader.get((fid, ref.kind, None))
        if data is None:
            job.reason = "not_ready"
            return [TextBlock(f"[{label}: not available]")], "omitted"
        native: ContentBlock = (
            image_block(data, ref.width, ref.height)
            if job.cls == "image"
            else DocumentBlock("pdf", label, data, file.pages or ref.pages or 1)
        )
        blocks += [TextBlock(f"[{label}]"), native]
    text = _text(job, reader) if job.text is not None else None
    if text is not None and text[1].strip():
        blocks.append(untrusted_block(label, text[0], text[1]))
    images = _page_images(job, reader, blocks)
    if not blocks:
        if job.text is not None:  # nothing to send: say why if a derivative the row needs is the cause
            needed: DerivativeKind = "ocr_text" if job.text == "ocr" else "markdown"
            readiness = derivative_problem(file, needed)
            if readiness is None and text is None:
                job.reason = "not_ready"  # the read failed
            elif readiness is not None and not (job.reason is None and job.cls == "image"):
                job.reason = readiness  # (a budget-cut image's missing OCR is already the plan's `omitted`)
            elif readiness is None and job.cls == "pdf" and job.text == "markdown" and job.page_ref is None:
                job.reason = derivative_problem(file, "page_images") or job.reason  # image-only row, empty Markdown
        return [TextBlock(f"[{label}: not available]")], "omitted"
    if ref is not None:
        return blocks, f"native+page_images({images})" if job.dropped else "native"
    if job.cls == "pdf" and job.text == "markdown":
        return blocks, f"markdown+page_images({images})" if images else "markdown"
    return blocks, text[0] if text is not None and text[1] else "omitted"


def _page_images(job: _Job, reader: DerivativeReader, blocks: list[ContentBlock]) -> int:
    """Appends the planned page images that may be sent and returns how many: each within the per-image limit, and
    all within the part's reservation in the plan (`len(planned.page_images)` average-size pages), since real pages
    differ in size. The others count as `job.dropped` (a per-attempt `size_limit`)."""
    ref, planned = job.page_ref, job.planned
    if ref is None or planned is None or not job.page_images:
        return 0
    assert job.file is not None
    fid = job.file.file_id
    data = [reader.get((fid, "page_images", page)) for page in job.page_images]
    reserved = len(planned.page_images) * page_image_b64(ref, MAX_IMAGE_BYTES)
    keep = page_images_within([None if d is None else len(d) for d in data], reserved, MAX_IMAGE_BYTES)
    job.dropped = sum(d is not None for d in data) - len(keep)  # a failed read is not a size problem
    for i in keep:
        page_data = data[i]
        assert page_data is not None
        blocks += [TextBlock(f"[{job.label} page {job.page_images[i]}]"), image_block(page_data, ref.width, ref.height)]
    return len(keep)


class MediaNegotiatorImpl:
    """Implements `MediaNegotiator` (spec-07 §4): stateless, so it re-runs for every attempt (fallbacks included)
    and every attempt of a turn gets the same labels, the plan's. Pass the media parts of one user message (the
    current turn's, or the previous media-bearing turn's); text and json parts produce no blocks (the prompt
    builder renders and spotlights them). Per-attempt degradations (`model_capability`, `size_limit`, `not_ready`,
    `extraction_failed`, `rejected`) carry `model` (the Bedrock id); the caller stamps `attempt_id`.

    The plan bounds every request: a part's page images stay within the plan's reservation for that part (planned
    count x average page size; the rest are `size_limit`), and a part the plan does not know sends only its
    `[label: not available]` line."""

    def __init__(self, files: FileService) -> None:
        self._files = files

    async def negotiate(
        self, alias: str, model: ModelCfg, parts: Sequence[ContentPart], plan: MediaPlan, *, principal: Principal
    ) -> tuple[tuple[ContentBlock, ...], tuple[Degradation, ...]]:
        """See the class docstring; reads bytes with `FileService.read_derivative` and never raises for media."""
        modalities: frozenset[str] = frozenset(model.modalities)
        labeler = Labeler()
        for entry in plan.parts:  # reserve the plan's labels, so unplanned parts never clash with them
            labeler("image" if entry.kind == "image" else "pdf", entry.label)
        jobs: list[_Job] = []
        for part in parts:
            if part.kind not in ("image", "file"):
                continue
            found = classify(part)
            cls, file = found if found is not None else (None, None)
            planned = _match(plan, part, cls) if cls is not None else None
            label = (
                planned.label
                if planned is not None
                else labeler(cls or ("image" if part.kind == "image" else "pdf"), part.label)
            )
            job = _Job(part, label, cls, file, planned)
            _prepare(job, modalities)
            jobs.append(job)
        reader = DerivativeReader(self._files, principal)
        await reader.fetch(key for job in jobs for key in _keys(job))
        blocks: list[ContentBlock] = []
        degradations: list[Degradation] = []
        for job in jobs:
            rendered, to = _render(job, reader)
            blocks += rendered
            if job.planned is None:
                continue
            if job.reason is not None:
                degradations.append(_degradation(job, job.reason, to, model.id))
            if job.dropped and job.reason != "size_limit":
                degradations.append(_degradation(job, "size_limit", to, model.id))
        return tuple(blocks), tuple(degradations)


def _degradation(job: _Job, reason: DegradationReason, to: str, model_id: str) -> Degradation:
    source = media_type(job.file) if job.file is not None else job.part.kind
    file_id = job.file.file_id if job.file is not None else None
    index = job.part.index if job.planned is None or job.planned.origin == "current" else None
    return Degradation(source, to, reason, file_id, index, model=model_id)
