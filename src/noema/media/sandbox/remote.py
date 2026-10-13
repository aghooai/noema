"""`RemoteSandboxRunner` (ADR-002 §1, §6-§7): the media-worker's client of `sandboxd` (required in staging/prod).

The worker (uid 10001) prepares `/spool/<job_id>/` itself and sends one request line over `/spool/sandboxd.sock`. It
waits at most `queue_wait_s` for the started line (the job's turn for a jail slot; `failed{busy}` past it) and then
at most `wall_s + 5` seconds for the response line, so the wall bound covers the jail run only. Leaving (timeout,
cancellation) closes the connection, which cancels the job in sandboxd. The manifest and outputs are validated with
trusted code in a worker thread. A failed connect (sandboxd restarting, no socket) dispatched nothing: it raises
`SandboxUnavailable`, so the job is retried later instead of failing its file; failures after the connect are
`failed{unavailable}` results. The slot wait is observed as `aghoo_sandbox_queue_seconds{kind}`. Stdlib + anyio
only: the worker image has no media libraries.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Final

import anyio
import anyio.abc

from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxKind, SandboxLimits
from noema.core.protocols.telemetry import Telemetry
from noema.media.sandbox import spool
from noema.media.sandbox.backends import assert_backend_allowed
from noema.media.sandbox.base import result_in_thread
from noema.media.sandbox.inproc import clear_out
from noema.media.sandbox.ipc import (
    MAX_MESSAGE_BYTES,
    BadRequest,
    decode_response,
    encode_request,
    is_started,
)
from noema.media.sandbox.limits import BadParams, check_params, clamp
from noema.media.sandbox.manifest import failure

BACKEND: Final = "remote"
SOCKET: Final = Path("/spool/sandboxd.sock")
SPOOL: Final = Path("/spool")
GRACE_S: Final = 5.0
QUEUE_WAIT_S: Final = 1800.0  # well below the 1 h orphan sweep of the job dir

log = logging.getLogger("noema.media.sandbox")


class QueueTimeout(Exception):
    """No sandboxd slot within `queue_wait_s`."""


class SandboxUnavailable(RuntimeError):
    """sandboxd could not be reached, so nothing was dispatched. Not a job failure: `run` raises it and the caller
    retries the job later."""


class _Lines:
    """Newline-delimited lines of one connection (the started line and the response may share a read)."""

    def __init__(self, stream: anyio.abc.ByteStream) -> None:
        self._stream = stream
        self._buf = bytearray()

    async def next(self) -> bytes:
        while b"\n" not in self._buf:
            try:
                chunk = await self._stream.receive(65536)
            except anyio.EndOfStream:
                break
            self._buf.extend(chunk)
            if len(self._buf) > MAX_MESSAGE_BYTES:
                raise BadRequest("response larger than 64 KiB")
        line, _, rest = bytes(self._buf).partition(b"\n")
        self._buf = bytearray(rest)
        return line


class RemoteSandboxRunner:
    """`SandboxRunner` over the sandboxd UDS (see the module docstring)."""

    def __init__(
        self,
        env: str,
        *,
        socket_path: Path = SOCKET,
        spool_root: Path = SPOOL,
        queue_wait_s: float = QUEUE_WAIT_S,
        grace_s: float = GRACE_S,
        telemetry: Telemetry | None = None,
    ) -> None:
        assert_backend_allowed(BACKEND, env)
        self._socket = socket_path
        self._spool = spool_root
        self._queue_wait_s = queue_wait_s
        self._grace_s = grace_s
        self._telemetry = telemetry

    def _queued(self, kind: SandboxKind, since: float) -> None:
        if self._telemetry is not None:
            self._telemetry.instruments.observe("aghoo_sandbox_queue_seconds", time.monotonic() - since, kind=kind)

    async def _exchange(
        self, request: bytes, job_id: str, kind: SandboxKind, wall_s: float
    ) -> tuple[bytes | None, int]:
        """The response line, or (None, ms since the jail started) when it did not come within wall_s + grace of the
        jail start. Raises `SandboxUnavailable` when the connect fails, `QueueTimeout` when no started line came
        within `queue_wait_s`."""
        try:
            stream = await anyio.connect_unix(self._socket)
        except OSError as exc:
            raise SandboxUnavailable(f"sandboxd is unreachable ({type(exc).__name__})") from None
        async with stream:
            await stream.send(request)
            lines = _Lines(stream)
            sent = time.monotonic()
            with anyio.move_on_after(self._queue_wait_s) as queued:
                first = await lines.next()
            if queued.cancelled_caught:
                self._queued(kind, sent)
                raise QueueTimeout
            if not is_started(first, job_id=job_id):
                return first, 0  # answered without a jail (bad_request)
            self._queued(kind, sent)
            started = time.monotonic()
            with anyio.move_on_after(wall_s + self._grace_s):
                return await lines.next(), 0
            return None, int((time.monotonic() - started) * 1000)

    async def run(self, job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        """Run one job in sandboxd; failures after the connect and timeouts are result statuses. Raises
        `SandboxUnavailable` when sandboxd cannot be reached (nothing was dispatched)."""
        lim = clamp(job.kind, limits)
        extractor = f"{BACKEND}:{job.kind}"
        try:
            params = check_params(job.kind, job.params)
            canonical = spool.input_path(self._spool, job.job_id)
        except (BadParams, ValueError):
            log.error("sandbox request rejected locally kind=%s", job.kind)
            return failure("failed", "bad_request", wall_ms=0, extractor=extractor)
        jd = spool.prepare(self._spool, job.job_id, None if job.input_path == canonical else job.input_path)
        clear_out(jd / "out")
        request = encode_request(job.job_id, job.kind, params, lim)
        try:
            raw, jail_ms = await self._exchange(request, job.job_id, job.kind, lim.wall_s)
            if raw is None:
                return failure("timeout", "timeout", wall_ms=jail_ms, extractor=extractor)
            resp = decode_response(raw, job_id=job.job_id)
        except QueueTimeout:
            log.warning("sandboxd had no slot within %.0f s kind=%s", self._queue_wait_s, job.kind)
            return failure("failed", "busy", wall_ms=0, extractor=extractor)
        except (OSError, BadRequest) as exc:
            log.warning("sandboxd call failed kind=%s error=%s", job.kind, type(exc).__name__)
            return failure("failed", "unavailable", wall_ms=0, extractor=extractor)
        if resp.status == "bad_request":
            log.error("sandboxd answered bad_request kind=%s job_id=%s", job.kind, job.job_id)
        log.debug("sandboxd stderr kind=%s tail=%s", job.kind, resp.stderr_tail)
        return await result_in_thread(
            job.kind, jd / "out", resp.status, wall_ms=resp.wall_ms, limits=lim, backend=BACKEND
        )
