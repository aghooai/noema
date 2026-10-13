"""Media fakes (spec-02 §6, spec-07, ADR-002 §7): `FakeFileService`, `FakeInputViewResolver`,
`FakeMediaNegotiator`, `FakeMediaBudgetPlanner`, `FakeSandboxRunner`.

`FakeFileService` keeps files in memory, scoped like RLS (tenant; project unless the principal is tenant_admin or
auditor). Uploads pass the synchronous pre-check of spec-07 §2.1 (normalized declared type and extension in the
Phase 1 allowlist, else 415) and the size cap (413). With `auto_ready=True` (default) a file becomes `ready` at
once with deterministic derivatives; otherwise tests drive `set_status`. Derivative bytes are deterministic
placeholders keyed by the file hash, so view and negotiation code can read real bytes. A ready PDF's
`pdf_sanitized.active_content` is `()`; `set_active_content` (after the file is ready) sets removed facts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final

import anyio
import anyio.lowlevel

from noema.core.clock import Clock, Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import IdGen
from noema.core.principal import Principal
from noema.core.protocols.media import DerivativeKind, DerivativeRef, FileRef, FileStatus
from noema.core.values import JsonValue

# Phase 1 type equivalence (declared type -> extensions), spec-07 §2 (the sync pre-check subset).
ALLOWLIST: Final[Mapping[str, tuple[str, ...]]] = {
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/webp": (".webp",),
    "image/gif": (".gif",),
    "application/pdf": (".pdf",),
    "text/markdown": (".md", ".markdown"),
    "text/plain": (".txt",),
}
MAX_UPLOAD_BYTES: Final = 50 * 1024 * 1024
PDF_PAGES: Final = 2  # every fake PDF; `page_images.bytes` declares all of its pages
MAX_INLINE_IMAGE_BYTES: Final = 3_750_000
TERMINAL: Final[frozenset[FileStatus]] = frozenset({"ready", "partial", "rejected", "failed", "deleted"})
TENANT_WIDE: Final = frozenset({"tenant_admin", "auditor"})
_CONTENT_TYPES: Final[Mapping[str, str]] = {
    "raster": "image/png",
    "raster_1568": "image/png",
    "thumbnail": "image/png",
    "page_images": "image/png",
    "ocr_text": "text/plain",
    "text": "text/plain",
    "markdown": "text/markdown",
    "pdf_sanitized": "application/pdf",
}


def normalize_type(declared: str) -> str:
    """Lower-cased media type without parameters."""
    return declared.split(";", 1)[0].strip().lower()


def derivative_kinds(media_type: str) -> tuple[DerivativeKind, ...]:
    """The derivatives the fake pipeline produces for a media type."""
    if media_type.startswith("image/"):
        return ("raster", "raster_1568", "thumbnail", "ocr_text")
    if media_type == "application/pdf":
        return ("pdf_sanitized", "markdown", "page_images")
    return ("text",)


def placeholder_bytes(sha256: str, kind: str, page: int | None = None) -> bytes:
    """Deterministic derivative content."""
    if kind in ("ocr_text", "text"):
        return f"text of {sha256[:12]}".encode()
    if kind == "markdown":
        return f"# Document {sha256[:12]}\n\nBody text.".encode()
    return hashlib.sha256(f"{sha256}:{kind}:{page}".encode()).digest() * 4


@dataclass
class _File:
    tenant_id: str
    project_id: str
    ref: FileRef
    content: bytes
    changed: anyio.Event = field(default_factory=anyio.Event)

    def signal(self) -> None:
        self.changed.set()
        self.changed = anyio.Event()


class FakeFileService:
    """Implements `FileService` (see the module docstring). `session_exists(principal, sid)` decides
    session_not_found (default: every session exists)."""

    def __init__(
        self,
        clock: Clock,
        ids: IdGen,
        *,
        auto_ready: bool = True,
        session_exists: Callable[[Principal, str], bool] | None = None,
    ) -> None:
        self._clock = clock
        self._ids = ids
        self.auto_ready = auto_ready
        self._session_exists = session_exists or (lambda _p, _s: True)
        self._files: dict[str, _File] = {}

    # --- helpers ---------------------------------------------------------------------------------------------------
    def _visible(self, principal: Principal, file_id: str) -> _File:
        f = self._files.get(file_id)
        wide = bool(principal.roles & TENANT_WIDE)
        if f is None or f.tenant_id != principal.tenant_id or (not wide and f.project_id != principal.project_id):
            raise NoemaError(ErrorCode.NOT_FOUND, "File not found.")
        return f

    def _derivatives(self, sha: str, media_type: str, status: str = "ready") -> tuple[DerivativeRef, ...]:
        refs = []
        for kind in derivative_kinds(media_type):
            data = placeholder_bytes(sha, kind)
            size = PDF_PAGES * len(placeholder_bytes(sha, kind, 1)) if kind == "page_images" else len(data)
            refs.append(
                DerivativeRef(
                    kind=kind,
                    id=self._ids.new("der"),
                    status="ready" if status == "ready" else "pending",
                    sha256=hashlib.sha256(data).hexdigest(),
                    bytes=size,
                    width=1568 if kind.startswith("raster") else None,
                    height=1176 if kind.startswith("raster") else None,
                    pages=PDF_PAGES if kind == "page_images" else None,
                    text_layer_pages=(1,) if kind == "page_images" else None,  # page 2 is scanned
                    active_content=() if kind == "pdf_sanitized" and status == "ready" else None,
                )
            )
        return tuple(refs)

    def _create(
        self,
        principal: Principal,
        content: bytes,
        declared: str,
        media_type: str,
        filename: str | None,
        origin: str,
        session_id: str | None,
    ) -> FileRef:
        sha = hashlib.sha256(content).hexdigest()
        status: FileStatus = "ready" if self.auto_ready else "processing"
        ref = FileRef(
            file_id=self._ids.new("file"),
            sha256=sha,
            declared_type=declared,
            media_type=media_type if self.auto_ready else None,
            status=status,
            bytes=len(content),
            origin="inline" if origin == "inline" else "upload",
            principal_id=principal.principal_id,
            session_id=session_id,
            filename=filename,
            derivatives=self._derivatives(sha, media_type, status),
            pages=PDF_PAGES if media_type == "application/pdf" else None,
        )
        self._files[ref.file_id] = _File(principal.tenant_id, principal.project_id, ref, content)
        return ref

    def set_status(
        self,
        file_id: str,
        status: FileStatus,
        *,
        rejection: Mapping[str, JsonValue] | None = None,
        failure: Mapping[str, JsonValue] | None = None,
    ) -> FileRef:
        """Move a file to `status` (ready marks every derivative ready), waking `watch`ers."""
        f = self._files[file_id]
        media_type = f.ref.media_type or normalize_type(f.ref.declared_type)
        derivatives = self._derivatives(f.ref.sha256, media_type) if status == "ready" else f.ref.derivatives
        f.ref = replace(
            f.ref, status=status, media_type=media_type, rejection=rejection, failure=failure, derivatives=derivatives
        )
        f.signal()
        return f.ref

    def set_active_content(self, file_id: str, facts: tuple[str, ...]) -> FileRef:
        """The facts the sanitizer removed from a ready PDF (`DerivativeRef.active_content` of pdf_sanitized;
        every fake PDF starts with `()`, nothing removed)."""
        f = self._files[file_id]
        derivatives = tuple(
            replace(d, active_content=tuple(sorted(set(facts)))) if d.kind == "pdf_sanitized" else d
            for d in f.ref.derivatives
        )
        f.ref = replace(f.ref, derivatives=derivatives)
        return f.ref

    def set_derivative(self, file_id: str, kind: DerivativeKind, **changes: Any) -> FileRef:
        """Replace fields of one derivative of a ready file (e.g. `status="failed"`, `bytes=...`; spec-10 M2)."""
        f = self._files[file_id]
        derivatives = tuple(replace(d, **changes) if d.kind == kind else d for d in f.ref.derivatives)
        f.ref = replace(f.ref, derivatives=derivatives)
        return f.ref

    # --- FileService -----------------------------------------------------------------------------------------------
    async def upload(
        self,
        principal: Principal,
        stream: AsyncIterator[bytes],
        *,
        declared_type: str,
        filename: str,
        session_id: str | None,
    ) -> FileRef:
        declared = normalize_type(declared_type)
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if declared not in ALLOWLIST or ext not in {e for exts in ALLOWLIST.values() for e in exts}:
            raise NoemaError(ErrorCode.UNSUPPORTED_MEDIA_TYPE, "This media type is not supported.")
        if session_id is not None and not self._session_exists(principal, session_id):
            raise NoemaError(ErrorCode.SESSION_NOT_FOUND, "Session not found.")
        chunks, size = [], 0
        async for chunk in stream:
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise NoemaError(ErrorCode.FILE_TOO_LARGE, "The file is too large.")
            chunks.append(chunk)
        return self._create(principal, b"".join(chunks), declared, declared, filename, "upload", session_id)

    async def ingest_inline(
        self,
        principal: Principal,
        data_b64: str,
        media_type: str,
        *,
        deadline: Deadline,
        session_id: str | None = None,
    ) -> FileRef:
        await anyio.lowlevel.checkpoint()
        declared = normalize_type(media_type)
        if len(data_b64) * 3 // 4 > MAX_INLINE_IMAGE_BYTES:  # arithmetic check before decoding (IMPL-074)
            raise NoemaError(ErrorCode.IMAGE_TOO_LARGE, "The image is too large.")
        if not declared.startswith("image/") or declared not in ALLOWLIST:
            raise NoemaError(ErrorCode.UNSUPPORTED_MEDIA_TYPE, "This media type is not supported.")
        try:
            content = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError):
            raise NoemaError(ErrorCode.FILE_REJECTED, "The image could not be decoded.") from None
        ref = self._create(principal, content, declared, declared, None, "inline", session_id)
        if ref.status in TERMINAL:
            return ref
        async for current in self.watch(principal, ref.file_id):  # waits for raster (spec-07 §3.2)
            if current.status in ("rejected", "failed"):
                raise NoemaError(ErrorCode.FILE_REJECTED, "The image was rejected.")
            if current.status in ("ready", "partial"):
                return current
            if deadline.expired():
                break
        raise NoemaError(ErrorCode.FILE_NOT_READY, "The image is not ready.")

    async def get(self, principal: Principal, file_id: str) -> FileRef:
        await anyio.lowlevel.checkpoint()
        f = self._visible(principal, file_id)
        if f.ref.status == "deleted":
            raise NoemaError(ErrorCode.NOT_FOUND, "File not found.")
        return f.ref

    async def delete(self, principal: Principal, file_id: str) -> None:
        await anyio.lowlevel.checkpoint()
        f = self._visible(principal, file_id)
        f.ref = replace(f.ref, status="deleted")
        f.signal()

    async def purge(self, tenant_id: str, file_id: str) -> None:
        await anyio.lowlevel.checkpoint()
        f = self._files.get(file_id)
        if f is not None and f.tenant_id == tenant_id:
            del self._files[file_id]
            f.ref = replace(f.ref, status="deleted")
            f.signal()

    async def watch(self, principal: Principal, file_id: str) -> AsyncIterator[FileRef]:
        f = self._visible(principal, file_id)
        while True:
            yield f.ref
            if f.ref.status in TERMINAL:
                return
            changed = f.changed
            await changed.wait()

    async def open_derivative(
        self, principal: Principal, file_id: str, kind: DerivativeKind, *, page: int | None = None
    ) -> tuple[str, AsyncIterator[bytes]]:
        if kind == "raster_1568":  # internal: never downloadable
            raise NoemaError(ErrorCode.NOT_FOUND, "Derivative not found.")
        f = self._visible(principal, file_id)
        if f.ref.status == "rejected":
            raise NoemaError(ErrorCode.FILE_REJECTED, "The file was rejected.")
        if any(d.kind == kind and d.status == "pending" for d in f.ref.derivatives):
            raise NoemaError(ErrorCode.FILE_NOT_READY, "The derivative is not ready yet.")
        data = await self.read_derivative(principal, file_id, kind, page=page)

        async def body() -> AsyncIterator[bytes]:
            yield data

        return _CONTENT_TYPES[kind], body()

    async def read_derivative(
        self, principal: Principal, file_id: str, kind: DerivativeKind, *, page: int | None = None
    ) -> bytes:
        await anyio.lowlevel.checkpoint()
        f = self._visible(principal, file_id)
        ref = next((d for d in f.ref.derivatives if d.kind == kind and d.status == "ready"), None)
        if ref is None or f.ref.status == "deleted" or (page is not None and page > (ref.pages or 1)):
            raise NoemaError(ErrorCode.NOT_FOUND, "Derivative not found.")
        return placeholder_bytes(f.ref.sha256, kind, page)
