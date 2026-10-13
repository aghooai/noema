"""File routes (owner: media-pipeline; spec-07 §2.1, §3, §3.3, §6; spec-09 §3, §6.8; IMPL-034, IMPL-062, IMPL-065).

uploadFile streams the multipart body (never buffered): fields before the file part are read first (`session_id` is
authorized there), the file part goes straight into `FileService.upload` (415 pre-check before any byte is stored,
the per-type cap mid-stream, 413), and the rest of the body is read before the upload commits. `session_id` must
precede the file part (400 `schema_violation` otherwise); `purpose` (= "input") may come anywhere. Everything but
the file part's data (boundaries, headers, fields, unknown parts) is capped at 64 KiB (413 `request_too_large`,
`MultipartReader`), and the file name's extension is taken from the full decoded name. With an
`Idempotency-Key`, the fingerprint is the content sha256 plus the form fields and Aghoo-Version, and the key is
claimed once the body is read; a replay returns the stored 202 and nothing is stored again.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Final, cast

from fastapi import APIRouter, Request
from starlette.responses import Response, StreamingResponse

from noema.api import deps
from noema.api.deps import ApiServices, ContextDep, PrincipalDep, ServicesDep
from noema.api.overload import WatchSlots
from noema.api.sse_frame import FrameSequence, SseResponse, WriteOutcome, write_frames
from noema.core.errors import ErrorCode, NoemaError
from noema.core.principal import Principal
from noema.core.protocols.auth import Action
from noema.core.protocols.events import SseFrame
from noema.core.protocols.media import DerivativeKind, FileRef
from noema.media.intake import BodyEnd, MultipartReader, PartData, PartEnd, PartStart, multipart_boundary
from noema.media.pipeline import download_filename, file_object, file_resource, session_resource

router = APIRouter()
MAX_FIELD_BYTES: Final = 1024
DOWNLOADABLE: Final = frozenset({"raster", "thumbnail", "page_images", "pdf_sanitized", "markdown", "ocr_text", "text"})


def _bad(message: str, param: str) -> NoemaError:
    return NoemaError(ErrorCode.SCHEMA_VIOLATION, message, param=param)


class _Form:
    """The non-file fields of one upload."""

    def __init__(self) -> None:
        self.purpose: str | None = None
        self.session_id: str | None = None


class _Replayed(Exception):
    """The Idempotency-Key was already used for this upload: return the stored response."""

    def __init__(self, response: Response) -> None:
        super().__init__("idempotent replay")
        self.response = response


async def _field(reader: MultipartReader, name: str) -> str:
    """The value of a small text field (≤ 1 KiB, strict UTF-8)."""
    buf = bytearray()
    while not isinstance(event := await reader.next(), PartEnd):
        if isinstance(event, PartData):
            buf.extend(event.data)
            if len(buf) > MAX_FIELD_BYTES:
                raise _bad(f"The form field {name} is too long.", name)
    try:
        return bytes(buf).decode("utf-8")
    except UnicodeDecodeError:
        raise _bad(f"The form field {name} is not UTF-8.", name) from None


async def _skip(reader: MultipartReader) -> None:
    while not isinstance(await reader.next(), PartEnd):
        pass


async def _fields_until_file(reader: MultipartReader, form: _Form, svc: ApiServices, who: Principal) -> PartStart:
    """Read the fields before the file part; `session_id` is authorized as it arrives (spec-09 §3)."""
    while True:
        event = await reader.next()
        if isinstance(event, BodyEnd):
            raise _bad("The upload has no file part.", "file")
        if not isinstance(event, PartStart):
            continue
        if event.name == "file":
            return event
        if event.name == "purpose":
            form.purpose = await _field(reader, "purpose")
        elif event.name == "session_id":
            form.session_id = await _field(reader, "session_id")
            resource = await session_resource(svc.db, who, form.session_id)
            svc.authz.check(who, "file.upload", resource)
        else:
            await _skip(reader)


def _fingerprint(digest: str, part: PartStart, form: _Form, version: str | None) -> str:
    fields = "\n".join(
        ["uploadFile", digest, part.content_type, part.filename or "", form.session_id or "", form.purpose or ""]
    )
    return hashlib.sha256(f"{fields}\n{version or ''}".encode()).hexdigest()


async def _file_stream(
    request: Request, reader: MultipartReader, part: PartStart, form: _Form, *, keyed: bool
) -> AsyncIterator[bytes]:
    """The file part's bytes; then the rest of the body (trailing fields), `purpose`, and — with an
    Idempotency-Key — the key claim, all before the stream ends (so the upload commits only a complete, valid
    request)."""
    digest = hashlib.sha256() if keyed else None
    while not isinstance(event := await reader.next(), PartEnd):
        if isinstance(event, PartData):
            if digest is not None:
                digest.update(event.data)
            yield event.data
    while not isinstance(event := await reader.next(), BodyEnd):
        if not isinstance(event, PartStart):
            continue
        if event.name == "purpose":
            form.purpose = await _field(reader, "purpose")
        elif event.name == "session_id":
            raise _bad("session_id must come before the file part.", "session_id")
        elif event.name == "file":
            raise _bad("Only one file per upload.", "file")
        else:
            await _skip(reader)
    if form.purpose is None:
        raise _bad("The form field purpose is required.", "purpose")
    if form.purpose != "input":
        raise _bad('purpose must be "input".', "purpose")
    if digest is not None:
        version = deps.get_context(request).version
        replay = await deps.begin_idempotency(
            request, fingerprint=_fingerprint(digest.hexdigest(), part, form, version)
        )
        if replay is not None:
            raise _Replayed(replay)


@router.post("/v1/files", name="uploadFile")
async def upload_file(request: Request, ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """uploadFile: 202 with the `quarantined` FileObject (spec-07 §3.1 steps 1-5)."""
    svc.authz.check(who, "file.upload")
    reader = MultipartReader(request.stream(), multipart_boundary(request.headers.get("content-type", "")))
    form = _Form()
    part = await _fields_until_file(reader, form, svc, who)
    keyed = ctx.idempotency is not None and ctx.idempotency.key is not None
    if not keyed:
        await deps.begin_idempotency(request)  # no key: returns None at once
    stream = _file_stream(request, reader, part, form, keyed=keyed)
    try:
        ref = await svc.files.upload(
            who, stream, declared_type=part.content_type, filename=part.filename or "", session_id=form.session_id
        )
    except _Replayed as replayed:
        return replayed.response
    deps.mark_model_dispatched(request)  # the 202 is stored for an Idempotency-Key replay (IMPL-077)
    return deps.json_response(file_object(ref), status_code=202)


async def _visible(svc: ApiServices, who: Principal, file_id: str, action: Action) -> FileRef:
    """The file, after the spec-09 §3 role/scope check and V(file) (404 when not visible)."""
    svc.authz.check(who, action)
    ref = await svc.files.get(who, file_id)
    svc.authz.check(who, action, await file_resource(svc.db, who, ref))
    return ref


@router.get("/v1/files/{file_id}", name="getFile")
async def get_file(request: Request, ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """getFile: the FileObject (spec-07 §3.3)."""
    ref = await _visible(svc, who, ctx.path_params["file_id"], "file.read")
    return deps.json_response(file_object(ref))


@router.delete("/v1/files/{file_id}", name="deleteFile")
async def delete_file(request: Request, ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """deleteFile: uploader or tenant_admin (403 for a visible file of someone else); the purge runs as a job."""
    file_id = ctx.path_params["file_id"]
    await _visible(svc, who, file_id, "file.delete")
    await svc.files.delete(who, file_id)
    return deps.json_response({"id": file_id, "deleted": True})


@router.get("/v1/files/{file_id}/events", name="watchFile")
async def watch_file(request: Request, ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """watchFile: `file.updated` FileObject snapshots, the current one first; closes after a terminal state
    (IMPL-034); `: ping` after 15 s of silence. At most `WatchSlots.cap` open streams per project and process
    (429 `rpm_exceeded` above it; Wave E2)."""
    file_id = ctx.path_params["file_id"]
    await _visible(svc, who, file_id, "file.watch")
    slots: WatchSlots | None = getattr(request.app.state, "watch_slots", None)
    project = who.project_id or ""
    if slots is not None:
        slots.acquire(who.tenant_id, project)
    frames = FrameSequence()

    async def snapshots() -> AsyncIterator[SseFrame]:
        async for ref in svc.files.watch(who, file_id):
            yield frames.next(file_object(ref), event_type="file.updated")

    async def on_close(outcome: WriteOutcome) -> None:
        if slots is not None:
            slots.release(who.tenant_id, project)

    return SseResponse(lambda send: write_frames(snapshots(), send, clock=svc.clock), on_close=on_close)


@router.get("/v1/files/{file_id}/derivatives/{kind}", name="getFileDerivative")
async def get_file_derivative(request: Request, ctx: ContextDep, svc: ServicesDep, who: PrincipalDep) -> Response:
    """getFileDerivative (spec-07 §6, IMPL-065): an `attachment` download with the generated name and nosniff."""
    file_id, kind = ctx.path_params["file_id"], ctx.path_params["kind"]
    raw_page = ctx.params.get("page")
    page = raw_page if isinstance(raw_page, int) and not isinstance(raw_page, bool) else None
    if kind == "page_images" and page is None:
        raise _bad("page is required for page_images.", "page")
    if kind != "page_images" and raw_page is not None:
        raise _bad("page is only allowed for page_images.", "page")
    ref = await _visible(svc, who, file_id, "file.derivative.read")
    if kind not in DOWNLOADABLE:
        raise NoemaError(ErrorCode.NOT_FOUND, "Derivative not found.")
    pages = next((d.pages for d in ref.derivatives if d.kind == "page_images" and d.status == "ready"), None)
    if page is not None and pages is not None and page > pages:
        raise _bad(f"page must be between 1 and {pages}.", "page")
    content_type, body = await svc.files.open_derivative(who, file_id, cast(DerivativeKind, kind), page=page)
    headers = {
        "content-disposition": f'attachment; filename="{download_filename(file_id, kind, page)}"',
        "x-content-type-options": "nosniff",
    }
    return StreamingResponse(body, media_type=content_type, headers=headers)
