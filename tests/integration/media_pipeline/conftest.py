"""Integration fixtures of the media pipeline: the real `PgDatabase` (noema_app, RLS), an aiobotocore S3 client on
the compose moto with a fresh bucket, a Redis client on the compose valkey, and a minimal `JobQueue` over the real
`JobRepo` (the ops-ledger queue is another role's; claiming goes through the real `PgSystemCalls`).

In the media image (`make test-media`, network_mode none) these services are unreachable: tests that need them are
skipped there (`needs_services`)."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import anyio
import pytest
from aiobotocore.session import get_session
from tests.fixtures.db import SeededTenant, seed_tenant

from noema.core.clock import Clock
from noema.core.ids import IdGen
from noema.core.protocols.storage import Database, Job, Worker
from noema.core.values import JsonValue
from noema.db.engine import Engine
from noema.db.repos import PgUnitOfWork, make_unit_of_work
from noema.db.uow import PgDatabase

IN_MEDIA_IMAGE = os.environ.get("NOEMA_MEDIA_IMAGE") == "1"
needs_services = pytest.mark.skipif(IN_MEDIA_IMAGE, reason="the media image has no network (compose services)")


class RepoJobQueue:
    """`JobQueue.enqueue` over the real `JobRepo` (INSERT + pg_notify at commit); `worker` is not needed here."""

    def __init__(self, db: Database, ids: IdGen, clock: Clock) -> None:
        self._db = db
        self._ids = ids
        self._clock = clock
        self.enqueued: list[str] = []

    async def enqueue(
        self,
        kind: str,
        tenant_id: str,
        payload: Mapping[str, JsonValue],
        *,
        priority: int = 100,
        run_after: datetime | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 5,
    ) -> str:
        row = {
            "id": self._ids.new("job"),
            "kind": kind,
            "payload": dict(payload),
            "priority": priority,
            "dedupe_key": dedupe_key,
            "max_attempts": max_attempts,
        }
        if run_after is not None:
            row["run_after"] = run_after
        async with self._db.tenant_uow(tenant_id) as u:
            job_id = await u.jobs.insert(row)
        self.enqueued.append(job_id)
        return job_id

    def worker(self, kinds: Sequence[str], handler: Callable[[Job], Awaitable[None]], *, concurrency: int) -> Worker:
        return _Worker(self, tuple(kinds), handler)

    async def claim(self, kinds: tuple[str, ...]) -> tuple[Job, ...]:
        async with self._db.system_uow() as sys:
            return await sys.claim_jobs(kinds, 10, "media-it", 60)

    async def finish(self, job: Job, *, ok: bool) -> None:
        async with self._db.system_uow() as sys:
            await sys.finish_job(job.id, "media-it", ok=ok, error=None if ok else "error", permanent=False)


class _Worker:
    """Claims and runs jobs until cancelled (polling; the tests drive time)."""

    def __init__(self, queue: RepoJobQueue, kinds: tuple[str, ...], handler: Callable[[Job], Awaitable[None]]) -> None:
        self._queue = queue
        self._kinds = kinds
        self._handler = handler

    async def run(self) -> None:
        while True:
            for job in await self._queue.claim(self._kinds):
                try:
                    await self._handler(job)
                except Exception:
                    await self._queue.finish(job, ok=False)
                else:
                    await self._queue.finish(job, ok=True)
            await anyio.sleep(0.05)


@pytest.fixture
async def media_db(pg_app_engine: Engine) -> PgDatabase[PgUnitOfWork]:
    return PgDatabase(pg_app_engine, make_unit_of_work)


@pytest.fixture
async def media_tenant(pg_admin: Any) -> SeededTenant:
    return await seed_tenant(pg_admin, projects=("prj_a", "prj_b"))


@pytest.fixture
async def s3_bucket(noema_services: Any, noema_run_id: str) -> AsyncIterator[tuple[Any, str]]:
    bucket = "noema-t-" + re.sub(r"[^a-z0-9-]", "-", noema_run_id.lower())[:36] + "-" + uuid.uuid4().hex[:6]
    async with get_session().create_client(
        "s3",
        endpoint_url=noema_services.moto_url,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    ) as client:
        await client.create_bucket(Bucket=bucket)
        try:
            yield client, bucket
        finally:
            listed = await client.list_objects_v2(Bucket=bucket)
            for item in listed.get("Contents", []):
                await client.delete_object(Bucket=bucket, Key=item["Key"])
            uploads = await client.list_multipart_uploads(Bucket=bucket)
            for up in uploads.get("Uploads", []):
                await client.abort_multipart_upload(Bucket=bucket, Key=up["Key"], UploadId=up["UploadId"])
            await client.delete_bucket(Bucket=bucket)


@pytest.fixture
async def redis_client(noema_services: Any) -> AsyncIterator[Any]:
    import redis.asyncio

    client = redis.asyncio.Redis.from_url(noema_services.redis_url, decode_responses=False)
    try:
        yield client
    finally:
        await client.aclose()
