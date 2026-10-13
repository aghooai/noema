"""The media pipeline on the real stores (spec-07 §3, §6; spec-03 file/derivative/job rows; spec-09 §6.7, §7):
Postgres with RLS (noema_app), S3 on moto (multipart, SSE args, purge), Redis file events (with the foreign-tenant
drop), and the real extractors through the InprocRlimitRunner."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from tests.fakes.config import FakeConfigRegistry
from tests.fakes.outbox import FakeOutbox
from tests.fakes.telemetry import FakeTelemetry
from tests.fixtures.db import SeededTenant
from tests.unit.media_pipeline import media_fixtures as fx
from tests.unit.media_pipeline.support import InlineRunner, md_script

from noema.core.clock import SystemClock
from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import UlidIdGen
from noema.core.principal import ALL_SCOPES, Principal, Role
from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxLimits
from noema.core.protocols.storage import Job
from noema.core.redis_keys import RedisKeys
from noema.core.values import JsonValue
from noema.media.pipeline import MEDIA_PROCESS, MediaFileService, MediaSecurityEvents, RedisFileEvents
from noema.media.quarantine import ObjectStore
from noema.media.sandbox.inproc import InprocRlimitRunner
from noema.media.worker import MediaProcessor

from .conftest import RepoJobQueue, needs_services

pytestmark = [pytest.mark.integration, needs_services]


def who(tenant: SeededTenant, project: str = "prj_a", *, roles: tuple[Role, ...] = ("developer",)) -> Principal:
    return Principal(
        tenant.tenant_id,
        project,
        tenant.principal_ids[project],
        "key_1",
        frozenset(roles),
        ALL_SCOPES,
        "req_1",
    )


class HostRunner:
    """The InprocRlimitRunner, except `pdf.markdown` (Docling needs the models baked into the media image) and `ocr`
    (no tesseract on the host), which are scripted; the media image tests run both for real."""

    def __init__(self, spool: Path) -> None:
        self.inproc = InprocRlimitRunner("test", spool_root=spool)
        self.inline = InlineRunner(spool)
        self.inline.script["pdf.markdown"] = md_script(["Invoice number 4471"])

    async def run(self, job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        if job.kind in ("pdf.markdown", "ocr"):
            return await self.inline.run(job, limits)
        return await self.inproc.run(job, limits)


class Stack:
    def __init__(self, db: Any, s3: Any, bucket: str, redis: Any, run_id: str, spool: Path) -> None:
        clock = SystemClock()
        ids = UlidIdGen(clock)
        self.outbox = FakeOutbox()
        self.telemetry = FakeTelemetry()
        security = MediaSecurityEvents(telemetry=self.telemetry, outbox=self.outbox, ids=ids, clock=clock)
        self.events = RedisFileEvents(redis, RedisKeys(f"ns:{run_id}:"), security=security)
        self.store = ObjectStore(s3, bucket=bucket, env="test")
        self.jobs = RepoJobQueue(db, ids, clock)
        config = FakeConfigRegistry()
        self.files = MediaFileService(
            db=db, jobs=self.jobs, store=self.store, config=config, clock=clock, ids=ids, events=self.events
        )
        self.processor = MediaProcessor(
            db=db,
            jobs=self.jobs,
            store=self.store,
            events=self.events,
            runner=HostRunner(spool),
            ids=ids,
            clock=clock,
            config=config,
            spool_root=spool,
            security=security,
            telemetry=self.telemetry,
        )
        self.redis = redis
        self.keys = RedisKeys(f"ns:{run_id}:")

    async def drain(self) -> None:
        while jobs := await self.jobs.claim((MEDIA_PROCESS,)):
            for job in jobs:
                await self.processor.process(job)
                await self.jobs.finish(job, ok=True)

    async def drop_queued(self) -> None:
        """Finish, unprocessed, the jobs earlier tests left queued (their buckets are gone)."""
        while jobs := await self.jobs.claim((MEDIA_PROCESS,)):
            for job in jobs:
                await self.jobs.finish(job, ok=True)


@pytest.fixture
def stack(media_db: Any, s3_bucket: tuple[Any, str], redis_client: Any, noema_run_id: str, tmp_path: Path) -> Stack:
    return Stack(media_db, s3_bucket[0], s3_bucket[1], redis_client, noema_run_id, tmp_path / "spool")


async def chunks(data: bytes, size: int = 1 << 20) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def test_upload_process_watch_download_and_purge(stack: Stack, media_tenant: SeededTenant) -> None:
    me = who(media_tenant)
    ref = await stack.files.upload(
        me, chunks(fx.text_pdf()), declared_type="application/pdf", filename="inv.pdf", session_id=None
    )
    seen: list[str] = []
    first = anyio.Event()

    async def watch() -> None:
        async for current in stack.files.watch(me, ref.file_id):
            seen.append(current.status)
            first.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(watch)
        await first.wait()
        await stack.drain()
    assert seen[0] == "quarantined" and seen[-1] == "ready", seen  # woken through Redis
    done = await stack.files.get(me, ref.file_id)
    pages = next(d for d in done.derivatives if d.kind == "page_images")
    assert done.media_type == "application/pdf" and done.pages == 1 and pages.text_layer_pages == (1,)
    assert (await stack.files.read_derivative(me, ref.file_id, "pdf_sanitized")).startswith(b"%PDF-")
    head = await stack.store._s3.head_object(
        Bucket=stack.store._bucket, Key=f"{media_tenant.tenant_id}/quarantine/{ref.file_id}"
    )
    assert head["ServerSideEncryption"] == "AES256"
    other = who(media_tenant, "prj_b")
    with pytest.raises(NoemaError) as hidden:  # RLS: another project's developer
        await stack.files.get(other, ref.file_id)
    assert hidden.value.code is ErrorCode.NOT_FOUND
    await stack.files.purge(media_tenant.tenant_id, ref.file_id)
    listed = await stack.store._s3.list_objects_v2(Bucket=stack.store._bucket, Prefix=media_tenant.tenant_id)
    assert listed.get("KeyCount", 0) == 0
    with pytest.raises(NoemaError):
        await stack.files.get(me, ref.file_id)


async def test_multipart_upload_and_abort_on_the_cap(stack: Stack, media_tenant: SeededTenant) -> None:
    me = who(media_tenant)
    big = fx.png(16, 16) + b"\x00" * (17 * 1024 * 1024)
    ref = await stack.files.upload(me, chunks(big), declared_type="image/png", filename="big.png", session_id=None)
    assert ref.bytes == len(big)
    with pytest.raises(NoemaError) as err:
        await stack.files.upload(
            me, chunks(b"#" * (21 * 1024 * 1024)), declared_type="image/png", filename="x.png", session_id=None
        )
    assert err.value.code is ErrorCode.FILE_TOO_LARGE
    uploads = await stack.store._s3.list_multipart_uploads(Bucket=stack.store._bucket)
    assert uploads.get("Uploads", []) == []  # aborted: no partial quarantine object (SEC-EDGE-01)


async def test_foreign_tenant_file_events_are_dropped(stack: Stack, media_tenant: SeededTenant) -> None:
    me = who(media_tenant)
    ref = await stack.files.upload(me, chunks(fx.png()), declared_type="image/png", filename="a.png", session_id=None)
    async with stack.events.subscribe(me.tenant_id, ref.file_id) as waiter:
        forged = json.dumps({"tenant_id": "t_forged", "file_id": ref.file_id, "status": "ready"})
        await stack.redis.publish(stack.keys.file_events(ref.file_id), forged)
        with anyio.move_on_after(1.5) as scope:
            await waiter.wait()
        assert scope.cancelled_caught  # the forged message did not wake the watcher
    assert [r.payload["action"] for r in stack.outbox.records] == ["security.pubsub_tenant_mismatch"]


async def test_active_content_facts_live_on_the_derivative_row(stack: Stack, media_tenant: SeededTenant) -> None:
    """media-pipeline request item 2: `derivative.facts` (revision 0002, CHECK derivative_facts_closed) on Postgres."""
    import io

    import pikepdf

    await stack.drop_queued()
    me = who(media_tenant)
    doc = pikepdf.open(io.BytesIO(fx.text_pdf("Invoice number 4471")))
    doc.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert(1)"))
    buf = io.BytesIO()
    doc.save(buf)
    ref = await stack.files.upload(
        me, chunks(buf.getvalue()), declared_type="application/pdf", filename="a.pdf", session_id=None
    )
    await stack.drain()
    got = await stack.files.get(me, ref.file_id)
    assert got.status == "ready"
    sanitized = next(d for d in got.derivatives if d.kind == "pdf_sanitized")
    assert sanitized.active_content == ("open_action",)  # DerivativeRef.active_content, read from the row
    async with stack.processor._db.tenant_uow(media_tenant.tenant_id) as u:
        rows = {r["kind"]: r for r in await u.derivatives.list_for_file(ref.file_id)}
    assert rows["pdf_sanitized"]["facts"] == {"active_content": ["open_action"]} and rows["markdown"]["facts"] is None


async def test_the_watchdog_requeues_a_lost_job_on_postgres(stack: Stack, media_tenant: SeededTenant) -> None:
    """A file whose `media.process` job ended without settling it (a crash) is re-queued by its watchdog; while the
    job is still live, the watchdog's enqueue is a dedupe no-op (C1 review: files stuck non-terminal)."""
    await stack.drop_queued()
    me = who(media_tenant)
    ref = await stack.files.upload(me, chunks(fx.png()), declared_type="image/png", filename="a.png", session_id=None)

    def check(round_: int) -> Job:
        payload: dict[str, JsonValue] = {"file_id": ref.file_id, "watchdog": round_}
        return Job(f"job_watchdog_{round_}", media_tenant.tenant_id, MEDIA_PROCESS, payload, attempt=1)

    before = len(stack.jobs.enqueued)
    await stack.processor.process(check(1))  # the run is still queued: nothing new
    live = await stack.jobs.claim((MEDIA_PROCESS,))
    assert [j.payload for j in live] == [{"file_id": ref.file_id}] and len(stack.jobs.enqueued) == before + 2
    for job in live:  # the worker "crashed": the job ends, the file is still quarantined
        await stack.jobs.finish(job, ok=True)
    assert (await stack.files.get(me, ref.file_id)).status == "quarantined"
    await stack.processor.process(check(2))  # re-queued: a fresh run
    await stack.drain()
    assert (await stack.files.get(me, ref.file_id)).status in ("ready", "partial")
    await stack.processor.process(check(3))  # terminal: nothing to do
    assert (await stack.files.get(me, ref.file_id)).status in ("ready", "partial")


@pytest.mark.parametrize("purge", [True, False])
async def test_delete_during_processing_on_postgres(stack: Stack, media_tenant: SeededTenant, purge: bool) -> None:
    """The row lock of the worker's status write against a concurrent delete/purge (real Postgres, RLS, moto)."""
    await stack.drop_queued()
    me = who(media_tenant)
    ref = await stack.files.upload(
        me, chunks(fx.png(64, 48)), declared_type="image/png", filename="a.png", session_id=None
    )
    runner = stack.processor._runner
    real_run = runner.run

    async def run(job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        if job.kind == "image.normalize":
            await stack.files.delete(me, ref.file_id)
            if purge:
                await stack.files.purge(media_tenant.tenant_id, ref.file_id)
        return await real_run(job, limits)

    runner.run = run  # type: ignore[method-assign]
    await stack.drain()
    prefix = f"{media_tenant.tenant_id}/derivatives/{ref.file_id}/"
    listed = await stack.store._s3.list_objects_v2(Bucket=stack.store._bucket, Prefix=prefix)
    assert listed.get("KeyCount", 0) == 0
    async with stack.processor._db.tenant_uow(media_tenant.tenant_id) as u:
        if purge:
            with pytest.raises(NoemaError):
                await u.files.update(ref.file_id)
        else:
            row = await u.files.update(ref.file_id)
            assert row["status"] == "deleted" and row["deleted_at"] is not None


async def test_watchers_share_one_pubsub_connection(noema_services: Any, noema_run_id: str) -> None:
    """Wave E (security finding 5): each watcher held its own pubsub connection from the process's one Redis pool,
    so ~100 watchFile streams of one tenant drained it and every Redis feature of that process degraded. With a pool
    of 4, 24 watchers (8 files x 3) must stay on Redis (all woken by a publish from another client, well before the
    2 s poll) while the pool still serves other commands."""
    import redis.asyncio

    keys = RedisKeys(f"ns:{noema_run_id}:share:")
    small = redis.asyncio.Redis.from_url(noema_services.redis_url, max_connections=4)
    other = redis.asyncio.Redis.from_url(noema_services.redis_url)
    events = RedisFileEvents(small, keys)
    files = [f"file_share{i}" for i in range(8)]
    woken: list[str] = []
    try:
        async with contextlib.AsyncExitStack() as stack:
            waiters = [
                (f, await stack.enter_async_context(events.subscribe("t_acme", f))) for f in files for _ in range(3)
            ]
            assert await small.ping()  # the pool is not drained by the watchers
            with anyio.fail_after(1.5):  # the publishes, not the 2 s poll, wake every watcher
                async with anyio.create_task_group() as tg:

                    async def wait(file_id: str, waiter: Any) -> None:
                        await waiter.wait()
                        woken.append(file_id)

                    for f, w in waiters:
                        tg.start_soon(wait, f, w)
                    await anyio.sleep(0.2)
                    for f in files:
                        await other.publish(keys.file_events(f), json.dumps({"tenant_id": "t_acme", "file_id": f}))
            assert await small.ping()
    finally:
        await small.aclose()
        await other.aclose()
    assert sorted(woken) == sorted(f for f in files for _ in range(3))
