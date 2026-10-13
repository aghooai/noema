"""Test doubles of the media-pipeline unit tests: `InlineRunner` (the real extractors in-process plus the trusted
manifest validation, with scripted overrides per kind) and `MediaEnv` (the real `MediaFileService` and
`MediaProcessor` over the fakes: FakeDatabase, FakeJobQueue, FakeS3Client, LocalFileEvents, FakeClock)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from tests.fakes.clock import FakeClock
from tests.fakes.s3 import FakeS3Client
from tests.fakes.world import FakeWorld, build_world

from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxKind, SandboxLimits
from noema.core.protocols.storage import Job
from noema.media.extractors.common import Outputs, Rejection
from noema.media.extractors.entry import load_extractor
from noema.media.pipeline import FILE_PURGE, MEDIA_PROCESS, LocalFileEvents, MediaFileService, MediaSecurityEvents
from noema.media.quarantine import ObjectStore
from noema.media.sandbox import spool
from noema.media.sandbox.base import local_job_id, result_for
from noema.media.sandbox.inproc import clear_out
from noema.media.sandbox.limits import check_params, clamp
from noema.media.sandbox.manifest import failure
from noema.media.worker import MediaProcessor

BUCKET = "noema-test"
Script = Callable[[SandboxJob, Path], ExtractResult | None]


class InlineRunner:
    """A `SandboxRunner` running the extractors in this process (no isolation: tests only). `script[kind]` may
    return an `ExtractResult` to replace the real run (failures, timeouts, violations) or None to run normally."""

    def __init__(self, spool_root: Path) -> None:
        self.spool = spool_root
        self.script: dict[str, Script] = {}
        self.jobs: list[SandboxJob] = []

    async def run(self, job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        self.jobs.append(job)
        lim = clamp(job.kind, limits)
        params = check_params(job.kind, job.params)
        job_id = local_job_id(job.job_id)
        canonical = spool.input_path(self.spool, job_id)
        jd = spool.prepare(self.spool, job_id, None if job.input_path == canonical else job.input_path)
        clear_out(jd / "out")
        if (scripted := self.script.get(job.kind)) is not None and (result := scripted(job, jd / "out")) is not None:
            return result
        run, extractor = load_extractor(job.kind)
        out = Outputs(job.kind, jd / "out", extractor)
        try:
            run(jd / "in" / "input.bin", out, params)
        except Rejection as rejection:
            out.write_manifest(rejection.code)
        except Exception:
            return failure("failed", "exit", wall_ms=1, extractor=f"inline:{job.kind}")
        else:
            out.write_manifest()
        return result_for(job.kind, jd / "out", "ok", wall_ms=1, limits=lim, backend="inline")

    def kinds(self) -> list[SandboxKind]:
        return [j.kind for j in self.jobs]


def ocr_text(text: str = "4471") -> Script:
    """A scripted `ocr` that writes `text` (one segment per listed page for PDFs) through the real manifest path."""

    def run(job: SandboxJob, out_dir: Path) -> ExtractResult:
        pages = job.params.get("pages")
        out = Outputs("ocr", out_dir, "scripted-ocr")
        if isinstance(pages, list):
            last = max([p for p in pages if isinstance(p, int)], default=1)
            body = "\f".join(text if i + 1 in pages else "" for i in range(last))
        else:
            body = text
        out.add("ocr_text.txt", body.encode(), kind="ocr_text", media_type="text/plain")
        out.write_manifest()
        return result_for("ocr", out_dir, "ok", wall_ms=1, limits=clamp("ocr"), backend="scripted")

    return run


def fail_with(status: str, reason: str) -> Script:
    return lambda job, out: failure(status, reason, wall_ms=3, extractor=f"scripted:{job.kind}")  # type: ignore[arg-type]


@dataclass
class MediaEnv:
    world: FakeWorld
    s3: FakeS3Client
    store: ObjectStore
    events: LocalFileEvents
    files: MediaFileService
    processor: MediaProcessor
    runner: InlineRunner
    finished: list[str] = field(default_factory=list)

    @property
    def clock(self) -> FakeClock:
        return self.world.fake_clock

    async def drain(self, kinds: tuple[str, ...] = (MEDIA_PROCESS,), *, attempts: int = 10) -> list[Job]:
        """Claim and run queued jobs of `kinds` (one claim per round) until none is left; returns them."""
        ran: list[Job] = []
        for _ in range(attempts):
            async with self.world.db.system_uow() as sys:
                jobs = await sys.claim_jobs(kinds, 10, "test-worker", 60)
            if not jobs:
                return ran
            for job in jobs:
                handler = self.processor.process if job.kind == MEDIA_PROCESS else self.purge
                ok = True
                try:
                    await handler(job)
                except Exception:
                    ok = False
                async with self.world.db.system_uow() as sys:
                    await sys.finish_job(job.id, "test-worker", ok=ok, error=None if ok else "error", permanent=False)
                ran.append(job)
        return ran

    async def purge(self, job: Job) -> None:
        await self.processor.purge(job, self.files)

    def objects(self) -> Mapping[str, Any]:
        return self.s3.objects[BUCKET]


def media_env(tmp_path: Path, *, sandbox: Any = None, spool_root: Path | None = None, **world_kwargs: Any) -> MediaEnv:
    """The real service and processor over the fakes; `sandbox` replaces the InlineRunner (e.g. NsjailRunner in
    the media image), `spool_root` the spool directory."""
    world = build_world(clock=FakeClock(), **world_kwargs)
    s3 = FakeS3Client(buckets=(BUCKET,))
    store = ObjectStore(cast(Any, s3), bucket=BUCKET, env="test")
    events = LocalFileEvents()
    files = MediaFileService(
        db=world.db,
        jobs=world.jobs,
        store=store,
        config=world.config,
        clock=world.clock,
        ids=world.ids,
        events=events,
        telemetry=world.telemetry,
    )
    spool_dir = spool_root or tmp_path / "spool"
    runner = InlineRunner(spool_dir)
    runner.script["ocr"] = ocr_text()  # tesseract runs in the media image tests (MED-02)
    security = MediaSecurityEvents(telemetry=world.telemetry, outbox=world.outbox, ids=world.ids, clock=world.clock)
    processor = MediaProcessor(
        db=world.db,
        jobs=world.jobs,
        store=store,
        events=events,
        runner=sandbox or runner,
        ids=world.ids,
        clock=world.clock,
        config=world.config,
        spool_root=spool_dir,
        security=security,
        telemetry=world.telemetry,
    )
    return MediaEnv(world, s3, store, events, files, processor, runner)


__all__ = ["BUCKET", "FILE_PURGE", "InlineRunner", "MediaEnv", "fail_with", "media_env", "ocr_text"]


def md_script(pages: list[str]) -> Script:
    """A scripted `pdf.markdown` (Docling runs in the media image tests) writing one segment per page."""

    def run(job: SandboxJob, out_dir: Path) -> ExtractResult:
        out = Outputs("pdf.markdown", out_dir, "scripted-docling")
        out.facts["pages"] = len(pages)
        out.add("markdown.md", "\f".join(pages).encode(), kind="markdown", media_type="text/markdown")
        out.write_manifest()
        return result_for("pdf.markdown", out_dir, "ok", wall_ms=1, limits=clamp("pdf.markdown"), backend="scripted")

    return run
