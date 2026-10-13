"""Upload intake (spec-07 §1-§2, IMPL-029g, IMPL-037, IMPL-062): the Phase 1 type-equivalence map, the synchronous
pre-check (415 before any byte is stored), the per-type size caps, the inline-image arithmetic size check and the
matching of the sandbox `detect` facts against the map. Pure stdlib: the API process never parses media.

HEIC deviation (request filed, media-pipeline.md item 1): the pinned Magika 1.0.3 model has no `heic`/`heif` output
label; it labels HEIC/HEIF files `mp4` (ISO-BMFF). The heic row therefore lists `mp4` as its Magika label; libmagic
(`image/heic`, `image/heif`) still has to agree, so a real MP4 video is never accepted (its libmagic type is in no
row).
"""

from __future__ import annotations

import base64
import binascii
import unicodedata
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import unquote

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser

from noema.core.errors import ErrorCode, NoemaError
from noema.core.values import JsonValue

MediaKind = Literal["image", "pdf", "markdown"]
RejectionCode = Literal[
    "type_mismatch",
    "not_allowlisted",
    "too_many_pages",
    "pixel_limit",
    "encrypted_pdf",
    "decompression_bomb",
    "corrupt",
    "empty_content",
]

MIB: Final = 1024 * 1024
IMAGE_CAP_BYTES: Final = 20 * MIB  # 20,971,520
PDF_CAP_BYTES: Final = 50 * MIB  # 52,428,800 (= the IMPL-064 body cap)
MARKDOWN_CAP_BYTES: Final = 5 * MIB  # 5,242,880
MAX_IMAGE_PIXELS: Final = 89_478_485
MAX_INLINE_IMAGE_BYTES: Final = 3_750_000  # decoded bytes (IMPL-037)
MAX_INLINE_LONG_EDGE: Final = 8_000
MAX_PDF_PAGES: Final = 600


@dataclass(frozen=True, slots=True)
class TypeRow:
    """One row of the spec-07 §2 equivalence map plus what the pipeline needs for it."""

    name: str
    kind: MediaKind
    declared: tuple[str, ...]
    extensions: tuple[str, ...]
    magika: tuple[str, ...]
    libmagic: tuple[str, ...]
    cap_bytes: int
    pil_format: str | None = None  # the `image.normalize` params.format (spec-09 §6.4)
    inline_ext: str | None = None  # FileObject.filename of inline files: "{file_id}.{inline_ext}" (spec-07 §3.3)


TYPE_ROWS: Final[tuple[TypeRow, ...]] = (
    TypeRow("png", "image", ("image/png",), (".png",), ("png",), ("image/png",), IMAGE_CAP_BYTES, "PNG", "png"),
    TypeRow(
        "jpeg", "image", ("image/jpeg",), (".jpg", ".jpeg"), ("jpeg",), ("image/jpeg",), IMAGE_CAP_BYTES, "JPEG", "jpg"
    ),
    TypeRow("webp", "image", ("image/webp",), (".webp",), ("webp",), ("image/webp",), IMAGE_CAP_BYTES, "WEBP", "webp"),
    TypeRow("gif", "image", ("image/gif",), (".gif",), ("gif",), ("image/gif",), IMAGE_CAP_BYTES, "GIF", "gif"),
    TypeRow(
        "heic",
        "image",
        ("image/heic", "image/heif"),
        (".heic", ".heif"),
        ("mp4",),  # see the module docstring: spec-07 lists heic, heif (absent from the pinned model)
        ("image/heic", "image/heif"),
        IMAGE_CAP_BYTES,
        "HEIF",
        "heic",
    ),
    TypeRow("pdf", "pdf", ("application/pdf",), (".pdf",), ("pdf",), ("application/pdf",), PDF_CAP_BYTES),
    TypeRow(
        "markdown",
        "markdown",
        ("text/markdown", "text/plain"),
        (".md", ".markdown"),
        ("markdown", "txt", "unknown"),
        ("text/plain", "text/markdown", "text/x-diff"),
        MARKDOWN_CAP_BYTES,
    ),
)
# Every Magika label whose `ContentTypeInfo.is_text` is true in the pinned model (magika 1.0.3; a test compares the
# set with the model): the Markdown row accepts them all, under its strict UTF-8 / no NUL condition (spec-07 §2;
# Magika gives some tiny Markdown texts other text labels, e.g. `gitattributes` for "# hi\n\nsome text\n").
_TEXT_LABELS: Final = (
    "aidl appleplist asm asp autohotkey autoit awk batch bazel bib c clojure cmake cobol coffeescript cpp cs "
    "csproj css csv dart diff dm dockerfile dxf elixir eml erb erlang fortran gemfile gemspec gitattributes "
    "gitmodules go gradle groovy handlebars haskell hcl htaccess html ics ignorefile ini internetshortcut "
    "ipynb java javascript jinja json jsonl julia kotlin latex lisp lua m3u m4 makefile markdown matlab mht "
    "mum objectivec ocaml pascal pem perl php po powershell prolog proteindb proto python r rdf rst rtf ruby "
    "rust scala scss sgml shell smali solidity sql srt stltext sum svg swift tcl textproto toml tsv twig txt "
    "typescript vba vcxproj verilog vhdl vtt vue winregistry xml yaml yara zig"
)
MAGIKA_TEXT_LABELS: Final = frozenset(_TEXT_LABELS.split())
ROWS_BY_NAME: Final[Mapping[str, TypeRow]] = {r.name: r for r in TYPE_ROWS}
_DECLARED: Final = frozenset(t for r in TYPE_ROWS for t in r.declared)
_BY_EXTENSION: Final[Mapping[str, TypeRow]] = {e: r for r in TYPE_ROWS for e in r.extensions}
_IMAGE_BY_DECLARED: Final[Mapping[str, TypeRow]] = {t: r for r in TYPE_ROWS if r.kind == "image" for t in r.declared}


def normalize_declared_type(content_type: str) -> str:
    """The declared type: lower-cased, parameters (`; charset=...`) stripped (spec-07 §2.1)."""
    return content_type.split(";", 1)[0].strip().lower()


def extension_of(filename: str) -> str:
    """The lower-cased suffix after the last `.` of the NFC-normalized name, with the dot; "" if there is none."""
    name = unicodedata.normalize("NFC", filename).replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    suffix = name.rsplit(".", 1)[1].lower()
    return f".{suffix}" if suffix else ""


@dataclass(frozen=True, slots=True)
class PreCheck:
    """The accepted pre-check of one upload: normalized declared type, extension and the byte cap."""

    declared: str
    extension: str
    cap_bytes: int


def precheck_upload(content_type: str, filename: str) -> PreCheck:
    """spec-07 §2.1: the declared type must be in some row's declared types and the extension in some row's
    extensions (not necessarily the same row; `detect` decides), else 415 `unsupported_media_type`. The cap is the
    extension's row cap (spec-07 §3.1 step 2)."""
    declared = normalize_declared_type(content_type)
    ext = extension_of(filename)
    row = _BY_EXTENSION.get(ext)
    if declared not in _DECLARED or row is None:
        raise NoemaError(ErrorCode.UNSUPPORTED_MEDIA_TYPE, "This file type is not supported.")
    return PreCheck(declared, ext, row.cap_bytes)


def inline_row(media_type: str) -> TypeRow:
    """The image row of an inline data-URL type (no extension check, spec-07 §2.1), else 415."""
    row = _IMAGE_BY_DECLARED.get(normalize_declared_type(media_type))
    if row is None:
        raise NoemaError(ErrorCode.UNSUPPORTED_MEDIA_TYPE, "This image type is not supported.")
    return row


def inline_decoded_size(data_b64: str) -> int:
    """The decoded size of a base64 payload computed arithmetically, without decoding (IMPL-037):
    `len(b64) * 3 // 4 - padding`."""
    n = len(data_b64)
    padding = 2 if data_b64.endswith("==") else 1 if data_b64.endswith("=") else 0
    return max(0, n * 3 // 4 - padding)


def check_inline_size(data_b64: str) -> None:
    """413 `image_too_large` when the decoded size exceeds 3,750,000 bytes (checked before any decode or job)."""
    if inline_decoded_size(data_b64) > MAX_INLINE_IMAGE_BYTES:
        raise NoemaError(
            ErrorCode.IMAGE_TOO_LARGE,
            f"The image exceeds {MAX_INLINE_IMAGE_BYTES} decoded bytes.",
            details={"max_bytes": MAX_INLINE_IMAGE_BYTES},
        )


def decode_inline(data_b64: str) -> bytes:
    """Byte-level base64 decode (not a media decode). Invalid base64 is a `corrupt` rejection (409)."""
    try:
        return base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        raise rejected_error("corrupt") from None


REJECTION_MESSAGES: Final[Mapping[str, str]] = {
    "type_mismatch": "The file content does not match its declared type or extension.",
    "not_allowlisted": "The file content is not an allowed type.",
    "too_many_pages": f"The document has more than {MAX_PDF_PAGES} pages.",
    "pixel_limit": "The image has too many pixels.",
    "encrypted_pdf": "The PDF is encrypted.",
    "decompression_bomb": "The file decompresses beyond the allowed limits.",
    "corrupt": "The file is corrupt or truncated.",
    "empty_content": "The file is empty.",
}
FAILURE_MESSAGES: Final[Mapping[str, str]] = {
    "extraction_failed": "Extraction failed.",
    "ocr_failed": "Text recognition failed.",
    "timeout": "Extraction timed out.",
}


def rejection_object(code: str) -> dict[str, JsonValue]:
    """The openapi `FileObject.rejection` object of a rejection code."""
    return {"code": code, "message": REJECTION_MESSAGES.get(code, "The file was rejected.")}


def failure_object(code: str) -> dict[str, JsonValue]:
    """The openapi `FileObject.failure` object of a failure code (`retryable` is the constant true)."""
    return {"code": code, "message": FAILURE_MESSAGES.get(code, "Extraction failed."), "retryable": True}


def rejected_error(code: str) -> NoemaError:
    """409 `file_rejected` with `details.rejection` (IMPL-074)."""
    return NoemaError(ErrorCode.FILE_REJECTED, "The file was rejected.", details={"rejection": rejection_object(code)})


@dataclass(frozen=True, slots=True)
class Detected:
    """`detect` accepted the file as `row`."""

    row: TypeRow


@dataclass(frozen=True, slots=True)
class Rejected:
    """`detect` (or a later job) rejected the file."""

    code: RejectionCode


def _text_ok(facts: Mapping[str, JsonValue]) -> bool:
    return facts.get("utf8_strict") is True and facts.get("has_nul") is False


def match_detection(facts: Mapping[str, JsonValue], declared: str, extension: str | None) -> Detected | Rejected:
    """spec-07 §2.2 steps 0-4 over the `detect` facts. `extension` is None for inline images (no extension check).
    The Markdown row needs strict UTF-8 without NUL bytes, and accepts any `text/*` libmagic type and any Magika
    text label (`MAGIKA_TEXT_LABELS`)."""
    size = facts.get("bytes")
    if not isinstance(size, int) or size == 0:
        return Rejected("empty_content")
    label, mime = facts.get("magika_label"), facts.get("libmagic_mime")
    text_ok = _text_ok(facts)
    by_magika = {
        r.name
        for r in TYPE_ROWS
        if (label in r.magika or (r.kind == "markdown" and label in MAGIKA_TEXT_LABELS))
        and (r.kind != "markdown" or text_ok)
    }
    by_libmagic = {
        r.name
        for r in TYPE_ROWS
        if isinstance(mime, str)
        and (mime in r.libmagic or (r.kind == "markdown" and mime.startswith("text/")))
        and (r.kind != "markdown" or text_ok)
    }
    both = by_magika & by_libmagic
    if not both:
        return Rejected("not_allowlisted" if not by_magika or not by_libmagic else "type_mismatch")
    row = ROWS_BY_NAME[sorted(both)[0]]  # rows are disjoint: exactly one
    if normalize_declared_type(declared) not in row.declared:
        return Rejected("type_mismatch")
    if extension is not None and extension not in row.extensions:
        return Rejected("type_mismatch")
    return Detected(row)


# --- multipart/form-data (uploadFile): parameters, file names and an incremental reader -----------------------------
MAX_FILENAME_CHARS: Final = 255
MAX_KEPT_SUFFIX_CHARS: Final = 32  # a cut display name keeps a suffix up to this long (".png", ".markdown")


def _split_params(value: str) -> list[str]:
    parts, current, quoted, escaped = [], [], False, False
    for ch in value:
        if escaped:
            current.append(ch)
            escaped = False
        elif quoted and ch == "\\":
            current.append(ch)
            escaped = True
        elif ch == '"':
            current.append(ch)
            quoted = not quoted
        elif ch == ";" and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts]


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        inner, out, i = value[1:-1], [], 0
        while i < len(inner):
            if inner[i] == "\\" and i + 1 < len(inner):
                i += 1
            out.append(inner[i])
            i += 1
        return "".join(out)
    return value


def header_params(value: str) -> tuple[str, dict[str, str]]:
    """`(lower-cased main value, {lower-cased name: value})` of a structured header such as Content-Type or
    Content-Disposition; quoted values are unescaped; the first occurrence of a parameter wins."""
    head, *rest = _split_params(value)
    params: dict[str, str] = {}
    for item in rest:
        name, sep, raw = item.partition("=")
        if sep and name.strip():
            params.setdefault(name.strip().lower(), _unquote(raw.strip()))
    return head.strip().lower(), params


def _rfc5987(value: str) -> str | None:
    charset, sep, rest = value.partition("'")
    _lang, sep2, encoded = rest.partition("'")
    if not (sep and sep2) or charset.lower() not in ("utf-8", "iso-8859-1"):
        return None
    try:
        return unquote(encoded, encoding=charset.lower(), errors="strict")
    except (UnicodeDecodeError, LookupError):
        return None


def decoded_filename(params: Mapping[str, str]) -> str | None:
    """The decoded upload name (RFC 7578 `filename`, else RFC 5987 `filename*`), NFC-normalized, without any path
    and control characters, not shortened (the part header bounds it to 16 KiB): the spec-07 §2.1 extension is taken
    from it. None when the part has neither parameter."""
    raw = params.get("filename")
    if raw is None and "filename*" in params:
        raw = _rfc5987(params["filename*"])
    if raw is None:
        return None
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf"))


def shorten_filename(name: str) -> str:
    """At most 255 characters: a longer name loses the end of its stem and keeps its suffix (up to 32
    characters), so the shortened name has the same extension."""
    if len(name) <= MAX_FILENAME_CHARS:
        return name
    stem, dot, suffix = name.rpartition(".")
    if dot and stem and len(suffix) < MAX_KEPT_SUFFIX_CHARS:
        return f"{stem[: MAX_FILENAME_CHARS - len(suffix) - 1]}.{suffix}"
    return name[:MAX_FILENAME_CHARS]


def display_filename(params: Mapping[str, str]) -> str | None:
    """`decoded_filename`, shortened to 255 characters (`shorten_filename`). Display only: storage names are
    server-generated and the name never reaches a model (spec-09 §6.1)."""
    name = decoded_filename(params)
    return None if name is None else shorten_filename(name)


def multipart_boundary(content_type: str) -> bytes:
    """The boundary of a `multipart/form-data` Content-Type; 400 `schema_violation` when it is missing."""
    main, params = header_params(content_type)
    boundary = params.get("boundary", "")
    if main != "multipart/form-data" or not 0 < len(boundary) <= 70:
        raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "The multipart boundary is missing or invalid.", param="file")
    return boundary.encode("latin-1", errors="replace")


@dataclass(frozen=True, slots=True)
class PartStart:
    """A part's headers: `name` and `filename` (`decoded_filename`, not shortened) from Content-Disposition,
    `content_type` (RFC 7578 default `text/plain` for fields, `application/octet-stream` for files)."""

    name: str
    filename: str | None
    content_type: str


@dataclass(frozen=True, slots=True)
class PartData:
    """A slice of the current part's body."""

    data: bytes


@dataclass(frozen=True, slots=True)
class PartEnd:
    """The current part ended."""


@dataclass(frozen=True, slots=True)
class BodyEnd:
    """The closing boundary was read."""


MultipartEvent = PartStart | PartData | PartEnd | BodyEnd
MAX_PART_HEADER_BYTES: Final = 16 * 1024
# Everything but the file part's data (boundaries, headers, fields, unknown parts): the file part is capped by its
# row while it is stored, the rest by this.
MAX_FORM_OVERHEAD_BYTES: Final = 64 * 1024


def _malformed(message: str = "The multipart body is malformed.") -> NoemaError:
    return NoemaError(ErrorCode.SCHEMA_VIOLATION, message, param="file")


class MultipartReader:
    """Incremental `multipart/form-data` reader (python-multipart's push parser driven by an ASGI byte stream).
    Part bodies are passed through as they arrive, never buffered; malformed input raises 400 `schema_violation`
    (param `file`). Every byte read counts: beyond the data of the `file_field` part, at most `max_overhead_bytes`
    may arrive (413 `request_too_large`), so unknown parts or a flood of blank lines cannot stream without end."""

    def __init__(
        self,
        chunks: AsyncIterator[bytes],
        boundary: bytes,
        *,
        max_parts: int = 16,
        file_field: str = "file",
        max_overhead_bytes: int = MAX_FORM_OVERHEAD_BYTES,
    ) -> None:
        self._chunks = chunks.__aiter__()
        self._events: deque[MultipartEvent] = deque()
        self._field = bytearray()
        self._value = bytearray()
        self._headers: dict[str, str] = {}
        self._header_bytes = 0
        self._parts = 0
        self._max_parts = max_parts
        self._file_field = file_field
        self._in_file = False
        self._received = 0
        self._file_bytes = 0
        self._max_overhead = max_overhead_bytes
        self._done = False
        self._parser = MultipartParser(
            boundary,
            {
                "on_part_begin": self._begin,
                "on_header_field": lambda d, s, e: self._field.extend(d[s:e]),
                "on_header_value": lambda d, s, e: self._value.extend(d[s:e]),
                "on_header_end": self._header_end,
                "on_headers_finished": self._headers_finished,
                "on_part_data": self._data,
                "on_part_end": lambda: self._events.append(PartEnd()),
                "on_end": self._end,
            },
            max_header_size=MAX_PART_HEADER_BYTES,
        )

    def _begin(self) -> None:
        self._in_file = False
        self._parts += 1
        if self._parts > self._max_parts:
            raise _malformed("The multipart body has too many parts.")
        self._headers, self._header_bytes = {}, 0

    def _header_end(self) -> None:
        self._header_bytes += len(self._field) + len(self._value)
        if self._header_bytes > MAX_PART_HEADER_BYTES:
            raise _malformed("A multipart part header is too large.")
        name = bytes(self._field).decode("latin-1").strip().lower()
        self._headers.setdefault(name, bytes(self._value).decode("utf-8", errors="replace").strip())
        self._field.clear()
        self._value.clear()

    def _headers_finished(self) -> None:
        kind, params = header_params(self._headers.get("content-disposition", ""))
        if kind != "form-data" or not params.get("name"):
            raise _malformed("A multipart part has no form-data name.")
        self._in_file = params["name"] == self._file_field
        filename = decoded_filename(params)  # the full name: the extension is taken from it
        default = "application/octet-stream" if filename is not None else "text/plain"
        content_type = self._headers.get("content-type") or default
        self._events.append(PartStart(params["name"], filename, content_type))

    def _data(self, data: bytes, start: int, end: int) -> None:
        if self._in_file:
            self._file_bytes += end - start
        self._events.append(PartData(bytes(data[start:end])))

    def _end(self) -> None:
        self._done = True
        self._events.append(BodyEnd())

    async def next(self) -> MultipartEvent:
        """The next event; 400 when the stream ends before the closing boundary."""
        while not self._events:
            if self._done:
                raise _malformed()
            try:
                chunk = await self._chunks.__anext__()
            except StopAsyncIteration:
                raise _malformed("The multipart body ended early.") from None
            self._received += len(chunk)
            try:
                self._parser.write(chunk)
            except FormParserError:
                raise _malformed() from None
            if self._received - self._file_bytes > self._max_overhead:
                raise NoemaError(
                    ErrorCode.REQUEST_TOO_LARGE,
                    f"The multipart body outside the file exceeds {self._max_overhead} bytes.",
                )
        return self._events.popleft()
