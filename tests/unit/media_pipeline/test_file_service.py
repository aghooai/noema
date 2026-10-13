"""`MediaFileService` + `MediaProcessor` over the fakes (spec-07 §3, §6; MED-02/03/04 at the pipeline level, MED-06,
SEC-EDGE-01 for uploads): upload → quarantine → media.process → derivatives → status, downloads and purge."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from tests.contract_internal.kits import principal

from noema.core.errors import ErrorCode, NoemaError
from noema.media.pipeline import SingleFlight, derivative_ref, file_object

from . import media_fixtures as fx
from .support import MediaEnv, md_script, media_env

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path: Path) -> MediaEnv:
    return media_env(tmp_path)


async def chunks(data: bytes, size: int = 1 << 20) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def upload(env: MediaEnv, data: bytes, declared: str = "image/png", filename: str = "cat.png") -> str:
    ref = await env.files.upload(principal(), chunks(data), declared_type=declared, filename=filename, session_id=None)
    return ref.file_id


async def test_image_upload_runs_the_pipeline(env: MediaEnv) -> None:
    file_id = await upload(env, fx.png(640, 480))
    ref = await env.files.get(principal(), file_id)
    assert ref.status == "quarantined" and ref.media_type is None and ref.derivatives == ()
    assert file_object(ref)["media_type"] == "image/png"  # declared type until detect
    await env.drain()
    ref = await env.files.get(principal(), file_id)
    assert env.runner.kinds() == ["detect", "image.normalize", "ocr"]
    statuses = {d.kind: d.status for d in ref.derivatives}
    assert statuses["raster"] == statuses["raster_1568"] == statuses["thumbnail"] == "ready"
    assert ref.media_type == "image/png" and ref.status == "ready" and statuses["ocr_text"] == "ready"
    listed = file_object(ref)["derivatives"]
    assert isinstance(listed, list)
    assert [d["kind"] for d in listed if isinstance(d, dict)] == ["raster", "thumbnail", "ocr_text"]
    raster = await env.files.read_derivative(principal(), file_id, "raster_1568")
    assert raster.startswith(b"\x89PNG")
    content_type, body = await env.files.open_derivative(principal(), file_id, "thumbnail")
    assert content_type == "image/png" and b"".join([c async for c in body]).startswith(b"\x89PNG")
    with pytest.raises(NoemaError) as err:
        await env.files.open_derivative(principal(), file_id, "raster_1568")
    assert err.value.code is ErrorCode.NOT_FOUND


async def test_pdf_upload_runs_sanitize_render_markdown_and_ocr_of_scanned_pages(env: MediaEnv) -> None:
    import io

    import pikepdf

    doc = pikepdf.open(io.BytesIO(fx.text_pdf("Invoice number 4471")))
    doc.add_blank_page(page_size=(200, 200))  # page 2: no text layer
    doc.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert(1)"))
    buf = io.BytesIO()
    doc.save(buf)
    env.runner.script["pdf.markdown"] = md_script(["Invoice number 4471", ""])
    file_id = await upload(env, buf.getvalue(), "application/pdf", "invoice.pdf")
    await env.drain()
    ref = await env.files.get(principal(), file_id)
    assert env.runner.kinds()[:2] == ["detect", "pdf.sanitize"] and env.runner.kinds()[-1] == "ocr"
    assert ref.status == "ready" and ref.pages == 2 and ref.media_type == "application/pdf"
    pages = next(d for d in ref.derivatives if d.kind == "page_images")
    assert pages.pages == 2 and pages.text_layer_pages == (1,)
    ocr_job = next(j for j in env.runner.jobs if j.kind == "ocr")
    assert ocr_job.params == {"pages": [2]}  # only the scanned page
    ocr = (await env.files.read_derivative(principal(), file_id, "ocr_text")).decode()
    assert ocr.split("\f") == ["", "4471"]
    assert (await env.files.read_derivative(principal(), file_id, "page_images", page=2)).startswith(b"\x89PNG")
    by_kind = {d.kind: d for d in ref.derivatives}
    assert by_kind["pdf_sanitized"].active_content == ("open_action",)  # the JS action went with the key
    assert by_kind["markdown"].active_content is None  # pdf_sanitized only
    (row,) = (r for r in env.world.db.store.rows("derivative") if r["kind"] == "pdf_sanitized")
    assert row["facts"] == {"active_content": ["open_action"]}
    assert not [k for k in env.objects() if k.endswith(".facts.json")]  # no S3 sidecar any more
    _, body = await env.files.open_derivative(principal(), file_id, "pdf_sanitized")
    assert b"JavaScript" not in b"".join([c async for c in body])


async def test_markdown_upload(env: MediaEnv) -> None:
    file_id = await upload(env, b"# Notes\n\n<b>bold</b> and [x](javascript:y)\n", "text/markdown", "notes.md")
    await env.drain()
    ref = await env.files.get(principal(), file_id)
    assert ref.status == "ready" and [d.kind for d in ref.derivatives] == ["markdown"]
    content_type, body = await env.files.open_derivative(principal(), file_id, "markdown")
    assert content_type == "text/markdown; charset=utf-8"
    assert b"\\<b\\>bold" in b"".join([c async for c in body])


async def test_unsupported_types_are_415_before_any_storage(env: MediaEnv) -> None:
    for declared, name in (("application/msword", "a.docx"), ("image/svg+xml", "a.svg"), ("audio/mpeg", "a.mp3")):
        with pytest.raises(NoemaError) as err:
            await upload(env, b"data", declared, name)
        assert err.value.code is ErrorCode.UNSUPPORTED_MEDIA_TYPE
    assert env.s3.calls == [] and env.world.jobs.enqueued == []


@pytest.mark.parametrize("size", [5 * 1024 * 1024 + 1, 20 * 1024 * 1024 + 1])
async def test_oversized_uploads_are_413_mid_stream_with_no_object_left(env: MediaEnv, size: int) -> None:
    ext, declared = (".md", "text/markdown") if size < 6 * 1024 * 1024 else (".png", "image/png")
    with pytest.raises(NoemaError) as err:
        await upload(env, b"#" * size, declared, f"big{ext}")
    assert err.value.code is ErrorCode.FILE_TOO_LARGE
    assert dict(env.objects()) == {} and env.s3.uploads.keys() <= set()  # aborted multipart, nothing stored
    assert env.world.jobs.enqueued == []


async def test_a_2_gb_sparse_upload_stops_at_the_cap(env: MediaEnv) -> None:
    sent = 0

    async def sparse() -> AsyncIterator[bytes]:  # 2 GiB of zeros, produced lazily
        nonlocal sent
        block = bytes(1 << 20)
        for _ in range(2048):
            sent += len(block)
            yield block

    with pytest.raises(NoemaError) as err:
        await env.files.upload(
            principal(), sparse(), declared_type="application/pdf", filename="s.pdf", session_id=None
        )
    assert err.value.code is ErrorCode.FILE_TOO_LARGE and sent <= 51 * 1024 * 1024
    assert dict(env.objects()) == {} and env.s3.uploads == {}


async def test_large_uploads_stream_as_multipart(env: MediaEnv) -> None:
    data = fx.png(32, 32) + b"\x00" * (18 * 1024 * 1024)
    file_id = await upload(env, data, "image/png", "big.png")
    ref = await env.files.get(principal(), file_id)
    assert ref.bytes == len(data) and len(ref.sha256) == 64
    assert [c[0] for c in env.s3.calls].count("UploadPart") == 3
    stored = env.objects()[f"t_acme/quarantine/{file_id}"]
    assert stored[0] == data and stored[1]["ServerSideEncryption"] == "AES256"


async def test_session_and_visibility_rules(env: MediaEnv) -> None:
    with pytest.raises(NoemaError) as err:
        await env.files.upload(
            principal(), chunks(fx.png()), declared_type="image/png", filename="a.png", session_id="ses_missing"
        )
    assert err.value.code is ErrorCode.SESSION_NOT_FOUND
    file_id = await upload(env, fx.png())
    for other in (principal(project_id="p2"), principal(tenant_id="t_other")):
        with pytest.raises(NoemaError) as nf:
            await env.files.get(other, file_id)
        assert nf.value.code is ErrorCode.NOT_FOUND
    admin = principal(roles=("tenant_admin",), project_id="p2", principal_id="prn_admin")
    assert (await env.files.get(admin, file_id)).file_id == file_id


async def test_delete_hides_the_file_and_the_purge_job_removes_everything(env: MediaEnv) -> None:
    from noema.media.pipeline import FILE_PURGE

    file_id = await upload(env, fx.png())
    await env.drain()
    assert any(k.startswith(f"t_acme/derivatives/{file_id}/") for k in env.objects())
    await env.files.delete(principal(), file_id)
    with pytest.raises(NoemaError) as err:
        await env.files.get(principal(), file_id)
    assert err.value.code is ErrorCode.NOT_FOUND
    await env.drain((FILE_PURGE,))
    assert not [k for k in env.objects() if file_id in k]
    await env.files.purge("t_acme", file_id)  # idempotent
    with pytest.raises(NoemaError):
        await env.files.delete(principal(), file_id)


async def test_derivative_access_while_pending_and_after_rejection(env: MediaEnv) -> None:
    file_id = await upload(env, fx.png())
    with pytest.raises(NoemaError) as pending:
        await env.files.open_derivative(principal(), file_id, "thumbnail")
    assert pending.value.code is ErrorCode.FILE_NOT_READY
    with pytest.raises(NoemaError) as unknown:
        await env.files.open_derivative(principal(), file_id, "markdown")  # not a kind this file will have
    assert unknown.value.code is ErrorCode.NOT_FOUND
    bad = await upload(env, fx.text_pdf(), "image/png", "fake.png")
    await env.drain()
    with pytest.raises(NoemaError) as rejected:
        await env.files.open_derivative(principal(), bad, "thumbnail")
    assert rejected.value.code is ErrorCode.FILE_REJECTED
    assert (await env.files.get(principal(), bad)).rejection == {
        "code": "type_mismatch",
        "message": "The file content does not match its declared type or extension.",
    }


def test_derivative_ref_carries_the_active_content_facts_of_pdf_sanitized_only() -> None:
    base: dict[str, object] = {"id": "der_1", "status": "ready", "sha256": "0" * 64}
    clean = derivative_ref({**base, "kind": "pdf_sanitized", "facts": {"active_content": []}})
    hit = derivative_ref({**base, "kind": "pdf_sanitized", "facts": {"active_content": ["xfa", "javascript"]}})
    pending = derivative_ref({**base, "kind": "pdf_sanitized", "status": "pending", "facts": None})
    other = derivative_ref({**base, "kind": "markdown", "facts": None})
    assert (clean.active_content, hit.active_content) == ((), ("javascript", "xfa"))
    assert pending.active_content is None and other.active_content is None


async def test_concurrent_derivative_reads_share_the_row_read_and_the_object_get(env: MediaEnv) -> None:
    """Wave E (finding 3): on a media decision every member resolves its view at once, and each derivative key read
    the file row in its own unit of work and, on a cold LRU, GOT the same object again. Concurrent reads of one
    file by one principal now share one row read, and concurrent misses of one object one S3 GET."""
    env.runner.script["pdf.markdown"] = md_script(["one", "two"])
    file_id = await upload(env, fx.text_pdf("x", pages=2), "application/pdf", "a.pdf")
    await env.drain()
    uows, real_uow = [0], env.world.db.uow

    def counting_uow(p: Any) -> Any:
        uows[0] += 1
        return real_uow(p)

    env.world.db.uow = counting_uow  # type: ignore[assignment]
    gets_before = sum(1 for op, _, _ in env.s3.calls if op == "GetObject")
    who, read = principal(), env.files.read_derivative
    keys: list[tuple[Any, int | None]] = [("markdown", None), ("page_images", 1), ("page_images", 2)]
    results: list[bytes] = []

    async def one(kind: Any, page: int | None) -> None:
        results.append(await read(who, file_id, kind, page=page))

    async with anyio.create_task_group() as tg:
        for _member in range(3):
            for kind, page in keys:
                tg.start_soon(one, kind, page)
    gets = sum(1 for op, _, _ in env.s3.calls if op == "GetObject") - gets_before
    assert len(results) == 9 and uows[0] == 1 and gets == 3


async def test_single_flight_shares_results_and_errors_but_not_a_cancelled_leader() -> None:
    flights, gate, ran = SingleFlight(), anyio.Event(), list[str]()

    async def call(name: str, result: str) -> str:
        ran.append(name)
        await gate.wait()
        return result

    async with anyio.create_task_group() as tg:
        leader = anyio.CancelScope()

        async def lead() -> None:
            with leader:
                await flights.run("k", lambda: call("leader", "a"))

        follower: list[str] = []

        async def follow() -> None:
            follower.append(await flights.run("k", lambda: call("follower", "b")))

        tg.start_soon(lead)
        await anyio.sleep(0.01)
        tg.start_soon(follow)
        await anyio.sleep(0.01)
        leader.cancel()  # the follower must not inherit the leader's cancellation: it runs its own call
        await anyio.sleep(0.01)
        gate.set()
    assert ran == ["leader", "follower"] and follower == ["b"] and flights.in_flight == 0

    async def boom() -> str:
        raise NoemaError(ErrorCode.NOT_FOUND, "x")

    with pytest.raises(NoemaError):
        await flights.run("e", boom)
    assert flights.in_flight == 0
