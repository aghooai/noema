"""Test helpers for media negotiation: a scripted `FileService` (derivative bytes only), file builders with exact
derivative facts, model configs from the shipped example, and a local enforcer of the Converse request limits
(02 §8.4) standing in for the enforcing wire mock (spec-10 §2.2)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, cast

import anyio.lowlevel
from tests.contract_internal.kits import principal as make_principal
from tests.fakes.config import example_snapshot

from noema.config.models import DecisionMemberCfg, ModelCfg
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.media import ContentPart, DerivativeKind, DerivativeRef, FileRef, FileService, FileStatus
from noema.core.values import ContentBlock, DocumentBlock, ImageBlock, TextBlock

MODELS: Final = ("haiku-5.5", "opus-5.5", "sonnet-5.5", "luna-6", "sol-6.1", "deepseek-3.2", "glm-5.3")
DOC_MODELS: Final = ("haiku-5.5", "opus-5.5", "sonnet-5.5")
IMAGE_ONLY_MODELS: Final = ("luna-6", "sol-6.1")
TEXT_MODELS: Final = ("deepseek-3.2", "glm-5.3")
PNG: Final = b"\x89PNG\r\n\x1a\n"
JPEG: Final = b"\xff\xd8\xff\xe0"
PDF: Final = b"%PDF-1.7\n"
_SNAPSHOT: Final = example_snapshot()


def model(alias: str) -> ModelCfg:
    """The example config's `models.<alias>`."""
    return _SNAPSHOT.model(alias)


def member(alias: str) -> DecisionMemberCfg:
    """A decision member on `alias` (the resolver reads only the model's modalities)."""
    return DecisionMemberCfg(model=alias, elicitation="verbalized", template="verbalized-dist@1", weight=1.0)


def principal() -> Principal:
    return make_principal()


def blob(magic: bytes, size: int) -> bytes:
    """`size` bytes starting with `magic`."""
    return (magic + b"\x00" * size)[:size]


@dataclass
class StubFiles:
    """`FileService.read_derivative` from a script: `data[(file_id, kind, page)]` -> bytes. `fail` keys raise
    (a missing key is NoemaError not_found, like the real service); `reads` logs every call."""

    data: dict[tuple[str, str, int | None], bytes] = field(default_factory=dict)
    fail: set[tuple[str, str, int | None]] = field(default_factory=set)
    reads: list[tuple[str, str, int | None]] = field(default_factory=list)

    async def read_derivative(
        self, principal: Principal, file_id: str, kind: DerivativeKind, *, page: int | None = None
    ) -> bytes:
        await anyio.lowlevel.checkpoint()
        key = (file_id, str(kind), page)
        self.reads.append(key)
        if key in self.fail:
            raise OSError("s3 down")
        if key not in self.data:
            raise NoemaError(ErrorCode.NOT_FOUND, "Derivative not found.")
        return self.data[key]

    def service(self) -> FileService:
        return cast(FileService, self)


DerivativeStatus = Literal["pending", "ready", "failed"]


def _ref(
    kind: DerivativeKind,
    size: int | None,
    status: DerivativeStatus = "ready",
    *,
    pages: int | None = None,
    text_layer_pages: tuple[int, ...] | None = None,
    width: int | None = None,
    height: int | None = None,
) -> DerivativeRef:
    return DerivativeRef(kind, f"der_{kind}", status, "0" * 64, size, width, height, pages, text_layer_pages)


def image_file(
    files: StubFiles,
    fid: str,
    *,
    raster_bytes: int = 200_000,
    ocr: str | None = "INVOICE 4471",
    ocr_status: DerivativeStatus = "ready",
    raster_status: DerivativeStatus = "ready",
    status: FileStatus = "ready",
    media_type: str = "image/png",
    filename: str | None = "photo.png",
    jpeg: bool = False,
) -> FileRef:
    """An image file with `raster_1568` (`raster_bytes`) and an optional `ocr_text`."""
    files.data[(fid, "raster_1568", None)] = blob(JPEG if jpeg else PNG, raster_bytes)
    derivatives = [
        _ref("raster", raster_bytes),
        _ref("raster_1568", raster_bytes, raster_status, width=1568, height=1176),
    ]
    if ocr is not None:
        files.data[(fid, "ocr_text", None)] = ocr.encode()
        derivatives.append(_ref("ocr_text", len(ocr.encode()), ocr_status))
    return FileRef(
        fid,
        "a" * 64,
        media_type,
        media_type,
        status,
        raster_bytes,
        "upload",
        "prn_dev",
        None,
        filename,
        tuple(derivatives),
    )


def pdf_file(
    files: StubFiles,
    fid: str,
    markdown: Sequence[str],
    *,
    sanitized_bytes: int = 100_000,
    text_layer: Sequence[int] | None = None,
    ocr: Sequence[str] | None = None,
    page_image_bytes: int = 50_000,
    page_images: bool = True,
    status: FileStatus = "ready",
    filename: str | None = "report.pdf",
) -> FileRef:
    """A PDF of `len(markdown)` pages: `markdown` segments (`\\f`-joined), `text_layer` pages (default: every page
    with Markdown text), optional `ocr` segments, and `page_images` of `page_image_bytes` each."""
    n = len(markdown)
    layer = tuple(text_layer) if text_layer is not None else tuple(i for i, t in enumerate(markdown, 1) if t.strip())
    files.data[(fid, "pdf_sanitized", None)] = blob(PDF, sanitized_bytes)
    files.data[(fid, "markdown", None)] = "\f".join(markdown).encode()
    derivatives = [_ref("pdf_sanitized", sanitized_bytes), _ref("markdown", len("\f".join(markdown).encode()))]
    if page_images:
        for page in range(1, n + 1):
            files.data[(fid, "page_images", page)] = blob(PNG, page_image_bytes)
        derivatives.append(
            _ref("page_images", page_image_bytes * n, pages=n, text_layer_pages=layer, width=1210, height=1568)
        )
    if ocr is not None:
        files.data[(fid, "ocr_text", None)] = "\f".join(ocr).encode()
        derivatives.append(_ref("ocr_text", len("\f".join(ocr).encode())))
    return FileRef(
        fid,
        "b" * 64,
        "application/pdf",
        "application/pdf",
        status,
        sanitized_bytes,
        "upload",
        "prn_dev",
        None,
        filename,
        tuple(derivatives),
        pages=n,
    )


def markdown_file(files: StubFiles, fid: str, text: str, *, filename: str | None = "notes.md") -> FileRef:
    """A Markdown upload (one page)."""
    files.data[(fid, "markdown", None)] = text.encode()
    return FileRef(
        fid,
        "c" * 64,
        "text/markdown",
        "text/markdown",
        "ready",
        len(text),
        "upload",
        "prn_dev",
        None,
        filename,
        (_ref("markdown", len(text.encode())),),
    )


def page_texts(markdown: Sequence[str], ocr: Sequence[str] | None = None) -> tuple[str, ...]:
    """What preflight pre-reads into `ContentPart.page_texts`: per page the Markdown segment, else the OCR one."""
    ocr = ocr or ()
    return tuple(md if md.strip() else (ocr[i] if i < len(ocr) else "") for i, md in enumerate(markdown))


def part(index: int, file: FileRef, label: str | None = None, *, texts: Sequence[str] | None = None) -> ContentPart:
    kind: Literal["image", "file"] = "image" if file.declared_type.startswith("image/") else "file"
    return ContentPart(index, kind, label, file=file, page_texts=None if texts is None else tuple(texts))


def text(index: int, value: str) -> ContentPart:
    return ContentPart(index, "text", None, text=value)


_DOC_NAME: Final = re.compile(r"[A-Za-z0-9\-()\[\]]+( [A-Za-z0-9\-()\[\]]+)*")


def b64(n: int) -> int:
    return 4 * ((n + 2) // 3)


def converse_violations(blocks: Sequence[ContentBlock], *, system_bytes: int = 0) -> list[str]:
    """The Bedrock Converse limits an enforcing mock applies to one user message (02 §8.4): at most 20 images, each
    at most 3,750,000 B and 8,000 px; at most 5 documents, each at most 4,500,000 B with a valid generated name and a
    text block in the same message; a request payload of about 20 MB (binaries as base64)."""
    images = [b for b in blocks if isinstance(b, ImageBlock)]
    documents = [b for b in blocks if isinstance(b, DocumentBlock)]
    texts = [b for b in blocks if isinstance(b, TextBlock)]
    problems: list[str] = []
    if len(images) > 20:
        problems.append(f"{len(images)} images")
    problems += [f"image of {len(b.data)} B" for b in images if len(b.data) > 3_750_000]
    problems += [f"image of {b.width}x{b.height}" for b in images if max(b.width, b.height) > 8000]
    if len(documents) > 5:
        problems.append(f"{len(documents)} documents")
    problems += [f"document of {len(b.data)} B" for b in documents if len(b.data) > 4_500_000]
    problems += [f"document name {b.name!r}" for b in documents if len(b.name) > 200 or not _DOC_NAME.fullmatch(b.name)]
    if documents and not texts:
        problems.append("documents without a text block")
    payload = system_bytes + sum(b64(len(b.data)) for b in images + documents)
    payload += sum(len(b.text.encode()) for b in texts)
    if payload > 20_000_000:
        problems.append(f"payload {payload} B")
    return problems
