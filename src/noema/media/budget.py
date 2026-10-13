"""`MediaBudgetPlanner` (spec-07 §4.5, 02 §8.4) and the pure helpers shared with `media.views` and
`media.negotiation`: part classes, `image-N`/`doc-N` labels, base64 sizes, the chars/4 text measure, the
question-term page selection and the text-budget rule (§4.5 step 2, which is also the `VIEW_BUDGET_V1` text rule of
spec-07 §5).

Everything in this module is sync, pure and deterministic: the same parts and caps always give the same plan.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from noema.config.models import CHARS_PER_TOKEN_V1
from noema.core.protocols.media import (
    ContentPart,
    DerivativeKind,
    DerivativeRef,
    FileRef,
    MediaCaps,
    MediaPlan,
    PlannedPart,
)
from noema.core.values import Degradation

MediaClass = Literal["image", "pdf", "markdown"]
Origin = Literal["current", "previous"]

PAGE_SEPARATOR: Final = "\n\n"  # kept pages are joined with it; the text measure counts it
# Question-term stopwords (spec-07 §4.5 step 1.3): English function words of 4+ letters plus the words every media
# question carries. A code constant: changing it changes page selection, so it is versioned (V1).
_STOPWORDS_V1_TEXT: Final = """
about above after again against also among another anything around because been before being below between both
cannot could does doing done down during each either else even ever every everything from further give given
have having help here hers herself himself into itself just know like look make many more most much must myself
need neither never only other others ours ourselves over please same shall should show some something such tell
than that their theirs them themselves then there these they this those though through thus under until upon
very want were what whatever when where whether which while whom whose will with within without would your yours
yourself yourselves file files document documents page pages image images picture photo attached attachment
"""
STOPWORDS_V1: Final = frozenset(_STOPWORDS_V1_TEXT.split())
_TERM: Final = re.compile(r"\w{4,}")
_WORD: Final = re.compile(r"\w+")
_LABEL: Final = re.compile(r"(image|doc)-([1-9][0-9]{0,5})")
# The least raw size reserved per page image: small pages vary the most against their average (a code constant,
# versioned with the plan rule; 20 such pages reserve 1.75 MB of base64).
PAGE_IMAGE_FLOOR_V1: Final = 65_536


def media_type(file: FileRef) -> str:
    """The detected media type, or the declared one before `detect`; lower-cased, without parameters."""
    return (file.media_type or file.declared_type).split(";", 1)[0].strip().lower()


def classify(part: ContentPart) -> tuple[MediaClass, FileRef] | None:
    """`(image | pdf | markdown, file)` for a media part that has its file; None for text/json parts (and for a
    media part without a `FileRef`, which every caller treats as not ready)."""
    file = part.file
    if part.kind not in ("image", "file") or file is None:
        return None
    mt = media_type(file)
    if part.kind == "image" or mt.startswith("image/"):
        return "image", file
    return ("pdf" if mt == "application/pdf" else "markdown"), file


def derivative(file: FileRef, kind: DerivativeKind) -> DerivativeRef | None:
    """The file's derivative of `kind`, whatever its status."""
    return next((d for d in file.derivatives if d.kind == kind), None)


def ready(file: FileRef, kind: DerivativeKind) -> DerivativeRef | None:
    """The file's derivative of `kind` if it is ready."""
    found = derivative(file, kind)
    return found if found is not None and found.status == "ready" else None


def has_text(file: FileRef, kind: DerivativeKind) -> bool:
    """A ready, non-empty text derivative (by its manifest size)."""
    found = ready(file, kind)
    return found is not None and (found.bytes is None or found.bytes > 0)


def b64_len(n: int) -> int:
    """The base64 size of `n` bytes, 4·ceil(n/3) (the Converse JSON carries binaries as base64)."""
    return 4 * ((n + 2) // 3)


def text_tokens(chars: int) -> int:
    """The `TokenEstimator` default text rule, ceil(chars / 4.0); model-independent (spec-11 §1)."""
    return math.ceil(chars / CHARS_PER_TOKEN_V1)


def page_image_b64(ref: DerivativeRef, max_image_bytes: int) -> int:
    """The base64 size reserved per page image (spec-07 §4.5): b64(ceil(page_images.bytes / pages)), at least
    b64(`PAGE_IMAGE_FLOOR_V1`), or b64(`max_image_bytes`) when the manifest has no size. Real pages vary, so senders
    keep a part's page images within `len(page_images)` times this (`page_images_within`)."""
    pages = ref.pages or 1
    total = max_image_bytes * pages if ref.bytes is None else ref.bytes
    return b64_len(min(max_image_bytes, max(-(-total // pages), PAGE_IMAGE_FLOOR_V1)))


def page_images_within(sizes: Sequence[int | None], reserved: int, max_image_bytes: int) -> list[int]:
    """The indexes of the page images to send, in order: each read (size not None), at most `max_image_bytes`, and
    within what is left of the `reserved` base64 budget; a page that would overrun it is dropped, later smaller
    pages may still fit."""
    keep: list[int] = []
    for i, size in enumerate(sizes):
        if size is None or size > max_image_bytes or b64_len(size) > reserved:
            continue
        reserved -= b64_len(size)
        keep.append(i)
    return keep


def scanned_pages(ref: DerivativeRef | None) -> frozenset[int]:
    """The 1-based pages without a text layer, from `page_images.text_layer_pages`; empty if unknown."""
    if ref is None or ref.pages is None or ref.text_layer_pages is None:
        return frozenset()
    return frozenset(range(1, ref.pages + 1)) - frozenset(ref.text_layer_pages)


class Labeler:
    """Numbers images and documents separately, 1-based, in call order (spec-07 §4.1). A given label is kept only if
    it is a well-formed, unused `image-N`/`doc-N` of the right class; anything else (a filename, a clash) is
    replaced by the next free number, so a filename can never become a label (spec-09 §5 item 5)."""

    __slots__ = ("_next", "_used")

    def __init__(self) -> None:
        self._used: set[str] = set()
        self._next = {"image": 1, "doc": 1}

    def __call__(self, cls: MediaClass, given: str | None = None) -> str:
        prefix = "image" if cls == "image" else "doc"
        match = _LABEL.fullmatch(given) if given is not None else None
        if given is not None and match is not None and match.group(1) == prefix and given not in self._used:
            self._used.add(given)
            return given
        n = self._next[prefix]
        while f"{prefix}-{n}" in self._used:
            n += 1
        self._next[prefix] = n + 1
        label = f"{prefix}-{n}"
        self._used.add(label)
        return label


@dataclass(frozen=True, slots=True)
class TextCut:
    """What one derived-text item keeps of its share (§4.5 step 2): `pages` whole pages from page 1, or, when
    `chars` is set, the first `chars` characters of page 1 (a partial page); `total` pages; `tokens` used."""

    pages: int
    total: int
    tokens: int
    chars: int | None = None

    @property
    def whole(self) -> bool:
        """Nothing was cut."""
        return self.chars is None and self.pages >= self.total

    @property
    def empty(self) -> bool:
        """Nothing at all was kept."""
        return self.chars == 0 or (self.chars is None and self.pages == 0 and self.total > 0)

    @property
    def note(self) -> str | None:
        """`pages 1-k of n` (ASCII hyphen), `page 1 of n, partial`, or None when whole (spec-07 §5)."""
        if self.whole:
            return None
        if self.chars is not None:
            return f"page 1 of {self.total}, partial"
        return f"pages 1-{self.pages} of {self.total}"


def _size(page: str) -> int:
    return 0 if not page or page.isspace() else len(page)


def fit_pages(pages: Sequence[str], share: int) -> TextCut:
    """Whole pages from page 1 while the joined text fits `share` tokens; if not even page 1 fits, page 1 is cut at
    the last `\\n` within the share, or at the share's last character when there is no newline there or only blank
    text before it (so a non-zero share always keeps something). Whitespace-only pages cost nothing and are not
    rendered (`kept_text`)."""
    chars = kept = rendered = 0
    for page in pages:
        size = _size(page)
        add = size + (len(PAGE_SEPARATOR) if size and rendered else 0)
        if text_tokens(chars + add) > share:
            break
        chars += add
        kept += 1
        rendered += size > 0
    if kept > 0 or not pages:
        return TextCut(kept, len(pages), text_tokens(chars))
    limit = max(0, int(share * CHARS_PER_TOKEN_V1))
    first = pages[0]
    newline = first.rfind("\n", 0, limit + 1)
    cut = newline if newline > 0 and not first[:newline].isspace() else min(limit, len(first))
    return TextCut(0, len(pages), text_tokens(cut), chars=cut)


def allocate_text(items: Sequence[Sequence[str]], max_tokens: int) -> list[TextCut]:
    """§4.5 step 2: the items share `max_tokens` in order; each takes floor(remaining / items left), and what it
    leaves unused passes to the next item."""
    remaining = max(0, max_tokens)
    cuts: list[TextCut] = []
    for i, pages in enumerate(items):
        cut = fit_pages(pages, remaining // (len(items) - i))
        remaining -= cut.tokens
        cuts.append(cut)
    return cuts


def kept_text(pages: Sequence[str], *, text_cut: str | None, text_pages: int, text_chars: int | None) -> str:
    """The text a plan (or a view cut) keeps: every page when `text_cut` is None, else the partial page 1
    (`text_chars`) or pages 1..`text_pages`, joined with `PAGE_SEPARATOR` (whitespace-only pages dropped)."""
    if text_cut is not None and text_chars is not None:
        return pages[0][:text_chars] if pages else ""
    kept = pages if text_cut is None else pages[: max(0, text_pages)]
    return PAGE_SEPARATOR.join(p for p in kept if _size(p))


def question_terms(turn_parts: Sequence[ContentPart]) -> frozenset[str]:
    """The current user text's terms: casefolded `\\w{4,}` words minus `STOPWORDS_V1` (§4.5 step 1.3)."""
    text = "\n".join(p.text for p in turn_parts if p.kind == "text" and p.text)
    return frozenset(_TERM.findall(text.casefold())) - STOPWORDS_V1


def _matching(terms: frozenset[str], page_texts: Sequence[str], n: int) -> list[int]:
    scored: list[tuple[int, int]] = []
    for page, text in enumerate(page_texts[:n], start=1):
        folded = text.casefold()
        candidates = [t for t in terms if t in folded]  # cheap substring pre-filter, then whole-word tokens
        if candidates:
            hits = len(set(candidates).intersection(_WORD.findall(folded)))
            if hits:
                scored.append((-hits, page))
    return [page for _, page in sorted(scored)]


def page_priority(
    n: int, terms: frozenset[str], page_texts: Sequence[str] | None, scanned: frozenset[int]
) -> Iterator[int]:
    """The page-image candidate order of §4.5 step 1.3, distinct pages lazily: pages 1-2; pages matching the
    question terms (more distinct terms first, then lower page); pages without a text layer; the rest in order."""
    seen: set[int] = set()

    def fresh(pages: Sequence[int] | range) -> Iterator[int]:
        for page in pages:
            if page not in seen:
                seen.add(page)
                yield page

    yield from fresh(range(1, min(2, n) + 1))
    if terms and page_texts:
        yield from fresh(_matching(terms, page_texts, n))
    yield from fresh(sorted(scanned))
    yield from fresh(range(1, n + 1))


@dataclass(slots=True)
class _Draft:
    part: ContentPart
    file: FileRef
    cls: MediaClass
    origin: Origin
    label: str
    native: bool = False
    page_images: tuple[int, ...] = ()
    cut: TextCut | None = None


class MediaBudgetPlannerImpl:
    """Implements `MediaBudgetPlanner` (spec-07 §4.5): allocates image and document slots and the binary payload
    budget to the current turn first, then the previous media-bearing turn; selects PDF page images; shares
    `caps.max_text_tokens` among the derived-text items; reports every cut as a `media_budget` degradation.

    Each allocation covers the worst case of every model row (a native document and all its page images; every
    planned page image), so no attempt of any model exceeds `caps`. A page image is reserved at the manifest's
    average page size (`page_image_b64`); the negotiator keeps each part's real page images within that reservation.
    Images and PDFs whose derivative is not ready, and PDFs over `max_document_bytes`, take no slot: the negotiator
    reports those per attempt. Given `image-N`/`doc-N` labels are kept for both turns when well-formed and unused;
    a text item without `page_texts` is estimated from its manifest sizes (`_fit_unmeasured`)."""

    def plan(
        self, turn_parts: Sequence[ContentPart], history_parts: Sequence[ContentPart], caps: MediaCaps
    ) -> MediaPlan:
        """Sync and pure; see the class docstring."""
        terms = question_terms(turn_parts)
        labeler = Labeler()
        images_left, docs_left = caps.max_images, caps.max_documents
        binary_left = caps.max_payload_bytes - caps.text_reserve_bytes
        drafts: list[_Draft] = []
        cuts: list[Degradation] = []
        origins: tuple[tuple[Origin, Sequence[ContentPart]], ...] = (
            ("current", turn_parts),
            ("previous", history_parts),
        )
        for origin, parts in origins:
            batch = [
                _Draft(p, found[1], found[0], origin, labeler(found[0], p.label))
                for p in parts
                if (found := classify(p)) is not None
            ]
            drafts += batch
            for d in batch:  # 1.1 images
                raster = ready(d.file, "raster_1568")
                size = caps.max_image_bytes if raster is None or raster.bytes is None else raster.bytes
                if d.cls != "image" or raster is None or size > caps.max_image_bytes:
                    continue
                if images_left > 0 and b64_len(size) <= binary_left:
                    d.native, images_left, binary_left = True, images_left - 1, binary_left - b64_len(size)
                else:
                    cuts.append(_cut(d, "ocr_text" if has_text(d.file, "ocr_text") else "omitted"))
            for d in batch:  # 1.2 PDFs
                pdf = ready(d.file, "pdf_sanitized")
                size = caps.max_document_bytes if pdf is None or pdf.bytes is None else pdf.bytes
                if d.cls != "pdf" or pdf is None or size > caps.max_document_bytes:
                    continue
                if docs_left > 0 and b64_len(size) <= binary_left:
                    d.native, docs_left, binary_left = True, docs_left - 1, binary_left - b64_len(size)
                else:
                    cuts.append(_cut(d, "markdown"))
            pdfs = [d for d in batch if d.cls == "pdf"]
            for j, d in enumerate(pdfs):  # 1.3 page images
                share = images_left // (len(pdfs) - j)
                pages_ref = ready(d.file, "page_images")
                if share <= 0 or pages_ref is None or not pages_ref.pages:
                    continue
                per_page = page_image_b64(pages_ref, caps.max_image_bytes)
                chosen: list[int] = []
                for page in page_priority(pages_ref.pages, terms, d.part.page_texts, scanned_pages(pages_ref)):
                    if len(chosen) >= share or per_page > binary_left:
                        break
                    chosen.append(page)
                    binary_left -= per_page
                images_left -= len(chosen)
                d.page_images = tuple(sorted(chosen))
        items = [d for d in drafts if d.cls != "image"]  # 2. text: every PDF and Markdown part, current first
        remaining = max(0, caps.max_text_tokens)
        for i, d in enumerate(items):
            share = remaining // (len(items) - i)  # the allocate_text rule, with unmeasured items estimated
            pages = d.part.page_texts
            d.cut = cut = fit_pages(pages, share) if pages is not None else _fit_unmeasured(d, share)
            remaining -= cut.tokens
            if not cut.whole:
                cuts.append(_cut(d, "omitted" if cut.empty else f"markdown({cut.note})"))
        return MediaPlan(tuple(_planned(d) for d in drafts), tuple(cuts))


def _fit_unmeasured(d: _Draft, share: int) -> TextCut:
    """A text item whose `page_texts` were not read: kept whole when an upper bound of its rendered text (the
    `markdown` plus, for a PDF, the `ocr_text` manifest bytes) fits the share; otherwise page 1 is hard-cut at the
    share. Nothing is sent without a ready `markdown` (the negotiator reads ready derivatives only)."""
    n = 1 if d.cls == "markdown" else d.file.pages or 1
    markdown = ready(d.file, "markdown")
    if markdown is None:
        return TextCut(n, n, 0)
    ocr = ready(d.file, "ocr_text") if d.cls == "pdf" else None
    ocr_bytes = 0 if ocr is None else ocr.bytes
    if markdown.bytes is not None and ocr_bytes is not None:
        tokens = text_tokens(markdown.bytes + ocr_bytes + n)  # chars <= UTF-8 bytes; + n for the page joins
        if tokens <= share:
            return TextCut(n, n, tokens)
    return TextCut(0, n, share, chars=int(share * CHARS_PER_TOKEN_V1))


def _cut(d: _Draft, to: str) -> Degradation:
    index = d.part.index if d.origin == "current" else None  # a history part's index is not this turn's
    return Degradation(media_type(d.file), to, "media_budget", file_id=d.file.file_id, part_index=index)


def _planned(d: _Draft) -> PlannedPart:
    cut = d.cut
    text_pages = 0 if cut is None else cut.pages  # images have no text cut
    return PlannedPart(
        part_index=d.part.index,
        label=d.label,
        origin=d.origin,
        kind=d.cls,
        native=d.native,
        text_pages=text_pages,
        text_cut=None if cut is None else cut.note,
        page_images=d.page_images,
        file_id=d.file.file_id,
        text_chars=None if cut is None else cut.chars,
    )
