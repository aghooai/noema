"""`InputViewResolver`: decision-member views of the input (spec-07 §5, spec-06 §4, IMPL-058, IMPL-075), plus the
async derivative helpers shared with `media.negotiation` (reads through `FileService.read_derivative`, page
segments, the OCR splice and the per-modality adequacy ranks).

Views carry content blocks and metadata only, unescaped; decision renders the label lines, wrapping, escaping and
datamarking (IMPL-050, spec-06 §5.3). Media problems never raise: they give `metadata_only` plus a degradation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

import anyio

from noema.core.protocols.media import (
    CONVERSE_CAPS_V1,
    ContentPart,
    DecisionViewBudget,
    DerivativeKind,
    DerivativeRef,
    FileRef,
    FileService,
    InputView,
    ViewKind,
    ViewPart,
)
from noema.core.values import (
    ContentBlock,
    Degradation,
    DegradationReason,
    DocumentBlock,
    ImageBlock,
    Modality,
    TextBlock,
)

from .budget import (
    Labeler,
    MediaClass,
    TextCut,
    allocate_text,
    b64_len,
    classify,
    derivative,
    has_text,
    kept_text,
    media_type,
    page_image_b64,
    page_images_within,
    ready,
    scanned_pages,
)

if TYPE_CHECKING:
    from noema.config.models import DecisionMemberCfg, ModelCfg
    from noema.core.principal import Principal

TextKind = Literal["markdown", "ocr_text"]
ReadKey = tuple[str, DerivativeKind, int | None]  # (file_id, kind, page)

PAGE_BREAK: Final = "\f"  # markdown and PDF ocr_text hold one \f-separated segment per page (spec-07 §1)
DEFAULT_LONG_EDGE: Final = 1568  # raster_1568 / page_images size when the manifest has no dimensions
BINARY_BUDGET_V1: Final = CONVERSE_CAPS_V1.max_payload_bytes - CONVERSE_CAPS_V1.text_reserve_bytes
MAX_IMAGE_BYTES: Final = CONVERSE_CAPS_V1.max_image_bytes
_USABLE: Final = frozenset({"ready", "partial"})
_SEVERITY: Final[tuple[ViewKind, ...]] = (
    "native",
    "derived_text",
    "thumbnail",
    "ocr_text",
    "metadata_only",
    "skipped",
)


class DerivativeReader:
    """Derivative reads for one `resolve`/`negotiate` call, as one principal: concurrent prefetch, memoized, and
    never raising (a failed or refused read is None; FileService keeps the per-pod LRU)."""

    __slots__ = ("_data", "_files", "_principal")

    def __init__(self, files: FileService, principal: Principal) -> None:
        self._files = files
        self._principal = principal
        self._data: dict[ReadKey, bytes | None] = {}

    async def fetch(self, keys: Iterable[ReadKey]) -> None:
        """Read every key not read yet, concurrently."""
        missing = [k for k in dict.fromkeys(keys) if k not in self._data]
        if len(missing) == 1:
            await self._read(missing[0])
        elif missing:
            async with anyio.create_task_group() as tg:
                for key in missing:
                    tg.start_soon(self._read, key)

    async def _read(self, key: ReadKey) -> None:
        file_id, kind, page = key
        try:
            self._data[key] = await self._files.read_derivative(self._principal, file_id, kind, page=page)
        except Exception:  # never raise for media problems (spec-02 §6): S3, LRU or authz failures alike
            self._data[key] = None

    def get(self, key: ReadKey) -> bytes | None:
        """The bytes of a fetched key; None if it failed or was never fetched."""
        return self._data.get(key)

    def segments(self, file_id: str, kind: DerivativeKind, *, split: bool = True) -> tuple[str, ...] | None:
        """A fetched text derivative as page segments (split on `\\f` before any text processing, spec-07 §5)."""
        data = self.get((file_id, kind, None))
        if data is None:
            return None
        text = data.decode("utf-8", errors="replace")
        return tuple(text.split(PAGE_BREAK)) if split else (text,)


def file_problem(file: FileRef | None) -> DegradationReason | None:
    """Why a media part's file cannot be used at all, or None if it is ready or partial."""
    if file is None:
        return "not_ready"
    if file.status in _USABLE:
        return None
    if file.status == "failed":
        return "extraction_failed"
    return "rejected" if file.status == "rejected" else "not_ready"


def derivative_problem(file: FileRef, kind: DerivativeKind) -> DegradationReason | None:
    """Why the derivative cannot be used (missing or pending: not_ready; failed: extraction_failed), or None."""
    found = derivative(file, kind)
    if found is None or found.status == "pending":
        return "not_ready"
    return "extraction_failed" if found.status == "failed" else None


def splice(markdown: Sequence[str], ocr: Sequence[str] | None) -> tuple[str, ...]:
    """Per page, the Markdown segment, else the OCR segment of that page (the text-only row, spec-07 §4.2)."""
    ocr = ocr or ()
    return tuple(
        md if i < len(markdown) and (md := markdown[i]).strip() else (ocr[i] if i < len(ocr) else "")
        for i in range(max(len(markdown), len(ocr)))
    )


def has_content(pages: Sequence[str] | None) -> bool:
    """Some page has a non-whitespace character."""
    return pages is not None and any(p.strip() for p in pages)


def image_block(data: bytes, width: int | None, height: int | None) -> ImageBlock:
    """An ImageBlock of a re-encoded derivative; the format is sniffed (raster_1568 is PNG, or JPEG when the PNG
    would exceed 3,750,000 B)."""
    fmt: Literal["png", "jpeg", "webp", "gif"] = "png"
    if data.startswith(b"\xff\xd8\xff"):
        fmt = "jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        fmt = "gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        fmt = "webp"
    return ImageBlock(fmt, data, width or DEFAULT_LONG_EDGE, height or DEFAULT_LONG_EDGE)


def rank(modality: Modality, kind: ViewKind) -> int:
    """Adequacy (spec-06 §4.4): 2 adequate, 1 degraded (votes, does not count), 0 neither."""
    if kind == "native" or (kind == "derived_text" and modality != "image"):
        return 2
    return 1 if kind in ("ocr_text", "thumbnail", "derived_text") else 0


def least(modality: Modality, kinds: Iterable[ViewKind]) -> ViewKind:
    """The least adequate of a modality's part views (lowest rank, then the more derived kind)."""
    return min(kinds, key=lambda k: (rank(modality, k), -_SEVERITY.index(k)))


def summary_kind(by_modality: Mapping[Modality, ViewKind]) -> ViewKind:
    """`InputView.kind`: the view of the media modality with the lowest rank, ties to the first of image, document;
    `native` for text-only input (spec-06 §4.4)."""
    media: list[Modality] = [m for m in ("image", "document") if m in by_modality]
    if not media:
        return "native"
    return by_modality[min(media, key=lambda m: rank(m, by_modality[m]))]


@dataclass(slots=True)
class _Media:
    """One media part while its view is resolved. `kind` is the adequacy view ignoring budget cuts (spec-06 §4.4);
    `final` is what the member gets; `converted` = a budget cut changed the view (so `kind` keeps the pre-cut one);
    `text` is the kept text after the text cut."""

    part: ContentPart
    label: str
    modality: Modality
    cls: MediaClass | None
    file: FileRef | None
    kind: ViewKind = "metadata_only"
    final: ViewKind = "metadata_only"
    converted: bool = False
    splice: bool = False  # text-only PDF row: OCR segments of scanned pages spliced into the Markdown
    native_ref: DerivativeRef | None = None
    text_kind: TextKind | None = None
    pages: tuple[str, ...] | None = None
    page_ref: DerivativeRef | None = None
    page_images: list[int] = field(default_factory=list)
    cut: TextCut | None = None
    text: str = ""
    issues: list[tuple[DegradationReason, str | None]] = field(default_factory=list)  # to None = the row's `to`

    def show(self, kind: ViewKind) -> None:
        self.final = kind
        if not self.converted:
            self.kind = kind

    def fail(self, reason: DegradationReason) -> None:
        self.show("metadata_only")
        self.native_ref, self.text_kind, self.pages, self.page_images = None, None, None, []
        self.issues.append((reason, "omitted"))

    def row_to(self) -> str:
        """The §4.3 `to` of what this part shows."""
        if self.final == "derived_text" and self.cls == "pdf" and not self.splice:
            return f"markdown+page_images({len(self.page_images)})" if self.page_images else "markdown"
        return {"derived_text": "markdown", "ocr_text": "ocr_text", "native": "native"}.get(self.final, "omitted")


def _classify(part: ContentPart, labeler: Labeler, modalities: frozenset[str]) -> _Media:
    found = classify(part)
    cls, file = found if found is not None else (None, None)
    image = cls == "image" or (cls is None and part.kind == "image")
    m = _Media(
        part, labeler("image" if image else (cls or "pdf"), part.label), "image" if image else "document", cls, file
    )
    problem = file_problem(file)
    if file is None or problem is not None:
        m.fail(problem or "not_ready")
    elif cls == "image":
        need: DerivativeKind = "raster_1568" if "image" in modalities else "ocr_text"
        raster = ready(file, "raster_1568")
        if need == "raster_1568" and raster is not None and (raster.bytes or 0) > MAX_IMAGE_BYTES:
            m.issues.append(("size_limit", None))  # a raster_1568 JPEG over the limit: the OCR row, as in generation
            need = "ocr_text"
        if (problem := derivative_problem(file, need)) is not None:
            m.fail(problem)
        elif need == "raster_1568":
            m.native_ref = ready(file, need)
            m.show("native")
        else:
            m.text_kind = "ocr_text"
            m.show("ocr_text")
    elif cls == "pdf" and "document" in modalities:
        if (problem := derivative_problem(file, "pdf_sanitized")) is not None:
            m.fail(problem)
        elif (ref := ready(file, "pdf_sanitized")) is not None and (
            ref.bytes or 0
        ) > CONVERSE_CAPS_V1.max_document_bytes:
            m.issues.append(("size_limit", None))
            _derived(m, modalities)
        else:
            m.native_ref = ref
            m.show("native")
    else:
        _derived(m, modalities)
    return m


def _derived(m: _Media, modalities: frozenset[str]) -> None:
    """The derived row of a PDF (image models: Markdown + page images of scanned pages; text models: Markdown with
    OCR spliced in) or of a Markdown upload. Text is read later; emptiness is settled in `_load_text`."""
    assert m.file is not None
    if (problem := derivative_problem(m.file, "markdown")) is not None:
        m.fail(problem)
        return
    m.text_kind = "markdown"
    if m.cls == "pdf" and "image" in modalities:
        m.page_ref = ready(m.file, "page_images")
        m.page_images = sorted(scanned_pages(m.page_ref))
    else:
        m.splice = m.cls == "pdf"
    m.show("derived_text")


def _text_keys(m: _Media) -> list[ReadKey]:
    if m.text_kind is None or m.file is None:
        return []
    fid = m.file.file_id
    if m.cls == "image":
        return [(fid, "ocr_text", None)]
    keys: list[ReadKey] = [(fid, "markdown", None)]
    if m.splice and ready(m.file, "ocr_text") is not None:
        keys.append((fid, "ocr_text", None))  # text-only row: OCR segments are spliced in
    return keys


def _load_text(m: _Media, reader: DerivativeReader) -> None:
    if m.text_kind is None or m.file is None:
        return
    fid = m.file.file_id
    if m.cls == "image":
        m.pages = reader.segments(fid, "ocr_text", split=False)
    else:
        m.pages = reader.segments(fid, "markdown", split=m.cls == "pdf")
    if m.pages is None:
        m.fail("not_ready")
        return
    if m.splice:
        ocr = reader.segments(fid, "ocr_text")
        if has_content(m.pages):
            m.pages = splice(m.pages, ocr)
        elif has_content(ocr):
            m.pages, m.text_kind = ocr, "ocr_text"  # the Markdown is empty: OCR text alone (a degraded view)
            m.show("ocr_text")
    if not has_content(m.pages) and not m.page_images:
        image_row = m.cls == "pdf" and not m.splice and m.page_ref is None  # it needs its page images
        if image_row and (problem := derivative_problem(m.file, "page_images")) is not None:
            m.issues.append((problem, "omitted"))
        m.show("metadata_only")
        m.pages, m.text_kind = None, None


def _keep_images(media: Sequence[_Media], budget: DecisionViewBudget, binary: int) -> tuple[int, int]:
    """Step 1: ImageBlocks (images, and page images of PDFs already on their derived row) in part order, up to
    `max_images` within the payload. A cut image falls back to its `ocr_text` when that is ready and non-empty
    (read and text-cut later, like any OCR row), else it is omitted (`metadata_only`; ruling R3); a PDF keeps the
    page images that fit. Returns the image slots and the binary budget left."""
    slots = budget.max_images
    for m in media:
        if m.final == "native" and m.modality == "image" and m.native_ref is not None:
            size = b64_len(m.native_ref.bytes or MAX_IMAGE_BYTES)
            if slots > 0 and size <= binary:
                slots, binary = slots - 1, binary - size
            else:
                m.converted, m.native_ref = True, None
                if m.file is not None and has_text(m.file, "ocr_text"):
                    m.text_kind = "ocr_text"
                    m.show("ocr_text")
                    m.issues.append(("media_budget", None))  # `to` follows the view: ocr_text, or omitted if blank
                else:
                    m.show("metadata_only")
                    m.issues.append(("media_budget", "omitted"))
        elif m.page_images and m.page_ref is not None:
            slots, binary = _fit_page_images(m, slots, binary)
    return slots, binary


def _fit_page_images(m: _Media, slots: int, binary: int) -> tuple[int, int]:
    """Keeps the first page images that fit the slots and the binary budget left, each at the average page size;
    a cut is `media_budget` (the adequacy view stays). Returns what is left."""
    assert m.page_ref is not None
    per_page = page_image_b64(m.page_ref, MAX_IMAGE_BYTES)
    fit = min(len(m.page_images), max(0, slots), binary // per_page if per_page else len(m.page_images))
    if fit < len(m.page_images):
        m.page_images = m.page_images[:fit]
        m.converted = True
        m.issues.append(("media_budget", None))
    return slots - fit, binary - fit * per_page


def _keep_documents(
    media: Sequence[_Media], budget: DecisionViewBudget, slots: int, binary: int, mods: frozenset[str]
) -> None:
    """Step 2: the first `max_documents` native PDFs that fit the payload left stay native; the rest get their
    derived row (`media_budget`), whose page images take the image slots and payload still left."""
    kept = 0
    for m in media:
        if m.final != "native" or m.modality != "document" or m.native_ref is None:
            continue
        size = b64_len(m.native_ref.bytes or CONVERSE_CAPS_V1.max_document_bytes)
        if kept < budget.max_documents and size <= binary:
            kept, binary = kept + 1, binary - size
            continue
        m.converted, m.native_ref = True, None
        m.issues.append(("media_budget", None))
        _derived(m, mods)
        if m.page_images and m.page_ref is not None:
            slots, binary = _fit_page_images(m, slots, binary)


def _cut_text(media: Sequence[_Media], budget: DecisionViewBudget) -> None:
    """Step 3: the derived text parts share `max_text_tokens` in part order (the §4.5 step 2 rule). A part left
    with blank text and no page images shows nothing: `metadata_only` (a budget cut)."""
    items = [m for m in media if m.pages is not None and m.text_kind is not None]
    for m, cut in zip(items, allocate_text([m.pages or () for m in items], budget.max_text_tokens), strict=True):
        m.cut = cut
        m.text = kept_text(m.pages or (), text_cut=cut.note, text_pages=cut.pages, text_chars=cut.chars)
        blank = not m.text.strip()
        if not cut.whole:
            m.issues.append(("media_budget", "omitted" if blank else f"{m.text_kind}({cut.note})"))
        if blank and not m.page_images:
            m.converted = True
            m.show("metadata_only")


def _binary_keys(m: _Media) -> list[ReadKey]:
    if m.file is None:
        return []
    if m.final == "native" and m.native_ref is not None:
        return [(m.file.file_id, m.native_ref.kind, None)]
    return [(m.file.file_id, "page_images", page) for page in m.page_images]


def _blocks(m: _Media, reader: DerivativeReader) -> tuple[ContentBlock, ...]:
    if m.file is None:
        return ()
    fid, ref = m.file.file_id, m.native_ref
    if m.final == "native" and ref is not None:
        data = reader.get((fid, ref.kind, None))
        if data is None:
            m.converted = False
            m.fail("not_ready")
            return ()
        if m.modality == "image":
            return (image_block(data, ref.width, ref.height),)
        return (DocumentBlock("pdf", m.label, data, m.file.pages or ref.pages or 1),)
    if m.final == "metadata_only":
        return ()
    blocks: list[ContentBlock] = [TextBlock(m.text)] if m.text.strip() else []
    page_ref = m.page_ref
    if page_ref is not None and m.page_images:
        images = [reader.get((fid, "page_images", page)) for page in m.page_images]
        reserved = len(m.page_images) * page_image_b64(page_ref, MAX_IMAGE_BYTES)  # what step 1/2 allocated
        keep = page_images_within([None if d is None else len(d) for d in images], reserved, MAX_IMAGE_BYTES)
        if None in images:
            m.issues.append(("not_ready", None))  # a page image could not be read: the part keeps the rest
        if len(keep) < len(images) - images.count(None):
            m.issues.append(("size_limit", None))  # real pages over the average-size reservation or the image cap
        m.page_images = [m.page_images[i] for i in keep]
        blocks += [image_block(d, page_ref.width, page_ref.height) for i in keep if (d := images[i]) is not None]
    if not blocks:
        m.show("metadata_only")
    return tuple(blocks)


def _degradations(m: _Media) -> list[Degradation]:
    source = media_type(m.file) if m.file is not None else m.part.kind
    fid = m.file.file_id if m.file is not None else None
    return [Degradation(source, to or m.row_to(), reason, fid, m.part.index) for reason, to in m.issues]


def _text_part(part: ContentPart) -> ViewPart:
    if part.kind == "json":
        text = json.dumps(part.json, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    else:
        text = part.text or ""
    return ViewPart(part.index, part.label, "native", (TextBlock(text),))


def _by_modality(media: Sequence[_Media], has_text: bool) -> dict[Modality, ViewKind]:
    """Per media modality, the least adequate part view ignoring budget cuts (`_Media.kind`), unless no part of
    the modality shows anything: then `metadata_only`, since the member saw none of it (IMPL-058)."""
    kinds: dict[Modality, list[ViewKind]] = {}
    shown: set[Modality] = set()
    for m in media:
        kinds.setdefault(m.modality, []).append(m.kind)
        if m.final != "metadata_only":
            shown.add(m.modality)
    out: dict[Modality, ViewKind] = {"text": "native"} if has_text else {}
    out.update({mod: least(mod, found) if mod in shown else "metadata_only" for mod, found in kinds.items()})
    return out


def _skipped(by_modality: Mapping[Modality, ViewKind], policy: str) -> frozenset[Modality]:
    """Under `skip_incapable_members`, the media modalities this member would see degraded or not at all."""
    if policy != "skip_incapable_members":
        return frozenset()
    return frozenset(m for m, kind in by_modality.items() if m != "text" and rank(m, kind) < 2)


class InputViewResolverImpl:
    """Implements `InputViewResolver` (spec-07 §5): per part, the best view the member's model accepts
    (`models.<alias>.modalities`), then the `budget` cuts (images, then documents, then text; spec-07 §5), each
    reported as a `media_budget` degradation. `by_modality` and `kind` are the adequacy views ignoring budget cuts
    (the budget is the same for every member), except that a modality none of whose parts shows anything is
    `metadata_only` (IMPL-058). Native images, PDFs and page images also stay within the Converse limits
    (`CONVERSE_CAPS_V1`, 02 §8.4). Labels are the given `image-N`/`doc-N`, never filenames."""

    def __init__(self, files: FileService) -> None:
        self._files = files

    async def resolve(
        self,
        member: DecisionMemberCfg,
        parts: Sequence[ContentPart],
        *,
        policy: Literal["derive_for_text_members", "skip_incapable_members"],
        budget: DecisionViewBudget,
        model: ModelCfg,
        principal: Principal,
    ) -> InputView:
        """See the class docstring; reads derivatives as `principal` and never raises for media problems."""
        modalities: frozenset[str] = frozenset(model.modalities)
        labeler = Labeler()
        reader = DerivativeReader(self._files, principal)
        entries = [_text_part(p) if p.kind in ("text", "json") else _classify(p, labeler, modalities) for p in parts]
        media = [e for e in entries if isinstance(e, _Media)]
        slots, binary = _keep_images(media, budget, BINARY_BUDGET_V1)  # spec-07 §5 order: images, documents, text
        _keep_documents(media, budget, slots, binary, modalities)
        await reader.fetch(key for m in media for key in _text_keys(m))
        for m in media:
            _load_text(m, reader)
        _cut_text(media, budget)
        has_text = len(media) < len(entries)
        skipped = _skipped(_by_modality(media, has_text), policy)  # no binary reads for a skipped modality
        await reader.fetch(key for m in media if m.modality not in skipped for key in _binary_keys(m))
        blocks = {id(m): _blocks(m, reader) for m in media if m.modality not in skipped}
        by_modality = _by_modality(media, has_text)
        skipped = _skipped(by_modality, policy)
        by_modality.update(dict.fromkeys(skipped, "skipped"))
        views: list[ViewPart] = []
        for e in entries:
            if isinstance(e, ViewPart):
                views.append(e)
            elif e.modality in skipped:
                views.append(ViewPart(e.part.index, e.label, "skipped", ()))
            else:
                cut = e.cut if e.cut is not None and not e.cut.whole and e.final != "metadata_only" else None
                note = None if cut is None else cut.note
                views.append(ViewPart(e.part.index, e.label, e.final, blocks.get(id(e), ()), cut is not None, note))
        degradations = tuple(dict.fromkeys(d for m in media for d in _degradations(m)))
        return InputView(summary_kind(by_modality), by_modality, tuple(views), degradations)
