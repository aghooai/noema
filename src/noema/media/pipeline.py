"""Files: the status machine (spec-07 §1.1), the row → `FileRef` → `FileObject` mappings (§3.3), file events
(§3.1 step 7), the per-pod derivative cache, security events, and `MediaFileService`, the `FileService`
implementation (upload, ingest_inline, get, delete, purge, watch, open_derivative, read_derivative).

No media library is imported here: the API process never parses media (ADR-002 §1).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol, cast

import anyio
from redis.exceptions import RedisError

from noema.core.clock import Clock, Deadline, SystemClock, deadline_scope
from noema.core.errors import ErrorCode, NoemaError
from noema.core.ids import IdGen
from noema.core.principal import Principal
from noema.core.protocols.auth import ResourceRef
from noema.core.protocols.config import ConfigRegistry
from noema.core.protocols.media import DerivativeKind, DerivativeRef, FileRef, FileStatus
from noema.core.protocols.services import ServiceSet
from noema.core.protocols.storage import Database, JobQueue, Outbox, OutboxRecord, Row, UnitOfWork
from noema.core.protocols.telemetry import Telemetry
from noema.core.redis_keys import RedisKeys
from noema.core.values import DataClass, JsonValue
from noema.media.intake import (
    MAX_INLINE_LONG_EDGE,
    TYPE_ROWS,
    MediaKind,
    TypeRow,
    check_inline_size,
    decode_inline,
    display_filename,
    failure_object,
    inline_row,
    normalize_declared_type,
    precheck_upload,
    rejected_error,
)
from noema.media.quarantine import ObjectStore, derivative_key, derivative_prefix, quarantine_key

REQUIRED: Final[Mapping[MediaKind, tuple[DerivativeKind, ...]]] = {
    "image": ("raster", "raster_1568"),
    "pdf": ("pdf_sanitized", "markdown"),
    "markdown": ("markdown",),
}
EXPECTED: Final[Mapping[MediaKind, tuple[DerivativeKind, ...]]] = {  # every kind the pipeline may produce
    "image": ("raster", "raster_1568", "thumbnail", "ocr_text"),
    "pdf": ("pdf_sanitized", "markdown", "page_images", "ocr_text"),
    "markdown": ("markdown",),
}
TERMINAL: Final[frozenset[FileStatus]] = frozenset({"ready", "rejected", "failed", "deleted"})
# openapi DerivativeKind order; raster_1568 is internal and never listed (spec-07 §3.3, IMPL-065)
WIRE_ORDER: Final = ("text", "markdown", "page_images", "pdf_sanitized", "raster", "thumbnail", "ocr_text")
CONTENT_TYPES: Final[Mapping[str, str]] = {
    "raster": "image/png",
    "thumbnail": "image/png",
    "page_images": "image/png",
    "pdf_sanitized": "application/pdf",
    "markdown": "text/markdown; charset=utf-8",
    "ocr_text": "text/plain; charset=utf-8",
    "text": "text/plain; charset=utf-8",
}
DOWNLOAD_EXT: Final[Mapping[str, str]] = {
    "raster": "png",
    "thumbnail": "png",
    "page_images": "png",
    "pdf_sanitized": "pdf",
    "markdown": "md",
    "ocr_text": "txt",
    "text": "txt",
}
_BY_DECLARED: Final[Mapping[str, TypeRow]] = {t: r for r in TYPE_ROWS for t in r.declared if t != "text/plain"}


def row_for_type(media_type: str | None) -> TypeRow | None:
    """The equivalence-map row of a (declared or detected) media type; `text/plain` counts as Markdown."""
    if media_type is None:
        return None
    t = normalize_declared_type(media_type)
    return _BY_DECLARED.get(t) or (_BY_DECLARED["text/markdown"] if t == "text/plain" else None)


def media_kind(ref: FileRef) -> MediaKind | None:
    """image, pdf or markdown, from the detected type (else the declared type)."""
    row = row_for_type(ref.media_type or ref.declared_type)
    return None if row is None else row.kind


@dataclass(frozen=True, slots=True)
class StatusUpdate:
    """The re-evaluated file status and, for `failed`, the failure code."""

    status: FileStatus
    failure_code: str | None = None


def evaluate_status(kind: MediaKind, derivatives: Mapping[str, tuple[str, str | None]]) -> StatusUpdate:
    """spec-07 §1.1 after a job result, from `{derivative kind: (status, error_code)}`: a failed required
    derivative → `failed{code}`; every required one ready → `ready` if nothing else is pending or failed, else
    `partial`; otherwise `processing`. Rejections are set directly by the worker."""
    required = REQUIRED[kind]
    for k in required:
        status, code = derivatives.get(k, ("pending", None))
        if status == "failed":
            return StatusUpdate("failed", code or "extraction_failed")
    if all(derivatives.get(k, ("pending", None))[0] == "ready" for k in required):
        others = [s for k, (s, _) in derivatives.items() if k not in required]
        return StatusUpdate("ready" if all(s == "ready" for s in others) else "partial")
    return StatusUpdate("processing")


def is_terminal(ref: FileRef) -> bool:
    """watchFile/wait_for_files terminal: ready, rejected, failed, deleted, or partial with nothing pending."""
    if ref.status in TERMINAL:
        return True
    return ref.status == "partial" and not any(d.status == "pending" for d in ref.derivatives)


def _opt_str(row: Row, key: str) -> str | None:
    value = row.get(key)
    return None if value is None else str(value)


def _opt_int(row: Row, key: str) -> int | None:
    value = row.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_obj(row: Row, key: str) -> Mapping[str, JsonValue] | None:
    value = row.get(key)
    return cast(Mapping[str, JsonValue], value) if isinstance(value, Mapping) else None


def _pages(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, list | tuple):
        return None
    return tuple(int(p) for p in value if isinstance(p, int))


def _active_content(row: Row) -> tuple[str, ...] | None:
    """`derivative.facts.active_content` of a pdf_sanitized row (spec-09 §6.3 step 5), sorted; None otherwise."""
    facts = _opt_obj(row, "facts")
    if row.get("kind") != "pdf_sanitized" or facts is None:
        return None
    values = facts.get("active_content")
    return tuple(sorted({str(v) for v in values})) if isinstance(values, list) else None


def derivative_ref(row: Row) -> DerivativeRef:
    """A derivative row as a `DerivativeRef`."""
    return DerivativeRef(
        kind=cast(DerivativeKind, row["kind"]),
        id=str(row["id"]),
        status=cast(Literal["pending", "ready", "failed"], row["status"]),
        sha256=_opt_str(row, "sha256"),
        bytes=_opt_int(row, "bytes"),
        width=_opt_int(row, "width"),
        height=_opt_int(row, "height"),
        pages=_opt_int(row, "pages"),
        text_layer_pages=_pages(row.get("text_layer_pages")),
        active_content=_active_content(row),
    )


def file_ref(row: Row, derivative_rows: Sequence[Row]) -> FileRef:
    """A file row and its derivative rows as a `FileRef`."""
    return FileRef(
        file_id=str(row["id"]),
        sha256=str(row["sha256"]),
        declared_type=str(row["declared_type"]),
        media_type=_opt_str(row, "media_type"),
        status=cast(FileStatus, row["status"]),
        bytes=_opt_int(row, "bytes") or 0,
        origin=cast(Literal["upload", "inline"], row["origin"]),
        principal_id=str(row["principal_id"]),
        session_id=_opt_str(row, "session_id"),
        filename=_opt_str(row, "filename_display"),
        derivatives=tuple(derivative_ref(d) for d in derivative_rows),
        pages=_opt_int(row, "pages"),
        rejection=_opt_obj(row, "rejection"),
        failure=_opt_obj(row, "failure"),
    )


def display_name(ref: FileRef) -> str:
    """`FileObject.filename`: the display name, or `"{file_id}.{ext}"` for inline files (spec-07 §3.3)."""
    if ref.filename:
        return ref.filename
    row = row_for_type(ref.declared_type)
    ext = (row.inline_ext if row is not None else None) or "bin"
    return f"{ref.file_id}.{ext}"


def file_object(ref: FileRef) -> dict[str, JsonValue]:
    """The openapi `FileObject` of a file (spec-07 §3.3): derivatives without `raster_1568`, in DerivativeKind
    order, `pages` on `page_images` only."""
    order = {k: i for i, k in enumerate(WIRE_ORDER)}
    derivatives: list[JsonValue] = []
    for d in sorted((d for d in ref.derivatives if d.kind in order), key=lambda d: order[d.kind]):
        item: dict[str, JsonValue] = {"kind": d.kind, "id": d.id, "status": d.status}
        if d.kind == "page_images" and d.pages:
            item["pages"] = d.pages
        derivatives.append(item)
    obj: dict[str, JsonValue] = {
        "id": ref.file_id,
        "filename": display_name(ref),
        "media_type": ref.media_type or ref.declared_type,
        "bytes": ref.bytes,
        "sha256": ref.sha256,
        "status": ref.status,
        "derivatives": derivatives,
    }
    if ref.rejection is not None:
        obj["rejection"] = dict(ref.rejection)
    if ref.failure is not None:
        obj["failure"] = dict(ref.failure)
    return obj


def download_filename(file_id: str, kind: str, page: int | None) -> str:
    """`<file_id>-<kind>[-<page>].<ext>`, never the uploaded name (IMPL-065)."""
    suffix = f"-{page}" if page is not None else ""
    return f"{file_id}-{kind}{suffix}.{DOWNLOAD_EXT.get(kind, 'bin')}"


def snapshot_key(ref: FileRef) -> tuple[object, ...]:
    """What a watcher compares to decide whether a re-read is a new `file.updated` frame."""
    return (ref.status, ref.media_type, ref.pages, tuple((d.kind, d.status, d.pages) for d in ref.derivatives))


# --- security events (spec-09 §1) ------------------------------------------------------------------------------------
SecurityName = Literal[
    "sandbox.violation", "sandbox.manifest_invalid", "sandbox.sanitize_incomplete", "pubsub_tenant_mismatch"
]
SECURITY_ACTOR: Final = "system:media"
security_log = logging.getLogger("noema.media.security")
log = logging.getLogger("noema.media")


class MediaSecurityEvents:
    """One content-free log record, a counter (`aghoo_sandbox_violations_total{kind=<job kind>}` for `sandbox.*`,
    else `aghoo_security_violations_total{kind=<name>}`) and an `audit_event` row through the outbox."""

    def __init__(self, *, telemetry: Telemetry | None, outbox: Outbox | None, ids: IdGen, clock: Clock) -> None:
        self._telemetry = telemetry
        self._outbox = outbox
        self._ids = ids
        self._clock = clock

    def emit(self, name: SecurityName, *, tenant_id: str, job_id: str | None = None, kind: str | None = None) -> None:
        """Log, count and audit one event (ids and the job kind only, never content)."""
        security_log.warning(
            "security name=%s tenant_id=%s job_id=%s kind=%s",
            name,
            tenant_id,
            job_id,
            kind,
            extra={"event": "security", "code": name, "tenant": tenant_id},
        )
        if self._telemetry is not None:
            if name.startswith("sandbox."):
                self._telemetry.instruments.add("aghoo_sandbox_violations_total", kind=kind or "unknown")
            else:
                self._telemetry.instruments.add("aghoo_security_violations_total", kind=name)
        if self._outbox is None:
            return
        audit_id = self._ids.new("aud")
        details: dict[str, JsonValue] = {"job_kind": kind} if kind else {}
        payload: dict[str, JsonValue] = {
            "tenant_id": tenant_id,
            "id": audit_id,
            "actor_type": "system",
            "actor_id": SECURITY_ACTOR,
            "action": f"security.{name}",
            "resource_kind": "file",
            "resource_id": job_id,
            "details": details,
            "request_id": None,
            "at": self._clock.now().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        self._outbox.put(OutboxRecord(kind="audit_event", id=audit_id, tenant_id=tenant_id, payload=payload))


# --- the per-pod derivative cache (spec-02 §6 read_derivative) -------------------------------------------------------
CACHE_BYTES: Final = 256 * 1024 * 1024


class DerivativeCache:
    """An LRU of derivative bytes keyed by (tenant_id, sha256, page), bounded by total bytes; entries larger than a
    quarter of the bound are not cached."""

    def __init__(self, max_bytes: int = CACHE_BYTES) -> None:
        self._max = max_bytes
        self._items: OrderedDict[tuple[str, str, int | None], bytes] = OrderedDict()
        self._size = 0

    def get(self, key: tuple[str, str, int | None]) -> bytes | None:
        """The cached bytes (refreshing their recency), or None."""
        data = self._items.get(key)
        if data is not None:
            self._items.move_to_end(key)
        return data

    def put(self, key: tuple[str, str, int | None], data: bytes) -> None:
        """Cache `data`, evicting the least recently used entries beyond the bound."""
        if len(data) > self._max // 4 or key in self._items:
            return
        self._items[key] = data
        self._size += len(data)
        while self._size > self._max:
            _, old = self._items.popitem(last=False)
            self._size -= len(old)

    @property
    def size(self) -> int:
        """Cached bytes."""
        return self._size


class _Call:
    __slots__ = ("done", "error", "ok", "value")

    def __init__(self) -> None:
        self.done = anyio.Event()
        self.ok = False
        self.value: Any = None
        self.error: Exception | None = None


class SingleFlight:
    """Concurrent calls with the same key share one in-flight call; nothing is kept once it ends (Wave E: every
    member of a media decision resolves its view at once, and each derivative key re-read the same file row and,
    on a cold LRU, GOT the same object again). A cancelled leader is not shared: its followers try again."""

    def __init__(self) -> None:
        self._calls: dict[Any, _Call] = {}

    async def run(self, key: Any, fn: Callable[[], Awaitable[Any]]) -> Any:
        while (call := self._calls.get(key)) is not None:
            await call.done.wait()
            if call.ok:
                return call.value
            if call.error is not None:
                raise call.error
        call = self._calls[key] = _Call()
        try:
            call.value = await fn()
            call.ok = True
            return call.value
        except Exception as exc:
            call.error = exc
            raise
        finally:
            del self._calls[key]
            call.done.set()

    @property
    def in_flight(self) -> int:
        return len(self._calls)


# --- file events (spec-07 §3.1 step 7; RedisKeys.file_events, spec-09 §7) ---------------------------------------------
class FileEventWaiter(Protocol):
    async def wait(self) -> None:
        """Return at the next status change of the file (same tenant); cancellable."""
        ...

    @property
    def poll_s(self) -> float:
        """How long the watcher may wait before it re-reads the row anyway: `POLL_S`, or `WATCH_POLL_S` while
        its event channel is live (Wave E2)."""
        ...


class FileEvents(Protocol):
    """Status-change notifications of files. Receivers re-read the row, so a lost message only costs the 2 s
    poll."""

    async def publish(self, tenant_id: str, file_id: str, status: str) -> None:
        """Best effort: never raises."""
        ...

    def subscribe(self, tenant_id: str, file_id: str) -> AbstractAsyncContextManager[FileEventWaiter]:
        """Subscribe before reading the row, so no change between the read and the wait is missed."""
        ...


class _LocalWaiter:
    def __init__(self, events: LocalFileEvents, file_id: str) -> None:
        self._events = events
        self._file_id = file_id
        self._seen = events.generation(file_id)

    async def wait(self) -> None:
        while self._events.generation(self._file_id) == self._seen:
            await self._events.changed(self._file_id).wait()
        self._seen = self._events.generation(self._file_id)

    @property
    def poll_s(self) -> float:
        return POLL_S


class LocalFileEvents:
    """In-process notifications (one process: tests, or api and worker co-located in dev). State is kept only for
    files with a subscriber and dropped when the last one leaves, so a long-running process that publishes for every
    file it handles does not grow."""

    def __init__(self) -> None:
        self._gen: dict[str, int] = {}
        self._events: dict[str, anyio.Event] = {}
        self._subscribers: dict[str, int] = {}

    def generation(self, file_id: str) -> int:
        return self._gen.get(file_id, 0)

    def changed(self, file_id: str) -> anyio.Event:
        return self._events.setdefault(file_id, anyio.Event())

    async def publish(self, tenant_id: str, file_id: str, status: str) -> None:
        if file_id not in self._subscribers:
            return  # nobody listens: nothing to keep
        self._gen[file_id] = self.generation(file_id) + 1
        event = self._events.pop(file_id, None)
        if event is not None:
            event.set()

    @asynccontextmanager
    async def subscribe(self, tenant_id: str, file_id: str) -> AsyncIterator[FileEventWaiter]:
        self._subscribers[file_id] = self._subscribers.get(file_id, 0) + 1
        try:
            yield _LocalWaiter(self, file_id)
        finally:
            left = self._subscribers[file_id] - 1
            if left:
                self._subscribers[file_id] = left
            else:  # the last subscriber left (also a cancelled or timed-out waiter): drop the file's state
                del self._subscribers[file_id]
                self._gen.pop(file_id, None)
                self._events.pop(file_id, None)

    @property
    def tracked(self) -> int:
        """Files with state (subscribed ones only)."""
        return len(self._gen.keys() | self._events.keys() | self._subscribers.keys())


class _SharedWaiter:
    """A watcher on the process's shared file-events connection: woken through the local fan-out; while it waits it
    takes its turn reading the shared connection for every watcher of the process (one reader at a time)."""

    def __init__(self, events: RedisFileEvents, local: FileEventWaiter) -> None:
        self._events = events
        self._local = local

    async def wait(self) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._events.pump)
            await self._local.wait()
            tg.cancel_scope.cancel()

    @property
    def poll_s(self) -> float:
        return WATCH_POLL_S if self._events.live else POLL_S


class RedisFileEvents:
    """Notifications over `RedisKeys.file_events(file_id)` (payload `{tenant_id, file_id, status}`), plus local
    delivery for watchers in the same process. Every watcher of the process shares ONE pubsub connection (Wave E:
    one connection per watcher let ~100 watchFile streams of one tenant drain the process's Redis pool): channels
    are subscribed while they have a watcher, the connection is open while any channel is, and a waiting watcher
    reads it for all of them (`pump`), dispatching each message to the local fan-out after the tenant check.

    Wave E2: while read, a quiet connection is PINGed every `EVENTS_PING_S`; it is `live` until `EVENTS_DEAD_S`
    pass without any reply or a Redis error occurs (a hung proxy keeps TCP alive, so keepalives never notice).
    Then every watcher is woken to re-read at once and polls every `POLL_S` again (while live: `WATCH_POLL_S`);
    the connection is dropped so the next reader's turn reconnects (redis-py re-subscribes the channels), and the
    first reply makes it live again."""

    def __init__(
        self, redis: Any, keys: RedisKeys, *, security: MediaSecurityEvents | None = None, clock: Clock | None = None
    ) -> None:
        self._redis = redis
        self._keys = keys
        self._security = security
        self._clock = clock or SystemClock()
        self._local = LocalFileEvents()
        self._pubsub: Any = None
        self._channels: dict[str, tuple[str, dict[str, int]]] = {}  # channel -> (file_id, watchers per tenant)
        self._commands = anyio.Lock()  # (un)subscribe in order on the shared connection
        self._reader = anyio.Lock()  # one reader of the shared connection at a time
        self._live = False
        self._heard = self._pinged = 0.0

    @property
    def live(self) -> bool:
        """True while the shared connection is open and answering (see the class docstring)."""
        return self._pubsub is not None and self._live

    async def publish(self, tenant_id: str, file_id: str, status: str) -> None:
        await self._local.publish(tenant_id, file_id, status)
        payload = json.dumps({"tenant_id": tenant_id, "file_id": file_id, "status": status})
        try:
            await self._redis.publish(self._keys.file_events(file_id), payload)
        except (RedisError, OSError) as exc:
            log.warning("file event not published file_id=%s error=%s", file_id, type(exc).__name__)

    @asynccontextmanager
    async def subscribe(self, tenant_id: str, file_id: str) -> AsyncIterator[FileEventWaiter]:
        channel = self._keys.file_events(file_id)
        async with self._local.subscribe(tenant_id, file_id) as local:
            try:
                await self._join(channel, tenant_id, file_id)
            except (RedisError, OSError) as exc:
                log.warning("file events unavailable file_id=%s error=%s", file_id, type(exc).__name__)
                yield local
                return
            try:
                yield _SharedWaiter(self, local)
            finally:
                with anyio.CancelScope(shield=True):
                    await self._leave(channel, tenant_id)

    @property
    def channels(self) -> int:
        """Channels subscribed on the shared connection (files with a watcher)."""
        return len(self._channels)

    async def _join(self, channel: str, tenant_id: str, file_id: str) -> None:
        async with self._commands:
            entry = self._channels.get(channel)
            if entry is None:
                created = self._pubsub is None
                if created:
                    self._pubsub = self._redis.pubsub()
                    self._live, self._heard = True, self._clock.monotonic()
                    self._pinged = self._heard
                try:
                    await self._pubsub.subscribe(channel)
                except BaseException:
                    if created:
                        await self._close()
                    raise
                entry = self._channels[channel] = (file_id, {})
            entry[1][tenant_id] = entry[1].get(tenant_id, 0) + 1

    async def _leave(self, channel: str, tenant_id: str) -> None:
        async with self._commands:
            tenants = self._channels[channel][1]
            tenants[tenant_id] -= 1
            if tenants[tenant_id] == 0:
                del tenants[tenant_id]
            if tenants:
                return
            del self._channels[channel]
            if not self._channels:
                await self._close()
                return
            with contextlib.suppress(RedisError, OSError):
                await self._pubsub.unsubscribe(channel)

    async def _close(self) -> None:
        pubsub, self._pubsub = self._pubsub, None
        if pubsub is not None:
            with contextlib.suppress(RedisError, OSError):
                await pubsub.aclose()

    async def pump(self) -> None:
        """Read the shared connection for every watcher until cancelled (the reading watcher's wait ended; the next
        waiting watcher takes over). A Redis error parks the reader: the watchers' 2 s poll carries on, and the
        next reader's turn reconnects (redis-py re-subscribes the channels)."""
        async with self._reader:
            while True:
                pubsub = self._pubsub
                if pubsub is None:
                    await anyio.sleep_forever()
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    now = self._clock.monotonic()
                    if message is not None:
                        self._live, self._heard = True, now
                        if message.get("type") != "pong":
                            await self._dispatch(message)
                    elif now - self._heard >= EVENTS_DEAD_S:
                        raise ConnectionError(f"no reply for {now - self._heard:.1f} s")
                    if now - self._heard >= EVENTS_PING_S and now - self._pinged >= EVENTS_PING_S:
                        self._pinged = now
                        await pubsub.ping()
                except (RedisError, OSError) as exc:  # ConnectionError included
                    await self._lost(pubsub, exc)
                    await anyio.sleep_forever()

    async def _lost(self, pubsub: Any, exc: BaseException) -> None:
        """The shared connection failed or stopped answering: wake every watcher once (they re-read and poll every
        `POLL_S`) and drop the connection, so the next reader's turn reconnects."""
        self._heard = self._pinged = self._clock.monotonic()
        if not self._live:
            return
        self._live = False
        log.warning("file events connection lost (%s); watchers poll", type(exc).__name__)
        connection = getattr(pubsub, "connection", None)
        if connection is not None:
            with contextlib.suppress(RedisError, OSError):
                async with deadline_scope(self._clock, self._clock.monotonic() + EVENTS_DISCONNECT_S):
                    await connection.disconnect()
        for file_id, tenants in tuple(self._channels.values()):
            for tenant in tuple(tenants):
                await self._local.publish(tenant, file_id, "")

    async def _dispatch(self, message: Mapping[str, Any]) -> None:
        channel = message.get("channel")
        entry = self._channels.get(channel.decode() if isinstance(channel, bytes) else str(channel))
        if entry is None:
            return  # its last watcher left meanwhile
        file_id, tenants = entry
        try:
            payload = json.loads(message.get("data") or b"{}")
        except (TypeError, ValueError):
            return
        tenant = payload.get("tenant_id") if isinstance(payload, dict) else None
        if isinstance(tenant, str) and tenant in tenants:
            await self._local.publish(tenant, file_id, str(payload.get("status")))
            return
        if self._security is not None:  # a foreign tenant on our channel: drop it (spec-09 §7)
            for watcher in tuple(tenants):
                self._security.emit("pubsub_tenant_mismatch", tenant_id=watcher, job_id=file_id)


# --- MediaFileService ------------------------------------------------------------------------------------------------
POLL_S: Final = 2.0
WATCH_POLL_S: Final = 30.0  # a watcher's re-read while its event channel is live (Wave E2: no 2 s re-read storm)
EVENTS_PING_S: Final = 5.0
EVENTS_DEAD_S: Final = 15.0
EVENTS_DISCONNECT_S: Final = 0.5
UPLOAD_PRIORITY: Final = 100
INLINE_PRIORITY: Final = 10
PURGE_PRIORITY: Final = 300
MEDIA_PROCESS: Final = "media.process"
FILE_PURGE: Final = "file.purge"
MEDIA_MAX_ATTEMPTS: Final = 2
SWEEP_DELAY_S: Final = 3600.0  # the purge re-sweep of a file whose run may still be in flight
# A file still not terminal this long after its upload (or the last check) is re-queued, and settled
# failed{extraction_failed} at the last round.
WATCHDOG_DELAY_S: Final = 7200.0
WATCHDOG_ROUNDS: Final = 3


async def enqueue_process(jobs: JobQueue, tenant_id: str, file_id: str, *, priority: int) -> str:
    """The `media.process` job of a file (dedupe `file_id`: nothing new while a run is queued or running)."""
    return await jobs.enqueue(
        MEDIA_PROCESS,
        tenant_id,
        {"file_id": file_id},
        priority=priority,
        dedupe_key=file_id,
        max_attempts=MEDIA_MAX_ATTEMPTS,
    )


async def arm_watchdog(jobs: JobQueue, clock: Clock, tenant_id: str, file_id: str, round_: int) -> None:
    """Check `round_` of a file in `WATCHDOG_DELAY_S`: a `media.process` job with payload `{file_id, watchdog:
    round_}` (`MediaProcessor` re-queues a file that is not terminal by then, and settles it at the last round), so a
    crash between the file INSERT and its job, or a worker killed on the last attempt, never leaves a file pending."""
    await jobs.enqueue(
        MEDIA_PROCESS,
        tenant_id,
        {"file_id": file_id, "watchdog": round_},
        priority=PURGE_PRIORITY,
        run_after=clock.now() + timedelta(seconds=WATCHDOG_DELAY_S),
        dedupe_key=f"{file_id}:watchdog:{round_}",
    )


def clean_filename(filename: str) -> str | None:
    """A display name from any caller: NFC, no path, no control characters, ≤ 255 characters."""
    return display_filename({"filename": filename}) or None


class MediaFileService:
    """`FileService` (spec-02 §6, spec-07 §3, §6). Reads and writes go through `db.uow(principal)` (RLS: tenant;
    project unless tenant_admin/auditor); jobs run under `tenant_uow`. Objects live in `store`; status changes are
    published on `events`. The upload stream is never buffered beyond one 8 MiB part. `telemetry` observes
    `aghoo_inline_ingest_seconds` (IMPL-074)."""

    def __init__(
        self,
        *,
        db: Database,
        jobs: JobQueue,
        store: ObjectStore,
        config: ConfigRegistry,
        clock: Clock,
        ids: IdGen,
        events: FileEvents,
        cache: DerivativeCache | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._db = db
        self._jobs = jobs
        self._store = store
        self._config = config
        self._clock = clock
        self._ids = ids
        self._events = events
        self._cache = cache or DerivativeCache()
        self._flights = SingleFlight()  # concurrent row reads per (principal, file) and object GETs per cache key
        self._telemetry = telemetry

    @classmethod
    def from_services(cls, services: ServiceSet) -> MediaFileService:
        """The production wiring: `services.s3` in `settings.s3_bucket`, Redis file events."""
        security = MediaSecurityEvents(
            telemetry=services.telemetry, outbox=services.outbox, ids=services.ids, clock=services.clock
        )
        return cls(
            db=services.db,
            jobs=services.jobs,
            store=ObjectStore(services.s3, bucket=services.settings.s3_bucket, env=services.settings.env),
            config=services.config,
            clock=services.clock,
            ids=services.ids,
            events=RedisFileEvents(services.redis, services.keys, security=security, clock=services.clock),
            telemetry=services.telemetry,
        )

    @property
    def store(self) -> ObjectStore:
        """The object store (the media worker shares it)."""
        return self._store

    @property
    def events(self) -> FileEvents:
        """The file-event channel (the media worker publishes on it)."""
        return self._events

    async def _expires_at(self, u: UnitOfWork, principal: Principal, session_id: str | None) -> datetime:
        """`now + retain_days` of the data class (the session's, else the project's default; spec-03 §5).
        Raises 404 `session_not_found` for an unknown or invisible session."""
        data_class = "internal"
        if session_id is not None:
            session = await u.sessions.get(session_id)
            if session is None:
                raise NoemaError(ErrorCode.SESSION_NOT_FOUND, "Session not found.", param="session_id")
            data_class = str(session.get("data_class") or data_class)
        else:
            project = await u.projects.get(principal.project_id) if principal.project_id else None
            if project is not None:
                data_class = str(project.get("default_data_class") or data_class)
        policy = self._config.current().platform.data_classes.for_class(cast(DataClass, data_class))
        return self._clock.now() + timedelta(days=policy.retain_days)

    async def _check_session(self, principal: Principal, session_id: str | None) -> datetime:
        async with self._db.uow(principal) as u:
            return await self._expires_at(u, principal, session_id)

    def _file_row(
        self,
        principal: Principal,
        *,
        file_id: str,
        origin: str,
        declared: str,
        size: int,
        sha256: str,
        filename: str | None,
        session_id: str | None,
        expires_at: datetime,
    ) -> dict[str, object]:
        return {
            "id": file_id,
            "project_id": principal.project_id,
            "principal_id": principal.principal_id,
            "session_id": session_id,
            "origin": origin,
            "filename_display": filename,
            "declared_type": declared,
            "media_type": None,
            "bytes": size,
            "sha256": sha256,
            "status": "quarantined",
            "quarantine_key": quarantine_key(principal.tenant_id, file_id),
            "expires_at": expires_at,
        }

    async def _create(self, principal: Principal, row: dict[str, object], *, priority: int) -> FileRef:
        """Arm the file's watchdog, INSERT the file row, enqueue `media.process` and publish; on failure the
        quarantine object (and the row) are removed again, so nothing half-created survives. The watchdog comes
        first, so a crash after the INSERT still reaches a terminal state (`arm_watchdog`)."""
        key = str(row["quarantine_key"])
        file_id = str(row["id"])
        try:
            await arm_watchdog(self._jobs, self._clock, principal.tenant_id, file_id, 1)
            async with self._db.uow(principal) as u:
                stored = await u.files.create(row)
        except BaseException:
            with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
                await self._store.delete(key)
            raise
        try:
            await enqueue_process(self._jobs, principal.tenant_id, file_id, priority=priority)
        except BaseException:
            with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
                async with self._db.uow(principal) as u:
                    await u.files.delete(str(row["id"]))
                await self._store.delete(key)
            raise
        await self._events.publish(principal.tenant_id, str(row["id"]), "quarantined")
        return file_ref(stored, ())

    async def upload(
        self,
        principal: Principal,
        stream: AsyncIterator[bytes],
        *,
        declared_type: str,
        filename: str,
        session_id: str | None,
    ) -> FileRef:
        """spec-07 §3.1 steps 1-5: 415 pre-check, 404 session, streamed quarantine put with the per-type cap (413)
        and a streaming sha256, the `quarantined` row, then `media.process` (priority 100, dedupe file_id, 2
        attempts)."""
        pre = precheck_upload(declared_type, filename)
        expires_at = await self._check_session(principal, session_id)
        file_id = self._ids.new("file")
        stored = await self._store.write_stream(
            quarantine_key(principal.tenant_id, file_id), principal.tenant_id, stream, cap=pre.cap_bytes
        )
        row = self._file_row(
            principal,
            file_id=file_id,
            origin="upload",
            declared=pre.declared,
            size=stored.bytes,
            sha256=stored.sha256,
            filename=clean_filename(filename),
            session_id=session_id,
            expires_at=expires_at,
        )
        return await self._create(principal, row, priority=UPLOAD_PRIORITY)

    async def ingest_inline(
        self,
        principal: Principal,
        data_b64: str,
        media_type: str,
        *,
        deadline: Deadline,
        session_id: str | None = None,
    ) -> FileRef:
        """IMPL-074 / spec-07 §3.2: arithmetic size check (413 before any decode or job), byte-level base64
        decode, quarantine put, `media.process` at priority 10, then wait for `raster` + `raster_1568` until
        `min(deadline, now + platform.media.inline_wait_ms)`. The time from here to the answer (after the 415/413
        checks) is `aghoo_inline_ingest_seconds`."""
        started = self._clock.monotonic()
        inline_row(media_type)  # 415 outside the image rows
        check_inline_size(data_b64)
        try:
            return await self._ingest(principal, data_b64, media_type, deadline=deadline, session_id=session_id)
        finally:
            if self._telemetry is not None:
                elapsed = self._clock.monotonic() - started
                self._telemetry.instruments.observe("aghoo_inline_ingest_seconds", elapsed)

    async def _ingest(
        self, principal: Principal, data_b64: str, media_type: str, *, deadline: Deadline, session_id: str | None
    ) -> FileRef:
        data = decode_inline(data_b64)
        expires_at = await self._check_session(principal, session_id)
        file_id = self._ids.new("file")
        key = quarantine_key(principal.tenant_id, file_id)
        await self._store.put(key, principal.tenant_id, data, "application/octet-stream")
        row = self._file_row(
            principal,
            file_id=file_id,
            origin="inline",
            declared=normalize_declared_type(media_type),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            filename=None,
            session_id=session_id,
            expires_at=expires_at,
        )
        wait_ms = self._config.current().platform.media.inline_wait_ms
        bound = deadline.earliest(Deadline(self._clock.monotonic() + wait_ms / 1000, self._clock))
        async with self._events.subscribe(principal.tenant_id, file_id) as waiter:
            ref = await self._create(principal, row, priority=INLINE_PRIORITY)
            return await self._await_raster(principal, ref, waiter, bound)

    async def _await_raster(
        self, principal: Principal, ref: FileRef, waiter: FileEventWaiter, bound: Deadline
    ) -> FileRef:
        """Wait for the `image.normalize` result of an inline image (spec-07 §3.2 step 5)."""
        while True:
            if ref.status == "rejected":
                code = str((ref.rejection or {}).get("code", "corrupt"))
                if code == "pixel_limit":  # the long edge > 8,000 px (or a pixel bomb): IMPL-074
                    raise NoemaError(
                        ErrorCode.IMAGE_TOO_LARGE,
                        f"The image exceeds {MAX_INLINE_LONG_EDGE} px on its long edge.",
                        details={"max_long_edge_px": MAX_INLINE_LONG_EDGE},
                    )
                raise rejected_error(code)
            if ref.status in ("failed", "deleted"):
                failure = dict(ref.failure or failure_object("extraction_failed"))
                raise NoemaError(
                    ErrorCode.FILE_REJECTED, "The image could not be processed.", details={"failure": failure}
                )
            ready = {d.kind for d in ref.derivatives if d.status == "ready"}
            if {"raster", "raster_1568"} <= ready:
                return ref
            if bound.expired():
                raise NoemaError(
                    ErrorCode.FILE_NOT_READY, "The image is not ready yet.", details={"file_id": ref.file_id}
                )
            at = min(bound.at, self._clock.monotonic() + POLL_S)
            async with deadline_scope(self._clock, at):
                await waiter.wait()
            ref = await self._read(principal, ref.file_id, include_deleted=True)

    async def _read(self, principal: Principal, file_id: str, *, include_deleted: bool = False) -> FileRef:
        """The file under the caller's scope; 404 `not_found` when missing or invisible. `include_deleted` also
        returns a file marked deleted (watchers see its final state)."""
        async with self._db.uow(principal) as u:
            row = await u.files.get(file_id)
            if row is None and include_deleted:
                try:
                    row = await u.files.update(file_id)  # no changes: reads the row, deleted ones too
                except NoemaError:
                    row = None
            if row is None:
                raise NoemaError(ErrorCode.NOT_FOUND, "File not found.")
            derivatives = await u.derivatives.list_for_file(file_id)
        return file_ref(row, derivatives)

    async def get(self, principal: Principal, file_id: str) -> FileRef:
        """The file; 404 `not_found` for unknown, deleted or foreign ids."""
        return await self._read(principal, file_id)

    async def delete(self, principal: Principal, file_id: str) -> None:
        """Mark the file deleted (it disappears from reads at once) and enqueue `file.purge`."""
        async with self._db.uow(principal) as u:
            if await u.files.get(file_id) is None:
                raise NoemaError(ErrorCode.NOT_FOUND, "File not found.")
            await u.files.update(file_id, status="deleted", deleted_at=self._clock.now())
        await self._jobs.enqueue(
            FILE_PURGE, principal.tenant_id, {"file_id": file_id}, priority=PURGE_PRIORITY, dedupe_key=file_id
        )
        await self._events.publish(principal.tenant_id, file_id, "deleted")

    async def purge(self, tenant_id: str, file_id: str) -> None:
        """Mark the row deleted if it is not yet (retention, session.delete: a running `media.process` then stops and
        discards its outputs), delete the S3 objects (quarantine and derivatives), schedule a re-sweep after
        `SWEEP_DELAY_S` when a derivative was still pending (an in-flight run may store one more object before it
        sees the deletion), then delete the row (derivative rows cascade); idempotent."""
        async with self._db.tenant_uow(tenant_id) as u:
            try:
                row = await u.files.update(file_id)
            except NoemaError:
                return
            if row.get("deleted_at") is None:
                row = await u.files.update(file_id, status="deleted", deleted_at=self._clock.now())
            in_flight = any(d.get("status") == "pending" for d in await u.derivatives.list_for_file(file_id))
        await self._store.delete(str(row.get("quarantine_key") or quarantine_key(tenant_id, file_id)))
        await self._store.delete_prefix(derivative_prefix(tenant_id, file_id))
        if in_flight:
            await self._jobs.enqueue(
                FILE_PURGE,
                tenant_id,
                {"file_id": file_id, "sweep": True},
                priority=PURGE_PRIORITY,
                run_after=self._clock.now() + timedelta(seconds=SWEEP_DELAY_S),
                dedupe_key=f"{file_id}:sweep",
            )
        async with self._db.tenant_uow(tenant_id) as u:
            await u.files.delete(file_id)
        await self._events.publish(tenant_id, file_id, "deleted")

    async def watch(self, principal: Principal, file_id: str) -> AsyncIterator[FileRef]:
        """The current state, then each change, until a terminal state (`is_terminal`). Woken by file events,
        with a 2 s poll fallback; a purged file ends the stream with a `deleted` snapshot."""
        async with self._events.subscribe(principal.tenant_id, file_id) as waiter:
            ref = await self._read(principal, file_id)
            yield ref
            last = snapshot_key(ref)
            while not is_terminal(ref):
                async with deadline_scope(self._clock, self._clock.monotonic() + waiter.poll_s):
                    await waiter.wait()
                try:
                    ref = await self._read(principal, file_id, include_deleted=True)
                except NoemaError:
                    ref = replace(ref, status="deleted")
                if (key := snapshot_key(ref)) != last:
                    last = key
                    yield ref

    def _derivative(self, ref: FileRef, kind: DerivativeKind) -> DerivativeRef:
        """The derivative a download or read needs: 409 `file_rejected`, 409 `file_not_ready` (pending, or not
        created yet while the file is still processing), else 404 `not_found` (no such kind, or it failed)."""
        if ref.status == "rejected":
            raise NoemaError(
                ErrorCode.FILE_REJECTED, "The file was rejected.", details={"rejection": dict(ref.rejection or {})}
            )
        found = next((d for d in ref.derivatives if d.kind == kind), None)
        kind_of = media_kind(ref)
        expected = kind_of is not None and kind in (*EXPECTED[kind_of], "raster_1568")
        if found is None and expected and ref.status in ("quarantined", "processing"):
            raise NoemaError(ErrorCode.FILE_NOT_READY, "The derivative is not ready yet.")
        if found is not None and found.status == "pending":
            raise NoemaError(ErrorCode.FILE_NOT_READY, "The derivative is not ready yet.")
        if found is None or found.status != "ready":
            raise NoemaError(ErrorCode.NOT_FOUND, "Derivative not found.")
        return found

    def _key(self, tenant_id: str, ref: FileRef, d: DerivativeRef, page: int | None) -> str:
        if d.kind == "page_images":
            if page is None or not 1 <= page <= (d.pages or 0):
                raise NoemaError(ErrorCode.NOT_FOUND, "Page not found.")
            return derivative_key(tenant_id, ref.file_id, d.kind, page)
        if page is not None:
            raise NoemaError(ErrorCode.NOT_FOUND, "Derivative not found.")
        return derivative_key(tenant_id, ref.file_id, d.kind)

    async def open_derivative(
        self, principal: Principal, file_id: str, kind: DerivativeKind, *, page: int | None = None
    ) -> tuple[str, AsyncIterator[bytes]]:
        """`(content type, byte stream)` of one downloadable derivative (spec-07 §6); `raster_1568` is internal
        (404)."""
        if kind == "raster_1568" or kind not in CONTENT_TYPES:
            raise NoemaError(ErrorCode.NOT_FOUND, "Derivative not found.")
        ref = await self._read(principal, file_id)
        d = self._derivative(ref, kind)
        key = self._key(principal.tenant_id, ref, d, page)
        return CONTENT_TYPES[kind], await self._store.open(key)

    async def read_derivative(
        self, principal: Principal, file_id: str, kind: DerivativeKind, *, page: int | None = None
    ) -> bytes:
        """The whole derivative for model blocks, through the per-pod LRU keyed by (tenant, sha256, page).
        404 `not_found` when it is missing, not ready or failed. Concurrent reads of one file by one principal share
        its row read, and concurrent misses of one object its GET (`SingleFlight`)."""
        ref: FileRef = await self._flights.run(("row", principal, file_id), lambda: self._read(principal, file_id))
        try:
            d = self._derivative(ref, kind)
        except NoemaError as exc:
            raise NoemaError(ErrorCode.NOT_FOUND, "Derivative not available.") from exc
        key = self._key(principal.tenant_id, ref, d, page)
        cache_key = (principal.tenant_id, d.sha256 or key, page)
        if (cached := self._cache.get(cache_key)) is not None:
            return cached
        data: bytes | None = await self._flights.run(("object", cache_key), lambda: self._fetch(key, cache_key))
        if data is None:
            raise NoemaError(ErrorCode.NOT_FOUND, "Derivative not available.")
        return data

    async def _fetch(self, key: str, cache_key: tuple[str, str, int | None]) -> bytes | None:
        data = await self._store.get(key)
        if data is not None:
            self._cache.put(cache_key, data)
        return data


# --- authorization resources for the routes (spec-09 §3) --------------------------------------------------------------
async def session_resource(db: Database, principal: Principal, session_id: str) -> ResourceRef:
    """The session an upload names, for `file.upload` (404 `session_not_found` when missing or invisible)."""
    async with db.uow(principal) as u:
        row = await u.sessions.get(session_id)
    if row is None:
        raise NoemaError(ErrorCode.SESSION_NOT_FOUND, "Session not found.", param="session_id")
    return ResourceRef(
        kind="session",
        id=session_id,
        project_id=_opt_str(row, "project_id"),
        owner_principal_id=_opt_str(row, "owner_principal_id"),
    )


async def file_resource(db: Database, principal: Principal, ref: FileRef) -> ResourceRef:
    """The file as an authz resource: its uploader and, for session files, the session owner (V(file) is
    "uploader, or owner of its session"). The project scope was applied by RLS when the file was read."""
    session_owner: str | None = None
    if ref.session_id is not None:
        async with db.uow(principal) as u:
            row = await u.sessions.get(ref.session_id)
        session_owner = None if row is None else _opt_str(row, "owner_principal_id")
    return ResourceRef(
        kind="file",
        id=ref.file_id,
        project_id=None,
        owner_principal_id=ref.principal_id,
        session_owner_principal_id=session_owner,
    )
