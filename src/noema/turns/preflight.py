"""Preflight (spec-04 §9; IMPL-046, IMPL-074; spec-07 §3.2, §4.1, §4.5).

After `turn.created` and before any model call: ingest inline images (waits for `raster`), wait for files
(`wait_for_files`), number the media (`image-N` / `doc-N`: current parts first, then the previous media-bearing
turn, then older turns), pre-read page texts for the budget planner, re-estimate the input tokens with the real
image sizes, and plan the media within `CONVERSE_CAPS_V1` with the derived-text allowance `T`. A current-turn PDF
whose sanitizer removed active content (`pdf_sanitized.active_content`) adds one `active_content_removed`
degradation (spec-09 §6.3 step 5). `language` is "und" (IMPL-046). Failures raise `NoemaError` (the orchestrator
turns them into an SSE `error`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final, Literal

from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.media import (
    CONVERSE_CAPS_V1,
    ContentPart,
    DerivativeKind,
    FileRef,
    FileService,
    MediaBudgetPlanner,
    MediaPlan,
)
from noema.core.protocols.ops import TokenEstimator
from noema.core.values import ContentBlock, Degradation, DocumentBlock, ImageBlock, Modality, TextBlock
from noema.turns.concurrent import gather
from noema.turns.context.budget import ContextBudget
from noema.turns.context.history import HistoryTurn, MediaText, part_text
from noema.turns.request import RequestPart

LANGUAGE: Final = "und"
MODALITY_ORDER: Final[tuple[Modality, ...]] = ("text", "image", "document")
_READY: Final = frozenset({"ready", "partial"})
_FAILED: Final = frozenset({"rejected", "failed", "deleted"})
_TEXT_MEDIA: Final = frozenset({"application/pdf", "text/markdown"})
OLDER_BATCH: Final = 4  # older history media whose text derivatives are read per round (1-2 reads each)
PAGE_BREAK: Final = "\f"  # markdown and PDF ocr_text: one segment per page (spec-07 §1)
IMAGE_CAP_TOKENS: Final = 1600
DOC_PAGE_TOKENS: Final = 1500


@dataclass(frozen=True, slots=True)
class Preflight:
    """What preflight resolved (see the module docstring)."""

    parts: tuple[ContentPart, ...]
    modalities: tuple[Modality, ...]
    plan: MediaPlan
    degradations: tuple[Degradation, ...]
    est_input_tokens: int
    previous: tuple[ContentPart, ...] = ()
    previous_turn_id: str | None = None
    history_labels: Mapping[str, str] = field(default_factory=dict)
    history_texts: Mapping[str, MediaText] = field(default_factory=dict)
    language: str = LANGUAGE

    @property
    def turn_files(self) -> tuple[tuple[int, str], ...]:
        """(part_index, file_id) of every media part (the `turn_file` rows)."""
        return tuple((p.index, p.file.file_id) for p in self.parts if p.file is not None)


def file_modality(ref: FileRef) -> Literal["image", "document"]:
    media_type = ref.media_type or ref.declared_type
    return "image" if media_type.startswith("image/") else "document"


def part_modality(part: RequestPart, ref: FileRef | None) -> Modality:
    """The modality of a request part (`text` for text and JSON)."""
    if part.kind == "image":
        return "image"
    if part.kind == "file":
        return file_modality(ref) if ref is not None else "document"
    return "text"


def modalities_of(parts: Sequence[RequestPart], refs: Mapping[str, FileRef]) -> tuple[Modality, ...]:
    """The modality inventory in canonical order (text, image, document)."""
    seen = {part_modality(p, refs.get(p.file_id or "")) for p in parts}
    return tuple(m for m in MODALITY_ORDER if m in seen)


def ingress_blocks(parts: Sequence[RequestPart], refs: Mapping[str, FileRef]) -> list[ContentBlock]:
    """Estimator blocks of the request at ingress: text as is, inline and uploaded images at the 1,600-token cap
    (an ImageBlock of 1200x1000 px, ceil(w*h/750) = 1600), documents at 1,500 x pages."""
    blocks: list[ContentBlock] = []
    for p in parts:
        if p.kind == "text":
            blocks.append(TextBlock(p.text or ""))
        elif p.kind == "json":
            blocks.append(TextBlock(part_text({"type": "json", "json": p.json})))
        elif part_modality(p, refs.get(p.file_id or "")) == "image":
            blocks.append(ImageBlock("png", b"", 1200, 1000))
        else:
            ref = refs.get(p.file_id or "")
            blocks.append(DocumentBlock("pdf", "doc", b"", (ref.pages if ref is not None else None) or 1))
    return blocks


def history_blocks(history: Sequence[HistoryTurn]) -> list[ContentBlock]:
    """Estimator blocks of the selected history text (the older media's text derivatives are added by preflight)."""
    out: list[ContentBlock] = []
    for t in history:
        out.append(TextBlock(t.user_text()))
        out.append(TextBlock(t.output_text))
    return out


def estimate(
    estimator: TokenEstimator, aliases: Sequence[str], system: Sequence[str], blocks: Sequence[ContentBlock]
) -> int:
    """The largest estimate over the candidate model aliases (model-independent worst case); `aliases` is never
    empty (the default profile is always a candidate)."""
    return max(estimator.estimate(a, system, blocks) for a in aliases)


async def _wait_ready(files: FileService, principal: Principal, file_id: str, deadline: Deadline) -> FileRef:
    """Wait (file events) until the file is usable; raises file_rejected or, at the deadline, deadline_exceeded."""
    async with deadline.scope() as scope:
        async for ref in files.watch(principal, file_id):
            if ref.status in _READY:
                return ref
            if ref.status in _FAILED:
                raise NoemaError(ErrorCode.FILE_REJECTED, "A file of this turn was rejected.")
    if scope.cancelled_caught:
        raise NoemaError(ErrorCode.DEADLINE_EXCEEDED, "The deadline passed while waiting for the files.")
    raise NoemaError(ErrorCode.FILE_NOT_READY, "A file of this turn is not ready.")


async def _derivative_text(files: FileService, principal: Principal, file_id: str, kind: DerivativeKind) -> str | None:
    """A whole text derivative, or None when it is missing, not ready or unreadable."""
    try:
        data = await files.read_derivative(principal, file_id, kind)
    except NoemaError:
        return None
    return data.decode("utf-8", "replace")


async def _derivative_texts(
    files: FileService, principal: Principal, keys: Sequence[tuple[str, DerivativeKind]]
) -> dict[tuple[str, DerivativeKind], str | None]:
    """Several whole text derivatives, read concurrently."""
    out: dict[tuple[str, DerivativeKind], str | None] = {}

    def read(key: tuple[str, DerivativeKind]) -> Callable[[], Awaitable[None]]:
        async def call() -> None:
            out[key] = await _derivative_text(files, principal, *key)

        return call

    await gather([read(key) for key in dict.fromkeys(keys)])
    return out


def _segment(segments: Sequence[str], i: int) -> str:
    return segments[i] if i < len(segments) and segments[i].strip() else ""


async def page_texts(files: FileService, principal: Principal, ref: FileRef) -> tuple[str, ...] | None:
    """The text of each page of a PDF or Markdown file for the budget planner: the page's `markdown` segment, else
    its `ocr_text` segment (scanned pages), else "". Both derivatives hold one `\\f`-separated segment per page
    (spec-07 §1) and are read whole, concurrently; a Markdown upload is one page. None for other files, or when
    neither derivative is readable (the planner then sizes the part from its manifest)."""
    media_type = ref.media_type or ref.declared_type
    if media_type not in _TEXT_MEDIA or ref.status not in _READY:
        return None
    if media_type == "text/markdown":
        text = await _derivative_text(files, principal, ref.file_id, "markdown")
        return None if text is None else (text,)
    read = await _derivative_texts(files, principal, [(ref.file_id, "markdown"), (ref.file_id, "ocr_text")])
    markdown, ocr = read[(ref.file_id, "markdown")], read[(ref.file_id, "ocr_text")]
    if markdown is None and ocr is None:
        return None
    md = markdown.split(PAGE_BREAK) if markdown is not None else []
    oc = ocr.split(PAGE_BREAK) if ocr is not None else []
    pages = ref.pages or max(len(md), len(oc), 1)
    return tuple(_segment(md, i) or _segment(oc, i) for i in range(pages))


async def _page_texts_of(
    files: FileService, principal: Principal, refs: Sequence[FileRef]
) -> dict[str, tuple[str, ...] | None]:
    """`page_texts` of several documents, concurrently (file_id -> texts)."""
    out: dict[str, tuple[str, ...] | None] = {}

    def read(ref: FileRef) -> Callable[[], Awaitable[None]]:
        async def call() -> None:
            out[ref.file_id] = await page_texts(files, principal, ref)

        return call

    await gather([read(ref) for ref in refs])
    return out


class _Labels:
    def __init__(self) -> None:
        self.images = 0
        self.docs = 0

    def next(self, modality: Literal["image", "document"]) -> str:
        if modality == "image":
            self.images += 1
            return f"image-{self.images}"
        self.docs += 1
        return f"doc-{self.docs}"


def _image_block(ref: FileRef) -> ImageBlock:
    raster = next((d for d in ref.derivatives if d.kind == "raster_1568" and d.width and d.height), None)
    if raster is None or raster.width is None or raster.height is None:
        return ImageBlock("png", b"", 1200, 1000)  # the 1,600-token cap
    return ImageBlock("png", b"", raster.width, raster.height)


@dataclass(frozen=True, slots=True)
class PreflightInputs:
    """The ingress results preflight builds on."""

    principal: Principal
    parts: tuple[RequestPart, ...]
    refs: Mapping[str, FileRef]
    deadline: Deadline
    session_id: str | None
    wait_for_files: bool
    allow_partial_files: bool
    instructions: str | None
    history: tuple[HistoryTurn, ...]
    budget: ContextBudget
    aliases: tuple[str, ...]


async def _resolve_files(files: FileService, inputs: PreflightInputs) -> tuple[dict[int, FileRef], list[Degradation]]:
    """Inline ingest and the file wait: part index -> FileRef of every usable media part, plus the degradations
    of the parts dropped under `allow_partial_files`."""
    resolved: dict[int, FileRef] = {}
    dropped: list[Degradation] = []
    for p in inputs.parts:
        if p.data_b64 is not None:
            resolved[p.index] = await files.ingest_inline(
                inputs.principal,
                p.data_b64,
                p.media_type or "image/png",
                deadline=inputs.deadline,
                session_id=inputs.session_id,
            )
            continue
        if p.file_id is None:
            continue
        ref = inputs.refs.get(p.file_id)
        if ref is None:
            ref = await files.get(inputs.principal, p.file_id)
        if ref.status not in _READY and ref.status not in _FAILED and inputs.wait_for_files:
            try:
                ref = await _wait_ready(files, inputs.principal, p.file_id, inputs.deadline)
            except NoemaError as exc:
                if exc.code is not ErrorCode.FILE_REJECTED or not inputs.allow_partial_files:
                    raise
                ref = await files.get(inputs.principal, p.file_id)
        if ref.status in _FAILED:
            if not inputs.allow_partial_files:
                raise NoemaError(ErrorCode.FILE_REJECTED, "A file of this turn was rejected.", param=p.param)
            dropped.append(
                Degradation(ref.media_type or ref.declared_type, "omitted", "rejected", ref.file_id, p.index)
            )
            continue
        if ref.status not in _READY:
            raise NoemaError(ErrorCode.FILE_NOT_READY, "A file of this turn is not ready.", param=p.param)
        resolved[p.index] = ref
    return resolved, dropped


@dataclass(frozen=True, slots=True)
class _Older:
    """A history media part outside the previous media-bearing turn (text derivatives only, IMPL-045)."""

    file_id: str
    label: str
    image: bool


async def _refs(files: FileService, principal: Principal, file_ids: Sequence[str]) -> dict[str, FileRef]:
    """`FileService.get` of several files, concurrently; a file that can't be read is left out."""
    out: dict[str, FileRef] = {}

    def get(file_id: str) -> Callable[[], Awaitable[None]]:
        async def call() -> None:
            try:
                out[file_id] = await files.get(principal, file_id)
            except NoemaError:
                return

        return call

    await gather([get(file_id) for file_id in dict.fromkeys(file_ids)])
    return out


async def _previous_media(
    files: FileService, principal: Principal, history: Sequence[HistoryTurn], labels: _Labels
) -> tuple[str | None, list[tuple[int, Literal["image", "file"], str, FileRef]], dict[str, str], list[_Older]]:
    """The previous media-bearing turn's media (resolved, labelled: `(index, kind, label, ref)`), every history
    label, and the older media (newest turn first)."""
    prev = next((t for t in reversed(history) if t.media), None)
    names: dict[str, str] = {}
    found: list[tuple[int, Literal["image", "file"], str, FileRef]] = []
    if prev is not None:
        refs = await _refs(files, principal, [str(m.get("file_id", "")) for m in prev.media])
        for i, stored in enumerate(prev.media):
            file_id = str(stored.get("file_id", ""))
            image = stored.get("type") == "image"
            ref = refs.get(file_id)
            if ref is None:
                names[file_id] = labels.next("image" if image else "document")
                continue
            names[file_id] = labels.next(file_modality(ref))
            found.append((i, "image" if image else "file", names[file_id], ref))
    older: list[_Older] = []
    for turn in reversed(history):  # older turns, newest first after the previous media-bearing turn
        if turn is prev:
            continue
        for stored in turn.media:
            file_id = str(stored.get("file_id", ""))
            if file_id not in names:
                image = stored.get("type") == "image"
                names[file_id] = labels.next("image" if image else "document")
                older.append(_Older(file_id, names[file_id], image))
    return (prev.turn_id if prev is not None else None), found, names, older


async def _older_texts(
    files: FileService, principal: Principal, older: Sequence[_Older], tokens: Callable[[str], int], allowance: int
) -> dict[str, MediaText]:
    """The text derivatives of the older history media within `allowance` tokens, newest first: an image's
    `ocr_text`; a document's `markdown` (scanned pages from `ocr_text`), cut at a page boundary. Parts with no
    text (or no room left) are absent (the prompt sends `[label: not available]`). Read `OLDER_BATCH` media at a
    time; reading stops once the allowance is used up (no room left, or a page that does not fit: an older part
    never passes a cut newer one)."""
    out: dict[str, MediaText] = {}
    left = allowance
    for start in range(0, len(older), OLDER_BATCH):
        if left <= 0:
            break
        batch = older[start : start + OLDER_BATCH]
        keys: list[tuple[str, DerivativeKind]] = []
        for o in batch:
            keys += [(o.file_id, "ocr_text")] if o.image else [(o.file_id, "markdown"), (o.file_id, "ocr_text")]
        read = await _derivative_texts(files, principal, keys)
        left = _fill_older(batch, read, tokens, left, out)
    return out


def _fill_older(
    batch: Sequence[_Older],
    read: Mapping[tuple[str, DerivativeKind], str | None],
    tokens: Callable[[str], int],
    left: int,
    out: dict[str, MediaText],
) -> int:
    """`_older_texts` for one batch: adds the kept texts to `out`; returns the allowance left (0 after a cut)."""
    for o in batch:
        md, ocr = (None if o.image else read.get((o.file_id, "markdown"))), read.get((o.file_id, "ocr_text"))
        md_pages, ocr_pages = (md.split(PAGE_BREAK) if md else []), (ocr.split(PAGE_BREAK) if ocr else [])
        pages = [_segment(md_pages, i) or _segment(ocr_pages, i) for i in range(max(len(md_pages), len(ocr_pages)))]
        kept: list[str] = []
        for page in pages:
            cost = tokens(page) if page else 0
            if cost > left:
                left = 0  # the allowance is used up
                break
            kept.append(page)
            left -= cost
        text = "\n\n".join(page for page in kept if page)
        if not text:
            continue
        cut = None if len(kept) == len(pages) else f"[{o.label}: pages 1-{len(kept)} of {len(pages)}]"
        out[o.file_id] = MediaText("markdown" if md and md.strip() else "ocr_text", text, cut)
    return left


def active_content_removed(media: Mapping[int, tuple[str, FileRef]]) -> list[Degradation]:
    """spec-09 §6.3 step 5: one `active_content_removed` per current-turn file whose `pdf_sanitized` lists removed
    active content (`from` = the file's media type, `to: pdf_sanitized`), at the file's first part index."""
    out: list[Degradation] = []
    seen: set[str] = set()
    for index, (_, ref) in sorted(media.items()):
        sanitized = next((d for d in ref.derivatives if d.kind == "pdf_sanitized"), None)
        if sanitized is None or not sanitized.active_content or ref.file_id in seen:
            continue
        seen.add(ref.file_id)
        media_type = ref.media_type or ref.declared_type
        out.append(Degradation(media_type, "pdf_sanitized", "active_content_removed", ref.file_id, index))
    return out


async def run_preflight(
    files: FileService, estimator: TokenEstimator, media_budget: MediaBudgetPlanner, inputs: PreflightInputs
) -> Preflight:
    """spec-04 §9 preflight (see the module docstring)."""
    resolved, dropped = await _resolve_files(files, inputs)
    labels = _Labels()
    who = inputs.principal
    est_blocks: list[ContentBlock] = history_blocks(inputs.history)
    text_blocks: list[ContentBlock] = list(est_blocks)
    modalities: set[Modality] = set()
    media: dict[int, tuple[str, FileRef]] = {}  # part index -> (label, ref), labels in part order
    for p in inputs.parts:
        if p.kind in ("text", "json"):
            text = p.text if p.kind == "text" else part_text({"type": "json", "json": p.json})
            est_blocks.append(TextBlock(text or ""))
            text_blocks.append(TextBlock(text or ""))
            modalities.add("text")
            continue
        ref = resolved.get(p.index)
        if ref is None:
            continue
        modality = "image" if p.kind == "image" else file_modality(ref)
        modalities.add(modality)
        label = labels.next(modality)
        media[p.index] = (label, ref)
        est_blocks.append(
            _image_block(ref) if modality == "image" else DocumentBlock("pdf", label, b"", ref.pages or 1)
        )
    previous_turn_id, prev_media, history_labels, older = await _previous_media(files, who, inputs.history, labels)
    docs = [ref for _, ref in media.values() if file_modality(ref) == "document"]
    texts = await _page_texts_of(files, who, [*docs, *(ref for _, kind, _, ref in prev_media if kind == "file")])
    parts: list[ContentPart] = []
    for p in inputs.parts:
        if p.kind in ("text", "json"):
            parts.append(ContentPart(p.index, p.kind, None, text=p.text, json=p.json))
        elif p.index in media:
            label, ref = media[p.index]
            kind: Literal["image", "file"] = "image" if p.kind == "image" else "file"
            parts.append(ContentPart(p.index, kind, label, file=ref, page_texts=texts.get(ref.file_id)))
    previous = tuple(
        ContentPart(i, kind, label, file=ref, page_texts=texts.get(ref.file_id) if kind == "file" else None)
        for i, kind, label, ref in prev_media
    )
    system = (inputs.instructions,) if inputs.instructions else ()
    allowance = inputs.budget.available(estimate(estimator, inputs.aliases, system, text_blocks))
    plan = media_budget.plan(parts, previous, replace(CONVERSE_CAPS_V1, max_text_tokens=allowance))
    # older media: text derivatives in what the planner can't use of T (an upper bound: every page text)
    planned = [TextBlock(t) for part in (*parts, *previous) for t in part.page_texts or ()]
    left = allowance - (estimate(estimator, inputs.aliases, (), planned) if planned else 0)
    history_texts = await _older_texts(
        files, who, older, lambda text: estimate(estimator, inputs.aliases, (), [TextBlock(text)]), max(0, left)
    )
    est_blocks += [TextBlock(t.text) for t in history_texts.values()]
    return Preflight(
        parts=tuple(parts),
        modalities=tuple(m for m in MODALITY_ORDER if m in modalities),
        plan=plan,
        degradations=(*dropped, *active_content_removed(media), *plan.degradations),
        est_input_tokens=estimate(estimator, inputs.aliases, system, est_blocks),
        previous=previous,
        previous_turn_id=previous_turn_id,
        history_labels=history_labels,
        history_texts=history_texts,
    )
