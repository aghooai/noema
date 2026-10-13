# noema.media pipeline (owner: media-pipeline; Wave C1, adapted in C2a)

Specs: spec-07 §1-§3 and §6-§7, ADR-002 (all), spec-09 §6, spec-03 (file, derivative, job), spec-11 §4; IMPL-029f/g,
IMPL-037, IMPL-062, IMPL-065, IMPL-074, IMPL-081. Change requests: `.orchestration/requests/media-pipeline.md`.

## Purpose
Uploads (`POST /v1/files`) and inline images (`FileService.ingest_inline`) go into quarantine, then the
`media.process` job runs the sandbox jobs of the detected type and stores only re-encoded, manifest-validated
derivatives. The API and the media-worker never parse media; every parser runs in `python -I -m
noema.media.extractors.entry <kind>` inside a jail (nsjail in the network-less `media-sandbox` container).

## Module layout (`src/noema/media/`)
| Module | Contents |
|---|---|
| `intake.py` | `TYPE_ROWS` (spec-07 §2 map), `MAGIKA_TEXT_LABELS` (the pinned model's `is_text` labels, accepted for Markdown), `precheck_upload` (415), caps, `inline_row`, `check_inline_size` (IMPL-037 arithmetic), `decode_inline`, `match_detection` (spec-07 §2.2 steps 0-4), rejection/failure objects, `MultipartReader` (incremental multipart, python-multipart; 64 KiB cap on everything but the file data), `decoded_filename` (RFC 7578 `filename`, else RFC 5987 `filename*`, NFC, full length: the extension source), `display_filename`/`shorten_filename` (≤ 255 characters, suffix kept) |
| `quarantine.py` | S3 layout (`quarantine_key`, `derivative_key`, `derivative_prefix`), `ObjectStore` (streaming capped put with sha256, multipart over 8 MiB and abort on failure, `open`, `get`, `download_to`, `delete_prefix`: a key S3 keeps, a per-key `Errors` entry of DeleteObjects such as AccessDenied, raises RuntimeError so the job fails and is retried with backoff instead of re-listing forever (Wave E); `sse_args` on every put) |
| `pipeline.py` | `MediaFileService` (FileService), `evaluate_status`/`is_terminal` (spec-07 §1.1), `file_ref`/`file_object`/`download_filename` (§3.3, IMPL-065), `LocalFileEvents`/`RedisFileEvents`, `DerivativeCache` (256 MiB LRU), `SingleFlight` (Wave E: concurrent `read_derivative` calls of one principal share the file's row read, and concurrent LRU misses of one object share its S3 GET; a media decision's members resolve their views at once), `MediaSecurityEvents`, `session_resource`/`file_resource` (authz resources), `enqueue_process`, `arm_watchdog` (the per-file watchdog) |
| `worker.py` | `MediaProcessor` (`media.process`: runs and watchdog checks), `sandbox_runner(settings)`, `make_handlers(services, *, runner=None, spool_root=None)` |
| `sandbox/` | `limits` (ADR-002 §5 table, `clamp`, closed params/facts), `ipc` (UDS request/response), `spool` (layout, `reclaim_out` ownership walk, orphan sweep), `manifest` (trusted validator), `base` (exit mapping), `child` (subprocess with rlimits/affinity), `backends` (`assert_backend_allowed`), `inproc`, `bwrap`, `nsjail`, `remote`, `sandboxd` |
| `extractors/` | `entry`, `detect`, `image`, `pdf_sanitize`, `pdf_render`, `pdf_markdown`, `ocr`, `markdown`, `md_render` (spec-09 §6.5 renderer), `common` (manifest writer) |
| `api/routes/files.py` | uploadFile, getFile, deleteFile, watchFile, getFileDerivative |

## Public surface and constructors (exact)
```python
from noema.media.pipeline import MediaFileService, RedisFileEvents, LocalFileEvents, MediaSecurityEvents, DerivativeCache
from noema.media.quarantine import ObjectStore
from noema.media.worker import MediaProcessor, make_handlers, sandbox_runner
from noema.media.sandbox.remote import RemoteSandboxRunner, SandboxUnavailable   # media-worker (staging/prod)
from noema.media.sandbox.nsjail import NsjailRunner             # inside sandboxd only
from noema.media.sandbox.bwrap import BwrapRunner               # host dev (test, dev)
from noema.media.sandbox.inproc import InprocRlimitRunner       # unit tests (test only)
from noema.media.sandbox.backends import assert_backend_allowed, COMPONENT_KIND

MediaFileService(*, db: Database, jobs: JobQueue, store: ObjectStore, config: ConfigRegistry, clock: Clock,
                 ids: IdGen, events: FileEvents, cache: DerivativeCache | None = None,
                 telemetry: Telemetry | None = None)   # telemetry: aghoo_inline_ingest_seconds
MediaFileService.from_services(services: ServiceSet)   # ObjectStore(services.s3, bucket=settings.s3_bucket,
                                                        # env=settings.env) + RedisFileEvents(services.redis, keys)
ObjectStore(s3: S3Client, *, bucket: str, env: str)
RedisFileEvents(redis, keys: RedisKeys, *, security: MediaSecurityEvents | None = None)
MediaSecurityEvents(*, telemetry: Telemetry | None, outbox: Outbox | None, ids: IdGen, clock: Clock)
MediaProcessor(*, db, jobs: JobQueue, store, events, runner: SandboxRunner, ids, clock, config, spool_root: Path,
               security: MediaSecurityEvents, telemetry: Telemetry | None = None)   # jobs: C2a (the watchdog)
MediaProcessor.from_services(services, *, runner=None, spool_root=None)   # runner None -> sandbox_runner(settings)
sandbox_runner(settings: ProcessSettings, *, spool_root=None, telemetry=None) -> SandboxRunner
    # settings.sandbox_backend: remote (RemoteSandboxRunner on settings.sandbox_socket), bwrap, inproc_rlimit;
    # spool settings.spool_dir; assert_backend_allowed(backend, env, process="media-worker") first
make_handlers(services, *, runner=None, spool_root=None) -> {"media.process": ..., "file.purge": ...}
MediaProcessor.purge(job, files: FileService)   # the file.purge handler: files.purge, or the delayed sweep
RemoteSandboxRunner(env, *, socket_path=Path("/spool/sandboxd.sock"), spool_root=Path("/spool"),
                    queue_wait_s=1800.0 (QUEUE_WAIT_S), grace_s=5.0 (GRACE_S),
                    telemetry: Telemetry | None = None)   # aghoo_sandbox_queue_seconds{kind}
    # run() raises SandboxUnavailable (remote.py) when the connect fails: nothing was dispatched
Sandboxd(runner, *, spool_root, allowed_uid, read_timeout_s=5.0 (READ_TIMEOUT_S))
NsjailRunner(env, *, spool_root, nsjail="/usr/local/bin/nsjail", policy=None ($NOEMA_SANDBOX_POLICY),
             python="/opt/noema-sandbox/venv/bin/python", slots=2, reserved_short=1, slot_base_uid=100000,
             owner_uid=10001, owner_gid=10002)
NsjailRunner.execute(job_dir, kind, params_json, limits, *, argv=None, on_start=None)   # sandboxd's raw run
BwrapRunner(env, *, spool_root, bwrap="bwrap", python=None (sys.executable))
InprocRlimitRunner(env, *, spool_root, python=None (sys.executable))
```
Every runner constructor calls `assert_backend_allowed(backend, env)` and raises `ForbiddenInEnvironment` outside its
ADR-002 §7 cell (inproc: test; bwrap: test, dev; remote and nsjail: all). With `process="media-worker"`, staging/prod
require `remote`; sandboxd accepts only `nsjail`. `COMPONENT_KIND` maps backends to `composition.check_component`
kinds (`sandbox.remote`, `sandbox.nsjail`, `sandbox.bwrap`, `sandbox.inproc_rlimit`).

`DerivativeRef.active_content` (core, C2b; request item 2): `derivative_ref(row)` maps the `pdf_sanitized` row's
`facts.active_content` (revision 0002 column `noema.derivative.facts`) to a sorted tuple, `()` when nothing was
removed, None for every other kind. The interim `MediaFileService.derivative_facts` reader is gone. `.store` and
`.events` are shared with the worker.

## Wiring in `composition.py` (C2)
- **api** (and every process that reads files: jobs for session.delete/retention, decision/turn code paths):
  `files = MediaFileService.from_services(services)` once db, jobs, s3, redis, keys, outbox and telemetry exist (all
  are build-time objects or holders; nothing here is awaited at construction). Mount `noema.api.routes.files.router`
  (already listed). `ingest_inline` runs in the api process: it enqueues `media.process` at priority 10 and waits on
  the file events, so a media-worker must be running.
- **media-worker**: `handlers = make_handlers(services)`, merged with `merge_handlers`, run with `--kinds
  media.process,file.purge` (request item 5). The runner comes from `services.settings` (C2a, request item 4):
  `sandbox_runner(settings)` builds the `NOEMA_SANDBOX_BACKEND` backend (`remote` on `NOEMA_SANDBOX_SOCKET`; `bwrap`
  dev/test; `inproc_rlimit` test) on `NOEMA_SPOOL_DIR`, after `assert_backend_allowed(backend, env,
  process="media-worker")`, and passes `services.telemetry` to the remote runner. So the c2b brief's A8 needs only
  `make_handlers(services)`; an explicit `runner=`/`spool_root=` still wins. `media.process` also carries the
  watchdog checks (`payload.watchdog`) and `file.purge` the delayed sweeps (`payload.sweep`), so only the
  media-worker may claim either kind. The media-worker and media-sandbox must run the same release (the sandboxd IPC
  has a `started` line, request item 13, and a `bad_request` may carry job id `""`).
- **orchestrator (`active_content_removed`, spec-09 §6.3 step 5)**: nothing to wire. Preflight reads
  `DerivativeRef.active_content` of each current-turn PDF from `FileService.get` and emits the one degradation on
  `preflight.completed` (C2b).
- **media-sandbox**: the image CMD `python -I -m noema.media.sandbox.sandboxd` (options `--socket`, `--spool`,
  `--allowed-uid` (10001), `--slots` (2)); it reads `NOEMA_ENV` (default `prod`) and `NOEMA_SANDBOX_POLICY`, refuses to
  start (exit 2) with `AWS_*`, `NOEMA_DB_*` or `*_SECRET*` variables, listens on `/spool/sandboxd.sock` (0666;
  `SO_PEERCRED` decides) and sweeps orphaned job dirs hourly.

## Config and settings read
- `platform.media.inline_wait_ms` (default 3000): the inline wait is `min(deadline, now + inline_wait_ms)`.
- `platform.data_classes.<class>.retain_days`: `file.expires_at` = now + retain days of the session's data class, else
  the project's `default_data_class` (spec-03 §5).
- `services.settings.env` (SSE args, backend gate), `settings.s3_bucket`, `settings.sandbox_backend`,
  `settings.spool_dir`, `settings.sandbox_socket` (NOEMA_SANDBOX_BACKEND / NOEMA_SPOOL_DIR / NOEMA_SANDBOX_SOCKET).
- Jail env (fixed, ADR-002 §3): `LANG`, `HOME=/tmp`, `OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`, `OMP_THREAD_LIMIT=1`,
  `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `DOCLING_ARTIFACTS_PATH=/opt/docling-models`,
  `TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata`.

## Behaviour summary
- **Upload** (`upload`): 415 pre-check (declared type and extension in some row) → session visibility (404
  `session_not_found`) → streamed quarantine put with the extension row's cap (413 `file_too_large` mid-stream; the
  multipart upload is aborted) and a streaming sha256 → the file's watchdog (below) → `file` row `quarantined` →
  `media.process` (priority 100, `dedupe_key = file_id`, `max_attempts = 2`) → `quarantined` event. A failed INSERT
  or enqueue removes what was stored.
- **Route**: everything but the file part's data (boundaries, part headers, fields, unknown parts, leading blank lines)
  is capped at 64 KiB (`MAX_FORM_OVERHEAD_BYTES`, 413 `request_too_large`), so the body is bounded by the row cap
  plus 64 KiB. The extension comes from the full decoded name; `filename_display` is cut to 255 characters keeping
  the suffix. `session_id` must precede the `file` part (authorized as it arrives); `purpose` must be `input`; with an
  `Idempotency-Key` the fingerprint is sha256(content sha256, part Content-Type, filename, session_id, purpose,
  Aghoo-Version) and the key is claimed when the body has been read (a replay returns the stored 202; nothing is
  stored twice); the 202 is marked for storage (`mark_model_dispatched`).
- **media.process**: `detect` (Magika + libmagic + UTF-8/NUL facts; polyglot signal → `type_mismatch`, the hidden-PDF
  signal only for content that is not strict UTF-8 without NUL; Markdown accepts any Magika `is_text` label) → match →
  image: `image.normalize` → `ocr(raster)` (inline images: `detect` ∥ `image.normalize` in the declared format, the
  result stored only after `detect` matched; the two cold jail starts overlap, IMPL-074); PDF: `pdf.sanitize`
  (`derivative.facts = {"active_content": [...]}` on the `pdf_sanitized` row, `file.pages`) → `pdf.render` ∥
  `pdf.markdown` → `ocr(pdf_sanitized, pages without a text layer)`; Markdown: `markdown.sanitize`. Derivative rows are
  created `pending` before each job; outputs are stored first, then rows and the re-evaluated file status commit in one
  unit of work and the status is published. A required job's rejection rejects the file (pending rows fail with the
  code); an optional job's failure leaves the file `partial` (an `ok` OCR without its output: `ocr_failed`). Retries
  (attempt 2) skip finished derivatives; the last attempt settles the file `failed{extraction_failed}` on an
  unexpected error. sandboxd unreachable (`SandboxUnavailable`: the connect failed, nothing was dispatched) is only
  retried, never settled: after the last attempt the watchdog re-queues the file. Inline images whose original long edge
  exceeds 8,000 px are rejected `pixel_limit` with no derivative stored. A job that triggers OCR (`image.normalize`,
  `pdf.render` with scanned pages) creates `ocr_text` `pending` in its own commit, so the file goes `partial` and is
  never `ready` before OCR runs.
- **Watchdog** (C1 review: files stuck non-terminal): before the file INSERT, `arm_watchdog` enqueues `media.process`
  `{file_id, watchdog: 1}` (priority 300, `run_after = now + WATCHDOG_DELAY_S` (7200 s), dedupe
  `<file_id>:watchdog:<round>`). The check does nothing for a terminal or purged file. Otherwise it re-enqueues the
  run (`dedupe_key = file_id`: a no-op while the run is still queued or running) and arms the next round; round
  `WATCHDOG_ROUNDS` (3) settles the file `failed{extraction_failed}` (ERROR log). This covers a crash between the
  INSERT and the enqueue, a worker killed (OOM, SIGKILL) on its last attempt (the reaper makes the job dead without
  `_give_up`) and a sandboxd outage.
- **Deleted while processing**: every status write starts with `UPDATE file ... RETURNING` on the file row, which
  takes the row lock. If the row is deleted (`deleted_at` set) or purged, the unit of work rolls back, so no status is
  ever written onto a deleted row. The run then stops (both PDF siblings) and removes every derivative object of the
  file. A re-delivery that finds the row gone does the same.
- **Spool**: each sandbox job gets its own `sbx_<ULID>` dir, removed as soon as its outputs are stored (ADR-002 §6).
  The inputs later jobs read (original, pdf_sanitized, raster) stay in one keep dir per run. They are hard-linked
  into the job dirs, released after their last reader, and the keep dir goes at the end of the run. Output reads,
  job-dir removal and manifest hashing run in worker threads (`anyio.to_thread`).
- **Output budget**: `limits.OUTPUT_BUDGET_BYTES` caps the sum of one job's outputs (`pdf.render` 512 MiB; the others
  `max_outputs × fsize`). The extractor's writer raises `OutputBudgetExceeded` before passing it, so the job fails
  (`page_images` `failed{extraction_failed}`, file `partial`, no OCR). The validator refuses a manifest over it
  (`manifest_invalid`).
- **Sandbox queueing** (sandboxd, `NsjailRunner`): there are 2 slots, and the long kinds (`pdf.sanitize`,
  `pdf.render`, `pdf.markdown`, `ocr`) may use at most `slots - reserved_short` = 1. So `detect`, `image.normalize`
  and `markdown.sanitize` always get a slot within one short job. `pdf.markdown` takes the Docling lock before a slot,
  and waiters are served FIFO. sandboxd checks `SO_PEERCRED` right after accept (a refused peer gets `bad_request` with
  job id `""`, nothing of it is read) and wants the request line within `READ_TIMEOUT_S` (5 s; else `bad_request`), so
  no peer holds a handler. It sends a `started` line when the jail starts. `RemoteSandboxRunner` waits up to
  `queue_wait_s` for it (`failed{busy}` past it), then `wall_s + 5` for the response. A client that goes away cancels
  its job in sandboxd (a queued job never runs, a running jail is killed). Manifests and IPC lines nested deeper than
  the parser's stack are `manifest_invalid` / `bad_request` (RecursionError is caught).
- **watch**: subscribe (Redis `RedisKeys.file_events(file_id)`, payload `{tenant_id, file_id, status}`, foreign tenants
  dropped with a `pubsub_tenant_mismatch` event) → current state → each change, 2 s poll fallback, until `is_terminal`
  (`ready`, `rejected`, `failed`, `deleted`, or `partial` with nothing pending). `LocalFileEvents` (also the local
  half of `RedisFileEvents`) keeps state only while a file has a subscriber, so processes do not grow per file.
  Wave E: every watcher of a process shares ONE pubsub connection (before, one per watcher: ~100 watchFile streams
  of one tenant drained the process's 100-connection Redis pool and pushed every tenant on it into Redis-degraded
  mode). A channel is subscribed while its file has a watcher, the connection is open while any channel is, and a
  waiting watcher reads it for all of them (`RedisFileEvents.pump`, one reader at a time; the next waiting watcher
  takes over when its wait ends), dispatching to the local fan-out after the tenant check. Still unbounded: the
  number of watchFile streams per project and their 2 s row re-read (`.orchestration/requests/io-fixer.md` item 3).
- **Downloads**: `open_derivative` raises 404 (`raster_1568`, unknown kind/page, failed derivative), 409
  `file_not_ready` (pending, or not created yet while processing), 409 `file_rejected`; the route adds
  `Content-Disposition: attachment; filename="<file_id>-<kind>[-<page>].<ext>"` and `X-Content-Type-Options: nosniff`;
  page rules: required for `page_images` (≤ pages), rejected elsewhere (400 `schema_violation`, param `page`).
- **delete / purge**: `delete` marks the row deleted (reads stop at once) and enqueues `file.purge` (priority 300,
  dedupe `file_id`). `purge(tenant, id)` (also called directly by retention and session.delete) does four things. It
  marks a live row deleted first, so a running `media.process` stops at its next write. It deletes the quarantine
  object and the derivative prefix. If a derivative was still `pending`, it enqueues a sweep: `file.purge` with payload
  `{file_id, sweep: true}`, `run_after = now + SWEEP_DELAY_S` (3600 s) and dedupe `<file_id>:sweep`. Finally it deletes
  the row (derivatives cascade). Idempotent and tenant-scoped. The sweep (`MediaProcessor.purge`) removes the
  derivative prefix once the row is gone. It catches an object a run stored after the purge and then crashed before
  its next write.

## Metrics, spans, logs, events
- Span `media.extract` per sandbox job (`aghoo.tenant`, `aghoo.media.kind`, `aghoo.media.status`; `error.type` =
  `file_rejected` or `internal_error` for non-ok).
- `aghoo_media_jobs_total{kind,status}` and `aghoo_media_job_seconds{kind}` (wall) per sandbox job (worker);
  `aghoo_sandbox_queue_seconds{kind}` (request sent → `started` line, `RemoteSandboxRunner`; a `busy` timeout
  observes the full wait); `aghoo_inline_ingest_seconds` (`ingest_inline` after its 415/413 checks, every outcome;
  the IMPL-074 p95 < 400 ms budget).
- `aghoo_sandbox_violations_total{kind=<job kind>}` for `sandbox.violation`, `sandbox.manifest_invalid`,
  `sandbox.sanitize_incomplete`; `aghoo_security_violations_total{kind="pubsub_tenant_mismatch"}`. Each security event is
  also a WARNING on `noema.media.security` (`event=security`, name, tenant, job id, kind; content-free) and an
  `audit_event` outbox record (`actor_type=system`, `actor_id=system:media`, `action=security.<name>`).
- Logs: `noema.media.worker` (INFO per sandbox job: kind, status, wall_ms; INFO `discarded N objects of deleted
  file_id=...`; ERROR on `bad_request`), `noema.media.sandbox` (remote client; WARNING `sandboxd had no slot within
  ...`), `noema.media.sandboxd` (INFO `client went away, job cancelled`), `noema.media` (file events). sandboxd's stderr tail is
  logged at DEBUG only. Worker WARNING `sandboxd is unreachable ... retried later`, WARNING `watchdog re-queues`,
  ERROR `watchdog settles ... as failed`. The ADR-002 §6 step 2 reserved-tag pre-scan of text outputs
  (`manifest.reserved_tag_count` on `markdown`/`ocr_text`) is an INFO `reserved tags in a text output kind=...
  count=N` (content-free): no registry name exists for a counter.

## Error codes raised
`unsupported_media_type` (415), `file_too_large` (413), `image_too_large` (413; inline arithmetic or long edge >
8,000), `session_not_found` (404), `not_found` (404), `file_not_ready` (409; inline wait expired, derivative pending),
`file_rejected` (409; `details.rejection {code, message}` or `details.failure {code, message, retryable}`),
`schema_violation` (400; multipart, `purpose`, `session_id` order, `page`), `request_too_large` (413; more than 64 KiB
of multipart outside the file data), `permission_denied` (403, through
`Authorizer.check`), `idempotency_key_reused` (409, through the hook). Rejection codes: `type_mismatch`,
`not_allowlisted`, `empty_content`, `pixel_limit`, `decompression_bomb`, `corrupt`, `encrypted_pdf`, `too_many_pages`;
failure codes: `extraction_failed`, `ocr_failed`, `timeout`. Sandbox result reasons (`ExtractResult.facts.error`, not on
the wire): `manifest_invalid`, `exit`, `bad_request`, `unavailable`, `busy` (no sandboxd slot within `queue_wait_s`),
`timeout`, `violation`; all but `timeout` map to `extraction_failed` (`ocr`: `ocr_failed`).

## Tests (commands and ids)
- Host: `make test MOD=media_pipeline` (281 passed: unit, integration on Postgres/moto/valkey, image-only skipped),
  `make lint MOD=media_pipeline`, `make typecheck MOD=media_pipeline`, `make importlint`.
- Media image: `make test-media` (default paths: both media_pipeline dirs and tests/integration/infra/media_image;
  nsjail, sandboxd, tesseract, Docling, qpdf; the unit tests run there too; 295 passed, the network tests skipped).
- Conformance: `tests/integration/media_pipeline/conformance_impls.py` registers `file_service/media-pipeline`,
  `sandbox_runner/inproc_rlimit`, `sandbox_runner/bwrap` (`make test-conformance`).
- Ids: MED-01 (`test_intake`, `test_detect`), MED-02 (`test_media_image::test_med_02_*`, image), MED-03
  (`test_extractors`), MED-04 (`test_extractors` per §6.3 row + chain + field `/AA` + incremental update + encrypted +
  601 pages; `qpdf --check` in the image), MED-05 (`test_md_render`, hypothesis + corpus), MED-06 (`test_inline`),
  MED-07 (`test_status_machine`), SEC-SBX-02/04/05/06 (`test_media_image`, image; SEC-SBX-04 also `test_manifest`,
  `test_sandbox_units`), SEC-SBX-03 corpus (bombs, truncated, polyglots, zip bomb, 2 GB stream across the tests),
  SEC-SBX-07 (`test_sandbox_units`), SEC-SBX-08 harness (`fuzz_extractors.py`, smoke in the image), SEC-EDGE-01 upload
  half (`test_file_service`, `test_routes`, `test_pg_s3_redis`).
- C1 review regressions: `test_delete_race` (delete and purge during processing, image and PDF siblings; a retention
  purge of a live row; the delayed sweep; a re-delivery after the purge) plus
  `test_pg_s3_redis::test_delete_during_processing_on_postgres` (the row lock on real Postgres).
  `test_sandboxd_queueing` covers the long-lane slot reservation, Docling queueing without a slot, the wall bound
  starting at jail start, `busy` with the dropped queued job, and a killed jail after a client disconnect.
  `test_spool_budget` covers the writer and validator budgets, a render over budget, per-job dir removal with
  hard-linked inputs, and the thread offload. `test_status_machine::test_no_transient_ready_before_ocr` checks that
  `ready` is not published before OCR.
- C2a (the 13 C1 review minors and the interface changes): `test_watchdog` (lost job re-queued, live run not
  duplicated, sandbox outage recovered, last-round settle, PDF sibling outage unwrapped, purged file, bad payload)
  and `test_pg_s3_redis::test_the_watchdog_requeues_a_lost_job_on_postgres`; `test_file_events` (bounded
  `LocalFileEvents`); `test_manifest[deep_nesting]` and `test_sandbox_units` (RecursionError); `test_intake`
  (Magika text labels, long names keep their extension, the label constant equals the pinned model's `is_text`
  set); `test_detect` (a note quoting `%PDF-1.7`, a tiny note labelled `gitattributes`); `test_routes`
  (`test_unknown_parts_and_blank_lines_are_bounded`); `test_remote_and_daemon` (connect failure raises, a silent
  peer is answered after the read timeout, the queue histogram); `test_inline` (deadline before `inline_wait_ms`,
  detect ∥ normalize, no speculative output stored, the ingest histogram); `test_status_machine` (`ok` OCR without
  output → `ocr_failed`); `test_md_render` (manual GC output equality; a 1 MiB table's CPU time and peak RSS,
  measured in a fresh `python -I` child, within the `markdown.sanitize` limits); `test_wiring` (runner from the
  sandbox settings, job metrics, span attributes, reserved-tag pre-scan); `test_file_service` and `test_pg_s3_redis` (`derivative.facts` on the row, real CHECK).
- Fixtures: `/codes/aghooai/probes/fixtures` (number.png → OCR "4471", invoice.pdf) plus generated ones in
  `tests/unit/media_pipeline/media_fixtures.py` (HEIC, EXIF JPEG, animated GIF/WebP, pixel/zTXt/canvas bombs, truncated
  files, one PDF per spec-09 §6.3 row, encrypted, 601 pages, PDF+ZIP, PNG+PDF, zip bomb, a shared-noise-image PDF).

## Known limits
- Interim homes awaiting frozen changes: HEIC Magika label `mp4` (item 1, spec now agrees). Item 2 is closed (C2b):
  the facts are on the derivative row and reach callers as `DerivativeRef.active_content`.
- Per-kind limits are the ADR-002 §5 initial values except `markdown.sanitize`, tuned in C2a with evidence (C1
  review): 15 s wall / 10 s CPU / 512 MiB failed a legitimate 1 MiB pipe table (~15 s). markdown-it-py needs about
  250 MiB of tree per MiB of input; the sanitizer now holds one tree at a time and pauses the cyclic GC per pass
  (`manual_gc`), measured on this host through `InprocRlimitRunner`: 5 MiB prose 30.4 s wall in 1,536 MiB AS, a 5 MiB
  pipe table 23.0 s, 1 MiB prose 6.2 s in 512 MiB (before: 8.6 s CPU / 507 MiB RSS for 1 MiB, ~42 s / 2.5 GiB for
  5 MiB). New values: wall 90 s, CPU 60 s, 2,048 MiB (ADR-002 §5's table row still says 15/10/512: errata needed).
  The pathological bracket input of the review (~77 s per MiB) still ends `failed{timeout}`. A 90 s Markdown job holds
  a short-lane slot, so two at once delay inline images. Other evidence: Docling converts invoice.pdf within
  `pdf.markdown` limits; `detect` needs > 512 MiB address space (Magika/onnxruntime; children are pinned to one CPU so
  thread pools stay small). A scanned 600-page PDF may exceed the `ocr` limits (→ `ocr_text` `failed{timeout}`, file
  `partial`); page chunking is not implemented.
- Host backends: `BwrapRunner` has no seccomp policy and no tesseract on this host (`ocr` → `failed`, extractor
  `unavailable`); `pdf.markdown` needs the baked Docling models (media image).
- The inline latency budget (p95 < 400 ms): two cold `python -I` jail children cost ~200 ms each on this host (C1
  review). `detect` ∥ `image.normalize` now overlaps them, so the sandbox part is about one child, not two; with S3,
  the DB, the queue claim, NOTIFY and nsjail it is still unmeasured. C2 perf smoke: read `aghoo_inline_ingest_seconds`
  and `aghoo_sandbox_queue_seconds{kind="detect"}`; if it is over budget, the next step is pre-warmed jail workers.
- Spool bound: the output budget binds well-behaved extractors. A compromised jail can still fill the shared tmpfs
  for at most its `wall_s` (RLIMIT_FSIZE is per file). The hard per-slot quota is request item 14.
- Throughput: with 2 slots only one long job runs at a time, so `pdf.render` and `pdf.markdown` of one PDF run one
  after the other (the price of the reserved short slot; raise `--slots` with the container's `cpus`). A job that
  waits over `QUEUE_WAIT_S` (1800 s) for a slot fails `busy` → `extraction_failed`, which is load shedding under a
  sustained backlog. A PDF whose page renders exceed 512 MiB gets no `page_images` and therefore no OCR.
- If a run crashes between storing an object and its next row write, and the purge ran in that window, the object
  is removed by the re-delivery (attempt 2) or by the delayed sweep (`SWEEP_DELAY_S`, 1 h), whichever comes first.
- Polyglot rule for text: a strict UTF-8 note that quotes `%PDF-` within its first ~256 bytes is still refused
  `type_mismatch`, because libmagic itself then says `application/pdf` (the four-signal rule). Further in, it is
  accepted (the hidden-PDF signal skips text).
- Watchdog: a run still legitimately queued or running at the last round (6 h after the upload, e.g. a huge backlog)
  is settled `failed`, and that run's later writes can move the file on (`failed` → `ready`). An orphaned quarantine
  object of a crash between the put and the watchdog enqueue (no row yet) is not swept.
