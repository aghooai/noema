"""`sandboxd`: the jailer of the network-less media-sandbox container (ADR-002 §1-§6).

    python -I -m noema.media.sandbox.sandboxd [--socket /spool/sandboxd.sock] [--spool /spool]
                                              [--allowed-uid 10001] [--slots 2]

It refuses to start with credentials in its environment (`AWS_*`, `NOEMA_DB_*`, `*_SECRET*`) or with a backend the
environment disallows, listens on the spool UDS and serves one request per connection: `SO_PEERCRED` uid check
right after accept (nothing of a refused peer is read), the request line within `READ_TIMEOUT_S`, request
validation (closed schema, `sbx_<ULID>` job id, kind enum, params closed per kind), `/spool/<job_id>/{in,out}` real
directories opened `O_DIRECTORY|O_NOFOLLOW` and `in/input.bin` a regular file, then one nsjail run
(`NsjailRunner.execute`, which queues for a slot and reclaims `out/`). The started line goes out when the jail
starts; while the job is queued or running the connection is watched, and a client that goes away cancels it (a
queued job never runs, a running jail is killed), so abandoned work never fills the slots. Orphaned job directories
older than 1 h are swept hourly.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import socket
import stat
import struct
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

import anyio
import anyio.abc

from noema.media.sandbox import spool
from noema.media.sandbox.backends import assert_backend_allowed
from noema.media.sandbox.base import params_json
from noema.media.sandbox.ipc import (
    MANIFEST_REF,
    MAX_MESSAGE_BYTES,
    BadRequest,
    JobRequest,
    JobResponse,
    decode_request,
    encode_response,
    encode_started,
)
from noema.media.sandbox.nsjail import NsjailRunner

SOCKET: Final = Path("/spool/sandboxd.sock")
SPOOL: Final = Path("/spool")
SWEEP_EVERY_S: Final = 3600.0
READ_TIMEOUT_S: Final = 5.0  # a peer that sends no request line in time gets bad_request (no handler held forever)
log = logging.getLogger("noema.media.sandboxd")


class StartupRefused(RuntimeError):
    """sandboxd must not start (credentials in the environment, or a disallowed backend)."""


def assert_no_credentials(environ: Mapping[str, str]) -> None:
    """ADR-002 §1: no AWS, DB or secret variables in the sandbox container."""
    bad = sorted(k for k in environ if k.startswith(("AWS_", "NOEMA_DB_")) or "_SECRET" in k.upper())
    if bad:
        raise StartupRefused(f"credential variables present: {bad}")


def peer_uid(sock: socket.socket) -> int:
    """The uid of the connected peer (`SO_PEERCRED`)."""
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return int(uid)


def check_job_dir(spool_root: Path, job_id: str) -> Path:
    """The job dir, after checking `in/`, `out/` are real directories and `in/input.bin` a regular file."""
    jd = spool.job_dir(spool_root, job_id)
    for path in (jd, jd / "in", jd / "out"):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise BadRequest(f"{path.name} is not a real directory") from None
        os.close(fd)
    try:
        st = os.lstat(jd / "in" / spool.INPUT_NAME)
    except OSError:
        raise BadRequest("in/input.bin is missing") from None
    if not stat.S_ISREG(st.st_mode):
        raise BadRequest("in/input.bin is not a regular file")
    return jd


async def _read_line(stream: anyio.abc.ByteStream) -> bytes:
    buf = bytearray()
    while b"\n" not in buf:
        try:
            buf.extend(await stream.receive(65536))
        except anyio.EndOfStream:
            break
        if len(buf) > MAX_MESSAGE_BYTES:
            raise BadRequest("request larger than 64 KiB")
    return bytes(buf.split(b"\n", 1)[0])


def _claimed_job_id(raw: bytes) -> str:
    """The request's job id if it is well-formed (echoed in a bad_request answer), else ""."""
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):
        return ""
    value = doc.get("job_id") if isinstance(doc, dict) else None
    return value if isinstance(value, str) and spool.JOB_ID_RE.fullmatch(value) else ""


class _ClientGone(Exception):
    """The started line could not be sent: the client went away."""


class Sandboxd:
    """The UDS server (see the module docstring)."""

    def __init__(
        self, runner: NsjailRunner, *, spool_root: Path, allowed_uid: int, read_timeout_s: float = READ_TIMEOUT_S
    ) -> None:
        self._runner = runner
        self._spool = spool_root
        self._allowed_uid = allowed_uid
        self._read_timeout_s = read_timeout_s

    async def handle(self, stream: anyio.abc.SocketStream) -> None:
        """Serve one connection: one request, one response."""
        async with stream:
            raw_sock = stream.extra(anyio.abc.SocketAttribute.raw_socket)  # noqa: S610 (anyio, not Django)
            job_id = ""
            try:
                if peer_uid(raw_sock) != self._allowed_uid:  # SO_PEERCRED, before anything is read (job id "")
                    log.warning("sandboxd refused a peer with the wrong uid")
                    raise BadRequest("peer uid not allowed")
                with anyio.move_on_after(self._read_timeout_s) as reading:
                    raw = await _read_line(stream)
                if reading.cancelled_caught:
                    raise BadRequest("no request line in time")
                job_id = _claimed_job_id(raw)  # echoed in a bad_request answer
                req = decode_request(raw)
                job_id = req.job_id
                resp = await self._watched(stream, req)
                if resp is None:
                    log.info("sandboxd client went away, job cancelled job_id=%s", job_id)
                    return
            except BadRequest as exc:
                log.warning("sandboxd bad_request reason=%s", exc)
                resp = JobResponse(job_id, "bad_request", None, None, 0, None, "")
            except Exception as exc:  # one broken job never takes the daemon down
                log.error("sandboxd job failed job_id=%s error=%s", job_id, type(exc).__name__)
                resp = JobResponse(job_id, "failed", None, None, 0, None, "")
            try:
                await stream.send(encode_response(resp))
            except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                log.info("sandboxd client went away job_id=%s", job_id)

    async def _watched(self, stream: anyio.abc.SocketStream, req: JobRequest) -> JobResponse | None:
        """`run(req)` while watching the connection; None when the client went away (the job was cancelled).
        Exceptions of the run are re-raised for `handle`."""
        outcome: list[JobResponse | Exception] = []

        async def started() -> None:
            try:
                await stream.send(encode_started(req.job_id))
            except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                raise _ClientGone from None

        async with anyio.create_task_group() as tg:

            async def watch() -> None:
                with contextlib.suppress(anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
                    while True:  # the client sends nothing after its request: EOF means it went away
                        await stream.receive()
                tg.cancel_scope.cancel()

            tg.start_soon(watch)
            try:
                outcome.append(await self.run(req, on_start=started))
            except _ClientGone:
                pass
            except Exception as exc:  # re-raised outside the task group
                outcome.append(exc)
            tg.cancel_scope.cancel()
        if not outcome:
            return None
        if isinstance(outcome[0], Exception):
            raise outcome[0]
        return outcome[0]

    async def run(self, req: JobRequest, *, on_start: Callable[[], Awaitable[None]] | None = None) -> JobResponse:
        """Run one validated request through nsjail (`on_start`: when its jail starts)."""
        jd = check_job_dir(self._spool, req.job_id)
        status, outcome = await self._runner.execute(
            jd, req.kind, params_json(req.params), req.limits, on_start=on_start
        )
        code = outcome.returncode
        sig = -code if code < 0 else (code - 128 if 128 < code <= 192 else None)
        manifest = MANIFEST_REF if status == "ok" else None
        log.info("sandboxd job_id=%s kind=%s status=%s wall_ms=%d", req.job_id, req.kind, status, outcome.wall_ms)
        log.debug("sandboxd job_id=%s stderr=%s", req.job_id, outcome.stderr_tail)
        return JobResponse(req.job_id, status, code, sig, outcome.wall_ms, manifest, outcome.stderr_tail)

    async def sweep(self) -> None:
        """Hourly orphan sweep of the spool."""
        while True:
            await anyio.sleep(SWEEP_EVERY_S)
            removed = spool.sweep_orphans(self._spool)
            if removed:
                log.info("sandboxd swept %d orphaned job directories", removed)


async def serve(
    daemon: Sandboxd, socket_path: Path, *, task_status: anyio.abc.TaskStatus[Path] = anyio.TASK_STATUS_IGNORED
) -> None:
    """Listen on `socket_path` (any local user may connect; SO_PEERCRED decides) until cancelled; reports the
    socket to `task_status` once it accepts connections."""
    await anyio.Path(socket_path).unlink(missing_ok=True)
    listener = await anyio.create_unix_listener(socket_path)
    os.chmod(socket_path, 0o666)  # noqa: S103 (authorization is the SO_PEERCRED uid check)
    log.info("sandboxd listening on %s", socket_path)
    task_status.started(socket_path)
    async with listener, anyio.create_task_group() as tg:
        tg.start_soon(daemon.sweep)
        await listener.serve(daemon.handle)


def main(argv: Sequence[str] | None = None) -> int:
    """Process entry point; exits non-zero when startup is refused."""
    parser = argparse.ArgumentParser(prog="sandboxd", description=__doc__.split("\n\n")[0])
    parser.add_argument("--socket", type=Path, default=SOCKET)
    parser.add_argument("--spool", type=Path, default=SPOOL)
    parser.add_argument("--allowed-uid", type=int, default=spool.WORKER_UID)
    parser.add_argument("--slots", type=int, default=2)
    opts = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    env = os.environ.get("NOEMA_ENV", "prod")
    try:
        assert_no_credentials(os.environ)
        assert_backend_allowed("nsjail", env, process="sandboxd")
        runner = NsjailRunner(env, spool_root=opts.spool, slots=opts.slots)
    except Exception as exc:  # startup refusal: log the reason, exit non-zero
        log.error("sandboxd refused to start: %s", exc)
        return 2
    daemon = Sandboxd(runner, spool_root=opts.spool, allowed_uid=opts.allowed_uid)
    anyio.run(serve, daemon, opts.socket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
