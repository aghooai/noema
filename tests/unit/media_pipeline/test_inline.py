"""MED-06 (spec-07 §3.2, IMPL-037, IMPL-074): inline images through `ingest_inline`: the arithmetic 3,750,000-byte
check before any job, the wait for `raster` only (OCR continues), 413 for a long edge over 8,000 px from the job,
409 `file_rejected` for a type mismatch or a failure, 409 `file_not_ready` when the bounded wait expires."""

from __future__ import annotations

import base64
from pathlib import Path

import anyio
import anyio.lowlevel
import pytest
from tests.contract_internal.kits import principal
from tests.fakes.telemetry import FakeTelemetry

from noema.core.clock import Deadline
from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.media import FileRef
from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxLimits

from . import media_fixtures as fx
from .support import InlineRunner, MediaEnv, fail_with, media_env

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path: Path) -> MediaEnv:
    return media_env(tmp_path)


def telemetry(env: MediaEnv) -> FakeTelemetry:
    assert isinstance(env.world.telemetry, FakeTelemetry)
    return env.world.telemetry


def deadline(env: MediaEnv, seconds: float = 30) -> Deadline:
    return Deadline(env.clock.monotonic() + seconds, env.clock)


async def ingest_with_worker(env: MediaEnv, data: bytes, media_type: str = "image/png") -> FileRef:
    """`ingest_inline` while a worker drains the queue (the job is enqueued at priority 10)."""
    result: list[FileRef] = []
    error: list[BaseException] = []

    async def ingest() -> None:
        try:
            result.append(
                await env.files.ingest_inline(
                    principal(), base64.b64encode(data).decode(), media_type, deadline=deadline(env)
                )
            )
        except NoemaError as exc:
            error.append(exc)

    async with anyio.create_task_group() as tg:
        tg.start_soon(ingest)
        await anyio.wait_all_tasks_blocked()
        await env.drain()
    if error:
        raise error[0]
    return result[0]


async def test_exactly_3_75_mb_is_accepted_and_returns_at_raster(env: MediaEnv) -> None:
    ref = await ingest_with_worker(env, fx.png_of_size(3_750_000))
    assert ref.origin == "inline" and ref.bytes == 3_750_000 and ref.status in ("partial", "ready")
    assert {d.kind for d in ref.derivatives if d.status == "ready"} >= {"raster", "raster_1568"}
    job = next(j for j in env.runner.jobs if j.kind == "image.normalize")
    assert job.params == {"format": "PNG"}
    jobs = [r for r in env.world.db.store.rows("job") if r["kind"] == "media.process"]
    runs = [r for r in jobs if "watchdog" not in r["payload"]]
    assert [(r["priority"], r["max_attempts"], r["dedupe_key"]) for r in runs] == [(10, 2, ref.file_id)]
    (watchdog,) = (r for r in jobs if "watchdog" in r["payload"])  # armed for every file (C1 review)
    assert watchdog["dedupe_key"] == f"{ref.file_id}:watchdog:1" and watchdog["state"] == "queued"


async def test_3_75_mb_plus_one_is_413_with_no_job_or_object(env: MediaEnv) -> None:
    data = base64.b64encode(b"\x00" * 3_750_001).decode()
    with pytest.raises(NoemaError) as err:
        await env.files.ingest_inline(principal(), data, "image/png", deadline=deadline(env))
    assert err.value.code is ErrorCode.IMAGE_TOO_LARGE
    assert env.world.jobs.enqueued == [] and env.s3.calls == []


async def test_long_edge_8001_is_413_from_the_job(env: MediaEnv) -> None:
    with pytest.raises(NoemaError) as err:
        await ingest_with_worker(env, fx.png(8_001, 8))
    assert err.value.code is ErrorCode.IMAGE_TOO_LARGE
    file_id = next(iter(k for k in env.objects() if "/quarantine/" in k)).rsplit("/", 1)[1]
    ref = await env.files.get(principal(), file_id)
    assert ref.status == "rejected" and (ref.rejection or {}).get("code") == "pixel_limit"
    assert not [k for k in env.objects() if "/derivatives/" in k]  # no derivative stored
    assert all(d.status == "failed" for d in ref.derivatives)


async def test_type_mismatch_is_409_file_rejected(env: MediaEnv) -> None:
    with pytest.raises(NoemaError) as err:
        await ingest_with_worker(env, fx.jpeg(), "image/png")
    assert err.value.code is ErrorCode.FILE_REJECTED
    assert err.value.details is not None and err.value.details["rejection"] == {
        "code": "type_mismatch",
        "message": "The file content does not match its declared type or extension.",
    }


async def test_a_failed_normalize_is_409_with_details_failure(env: MediaEnv) -> None:
    env.runner.script["image.normalize"] = fail_with("timeout", "timeout")
    with pytest.raises(NoemaError) as err:
        await ingest_with_worker(env, fx.png())
    assert err.value.code is ErrorCode.FILE_REJECTED
    assert err.value.details is not None and err.value.details["failure"] == {
        "code": "timeout",
        "message": "Extraction timed out.",
        "retryable": True,
    }


async def test_the_bounded_wait_expiring_is_409_file_not_ready(env: MediaEnv) -> None:
    errors: list[NoemaError] = []

    async def ingest() -> None:
        try:
            await env.files.ingest_inline(
                principal(), base64.b64encode(fx.png()).decode(), "image/png", deadline=deadline(env, 30)
            )
        except NoemaError as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tg:
        tg.start_soon(ingest)
        await anyio.wait_all_tasks_blocked()
        await env.clock.advance(3.1)  # platform.media.inline_wait_ms = 3000 < the request deadline
    assert [e.code for e in errors] == [ErrorCode.FILE_NOT_READY]
    assert errors[0].spec.retryable


async def test_a_request_deadline_before_inline_wait_ms_bounds_the_wait(env: MediaEnv) -> None:
    """`min(deadline, now + inline_wait_ms)`: the deadline side (1 s < 3 s; C1 review)."""
    errors: list[NoemaError] = []

    async def ingest() -> None:
        try:
            await env.files.ingest_inline(
                principal(), base64.b64encode(fx.png()).decode(), "image/png", deadline=deadline(env, 1)
            )
        except NoemaError as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tg:
        tg.start_soon(ingest)
        await anyio.wait_all_tasks_blocked()
        await env.clock.advance(1.1)
        assert [e.code for e in errors] == [ErrorCode.FILE_NOT_READY]  # returned at the deadline, not at 3 s
    observed = [m for m in telemetry(env).metrics if m.name == "aghoo_inline_ingest_seconds"]
    assert len(observed) == 1 and 1.0 <= observed[0].value < 1.5


class Overlap:
    """Records whether `detect` and `image.normalize` were running at the same time."""

    def __init__(self, inner: InlineRunner) -> None:
        self.inner = inner
        self.running: set[str] = set()
        self.overlapped = False

    async def run(self, job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        self.running.add(job.kind)
        self.overlapped = self.overlapped or self.running >= {"detect", "image.normalize"}
        await anyio.lowlevel.checkpoint()
        try:
            return await self.inner.run(job, limits)
        finally:
            self.running.discard(job.kind)


async def test_inline_detect_and_normalize_run_at_once(env: MediaEnv) -> None:
    """IMPL-074 latency (C1 review): the two jail starts overlap; the result is the same."""
    overlap = Overlap(env.runner)
    env.processor._runner = overlap
    ref = await ingest_with_worker(env, fx.png(64, 48))
    assert overlap.overlapped and env.runner.kinds()[:2] in (
        ["detect", "image.normalize"],
        ["image.normalize", "detect"],
    )
    assert {d.kind for d in ref.derivatives if d.status == "ready"} >= {"raster", "raster_1568", "thumbnail"}
    assert ref.media_type == "image/png"
    observed = [m.value for m in telemetry(env).metrics if m.name == "aghoo_inline_ingest_seconds"]
    assert len(observed) == 1


async def test_a_mismatched_inline_image_stores_no_speculative_output(env: MediaEnv) -> None:
    with pytest.raises(NoemaError) as err:
        await ingest_with_worker(env, fx.png(), "image/jpeg")  # normalize ran as JPEG beside detect
    assert err.value.code is ErrorCode.FILE_REJECTED
    assert "image.normalize" in env.runner.kinds()
    assert not [k for k in env.objects() if "/derivatives/" in k]


async def test_unsupported_inline_types_are_415(env: MediaEnv) -> None:
    with pytest.raises(NoemaError) as err:
        await env.files.ingest_inline(principal(), "QUJD", "application/pdf", deadline=deadline(env))
    assert err.value.code is ErrorCode.UNSUPPORTED_MEDIA_TYPE
