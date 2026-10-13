"""Real media-pipeline implementations in the shared conformance suites (spec-02 §14):

- `file_service` / `media-pipeline`: `MediaFileService` + `MediaProcessor` over the fakes (FakeDatabase, FakeS3Client,
  LocalFileEvents), with jobs processed as soon as they are enqueued and a stub sandbox that fabricates valid outputs
  (the suite's 72-byte "PNG" is not a decodable image; the sandbox has its own suite).
- `sandbox_runner` / `inproc_rlimit`: `InprocRlimitRunner` (real extractors in child processes; NOEMA_ENV=test).
- `sandbox_runner` / `bwrap`: `BwrapRunner` (host development; skipped without a working bwrap).
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Final, cast

import pytest
from tests.contract_internal.kits import MediaKit, SandboxKit
from tests.contract_internal.registry import Harness, register
from tests.fakes.config import example_snapshot
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.s3 import FakeS3Client
from tests.fakes.world import build_world

from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxLimits
from noema.core.protocols.storage import Job
from noema.core.values import JsonValue
from noema.media.extractors.common import Outputs
from noema.media.intake import rejection_object
from noema.media.pipeline import MEDIA_PROCESS, LocalFileEvents, MediaFileService, MediaSecurityEvents
from noema.media.quarantine import ObjectStore
from noema.media.sandbox import spool
from noema.media.sandbox.base import local_job_id, result_for
from noema.media.sandbox.bwrap import BwrapRunner
from noema.media.sandbox.inproc import InprocRlimitRunner, clear_out
from noema.media.sandbox.limits import clamp
from noema.media.worker import MediaProcessor

BUCKET: Final = "noema-test"
# a valid 1x1 RGB PNG (what the stub sandbox writes for every raster output)
TINY_PNG: Final = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c49444154789c63f8cfc0000003010100"
    "c9fe92ef0000000049454e44ae426082"
)
TINY_PDF: Final = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


class StubSandbox:
    """A `SandboxRunner` that fabricates valid outputs per kind (PNG input → png facts) through the real manifest
    path, so `MediaFileService` is exercised end to end without decoding the suite's placeholder bytes."""

    def __init__(self, spool_root: Path) -> None:
        self._spool = spool_root

    async def run(self, job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        job_id = local_job_id(job.job_id)
        canonical = spool.input_path(self._spool, job_id)
        jd = spool.prepare(self._spool, job_id, None if job.input_path == canonical else job.input_path)
        clear_out(jd / "out")
        data = (jd / "in" / "input.bin").read_bytes()
        out = Outputs(job.kind, jd / "out", "stub")
        if job.kind == "detect":
            png, pdf = data.startswith(b"\x89PNG"), data.startswith(b"%PDF-")
            label, mime = ("png", "image/png") if png else ("pdf", "application/pdf") if pdf else ("txt", "text/plain")
            facts: dict[str, JsonValue] = {"magika_label": label, "magika_score": 1.0, "libmagic_mime": mime}
            out.facts.update({**facts, "bytes": len(data), "utf8_strict": not png, "has_nul": png})
        elif job.kind == "image.normalize":
            out.facts.update({"width": 1, "height": 1, "frames": 1})
            for name, kind in (
                ("raster.png", "raster"),
                ("raster_1568.png", "raster_1568"),
                ("thumbnail.png", "thumbnail"),
            ):
                out.add(name, TINY_PNG, kind=kind, media_type="image/png", width=1, height=1)
        elif job.kind == "ocr":
            out.add("ocr_text.txt", b"text", kind="ocr_text", media_type="text/plain")
        elif job.kind == "pdf.sanitize":
            out.facts.update({"pages": 1, "encrypted": False, "active_content": []})
            out.add("pdf_sanitized.pdf", TINY_PDF, kind="pdf_sanitized", media_type="application/pdf")
        elif job.kind == "pdf.render":
            out.facts.update({"pages": 1, "text_layer_pages": [1]})
            out.add("page-0001.png", TINY_PNG, kind="page_images", media_type="image/png", page=1)
        else:  # pdf.markdown, markdown.sanitize
            if job.kind == "pdf.markdown":
                out.facts["pages"] = 1
            out.add("markdown.md", b"# Document", kind="markdown", media_type="text/markdown")
        out.write_manifest()
        return result_for(job.kind, jd / "out", "ok", wall_ms=1, limits=clamp(job.kind, limits), backend="stub")


class EagerJobQueue(FakeJobQueue):
    """`FakeJobQueue` that runs `media.process` right after the INSERT (unless `hold` is set; delayed jobs such as
    the file watchdog stay queued)."""

    hold = False
    processor: MediaProcessor | None = None

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
        job_id = await super().enqueue(
            kind,
            tenant_id,
            payload,
            priority=priority,
            run_after=run_after,
            dedupe_key=dedupe_key,
            max_attempts=max_attempts,
        )
        if kind == MEDIA_PROCESS and run_after is None and not self.hold and self.processor is not None:
            await self.processor.process(Job(job_id, tenant_id, kind, payload, attempt=1))
        return job_id


@register("file_service", "media-pipeline")
@asynccontextmanager
async def media_file_service(h: Harness) -> AsyncIterator[MediaKit]:
    tmp: Path = h.fixture("tmp_path")
    world = build_world(clock=h.clock)
    jobs = EagerJobQueue(world.clock, world.db, world.ids)
    s3 = FakeS3Client(buckets=(BUCKET,))
    store = ObjectStore(cast(Any, s3), bucket=BUCKET, env="test")
    events = LocalFileEvents()
    files = MediaFileService(
        db=world.db, jobs=jobs, store=store, config=world.config, clock=world.clock, ids=world.ids, events=events
    )
    jobs.processor = MediaProcessor(
        db=world.db,
        jobs=jobs,
        store=store,
        events=events,
        runner=StubSandbox(tmp / "spool"),
        ids=world.ids,
        clock=world.clock,
        config=world.config,
        spool_root=tmp / "spool",
        security=MediaSecurityEvents(telemetry=world.telemetry, outbox=world.outbox, ids=world.ids, clock=world.clock),
    )

    def pending_uploads(pending: bool) -> None:
        jobs.hold = pending

    def reject(file_id: str) -> None:
        row = next(r for r in world.db.store.rows("file") if r["id"] == file_id)
        world.db.store.seed("file", {**row, "status": "rejected", "rejection": rejection_object("type_mismatch")})

    yield MediaKit(
        files,
        world.views,
        world.negotiator,
        world.media_budget,
        example_snapshot(),
        h.clock,
        pending_uploads=pending_uploads,
        reject=reject,
    )


@register("sandbox_runner", "inproc_rlimit")
@asynccontextmanager
async def inproc_sandbox(h: Harness) -> AsyncIterator[SandboxKit]:
    tmp: Path = h.fixture("tmp_path")
    yield SandboxKit(InprocRlimitRunner("test", spool_root=tmp / "spool"))


def _bwrap_works() -> bool:
    return shutil.which("bwrap") is not None and Path("/proc/self/ns/user").exists()


@register(
    "sandbox_runner",
    "bwrap",
    marks=(pytest.mark.skipif(not _bwrap_works(), reason="bwrap (unprivileged user namespaces) not available"),),
)
@asynccontextmanager
async def bwrap_sandbox(h: Harness) -> AsyncIterator[SandboxKit]:
    tmp: Path = h.fixture("tmp_path")
    yield SandboxKit(BwrapRunner("test", spool_root=tmp / "spool"))
