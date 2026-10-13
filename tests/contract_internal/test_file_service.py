"""FileService conformance (spec-02 §6, spec-07 §3): upload pre-checks (415/413), ingest_inline, scoped get,
delete, watch to a terminal status, derivative access (raster_1568 is internal)."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import pytest

from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError

from .kits import MediaKit, principal
from .registry import kit_fixture

kit = kit_fixture("file_service")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for p in parts:
        yield p


async def upload(kit: MediaKit, data: bytes = PNG, *, declared: str = "image/png", filename: str = "a.png") -> object:
    return await kit.files.upload(principal(), chunks(data), declared_type=declared, filename=filename, session_id=None)


async def test_upload_and_get(kit: MediaKit) -> None:
    ref = await kit.files.upload(
        principal(), chunks(PNG[:10], PNG[10:]), declared_type="Image/PNG; q=1", filename="cat.png", session_id=None
    )
    assert ref.bytes == len(PNG) and ref.origin == "upload" and ref.filename == "cat.png"
    assert ref.principal_id == "prn_dev" and len(ref.sha256) == 64
    again = await kit.files.get(principal(), ref.file_id)
    assert again.file_id == ref.file_id


async def test_unsupported_types_are_415(kit: MediaKit) -> None:
    for declared, filename in (("application/msword", "a.doc"), ("image/png", "a.svg"), ("audio/mpeg", "a.mp3")):
        with pytest.raises(NoemaError) as err:
            await upload(kit, declared=declared, filename=filename)
        assert err.value.code is ErrorCode.UNSUPPORTED_MEDIA_TYPE


async def test_foreign_files_are_not_found(kit: MediaKit) -> None:
    ref = await kit.files.upload(principal(), chunks(PNG), declared_type="image/png", filename="a.png", session_id=None)
    for other in (principal(project_id="p2"), principal(tenant_id="t_other")):
        with pytest.raises(NoemaError) as err:
            await kit.files.get(other, ref.file_id)
        assert err.value.code is ErrorCode.NOT_FOUND
    admin = principal(roles=("tenant_admin",), project_id="p2", principal_id="prn_admin")
    assert (await kit.files.get(admin, ref.file_id)).file_id == ref.file_id


async def test_delete_and_watch_end_at_terminal(kit: MediaKit) -> None:
    ref = await kit.files.upload(principal(), chunks(PNG), declared_type="image/png", filename="a.png", session_id=None)
    seen = [r.status async for r in kit.files.watch(principal(), ref.file_id)]
    assert seen[-1] in ("ready", "partial", "rejected", "failed", "deleted")
    await kit.files.delete(principal(), ref.file_id)
    with pytest.raises(NoemaError):
        await kit.files.get(principal(), ref.file_id)


async def test_ingest_inline(kit: MediaKit) -> None:
    deadline = Deadline(kit.clock.monotonic() + 30, kit.clock)
    ref = await kit.files.ingest_inline(principal(), base64.b64encode(PNG).decode(), "image/png", deadline=deadline)
    assert ref.origin == "inline" and ref.status in ("ready", "partial")
    assert any(d.kind == "raster" and d.status == "ready" for d in ref.derivatives)
    huge = "A" * 6_000_000
    with pytest.raises(NoemaError) as err:
        await kit.files.ingest_inline(principal(), huge, "image/png", deadline=deadline)
    assert err.value.code is ErrorCode.IMAGE_TOO_LARGE


async def test_derivatives(kit: MediaKit) -> None:
    ref = await kit.files.upload(principal(), chunks(PNG), declared_type="image/png", filename="a.png", session_id=None)
    content_type, body = await kit.files.open_derivative(principal(), ref.file_id, "thumbnail")
    data = b"".join([c async for c in body])
    assert content_type.startswith("image/") and data
    assert await kit.files.read_derivative(principal(), ref.file_id, "raster_1568")  # internal reads are fine
    with pytest.raises(NoemaError) as err:
        await kit.files.open_derivative(principal(), ref.file_id, "raster_1568")
    assert err.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(NoemaError):
        await kit.files.read_derivative(principal(), ref.file_id, "markdown")


async def test_derivatives_of_unready_or_rejected_files_are_409(kit: MediaKit) -> None:
    """spec-07 §6: getFileDerivative answers 409 file_not_ready while pending and file_rejected after rejection."""
    if kit.pending_uploads is None or kit.reject is None:
        pytest.skip("the kit cannot hold or reject uploads")
    kit.pending_uploads(True)
    try:
        ref = await kit.files.upload(
            principal(), chunks(PNG), declared_type="image/png", filename="a.png", session_id=None
        )
        with pytest.raises(NoemaError) as pending:
            await kit.files.open_derivative(principal(), ref.file_id, "thumbnail")
        assert pending.value.code is ErrorCode.FILE_NOT_READY
    finally:
        kit.pending_uploads(False)
    kit.reject(ref.file_id)
    with pytest.raises(NoemaError) as rejected:
        await kit.files.open_derivative(principal(), ref.file_id, "thumbnail")
    assert rejected.value.code is ErrorCode.FILE_REJECTED


async def test_purge_removes_the_file_for_good(kit: MediaKit) -> None:
    """FileService.purge (session.delete, retention, the file.purge job): idempotent, by tenant."""
    ref = await kit.files.upload(principal(), chunks(PNG), declared_type="image/png", filename="a.png", session_id=None)
    await kit.files.purge("t_other", ref.file_id)  # another tenant's purge does not touch it
    assert (await kit.files.get(principal(), ref.file_id)).file_id == ref.file_id
    await kit.files.purge("t_acme", ref.file_id)
    await kit.files.purge("t_acme", ref.file_id)  # absent: no-op
    with pytest.raises(NoemaError) as gone:
        await kit.files.get(principal(), ref.file_id)
    assert gone.value.code is ErrorCode.NOT_FOUND
