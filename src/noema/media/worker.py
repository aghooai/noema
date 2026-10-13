"""The media-worker's job handlers (spec-07 §3.1 step 6, §3.2; ADR-002 §1, §6): `media.process` and `file.purge`.

`media.process` claims a file, downloads the quarantined original into the spool, runs the sandbox jobs of its type
(`detect`, then image: `image.normalize` → `ocr`; PDF: `pdf.sanitize` → `pdf.render` ∥ `pdf.markdown` → `ocr` on the
pages without a text layer; Markdown: `markdown.sanitize`), and after each job stores the validated outputs in S3,
updates the derivative rows, re-evaluates the file status and publishes it. An extractor result is final for its
derivative; the 2 job attempts only recover from a lapsed lease or a crash (finished derivatives are not re-run).
The worker runs no parser: every untrusted byte is handled inside the sandbox.

An inline image runs `detect` ∥ `image.normalize` (IMPL-074 latency); the normalize result is stored only after
`detect` matched. A delayed `media.process` job with payload `watchdog` (armed by `MediaFileService` for every
file) re-queues a file that is still not terminal, and settles it `failed` at the last round. sandboxd being
unreachable (`SandboxUnavailable`) is retried and never fails a file.

A file deleted or purged during the run stops it at its next row write: no status is written onto the deleted row
and every derivative object of the file is removed (`Gone`). Each job's spool dir is removed as soon as its outputs
are stored (ADR-002 §6); the inputs later jobs read (original, pdf_sanitized, raster) stay in one keep dir per run
and are hard-linked into the job dirs. File reads and manifest hashing run in worker threads.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Final

import anyio

from noema.core.clock import Clock
from noema.core.errors import NoemaError
from noema.core.ids import IdGen
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.media import DerivativeRef, FileRef, FileService
from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxKind, SandboxOutput, SandboxRunner
from noema.core.protocols.services import JobHandlers, ServiceSet
from noema.core.protocols.storage import Database, Job, JobQueue, PermanentJobError, UnitOfWork
from noema.core.protocols.telemetry import Telemetry
from noema.core.settings import ProcessSettings
from noema.core.values import JsonValue
from noema.media.intake import (
    MAX_INLINE_LONG_EDGE,
    Detected,
    MediaKind,
    TypeRow,
    extension_of,
    failure_object,
    match_detection,
    rejection_object,
)
from noema.media.pipeline import (
    FILE_PURGE,
    MEDIA_MAX_ATTEMPTS,
    MEDIA_PROCESS,
    REQUIRED,
    UPLOAD_PRIORITY,
    WATCHDOG_ROUNDS,
    FileEvents,
    MediaSecurityEvents,
    RedisFileEvents,
    arm_watchdog,
    enqueue_process,
    evaluate_status,
    file_ref,
    is_terminal,
    row_for_type,
)
from noema.media.quarantine import ObjectStore, derivative_key, derivative_prefix, quarantine_key
from noema.media.sandbox import spool
from noema.media.sandbox.backends import assert_backend_allowed
from noema.media.sandbox.bwrap import BwrapRunner
from noema.media.sandbox.inproc import InprocRlimitRunner
from noema.media.sandbox.limits import KIND_LIMITS
from noema.media.sandbox.manifest import TEXT_TYPES, reserved_tag_count
from noema.media.sandbox.remote import RemoteSandboxRunner, SandboxUnavailable

log = logging.getLogger("noema.media.worker")
STORE_TYPES: Final[Mapping[str, str]] = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "application/pdf": "application/pdf",
    "text/markdown": "text/markdown; charset=utf-8",
    "text/plain": "text/plain; charset=utf-8",
}
OUTPUT_KINDS: Final[Mapping[SandboxKind, tuple[str, ...]]] = {
    "image.normalize": ("raster", "raster_1568", "thumbnail"),
    "pdf.sanitize": ("pdf_sanitized",),
    "pdf.render": ("page_images",),
    "pdf.markdown": ("markdown",),
    "ocr": ("ocr_text",),
    "markdown.sanitize": ("markdown",),
}
KEEP_KINDS: Final = frozenset({"pdf_sanitized", "raster"})  # outputs a later job of the same run reads


class Rejected(Exception):
    """The file was rejected (or is already final): stop the job graph."""


class Gone(Rejected):
    """The file was deleted or purged while this run worked on it: stop, and discard what the run stored."""


@dataclass
class _Work:
    """One `media.process` run."""

    tenant_id: str
    ref: FileRef
    kind: MediaKind = "image"  # set once `detect` matched
    derivatives: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    ids: dict[str, str] = field(default_factory=dict)
    job_dirs: list[str] = field(default_factory=list)  # spool dirs to remove at the end (the keep dir; safety net)
    staged: list[dict[str, object]] = field(default_factory=list)
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    fetch: anyio.Lock = field(default_factory=anyio.Lock)
    rejected: bool = False
    keep: Path | None = None  # the run's spool dir of inputs a later job reads (original, pdf_sanitized, raster)
    local: dict[str, Path] = field(default_factory=dict)  # "original" or derivative kind -> its file in `keep`

    @property
    def file_id(self) -> str:
        return self.ref.file_id


def failure_code(job_kind: SandboxKind, result: ExtractResult) -> str:
    """The derivative failure code of a non-ok result (spec-07 §1.2)."""
    if result.status == "timeout":
        return "timeout"
    return "ocr_failed" if job_kind == "ocr" else "extraction_failed"


def _int_fact(facts: Mapping[str, JsonValue], key: str) -> int:
    value = facts.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def _write_live(u: UnitOfWork, file_id: str, changes: Mapping[str, object]) -> None:
    """Apply `changes` to the file row before anything else, and refuse a deleted or purged file (`Gone`, which rolls
    the unit of work back). The UPDATE locks the row, so a concurrent delete or purge commits after this unit of work
    (and then sees its objects) or before it (and this run sees `deleted_at`); a status is never written onto a
    deleted row. Without changes it is a plain read (no status is written then)."""
    try:
        row = await u.files.update(file_id, **changes)
    except NoemaError:  # purged: the row is gone
        raise Gone(file_id) from None
    if row.get("deleted_at") is not None:
        raise Gone(file_id)


def _read_output(path: Path, media_type: str) -> tuple[bytes, int]:
    """An output's bytes and, for text, its reserved-tag openings (the ADR-002 §6 step 2 pre-scan: counted for
    observability only; escaping happens at prompt render). The validator already checked strict UTF-8."""
    data = path.read_bytes()
    return data, reserved_tag_count(data.decode("utf-8")) if media_type in TEXT_TYPES else 0


def sandbox_runner(
    settings: ProcessSettings, *, spool_root: Path | None = None, telemetry: Telemetry | None = None
) -> SandboxRunner:
    """The media-worker's runner from `settings.sandbox_backend` (ADR-002 §7: `remote` is required in staging/prod;
    `ForbiddenInEnvironment` otherwise): `RemoteSandboxRunner` on `settings.sandbox_socket`, `BwrapRunner` (dev,
    test) or `InprocRlimitRunner` (test), on `spool_root` (default `settings.spool_dir`)."""
    backend, env = settings.sandbox_backend, settings.env
    assert_backend_allowed(backend, env, process="media-worker")
    root = spool_root or Path(settings.spool_dir)
    if backend == "bwrap":
        return BwrapRunner(env, spool_root=root)
    if backend == "inproc_rlimit":
        return InprocRlimitRunner(env, spool_root=root)
    return RemoteSandboxRunner(env, socket_path=Path(settings.sandbox_socket), spool_root=root, telemetry=telemetry)


async def _concurrently(*steps: Callable[[], Awaitable[None]]) -> None:
    """Run the steps at once and let each finish (every sandbox job is bounded); then re-raise the first error,
    unwrapped (no ExceptionGroup), so the handler sees `SandboxUnavailable` or `Gone` as such."""
    errors: list[Exception] = []

    async def guarded(step: Callable[[], Awaitable[None]]) -> None:
        try:
            await step()
        except Exception as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tg:
        for step in steps:
            tg.start_soon(guarded, step)
    if errors:
        raise errors[0]


def page_digest(outputs: tuple[SandboxOutput, ...]) -> str:
    """The `page_images` row sha256: over the pages' sha256 values in page order."""
    joined = "\n".join(o.sha256 for o in sorted(outputs, key=lambda o: o.page or 0))
    return hashlib.sha256(joined.encode()).hexdigest()


class MediaProcessor:
    """The `media.process` handler (see the module docstring). Runs under `tenant_uow(job.tenant_id)`."""

    def __init__(
        self,
        *,
        db: Database,
        jobs: JobQueue,
        store: ObjectStore,
        events: FileEvents,
        runner: SandboxRunner,
        ids: IdGen,
        clock: Clock,
        config: ConfigRegistry,
        spool_root: Path,
        security: MediaSecurityEvents,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._db = db
        self._jobs = jobs
        self._store = store
        self._events = events
        self._runner = runner
        self._ids = ids
        self._clock = clock
        self._config = config
        self._spool = spool_root
        self._security = security
        self._telemetry = telemetry

    @classmethod
    def from_services(
        cls, services: ServiceSet, *, runner: SandboxRunner | None = None, spool_root: Path | None = None
    ) -> MediaProcessor:
        """Production wiring: the runner of `settings.sandbox_backend` (`sandbox_runner`; default remote on
        `settings.sandbox_socket`) unless `runner` is given; the spool is `spool_root` or `settings.spool_dir`."""
        env = services.settings.env
        root = spool_root or Path(services.settings.spool_dir)
        if runner is None:
            runner = sandbox_runner(services.settings, spool_root=root, telemetry=services.telemetry)
        security = MediaSecurityEvents(
            telemetry=services.telemetry, outbox=services.outbox, ids=services.ids, clock=services.clock
        )
        return cls(
            db=services.db,
            jobs=services.jobs,
            store=ObjectStore(services.s3, bucket=services.settings.s3_bucket, env=env),
            events=RedisFileEvents(services.redis, services.keys, security=security),
            runner=runner,
            ids=services.ids,
            clock=services.clock,
            config=services.config,
            spool_root=root,
            security=security,
            telemetry=services.telemetry,
        )

    # --- rows --------------------------------------------------------------------------------------------------------
    async def _load(self, tenant_id: str, file_id: str) -> FileRef | None:
        async with self._db.tenant_uow(tenant_id) as u:
            row = await u.files.get(file_id)
            if row is None:
                return None
            return file_ref(row, await u.derivatives.list_for_file(file_id))

    def _stage(self, work: _Work, kind: str, **columns: object) -> None:
        """Stage one derivative row change; `_flush` writes it together with the file status."""
        row: dict[str, object] = {"file_id": work.file_id, "kind": kind, **columns}
        if kind not in work.ids:
            work.ids[kind] = self._ids.new("der")
            row.setdefault("id", work.ids[kind])
            row.setdefault("extractor_version", "pending")
        work.staged.append(row)
        code = columns.get("error_code")
        work.derivatives[kind] = (str(columns.get("status", "pending")), None if code is None else str(code))

    def _stage_failed(self, work: _Work, kind: str, code: str, *, extractor: str | None = None) -> None:
        columns: dict[str, object] = {"status": "failed", "error_code": code, "completed_at": self._clock.now()}
        if extractor is not None:
            columns["extractor_version"] = extractor
        self._stage(work, kind, **columns)

    async def _flush(
        self, work: _Work, file_changes: Mapping[str, object] | None = None, *, evaluate: bool = True
    ) -> None:
        """Write the staged derivative rows and the file changes in ONE unit of work, so a reader never sees a
        derivative state the file status does not reflect; `evaluate` re-derives the status (spec-07 §1.1).
        Publishes the status."""
        async with work.lock:  # parallel jobs (render ∥ markdown) commit one at a time, newest state last
            rows, work.staged = work.staged, []
            changes = dict(file_changes or {})
            if changes.get("status") == "rejected":
                work.rejected = True
            elif work.rejected:  # final: a sibling job finishing later only settles its own rows
                changes.pop("status", None)
                evaluate = False
            if evaluate and "status" not in changes:
                update = evaluate_status(work.kind, work.derivatives)
                changes["status"] = update.status
                if update.failure_code is not None:
                    changes["failure"] = failure_object(update.failure_code)
            async with self._db.tenant_uow(work.tenant_id) as u:
                await _write_live(u, work.file_id, changes)
                for row in rows:
                    await u.derivatives.upsert(row)
        status = changes.get("status")
        if isinstance(status, str):
            await self._events.publish(work.tenant_id, work.file_id, status)

    async def _pending(self, work: _Work, kinds: tuple[str, ...]) -> None:
        """Create the rows of a job's outputs (`pending`) before the job runs."""
        for kind in kinds:
            if kind not in work.derivatives:
                self._stage(work, kind, status="pending")
        await self._flush(work)

    async def _reject(self, work: _Work, code: str) -> None:
        """File `rejected{code}`; every pending derivative fails with that code; the job graph stops."""
        for kind, (status, _) in list(work.derivatives.items()):
            if status == "pending":
                self._stage_failed(work, kind, code)
        await self._flush(work, {"status": "rejected", "rejection": rejection_object(code)})
        raise Rejected(code)

    # --- sandbox jobs ------------------------------------------------------------------------------------------------
    def _keep_dir(self, work: _Work) -> Path:
        """The run's keep dir (created on first use; named like a job dir, so sandboxd's orphan sweep covers a
        crashed run; its mtime is refreshed on every use)."""
        if work.keep is None:
            keep_id = self._ids.new("sbx")
            work.keep = spool.job_dir(self._spool, keep_id)
            work.keep.mkdir(mode=0o700, parents=True)
            work.job_dirs.append(keep_id)
        os.utime(work.keep)
        return work.keep

    async def _source(self, work: _Work, kind: str) -> Path:
        """The local file of `kind` ("original" or a derivative): this run's copy, else downloaded into keep."""
        async with work.fetch:  # render ∥ markdown share one download
            keep = self._keep_dir(work)
            local = work.local.get(kind)
            if local is None:
                local = keep / kind
                key = quarantine_key(work.tenant_id, work.file_id) if kind == "original" else None
                await self._store.download_to(key or derivative_key(work.tenant_id, work.file_id, kind), local)
                work.local[kind] = local
            return local

    @asynccontextmanager
    async def _job(self, work: _Work, source: str, *, release: bool = False) -> AsyncIterator[tuple[str, Path]]:
        """A fresh spool job dir whose input is `source` (hard-linked, not copied), removed as soon as the block
        ends (its outputs are stored by then; ADR-002 §6). `release`: this was the source's last reader."""
        local = await self._source(work, source)
        job_id = self._ids.new("sbx")
        try:
            await anyio.to_thread.run_sync(partial(spool.prepare, self._spool, job_id, local, link=True))
            yield job_id, spool.input_path(self._spool, job_id)
        finally:
            with anyio.CancelScope(shield=True):  # up to the output budget of files: off the event loop
                await anyio.to_thread.run_sync(spool.remove, self._spool, job_id)
            if release and work.local.pop(source, None) is not None:
                local.unlink(missing_ok=True)

    def _keep_output(self, work: _Work, out: SandboxOutput) -> None:
        """Move an output a later job reads from its job dir (removed next) into the keep dir."""
        target = self._keep_dir(work) / out.kind
        os.replace(out.path, target)
        work.local[out.kind] = target

    async def _run(
        self, work: _Work, job_id: str, path: Path, kind: SandboxKind, params: dict[str, JsonValue]
    ) -> ExtractResult:
        """One sandbox job, with its span (`aghoo.media.kind`, `aghoo.media.status`), the job counter and wall-time
        histogram, and the security events of its outcome."""
        attrs: dict[str, str | int | float | bool] = {"aghoo.tenant": work.tenant_id, "aghoo.media.kind": kind}
        span_cm = self._telemetry.span("media.extract", **attrs) if self._telemetry is not None else None
        if span_cm is None:
            result = await self._runner.run(SandboxJob(job_id, kind, path, params), KIND_LIMITS[kind])
        else:
            with span_cm as span:
                result = await self._runner.run(SandboxJob(job_id, kind, path, params), KIND_LIMITS[kind])
                span.set("aghoo.media.status", result.status)
                if result.status != "ok":
                    span.error("file_rejected" if result.status == "rejected" else "internal_error")
        if self._telemetry is not None:
            self._telemetry.instruments.add("aghoo_media_jobs_total", kind=kind, status=result.status)
            self._telemetry.instruments.observe("aghoo_media_job_seconds", result.wall_ms / 1000, kind=kind)
        log.info("sandbox job kind=%s status=%s wall_ms=%d", kind, result.status, result.wall_ms)
        if result.status == "violation":
            self._security.emit("sandbox.violation", tenant_id=work.tenant_id, job_id=job_id, kind=kind)
        elif result.status == "failed" and result.facts.get("error") == "manifest_invalid":
            self._security.emit("sandbox.manifest_invalid", tenant_id=work.tenant_id, job_id=job_id, kind=kind)
        elif result.status == "failed" and result.facts.get("error") == "bad_request":
            log.error("sandbox bad_request kind=%s (a worker bug)", kind)
        warnings = result.facts.get("warnings")
        if isinstance(warnings, list) and "sanitize_incomplete" in warnings:
            self._security.emit("sandbox.sanitize_incomplete", tenant_id=work.tenant_id, job_id=job_id, kind=kind)
        return result

    async def _store_output(self, work: _Work, out: SandboxOutput) -> None:
        key = derivative_key(work.tenant_id, work.file_id, out.kind, out.page)
        content_type = STORE_TYPES.get(out.media_type, "application/octet-stream")
        # up to 64 MiB: read (and pre-scanned) off the event loop
        data, tags = await anyio.to_thread.run_sync(_read_output, out.path, out.media_type)
        if tags:
            log.info("reserved tags in a text output kind=%s count=%d", out.kind, tags)
        await self._store.put(key, work.tenant_id, data, content_type)

    async def _finish(
        self,
        work: _Work,
        job_kind: SandboxKind,
        result: ExtractResult,
        *,
        extra: Mapping[str, object] | None = None,
        file_changes: Mapping[str, object] | None = None,
        then: tuple[str, ...] = (),
    ) -> bool:
        """Store a job's outputs (S3 first), then commit its derivative rows and the re-evaluated file status in one
        unit of work. True when every output kind is ready. A rejection of a required output rejects the file
        (raises `Rejected`); an optional one only fails its derivative. `then`: derivatives of the job that follows
        a success, created `pending` in the same commit (the file goes `partial`, never a transient `ready`)."""
        kinds = OUTPUT_KINDS[job_kind]
        if result.status == "rejected" and set(REQUIRED[work.kind]) & set(kinds):
            await self._reject(work, result.rejection_code or "corrupt")
        produced = {o.kind for o in result.outputs}
        if result.status != "ok" or not set(kinds) <= produced:
            code = failure_code(job_kind, result)  # also an `ok` without its outputs (`ocr`: ocr_failed)
            for kind in kinds:
                self._stage_failed(work, kind, code, extractor=result.extractor)
            await self._flush(work)
            return False
        for out in result.outputs:
            await self._store_output(work, out)
        now = self._clock.now()
        for kind in kinds:
            outs = tuple(o for o in result.outputs if o.kind == kind)
            columns: dict[str, object] = {
                "status": "ready",
                "extractor_version": result.extractor,
                "completed_at": now,
                "error_code": None,
                "storage_key": derivative_key(work.tenant_id, work.file_id, kind),
            }
            if kind == "page_images":
                columns.update(sha256=page_digest(outs), bytes=sum(o.bytes for o in outs), pages=len(outs))
            else:
                columns.update(sha256=outs[0].sha256, bytes=outs[0].bytes, width=outs[0].width, height=outs[0].height)
                if kind in KEEP_KINDS:
                    self._keep_output(work, outs[0])
            columns.update(extra or {})
            self._stage(work, kind, **columns)
        for kind in then:
            if kind not in work.derivatives:
                self._stage(work, kind, status="pending")
        await self._flush(work, file_changes)
        return True

    # --- the per-type job graphs -------------------------------------------------------------------------------------
    def _done(self, work: _Work, kinds: tuple[str, ...]) -> bool:
        """True when every output of a job is already final (a retried run skips the job)."""
        return all(work.derivatives.get(k, ("pending", None))[0] in ("ready", "failed") for k in kinds)

    async def _detect(self, work: _Work) -> TypeRow:
        if work.ref.media_type is not None and (row := row_for_type(work.ref.media_type)) is not None:
            return row  # detected by an earlier attempt
        declared = row_for_type(work.ref.declared_type)
        normalized = self._done(work, OUTPUT_KINDS["image.normalize"])
        if work.ref.origin == "inline" and declared is not None and declared.kind == "image" and not normalized:
            return await self._detect_inline(work, declared)
        async with self._job(work, "original") as (job_id, path):
            result = await self._run(work, job_id, path, "detect", {})
        return await self._matched(work, result)

    async def _matched(self, work: _Work, result: ExtractResult) -> TypeRow:
        """The row a `detect` result matched (stored as the file's `media_type`); a rejection, failure or mismatch
        settles the file and raises `Rejected`."""
        if result.status == "rejected":
            await self._reject(work, result.rejection_code or "type_mismatch")
        if result.status != "ok":
            code = failure_code("detect", result)
            await self._flush(work, {"status": "failed", "failure": failure_object(code)}, evaluate=False)
            raise Rejected(code)
        ext = extension_of(work.ref.filename or "") if work.ref.origin == "upload" else None
        match = match_detection(result.facts, work.ref.declared_type, ext)
        if not isinstance(match, Detected):
            await self._reject(work, match.code)
            raise AssertionError("unreachable")  # _reject raises
        await self._flush(work, {"media_type": match.row.declared[0]}, evaluate=False)
        return match.row

    async def _detect_inline(self, work: _Work, declared: TypeRow) -> TypeRow:
        """An inline image (IMPL-074 latency): `detect` ∥ `image.normalize` in the declared row's format, so the two
        cold jail starts overlap. The normalize result is stored only once `detect` matched the declared row (an
        inline type must match it); otherwise it is dropped with its job dir, unread."""
        results: dict[str, ExtractResult] = {}
        async with self._job(work, "original") as (det_id, det_path), self._job(work, "original") as (img_id, img_path):

            async def detect() -> None:
                results["detect"] = await self._run(work, det_id, det_path, "detect", {})

            async def normalize() -> None:
                params: dict[str, JsonValue] = {"format": declared.pil_format}
                results["image"] = await self._run(work, img_id, img_path, "image.normalize", params)

            await _concurrently(detect, normalize)
            row = await self._matched(work, results["detect"])
            if row.name == declared.name:
                work.kind = row.kind
                await self._normalized(work, results["image"])
        return row

    async def _ocr(self, work: _Work, source: str, pages: list[int] | None) -> None:
        if self._done(work, ("ocr_text",)):
            return
        await self._pending(work, ("ocr_text",))
        params: dict[str, JsonValue] = {"pages": None if pages is None else [*pages]}
        async with self._job(work, source, release=True) as (job_id, path):
            await self._finish(work, "ocr", await self._run(work, job_id, path, "ocr", params))

    async def _image(self, work: _Work, row: TypeRow) -> None:
        kinds = OUTPUT_KINDS["image.normalize"]
        if not self._done(work, kinds):
            await self._pending(work, kinds)
            async with self._job(work, "original", release=True) as (job_id, path):
                result = await self._run(work, job_id, path, "image.normalize", {"format": row.pil_format})
                await self._normalized(work, result)
        if work.derivatives.get("raster", ("", None))[0] == "ready":
            await self._ocr(work, "raster", None)

    async def _normalized(self, work: _Work, result: ExtractResult) -> None:
        """Settle an `image.normalize` result (inside its job block, the outputs are still there). An inline image
        whose original long edge is over 8,000 px is rejected `pixel_limit` with no derivative stored (IMPL-074:
        413 image_too_large)."""
        long_edge = max(_int_fact(result.facts, "width"), _int_fact(result.facts, "height"))
        if result.status == "ok" and work.ref.origin == "inline" and long_edge > MAX_INLINE_LONG_EDGE:
            await self._reject(work, "pixel_limit")
        await self._finish(work, "image.normalize", result, then=("ocr_text",))

    async def _pdf(self, work: _Work) -> None:
        if not self._done(work, ("pdf_sanitized",)):
            await self._pending(work, ("pdf_sanitized",))
            async with self._job(work, "original", release=True) as (job_id, path):
                result = await self._run(work, job_id, path, "pdf.sanitize", {})
                changes: dict[str, object] = {}
                extra: dict[str, object] = {}
                if result.status == "ok" and result.outputs:
                    changes["pages"] = result.facts.get("pages")
                    active = result.facts.get("active_content")
                    if isinstance(active, list):  # derivative.facts (spec-09 §6.3 step 5; the validator checked it)
                        extra["facts"] = {"active_content": sorted({str(a) for a in active})}
                await self._finish(work, "pdf.sanitize", result, extra=extra, file_changes=changes)
        if work.derivatives.get("pdf_sanitized", ("", None))[0] != "ready":
            return
        render_facts: dict[str, JsonValue] = {}
        stopped: list[Exception] = []
        async with anyio.create_task_group() as tg:

            async def guarded(step: Callable[[], Awaitable[None]]) -> None:
                try:
                    await step()
                except Gone as gone:  # the file is gone: stop the sibling now
                    stopped.append(gone)
                    tg.cancel_scope.cancel()
                except Exception as exc:  # the sibling finishes its own derivative (a rejected file stays rejected)
                    stopped.append(exc)

            tg.start_soon(guarded, lambda: self._render(work, render_facts))
            tg.start_soon(guarded, lambda: self._markdown_pdf(work))
        if stopped:  # Gone first, then an error (retried), then a rejection; never an ExceptionGroup
            stopped.sort(key=lambda e: 0 if isinstance(e, Gone) else 2 if isinstance(e, Rejected) else 1)
            raise stopped[0]
        page_images = work.derivatives.get("page_images", ("", None))[0]
        if page_images == "ready":
            pages = next((d for d in await self._rows(work) if d.kind == "page_images"), None)
            if pages is not None and pages.pages:
                text = set(pages.text_layer_pages or ())
                scanned = [p for p in range(1, pages.pages + 1) if p not in text]
                if scanned:
                    await self._ocr(work, "pdf_sanitized", scanned)

    async def _rows(self, work: _Work) -> tuple[DerivativeRef, ...]:
        ref = await self._load(work.tenant_id, work.file_id)
        return () if ref is None else ref.derivatives

    async def _render(self, work: _Work, facts_out: dict[str, JsonValue]) -> None:
        kinds = OUTPUT_KINDS["pdf.render"]
        if self._done(work, kinds):
            return
        await self._pending(work, kinds)
        async with self._job(work, "pdf_sanitized") as (job_id, path):
            result = await self._run(work, job_id, path, "pdf.render", {"long_edge_px": 1568})
            text_layer = result.facts.get("text_layer_pages")
            scanned = isinstance(text_layer, list) and _int_fact(result.facts, "pages") > len(text_layer)
            then = ("ocr_text",) if scanned else ()
            await self._finish(work, "pdf.render", result, extra={"text_layer_pages": text_layer}, then=then)
        facts_out.update(result.facts)

    async def _markdown_pdf(self, work: _Work) -> None:
        kinds = OUTPUT_KINDS["pdf.markdown"]
        if self._done(work, kinds):
            return
        await self._pending(work, kinds)
        async with self._job(work, "pdf_sanitized") as (job_id, path):
            result = await self._run(work, job_id, path, "pdf.markdown", {})
            await self._finish(work, "pdf.markdown", result, extra={"pages": result.facts.get("pages")})

    async def _markdown(self, work: _Work) -> None:
        kinds = OUTPUT_KINDS["markdown.sanitize"]
        if self._done(work, kinds):
            return
        await self._pending(work, kinds)
        async with self._job(work, "original", release=True) as (job_id, path):
            result = await self._run(work, job_id, path, "markdown.sanitize", {})
            await self._finish(work, "markdown.sanitize", result)

    # --- the handler -------------------------------------------------------------------------------------------------
    def _work(self, tenant_id: str, ref: FileRef) -> _Work:
        work = _Work(tenant_id, ref)
        for d in ref.derivatives:
            work.ids[d.kind] = d.id
            work.derivatives[d.kind] = (d.status, None)
        return work

    async def process(self, job: Job) -> None:
        """Handle one `media.process` job (see the module docstring); payload `watchdog` is a file check."""
        file_id = job.payload.get("file_id")
        if not isinstance(file_id, str) or not file_id.startswith("file_"):
            raise PermanentJobError("media.process needs payload.file_id")
        if "watchdog" in job.payload:
            await self._watchdog(job, file_id)
            return
        ref = await self._load(job.tenant_id, file_id)
        if ref is None:  # deleted or purged: drop what an interrupted earlier run may have stored after the purge
            await self._discard(job.tenant_id, file_id)
            return
        if is_terminal(ref):
            return  # already final: nothing to do (an idempotent re-delivery)
        work = self._work(job.tenant_id, ref)
        try:
            if ref.status == "quarantined":
                await self._flush(work, {"status": "processing"}, evaluate=False)
            row = await self._detect(work)
            work.kind = row.kind
            if row.kind == "image":
                await self._image(work, row)
            elif row.kind == "pdf":
                await self._pdf(work)
            else:
                await self._markdown(work)
        except Gone:
            with anyio.CancelScope(shield=True):
                await self._discard(work.tenant_id, work.file_id)
        except Rejected:
            return
        except SandboxUnavailable:  # nothing was dispatched: retried; after the last attempt the watchdog re-queues
            log.warning("sandboxd is unreachable file_id=%s attempt=%d: retried later", file_id, job.attempt)
            raise
        except Exception:
            if job.attempt >= MEDIA_MAX_ATTEMPTS:  # the last attempt: settle the file instead of leaving it pending
                with anyio.CancelScope(shield=True):
                    await self._give_up(work)
            raise
        finally:
            with anyio.CancelScope(shield=True):
                for job_id in work.job_dirs:  # the keep dir (job dirs are already gone)
                    await anyio.to_thread.run_sync(spool.remove, self._spool, job_id)

    async def _settle_failed(self, work: _Work) -> None:
        """File `failed{extraction_failed}`; every pending derivative fails with it."""
        for kind, (status, _) in list(work.derivatives.items()):
            if status == "pending":
                self._stage_failed(work, kind, "extraction_failed")
        await self._flush(work, {"status": "failed", "failure": failure_object("extraction_failed")}, evaluate=False)

    async def _give_up(self, work: _Work) -> None:
        try:
            await self._settle_failed(work)
        except Gone:
            await self._discard(work.tenant_id, work.file_id)
        except Exception as exc:  # the job is dead either way; the file's watchdog settles it later
            log.error("media.process could not settle file_id=%s error=%s", work.file_id, type(exc).__name__)

    async def _watchdog(self, job: Job, file_id: str) -> None:
        """Check `payload.watchdog` (the round) of a file (`pipeline.arm_watchdog`). A file that is not terminal by
        now is re-queued (dedupe `file_id`: nothing new while its run is still queued or running) and checked again
        `WATCHDOG_DELAY_S` later; at round `WATCHDOG_ROUNDS` it is settled `failed{extraction_failed}`. This covers a
        crash between the file INSERT and its job, a worker killed on the last attempt, and sandboxd outages."""
        round_ = job.payload.get("watchdog")
        if not isinstance(round_, int) or isinstance(round_, bool) or round_ < 1:
            raise PermanentJobError("a media.process watchdog needs payload.watchdog >= 1")
        ref = await self._load(job.tenant_id, file_id)
        if ref is None:
            await self._discard(job.tenant_id, file_id)
            return
        if is_terminal(ref):
            return
        if round_ >= WATCHDOG_ROUNDS:
            log.error("media.process watchdog settles file_id=%s status=%s as failed", file_id, ref.status)
            try:
                await self._settle_failed(self._work(job.tenant_id, ref))
            except Gone:
                await self._discard(job.tenant_id, file_id)
            return
        log.warning("media.process watchdog re-queues file_id=%s status=%s round=%d", file_id, ref.status, round_)
        await enqueue_process(self._jobs, job.tenant_id, file_id, priority=UPLOAD_PRIORITY)
        await arm_watchdog(self._jobs, self._clock, job.tenant_id, file_id, round_ + 1)

    async def _discard(self, tenant_id: str, file_id: str) -> None:
        """Remove every derivative object of a deleted or purged file (the purge may already have run)."""
        removed = await self._store.delete_prefix(derivative_prefix(tenant_id, file_id))
        if removed:
            log.info("media.process discarded %d objects of deleted file_id=%s", removed, file_id)

    async def purge(self, job: Job, files: FileService) -> None:
        """The `file.purge` handler: `files.purge`, or (payload `sweep: true`) the delayed re-sweep the purge
        schedules for a file that was still processing: once the row is gone, derivative objects an in-flight run
        stored after the purge are removed (a row still present belongs to its purge, which is retried)."""
        file_id = job.payload.get("file_id")
        if not isinstance(file_id, str):
            raise PermanentJobError("file.purge needs payload.file_id")
        if job.payload.get("sweep") is not True:
            await files.purge(job.tenant_id, file_id)
            return
        async with self._db.tenant_uow(job.tenant_id) as u:
            try:
                await u.files.update(file_id)  # a read, deleted rows included
            except NoemaError:
                gone = True
            else:
                gone = False
        if gone:
            await self._discard(job.tenant_id, file_id)


def make_handlers(
    services: ServiceSet, *, runner: SandboxRunner | None = None, spool_root: Path | None = None
) -> JobHandlers:
    """The media-worker's handlers: `media.process` (file runs and watchdog checks) and `file.purge`
    (`services.files.purge`, or the sweep). `runner` defaults to `sandbox_runner(services.settings)` (the backend of
    NOEMA_SANDBOX_BACKEND; remote, required in staging/prod), `spool_root` to `settings.spool_dir`."""
    processor = MediaProcessor.from_services(services, runner=runner, spool_root=spool_root)

    async def purge(job: Job) -> None:
        await processor.purge(job, services.files)

    return {MEDIA_PROCESS: processor.process, FILE_PURGE: purge}
