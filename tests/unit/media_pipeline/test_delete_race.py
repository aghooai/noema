"""deleteFile / purge while `media.process` is still working on the file (spec-07 §1.1 `deleted` from any status,
spec-03 retention): the worker never writes a status onto a deleted row, discards what it stored, and the purge
leaves a delayed sweep when a run may still be in flight."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import anyio
import anyio.lowlevel
import pytest
from tests.contract_internal.kits import principal

from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxLimits
from noema.media.pipeline import FILE_PURGE, MEDIA_PROCESS, SWEEP_DELAY_S
from noema.media.quarantine import derivative_key, derivative_prefix

from . import media_fixtures as fx
from .support import MediaEnv, md_script, media_env

pytestmark = pytest.mark.unit
Hook = Callable[[MediaEnv, str], Awaitable[None]]


@pytest.fixture
def env(tmp_path: Path) -> MediaEnv:
    return media_env(tmp_path)


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def upload_png(env: MediaEnv) -> str:
    ref = await env.files.upload(
        principal(), chunks(fx.png(64, 48)), declared_type="image/png", filename="a.png", session_id=None
    )
    return ref.file_id


def during(env: MediaEnv, kind: str, hook: Hook, file_id: str) -> list[str]:
    """Run `hook` while the sandbox job `kind` is in flight (before its outputs exist); returns the statuses
    published from then on."""
    published: list[str] = []
    real_run, real_publish = env.runner.run, env.events.publish

    async def run(job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        if job.kind == kind:
            with anyio.CancelScope(shield=True):  # another worker: the run's own cancellation does not reach it
                await hook(env, file_id)
            env.events.publish = publish  # type: ignore[method-assign]
        return await real_run(job, limits)

    async def publish(tenant_id: str, file_id: str, status: str) -> None:
        published.append(status)
        await real_publish(tenant_id, file_id, status)

    env.runner.run = run  # type: ignore[method-assign]
    return published


async def raw_row(env: MediaEnv, file_id: str) -> dict[str, object] | None:
    """The file row, deleted ones too; None once purged."""
    async with env.world.db.tenant_uow("t_acme") as u:
        try:
            return dict(await u.files.update(file_id))
        except Exception:
            return None


def objects_of(env: MediaEnv, file_id: str) -> list[str]:
    return sorted(k for k in env.objects() if file_id in k)


async def delete_and_purge(env: MediaEnv, file_id: str) -> None:
    await env.files.delete(principal(), file_id)
    await env.drain((FILE_PURGE,))  # the purge runs on another worker while the job is in the sandbox


async def delete_only(env: MediaEnv, file_id: str) -> None:
    await env.files.delete(principal(), file_id)


async def retention_purge(env: MediaEnv, file_id: str) -> None:
    await env.files.purge("t_acme", file_id)  # retention / session.delete call purge on a live row


async def test_delete_and_purge_during_processing_leave_no_object(env: MediaEnv) -> None:
    file_id = await upload_png(env)
    published = during(env, "image.normalize", delete_and_purge, file_id)
    await env.drain()
    assert objects_of(env, file_id) == []  # the derivatives stored after the purge were discarded
    assert await raw_row(env, file_id) is None and published == []
    assert "ocr" not in env.runner.kinds()  # the job graph stopped


async def test_delete_during_processing_never_overwrites_deleted(env: MediaEnv) -> None:
    file_id = await upload_png(env)
    published = during(env, "image.normalize", delete_only, file_id)
    await env.drain()
    row = await raw_row(env, file_id)
    assert row is not None and row["status"] == "deleted" and row["deleted_at"] is not None
    assert published == [] and objects_of(env, file_id) == [f"t_acme/quarantine/{file_id}"]
    await env.drain((FILE_PURGE,))
    assert objects_of(env, file_id) == [] and await raw_row(env, file_id) is None


async def test_a_purge_of_a_live_file_marks_it_deleted_first(env: MediaEnv) -> None:
    file_id = await upload_png(env)
    published = during(env, "image.normalize", retention_purge, file_id)
    await env.drain()
    assert objects_of(env, file_id) == [] and await raw_row(env, file_id) is None and published == []


async def test_purge_of_an_unfinished_file_schedules_a_sweep(env: MediaEnv) -> None:
    file_id = await upload_png(env)
    seen: list[str] = []

    async def purge_then_late_put(env: MediaEnv, fid: str) -> None:
        await delete_and_purge(env, fid)
        seen.extend(objects_of(env, fid))
        # a crashed run's last PUT after the purge: nothing of this run will ever flush again
        await env.store.put(derivative_key("t_acme", fid, "thumbnail"), "t_acme", b"x", "image/png")
        raise OSError("worker crashed")

    during(env, "image.normalize", purge_then_late_put, file_id)
    await env.drain()
    assert seen == [] and objects_of(env, file_id) == [derivative_key("t_acme", file_id, "thumbnail")]
    assert await env.drain((FILE_PURGE,)) == []  # the sweep waits SWEEP_DELAY_S
    await env.clock.advance(SWEEP_DELAY_S + 1)
    swept = await env.drain((FILE_PURGE,))
    assert [j.payload for j in swept] == [{"file_id": file_id, "sweep": True}]
    assert objects_of(env, file_id) == []


async def test_purge_of_a_finished_file_schedules_no_sweep(env: MediaEnv) -> None:
    file_id = await upload_png(env)
    await env.drain()
    await env.files.delete(principal(), file_id)
    assert len(await env.drain((FILE_PURGE,))) == 1
    await env.clock.advance(SWEEP_DELAY_S + 1)
    assert await env.drain((FILE_PURGE,)) == [] and objects_of(env, file_id) == []


async def test_redelivery_after_the_purge_sweeps_late_objects(env: MediaEnv) -> None:
    file_id = await upload_png(env)
    async with env.world.db.system_uow() as sys:
        (job,) = await sys.claim_jobs((MEDIA_PROCESS,), 1, "w", 60)
    await env.files.delete(principal(), file_id)
    await env.drain((FILE_PURGE,))
    await env.store.put(derivative_key("t_acme", file_id, "raster"), "t_acme", b"x", "image/png")  # a crashed run
    await env.processor.process(job)
    assert objects_of(env, file_id) == [] and env.runner.kinds() == []


async def test_delete_during_parallel_pdf_jobs_stops_both_and_discards(env: MediaEnv) -> None:
    env.runner.script["pdf.markdown"] = md_script(["text"])
    ref = await env.files.upload(
        principal(), chunks(fx.text_pdf()), declared_type="application/pdf", filename="a.pdf", session_id=None
    )
    published = during(env, "pdf.markdown", delete_and_purge, ref.file_id)
    await env.drain()
    assert objects_of(env, ref.file_id) == [] and await raw_row(env, ref.file_id) is None
    assert published == [] and "ocr" not in env.runner.kinds()


def lock(env: MediaEnv, *keys: str) -> dict[str, int]:
    """S3 keeps `keys` (AccessDenied per key: a policy without s3:DeleteObject); DeleteObjects with Quiet=True then
    reports only the per-key `Errors`. Returns the ListObjectsV2 / DeleteObjects call counts."""
    calls = {"list": 0, "delete": 0}
    real_list, real_delete = env.s3.list_objects_v2, env.s3.delete_objects

    async def list_objects_v2(**kw: Any) -> dict[str, Any]:
        calls["list"] += 1
        await anyio.lowlevel.checkpoint()  # a real network call yields
        return await real_list(**kw)

    async def delete_objects(*, Bucket: str, Delete: Mapping[str, Any], **kw: Any) -> dict[str, Any]:  # noqa: N803
        calls["delete"] += 1
        kept = [o for o in Delete["Objects"] if o["Key"] in keys]
        free = [o for o in Delete["Objects"] if o["Key"] not in keys]
        await real_delete(Bucket=Bucket, Delete={**Delete, "Objects": free}, **kw)
        errors = [{"Key": o["Key"], "Code": "AccessDenied", "Message": "Access Denied"} for o in kept]
        return {"Errors": errors} if errors else {}

    env.s3.list_objects_v2 = list_objects_v2  # type: ignore[method-assign]
    env.s3.delete_objects = delete_objects  # type: ignore[method-assign]
    return calls


async def test_a_key_s3_keeps_fails_the_delete_instead_of_spinning(env: MediaEnv) -> None:
    """Wave E: delete_prefix ignored the per-key Errors and re-listed forever (shielded in the worker's discard)."""
    kept, other = derivative_key("t_acme", "file_X", "raster"), derivative_key("t_acme", "file_X", "thumbnail")
    for key in (kept, other):
        await env.store.put(key, "t_acme", b"x", "image/png")
    calls = lock(env, kept)
    with anyio.fail_after(2), pytest.raises(RuntimeError, match="AccessDenied"):
        await env.store.delete_prefix(derivative_prefix("t_acme", "file_X"))
    assert objects_of(env, "file_X") == [kept] and calls == {"list": 1, "delete": 1}


async def test_a_redelivery_whose_discard_fails_fails_the_job(env: MediaEnv) -> None:
    file_id = await upload_png(env)
    async with env.world.db.system_uow() as sys:
        (job,) = await sys.claim_jobs((MEDIA_PROCESS,), 1, "w", 60)
    await env.files.delete(principal(), file_id)
    await env.drain((FILE_PURGE,))
    late = derivative_key("t_acme", file_id, "raster")
    await env.store.put(late, "t_acme", b"x", "image/png")  # a crashed run's object S3 will not delete
    lock(env, late)
    with anyio.fail_after(2), pytest.raises(RuntimeError, match="AccessDenied"):
        await env.processor.process(job)  # the job fails (the queue retries it with backoff) instead of hanging
