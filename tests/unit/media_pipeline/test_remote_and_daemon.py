"""RemoteSandboxRunner ↔ sandboxd over a real UDS on the host (ADR-002 §6): the worker-side result mapping for every
sandboxd status, SO_PEERCRED refusal, request validation and job-dir checks in sandboxd (with a stub in place of
nsjail, which only exists in the media image), and sandboxd's startup refusal with credentials in its env."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from tests.fakes.telemetry import FakeTelemetry

from noema.core.protocols.sandbox import SandboxJob, SandboxKind, SandboxLimits
from noema.media.extractors.common import Outputs
from noema.media.sandbox import spool
from noema.media.sandbox.child import ChildOutcome
from noema.media.sandbox.ipc import RawStatus, decode_response, encode_request
from noema.media.sandbox.limits import KIND_LIMITS
from noema.media.sandbox.nsjail import NsjailRunner
from noema.media.sandbox.remote import RemoteSandboxRunner, SandboxUnavailable
from noema.media.sandbox.sandboxd import Sandboxd, StartupRefused, assert_no_credentials, main, serve

from . import media_fixtures as fx

pytestmark = [pytest.mark.unit, pytest.mark.enable_socket]


def job_id() -> str:
    return "sbx_" + uuid.uuid4().hex.upper()[:26]


class StubJail:
    """Stands in for `NsjailRunner.execute`: answers `status` and, for ok, writes a valid thumbnail-only manifest."""

    def __init__(self, status: RawStatus = "ok", returncode: int = 0) -> None:
        self.status = status
        self.returncode = returncode
        self.calls: list[tuple[Path, str, str]] = []

    async def execute(
        self,
        job_dir: Path,
        kind: SandboxKind,
        params: str,
        limits: SandboxLimits,
        *,
        argv: list[str] | None = None,
        on_start: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[RawStatus, ChildOutcome]:
        self.calls.append((job_dir, kind, params))
        if on_start is not None:
            await on_start()
        if self.status == "ok":
            out = Outputs(kind, job_dir / "out", "stub-jail")
            out.facts.update({"width": 1, "height": 1, "frames": 1})
            for name, k in (
                ("raster.png", "raster"),
                ("raster_1568.png", "raster_1568"),
                ("thumbnail.png", "thumbnail"),
            ):
                out.add(name, fx.png(4, 4), kind=k, media_type="image/png")
            out.write_manifest()
        return self.status, ChildOutcome(self.returncode, False, 7, "tail")


@asynccontextmanager
async def daemon(spool_root: Path, jail: StubJail, *, allowed_uid: int | None = None) -> AsyncIterator[Path]:
    server = Sandboxd(
        cast(NsjailRunner, jail), spool_root=spool_root, allowed_uid=os.getuid() if allowed_uid is None else allowed_uid
    )
    async with anyio.create_task_group() as tg:
        sock = await tg.start(serve, server, spool_root / "sandboxd.sock")
        try:
            yield sock
        finally:
            tg.cancel_scope.cancel()


async def run(spool_root: Path, sock: Path, kind: SandboxKind = "image.normalize", **params: Any) -> Any:
    remote = RemoteSandboxRunner("test", socket_path=sock, spool_root=spool_root)
    jid = job_id()
    spool.prepare(spool_root, jid, fx.png())
    job = SandboxJob(jid, kind, spool.input_path(spool_root, jid), params or {"format": "PNG"})
    return await remote.run(job, KIND_LIMITS[kind])


async def test_ok_results_are_validated_on_the_worker_side(tmp_path: Path) -> None:
    jail = StubJail()
    async with daemon(tmp_path, jail) as sock:
        result = await run(tmp_path, sock)
    assert result.status == "ok" and result.extractor == "stub-jail"
    assert {o.kind for o in result.outputs} == {"raster", "raster_1568", "thumbnail"}
    assert jail.calls[0][1] == "image.normalize" and jail.calls[0][2] == '{"format":"PNG"}'


async def test_the_slot_wait_is_observed(tmp_path: Path) -> None:
    """aghoo_sandbox_queue_seconds{kind}: request sent -> started line (ADR-002 §6; media-pipeline item 13)."""
    telemetry = FakeTelemetry()
    async with daemon(tmp_path, StubJail()) as sock:
        remote = RemoteSandboxRunner("test", socket_path=sock, spool_root=tmp_path, telemetry=telemetry)
        jid = job_id()
        spool.prepare(tmp_path, jid, fx.png())
        job = SandboxJob(jid, "image.normalize", spool.input_path(tmp_path, jid), {"format": "PNG"})
        assert (await remote.run(job, KIND_LIMITS["image.normalize"])).status == "ok"
    (metric,) = telemetry.metrics
    assert metric.name == "aghoo_sandbox_queue_seconds" and metric.labels == {"kind": "image.normalize"}


@pytest.mark.parametrize(
    ("status", "returncode", "expected", "error"),
    [
        ("failed", 1, "failed", "exit"),
        ("timeout", 137, "timeout", "timeout"),
        ("violation", 159, "violation", "violation"),
        ("bad_request", 2, "failed", "bad_request"),
    ],
)
async def test_non_ok_statuses_map_to_results(
    tmp_path: Path, status: RawStatus, returncode: int, expected: str, error: str
) -> None:
    async with daemon(tmp_path, StubJail(status, returncode)) as sock:
        result = await run(tmp_path, sock)
    assert (result.status, result.facts["error"], result.outputs) == (expected, error, ())


async def test_peer_uid_mismatch_and_bad_job_dirs_are_bad_requests(tmp_path: Path) -> None:
    jail = StubJail()
    async with daemon(tmp_path, jail, allowed_uid=os.getuid() + 1) as sock:
        refused = await run(tmp_path, sock)
    assert refused.status == "failed" and refused.facts["error"] == "bad_request" and jail.calls == []
    async with daemon(tmp_path, jail) as sock:
        jid = job_id()
        jd = spool.prepare(tmp_path, jid, fx.png())
        (jd / "in" / "input.bin").unlink()
        (jd / "in" / "input.bin").symlink_to("/etc/passwd")  # not a regular file: refused before any jail
        raw = await exchange(sock, encode_request(jid, "detect", {}, KIND_LIMITS["detect"]))
        assert decode_response(raw, job_id=jid).status == "bad_request"
        garbage = await exchange(sock, b'{"v": 1, "job_id": "../x"}\n')
        assert b'"status":"bad_request"' in garbage
    assert jail.calls == []


async def test_an_unreachable_sandboxd_raises_and_a_silent_peer_is_answered(tmp_path: Path) -> None:
    """A failed connect dispatched nothing: `SandboxUnavailable` (the job is retried, C1 review). sandboxd answers a
    peer that sends no request line within its read timeout with bad_request (job id "") and frees the handler."""
    unreachable = RemoteSandboxRunner("test", socket_path=tmp_path / "missing.sock", spool_root=tmp_path)
    jid = job_id()
    spool.prepare(tmp_path, jid, b"x")
    with pytest.raises(SandboxUnavailable):
        await unreachable.run(SandboxJob(jid, "detect", spool.input_path(tmp_path, jid), {}), KIND_LIMITS["detect"])
    jail = StubJail()
    server = Sandboxd(cast(NsjailRunner, jail), spool_root=tmp_path, allowed_uid=os.getuid(), read_timeout_s=0.2)
    async with anyio.create_task_group() as tg:
        sock = await tg.start(serve, server, tmp_path / "sandboxd.sock")
        stream = await anyio.connect_unix(sock)
        async with stream:
            await stream.send(b'{"v": 1, "job_id": "sbx_')  # never a newline
            with anyio.fail_after(5):
                raw = await stream.receive()
        tg.cancel_scope.cancel()
    resp = decode_response(raw.rstrip(b"\n"), job_id=jid)
    assert resp.status == "bad_request" and resp.job_id == "" and jail.calls == []


async def exchange(sock: Path, request: bytes) -> bytes:
    stream = await anyio.connect_unix(sock)
    async with stream:
        await stream.send(request)
        buf = bytearray()
        while b"\n" not in buf:
            try:
                buf.extend(await stream.receive())
            except anyio.EndOfStream:
                break
    return bytes(buf).split(b"\n", 1)[0]


def test_prepare_never_writes_through_a_planted_link(tmp_path: Path) -> None:
    jid = job_id()
    jd = spool.prepare(tmp_path, jid, b"first")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (jd / "in" / "input.bin").unlink()
    (jd / "in" / "input.bin").symlink_to(victim)
    spool.prepare(tmp_path, jid, b"second")
    assert victim.read_text() == "keep" and (jd / "in" / "input.bin").read_bytes() == b"second"


def test_sandboxd_refuses_to_start_with_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for env in ({"AWS_ACCESS_KEY_ID": "x"}, {"NOEMA_DB_DSN": "x"}, {"MY_SECRET_TOKEN": "x"}):
        with pytest.raises(StartupRefused):
            assert_no_credentials(env)
    assert_no_credentials({"NOEMA_ENV": "test", "PATH": "/usr/bin"})
    monkeypatch.setenv("NOEMA_DB_PASSWORD", "x")
    assert main(["--socket", str(tmp_path / "s.sock"), "--spool", str(tmp_path)]) == 2
