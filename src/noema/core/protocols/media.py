"""Media input, views, files and negotiation (spec-02 §6, spec-07, IMPL-074, IMPL-075)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol

from noema.core.clock import Deadline
from noema.core.principal import Principal
from noema.core.values import ContentBlock, Degradation, JsonValue, Modality

if TYPE_CHECKING:
    from noema.config.models import DecisionMemberCfg, ModelCfg

ViewKind = Literal["native", "thumbnail", "ocr_text", "derived_text", "metadata_only", "skipped"]
DerivativeKind = Literal[
    "raster", "raster_1568", "thumbnail", "ocr_text", "pdf_sanitized", "markdown", "page_images", "text"
]
FileStatus = Literal["quarantined", "processing", "ready", "partial", "rejected", "failed", "deleted"]


@dataclass(frozen=True, slots=True)
class DerivativeRef:
    """One derivative of a file. `raster_1568` is internal: never listed in FileObject, never downloadable.
    `text_layer_pages` (page_images only): the 1-based pages whose text layer has a non-space character (the
    pdf.render facts, ADR-002 §5), ascending; the other pages are scanned (OCR'd, spec-07 §3.1).
    `active_content` (pdf_sanitized only, C2b): the distinct spec-09 §6.3 active-content facts the sanitizer
    removed, sorted (the `derivative.facts` column); `()` = nothing removed, None = other kinds or not sanitized
    yet. A non-empty value is the turn's `active_content_removed` degradation (spec-09 §6.3 step 5)."""

    kind: DerivativeKind
    id: str
    status: Literal["pending", "ready", "failed"]
    sha256: str | None
    bytes: int | None = None
    width: int | None = None
    height: int | None = None
    pages: int | None = None
    text_layer_pages: tuple[int, ...] | None = None
    active_content: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class FileRef:
    """Everything the routes need for FileObject and authz (spec-09 §3). `filename` is for display only and never
    reaches a model."""

    file_id: str
    sha256: str
    declared_type: str
    media_type: str | None
    status: FileStatus
    bytes: int
    origin: Literal["upload", "inline"]
    principal_id: str
    session_id: str | None
    filename: str | None
    derivatives: tuple[DerivativeRef, ...]
    pages: int | None = None
    rejection: Mapping[str, JsonValue] | None = None
    failure: Mapping[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class ContentPart:
    """A resolved input part. `label` is "image-1"/"doc-1" for media; `file` is set iff `kind` is image or file
    (inline images are ingested first, IMPL-074). `page_texts` (PDF and Markdown files): the text of each page,
    index i = page i + 1 (the page's Markdown segment, else its OCR segment, else ""; a Markdown upload is one
    page), pre-read by preflight through `FileService.read_derivative` for `MediaBudgetPlanner.plan`; None = not
    read (or not ready)."""

    index: int
    kind: Literal["text", "json", "image", "file"]
    label: str | None
    text: str | None = None
    json: JsonValue = None
    file: FileRef | None = None
    page_texts: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class DecisionViewBudget:
    """Caps on what one decision member may see of the media input (IMPL-075)."""

    max_text_tokens: int
    max_images: int
    max_documents: int
    image_long_edge: int


# The resolver also keeps native images, documents and page images within `CONVERSE_CAPS_V1.max_payload_bytes -
# text_reserve_bytes` of base64, cut like the count caps (spec-07 §5, IMPL-075; part of the versioned view rules).
VIEW_BUDGET_V1: Final = DecisionViewBudget(max_text_tokens=6000, max_images=20, max_documents=5, image_long_edge=1568)


@dataclass(frozen=True, slots=True)
class ViewPart:
    """One input part as one member sees it. Blocks are content only and unescaped; decision renders labels and
    escaping (IMPL-050). `note` is e.g. "pages 1-12 of 600"."""

    part_index: int
    label: str | None
    kind: ViewKind
    blocks: tuple[ContentBlock, ...]
    truncated: bool = False
    note: str | None = None


@dataclass(frozen=True, slots=True)
class InputView:
    """A member's view of the whole input; `kind` is the view of the least adequate modality (spec-06 §4.4)."""

    kind: ViewKind
    by_modality: Mapping[Modality, ViewKind]
    parts: tuple[ViewPart, ...]
    degradations: tuple[Degradation, ...]


class InputViewResolver(Protocol):
    """Builds decision-member views (spec-06 §4.2)."""

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
        """`model` = `snapshot.model(member.model)`; derivative reads go through FileService as `principal`.
        Never raises for media problems: an unreadable or not-ready derivative gives metadata_only plus a
        `not_ready` degradation. The view signature is computed only in decision/signatures.py."""
        ...


class FileService(Protocol):
    """Uploads, inline ingest, status and derivative access (spec-07 §3, §6)."""

    async def upload(
        self,
        principal: Principal,
        stream: AsyncIterator[bytes],
        *,
        declared_type: str,
        filename: str,
        session_id: str | None,
    ) -> FileRef:
        """spec-07 §3.1. Raises `NoemaError` file_too_large (413), unsupported_media_type (415),
        session_not_found (404)."""
        ...

    async def ingest_inline(
        self,
        principal: Principal,
        data_b64: str,
        media_type: str,
        *,
        deadline: Deadline,
        session_id: str | None = None,
    ) -> FileRef:
        """IMPL-074: arithmetic size check, quarantine, a high-priority sandbox job; waits for `raster` only
        (spec-07 §3.2). Raises image_too_large (413), file_rejected (409), file_not_ready (409, the wait
        expired)."""
        ...

    async def get(self, principal: Principal, file_id: str) -> FileRef:
        """Raises `NoemaError(not_found)` for unknown or foreign ids."""
        ...

    async def delete(self, principal: Principal, file_id: str) -> None:
        """Marks the file deleted (deleteFile) and enqueues its purge: the media-owned `file.purge` job calls
        `purge` (S3 objects, then the rows). Raises `NoemaError(not_found)`."""
        ...

    async def purge(self, tenant_id: str, file_id: str) -> None:
        """Removes the file for good: its quarantine and derivative S3 objects, then the file row (derivative rows
        cascade). Used by the file.purge job, session.delete (identity) and the retention sweeper (ops-ledger), so
        only media knows the S3 layout. Runs under `tenant_uow(tenant_id)`; idempotent (absent: no-op); S3
        failures raise (the job retries)."""
        ...

    def watch(self, principal: Principal, file_id: str) -> AsyncIterator[FileRef]:
        """First the current state, then each change; ends at a terminal status (ready, partial, rejected, failed,
        deleted). Woken by `RedisKeys.file_events(file_id)`, with a 2 s poll fallback."""
        ...

    async def open_derivative(
        self, principal: Principal, file_id: str, kind: DerivativeKind, *, page: int | None = None
    ) -> tuple[str, AsyncIterator[bytes]]:
        """`(content type, byte stream)` of one derivative (getFileDerivative, spec-07 §6). Raises
        `NoemaError(not_found)` (unknown or foreign file, no such derivative kind or page),
        `NoemaError(file_not_ready)` (409, the derivative is still pending) and `NoemaError(file_rejected)` (409,
        the file was rejected)."""
        ...

    async def read_derivative(
        self, principal: Principal, file_id: str, kind: DerivativeKind, *, page: int | None = None
    ) -> bytes:
        """The whole derivative, from a per-pod LRU (256 MiB) keyed by (tenant_id, sha256); used for model
        blocks by InputViewResolver and MediaNegotiator."""
        ...


@dataclass(frozen=True, slots=True)
class MediaCaps:
    """Converse limits per request (02 §8.4; algorithm spec-07 §4.5). `text_reserve_bytes` covers text, JSON
    framing and the system prompt; `max_text_tokens` is set per turn by preflight (spec-04 §9)."""

    max_images: int = 20
    max_image_bytes: int = 3_750_000
    max_documents: int = 5
    max_document_bytes: int = 4_500_000
    max_payload_bytes: int = 20_000_000
    text_reserve_bytes: int = 2_000_000
    max_text_tokens: int = 0


CONVERSE_CAPS_V1: Final = MediaCaps()


@dataclass(frozen=True, slots=True)
class PlannedPart:
    """The media plan for one part. `native`: an ImageBlock (image) or DocumentBlock (pdf) slot. `text_pages`:
    pages 1..text_pages of the text are kept (0 = none); `text_cut` is "pages 1-k of n" or "page 1 of n, partial"
    (None if whole); `page_images`: 1-based pdf pages that may be sent as images, ascending. `file_id`: the part's
    file, so a previous-turn part whose `part_index` repeats a current one is found ("" = not set: match by index
    and label). `text_chars`: the kept length of page 1 for "page 1 of n, partial" (None otherwise)."""

    part_index: int
    label: str
    origin: Literal["current", "previous"]
    kind: Literal["image", "pdf", "markdown"]
    native: bool
    text_pages: int
    text_cut: str | None
    page_images: tuple[int, ...]
    file_id: str = ""
    text_chars: int | None = None


@dataclass(frozen=True, slots=True)
class MediaPlan:
    """Model-independent; computed once per turn in preflight. Degradations are `media_budget` only."""

    parts: tuple[PlannedPart, ...]
    degradations: tuple[Degradation, ...]


class MediaNegotiator(Protocol):
    """Builds one attempt's media blocks for one model (spec-07 §4)."""

    async def negotiate(
        self, alias: str, model: ModelCfg, parts: Sequence[ContentPart], plan: MediaPlan, *, principal: Principal
    ) -> tuple[tuple[ContentBlock, ...], tuple[Degradation, ...]]:
        """Once per attempt (fallbacks included); reads bytes with `FileService.read_derivative`. Never raises for
        media problems. Blocks for the media parts only (the prompt builder renders text/json parts itself, spec-09
        §5). Degradations carry `model` (the Bedrock id) and `attempt_id=None`: the attempt id does not exist yet
        when the prompt is built, so the generation engine stamps it (`dataclasses.replace`, spec-07 §4.3)."""
        ...


class MediaBudgetPlanner(Protocol):
    """Plans the turn's media within the request caps."""

    def plan(
        self, turn_parts: Sequence[ContentPart], history_parts: Sequence[ContentPart], caps: MediaCaps
    ) -> MediaPlan:
        """Sync and pure (spec-07 §4.5). The question terms come from the `text` parts of `turn_parts` (the current
        user text); page selection and text budgeting read `ContentPart.page_texts` and the `page_images`
        derivative's `text_layer_pages` (pages without a text layer = scanned)."""
        ...
