"""MED-07 (spec-07 §1.1): every file-status transition, failures and rejections from each job, security events
(ADR-002 §4, §6; spec-09 §6.3), the retry rule (results are final per derivative) and `watch` ending only on terminal
states."""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest
from tests.contract_internal.kits import principal

from noema.core.protocols.media import DerivativeRef, FileRef
from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxLimits
from noema.core.protocols.storage import Job
from noema.media.extractors.common import Outputs
from noema.media.pipeline import evaluate_status, is_terminal
from noema.media.sandbox.base import result_for
from noema.media.sandbox.limits import clamp

from . import media_fixtures as fx
from .support import MediaEnv, fail_with, md_script, media_env

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path: Path) -> MediaEnv:
    e = media_env(tmp_path)
    e.runner.script["pdf.markdown"] = md_script(["text"])
    return e


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def upload(env: MediaEnv, data: bytes, declared: str = "image/png", filename: str = "a.png") -> str:
    ref = await env.files.upload(principal(), chunks(data), declared_type=declared, filename=filename, session_id=None)
    return ref.file_id


async def get(env: MediaEnv, file_id: str) -> FileRef:
    return await env.files.get(principal(), file_id)


def kinds(ref: FileRef) -> dict[str, tuple[str, str | None]]:
    return {d.kind: (d.status, None) for d in ref.derivatives}


def test_evaluate_status_table() -> None:
    p, r, f = ("pending", None), ("ready", None), ("failed", "timeout")
    assert evaluate_status("image", {"raster": p, "raster_1568": p}).status == "processing"
    assert evaluate_status("image", {"raster": r, "raster_1568": r, "thumbnail": r}).status == "ready"
    assert evaluate_status("image", {"raster": r, "raster_1568": r, "thumbnail": r, "ocr_text": p}).status == "partial"
    assert evaluate_status("image", {"raster": r, "raster_1568": r, "ocr_text": f}).status == "partial"
    failed = evaluate_status("pdf", {"pdf_sanitized": r, "markdown": f, "page_images": r})
    assert (failed.status, failed.failure_code) == ("failed", "timeout")
    assert evaluate_status("markdown", {"markdown": r}).status == "ready"


def test_terminal_states() -> None:
    def ref(status: str, *pending: bool) -> FileRef:
        ds = tuple(
            DerivativeRef("ocr_text", f"der_{i}", "pending" if p else "ready", None) for i, p in enumerate(pending)
        )
        return FileRef("file_1", "0" * 64, "image/png", None, status, 1, "upload", "prn", None, None, ds)  # type: ignore[arg-type]

    for status in ("ready", "rejected", "failed", "deleted"):
        assert is_terminal(ref(status))
    assert is_terminal(ref("partial", False)) and not is_terminal(ref("partial", True))
    assert not is_terminal(ref("quarantined")) and not is_terminal(ref("processing"))


async def test_partial_with_ocr_pending_then_ready(env: MediaEnv) -> None:
    gate = anyio.Event()
    seen: list[str] = []
    file_id = await upload(env, fx.png())

    partial = anyio.Event()

    async def watch() -> None:
        async for ref in env.files.watch(principal(), file_id):
            seen.append(ref.status)
            if ref.status == "partial":
                partial.set()

    original_run = env.runner.run

    async def gated_run(job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        if job.kind == "ocr":  # hold OCR: the file sits in `partial` with ocr_text pending
            await gate.wait()
        return await original_run(job, limits)

    env.runner.run = gated_run  # type: ignore[method-assign]
    async with anyio.create_task_group() as tg:
        tg.start_soon(watch)
        tg.start_soon(env.drain)
        await partial.wait()
        ref = await get(env, file_id)
        assert ref.status == "partial" and kinds(ref)["ocr_text"][0] == "pending" and not is_terminal(ref)
        gate.set()
    assert seen[0] == "quarantined" and "partial" in seen and seen[-1] == "ready", seen


@pytest.mark.parametrize(
    ("job_kind", "status", "reason", "file_status", "code"),
    [
        ("image.normalize", "timeout", "timeout", "failed", "timeout"),
        ("image.normalize", "failed", "exit", "failed", "extraction_failed"),
        ("image.normalize", "violation", "violation", "failed", "extraction_failed"),
        ("image.normalize", "failed", "manifest_invalid", "failed", "extraction_failed"),
        ("ocr", "failed", "exit", "partial", None),
        ("ocr", "timeout", "timeout", "partial", None),
        ("detect", "violation", "violation", "failed", "extraction_failed"),
    ],
)
async def test_job_failures(
    env: MediaEnv, job_kind: str, status: str, reason: str, file_status: str, code: str | None
) -> None:
    env.runner.script[job_kind] = fail_with(status, reason)
    file_id = await upload(env, fx.png())
    await env.drain()
    ref = await get(env, file_id)
    assert ref.status == file_status, ref
    assert (ref.failure or {}).get("code") == code
    if job_kind == "ocr":
        ocr = next(d for d in ref.derivatives if d.kind == "ocr_text")
        assert ocr.status == "failed" and is_terminal(ref)
    events = [r.payload["action"] for r in env.world.outbox.records if r.kind == "audit_event"]
    if status == "violation":
        assert events == ["security.sandbox.violation"]
    elif reason == "manifest_invalid":
        assert events == ["security.sandbox.manifest_invalid"]
    else:
        assert events == []


async def test_an_ocr_ok_without_its_output_is_ocr_failed(env: MediaEnv) -> None:
    """spec-07 §1.2: an `ok` OCR job that lists no `ocr_text` fails the derivative with `ocr_failed` (C1 review)."""

    def no_output(job: SandboxJob, out_dir: Path) -> ExtractResult:
        Outputs("ocr", out_dir, "scripted-ocr").write_manifest()
        return result_for("ocr", out_dir, "ok", wall_ms=1, limits=clamp("ocr"), backend="scripted")

    env.runner.script["ocr"] = no_output
    file_id = await upload(env, fx.png())
    await env.drain()
    ref = await get(env, file_id)
    assert ref.status == "partial" and is_terminal(ref)
    (row,) = (r for r in env.world.db.store.rows("derivative") if r["kind"] == "ocr_text")
    assert (row["status"], row["error_code"]) == ("failed", "ocr_failed")


async def test_sanitize_incomplete_fails_the_pdf_with_a_security_event(env: MediaEnv) -> None:
    def incomplete(job: object, out: Path) -> ExtractResult:
        return ExtractResult("ok", (), {"warnings": ["sanitize_incomplete"]}, None, 1, "pikepdf@test")

    env.runner.script["pdf.sanitize"] = incomplete
    file_id = await upload(env, fx.text_pdf(), "application/pdf", "a.pdf")
    await env.drain()
    ref = await get(env, file_id)
    assert ref.status == "failed" and (ref.failure or {}).get("code") == "extraction_failed"
    assert [r.payload["action"] for r in env.world.outbox.records] == ["security.sandbox.sanitize_incomplete"]


@pytest.mark.parametrize(
    ("data", "declared", "name", "code"),
    [
        (fx.encrypted_pdf(), "application/pdf", "a.pdf", "encrypted_pdf"),
        (fx.many_pages_pdf(601), "application/pdf", "a.pdf", "too_many_pages"),
        (fx.png_header_only(9_500, 9_500), "image/png", "a.png", "pixel_limit"),
        (b"", "image/png", "a.png", "empty_content"),
        (b"  \n", "text/markdown", "a.md", None),
    ],
    ids=["encrypted", "601-pages", "pixel-bomb", "empty", "blank-markdown"],
)
async def test_rejections_stop_the_graph(
    env: MediaEnv, data: bytes, declared: str, name: str, code: str | None
) -> None:
    file_id = await upload(env, data, declared, name)
    await env.drain()
    ref = await get(env, file_id)
    assert ref.status == "rejected", ref
    if code is not None:
        assert (ref.rejection or {}).get("code") == code
    assert all(d.status == "failed" for d in ref.derivatives)
    assert "ocr" not in env.runner.kinds()


async def test_a_crashed_attempt_resumes_without_re_running_finished_jobs(env: MediaEnv) -> None:
    file_id = await upload(env, fx.png())
    crash = {"done": False}
    original = env.processor._store.put

    async def flaky_put(key: str, tenant_id: str, data: bytes, content_type: str) -> None:
        if "ocr_text" in key and not crash["done"]:
            crash["done"] = True
            raise OSError("S3 went away")
        await original(key, tenant_id, data, content_type)

    env.processor._store.put = flaky_put  # type: ignore[method-assign]
    async with env.world.db.system_uow() as sys:
        (job,) = await sys.claim_jobs(("media.process",), 1, "w", 60)
    with pytest.raises(OSError, match="S3 went away"):
        await env.processor.process(job)
    assert (await get(env, file_id)).status == "partial"
    await env.processor.process(Job(job.id, job.tenant_id, job.kind, job.payload, attempt=2))
    assert env.runner.kinds() == ["detect", "image.normalize", "ocr", "ocr"]  # detect and normalize not re-run
    assert (await get(env, file_id)).status == "ready"


async def test_the_last_attempt_settles_the_file(env: MediaEnv) -> None:
    file_id = await upload(env, fx.png())

    async def broken(*args: object, **kwargs: object) -> None:
        raise OSError("S3 down")

    env.processor._store.put = broken  # type: ignore[method-assign]
    async with env.world.db.system_uow() as sys:
        (job,) = await sys.claim_jobs(("media.process",), 1, "w", 60)
    with pytest.raises(OSError, match="S3 down"):
        await env.processor.process(Job(job.id, job.tenant_id, job.kind, job.payload, attempt=2))
    ref = await get(env, file_id)
    assert ref.status == "failed" and all(d.status == "failed" for d in ref.derivatives)


async def test_watch_ends_on_deleted_and_redelivery_is_a_noop(env: MediaEnv) -> None:
    file_id = await upload(env, fx.png())
    seen: list[str] = []
    first = anyio.Event()

    async def watch() -> None:
        async for ref in env.files.watch(principal(), file_id):
            seen.append(ref.status)
            first.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(watch)
        await first.wait()
        await env.files.delete(principal(), file_id)
    assert seen == ["quarantined", "deleted"]
    assert await env.drain() != [] and env.runner.kinds() == []  # the deleted file is skipped


async def test_a_required_rejection_in_parallel_jobs_wins(env: MediaEnv) -> None:
    def rejected(job: object, out: Path) -> ExtractResult:
        return ExtractResult("rejected", (), {}, "corrupt", 1, "scripted-docling")

    env.runner.script["pdf.markdown"] = rejected
    file_id = await upload(env, fx.text_pdf(), "application/pdf", "a.pdf")
    jobs = await env.drain()
    ref = await get(env, file_id)
    assert ref.status == "rejected" and (ref.rejection or {}).get("code") == "corrupt"
    assert "ocr" not in env.runner.kinds() and len(jobs) == 1  # handled, not retried


def test_detect_polyglot_signals() -> None:
    from noema.media.extractors.detect import polyglot

    def check(data: bytes) -> bool:
        with tempfile.NamedTemporaryFile() as fh:
            fh.write(data)
            fh.flush()
            return polyglot(Path(fh.name), len(data))

    assert not check(fx.text_pdf()) and not check(b"\n \r\n" + fx.text_pdf()) and not check(fx.png())
    assert check(fx.png(8, 8)[:33] + b"%PDF-1.7" + fx.png(8, 8)[33:]) and check(fx.pdf_zip_polyglot())
    with tempfile.NamedTemporaryFile() as fh:  # strict UTF-8 text without NUL: no hidden-PDF check (C1 review)
        note = b"# Notes\n\nA PDF starts with %PDF-1.7.\n"
        fh.write(note)
        fh.flush()
        assert polyglot(Path(fh.name), len(note)) and not polyglot(Path(fh.name), len(note), text=True)


@pytest.mark.parametrize("kind", ["image", "scanned_pdf"])
async def test_no_transient_ready_before_ocr(env: MediaEnv, kind: str) -> None:
    """The OCR row is created with the result that triggers it, so the file goes partial, never ready → partial
    (watchFile stops at the first terminal status)."""
    published: list[str] = []
    real_publish = env.events.publish

    async def publish(tenant_id: str, file_id: str, status: str) -> None:
        published.append(status)
        await real_publish(tenant_id, file_id, status)

    env.events.publish = publish  # type: ignore[method-assign]
    if kind == "image":
        file_id = await upload(env, fx.png())
    else:
        file_id = await upload(env, fx._pdf_bytes(None), "application/pdf", "scan.pdf")  # no text layer: OCR runs
    await env.drain()
    assert "ocr" in env.runner.kinds() and published[-1] == "ready" and "partial" in published
    first_terminal = next(i for i, s in enumerate(published) if s == "ready")
    assert first_terminal == len(published) - 1, published
    assert (await get(env, file_id)).status == "ready"
