"""sandboxd queueing (ADR-002 §4-§6): the real `NsjailRunner` slot logic, `Sandboxd` and `RemoteSandboxRunner` over a
real UDS, with the jail itself replaced by gated fakes (nsjail exists only in the media image). A queued Docling job
holds no slot, the long kinds leave one slot to the short ones, the worker's wall bound starts at the jail start,
a full queue answers `busy`, and a client that goes away cancels its job (queued: never runs; running: killed)."""

from __future__ import annotations

import os
import re
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio import wait_all_tasks_blocked

from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxKind, SandboxLimits
from noema.media.extractors.common import Outputs
from noema.media.sandbox import nsjail, spool
from noema.media.sandbox.child import ChildOutcome
from noema.media.sandbox.limits import KIND_LIMITS
from noema.media.sandbox.remote import GRACE_S, QUEUE_WAIT_S, RemoteSandboxRunner
from noema.media.sandbox.sandboxd import Sandboxd, serve

pytestmark = [pytest.mark.unit, pytest.mark.enable_socket]
DETECT_FACTS: dict[str, Any] = {
    "magika_label": "png",
    "magika_score": 0.99,
    "libmagic_mime": "image/png",
    "bytes": 1,
    "utf8_strict": False,
    "has_nul": True,
}


def job_id() -> str:
    return "sbx_" + uuid.uuid4().hex.upper()[:26]


class Jails:
    """Stands in for `run_child`: each jail blocks until its gate opens (detect jails write a valid manifest)."""

    def __init__(self) -> None:
        self.running: list[str] = []
        self.cancelled: list[str] = []
        self.gates: defaultdict[str, anyio.Event] = defaultdict(anyio.Event)

    async def __call__(self, cmd: Sequence[str], *, env: Any, timeout_s: float, **_: Any) -> ChildOutcome:
        config = await anyio.Path(cmd[cmd.index("--config") + 1]).read_text()
        match = re.search(r'src: "([^"]+)/in"', config)
        assert match is not None
        jd = Path(match.group(1))
        self.running.append(jd.name)
        try:
            await self.gates[jd.name].wait()
        except anyio.get_cancelled_exc_class():
            self.cancelled.append(jd.name)
            raise
        if cmd[-2] == "detect":
            out = Outputs("detect", jd / "out", "fake-jail")
            out.facts.update(DETECT_FACTS)
            out.write_manifest()
        return ChildOutcome(0, False, 1, "")

    def open(self, name: str) -> None:
        self.gates[name].set()


@pytest.fixture
def jails(monkeypatch: pytest.MonkeyPatch) -> Jails:
    fake = Jails()
    monkeypatch.setattr(nsjail, "run_child", fake)
    monkeypatch.setattr(spool, "hand_out_to", lambda *a, **k: None)  # chown to a slot uid needs root
    return fake


def runner(spool_root: Path) -> nsjail.NsjailRunner:
    return nsjail.NsjailRunner(
        "test", spool_root=spool_root, policy="/dev/null", owner_uid=os.getuid(), owner_gid=os.getgid()
    )


async def test_long_jobs_leave_a_slot_and_docling_waits_without_one(tmp_path: Path, jails: Jails) -> None:
    jail = runner(tmp_path)
    started: list[str] = []

    async def job(name: str, kind: SandboxKind) -> None:
        jd = spool.prepare(tmp_path, name, b"x")

        async def on_start() -> None:
            started.append(name)

        await jail.execute(jd, kind, "{}", KIND_LIMITS[kind], on_start=on_start)

    md1, md2, render, detect, image, text = (job_id() for _ in range(6))
    async with anyio.create_task_group() as tg:
        tg.start_soon(job, md1, "pdf.markdown")
        await wait_all_tasks_blocked()
        tg.start_soon(job, md2, "pdf.markdown")  # queued behind Docling: holds no slot
        await wait_all_tasks_blocked()
        tg.start_soon(job, render, "pdf.render")  # queued behind the long lane
        await wait_all_tasks_blocked()
        tg.start_soon(job, detect, "detect")
        await wait_all_tasks_blocked()
        assert started == [md1, detect]  # the reserved slot: a short job never waits for long ones
        tg.start_soon(job, image, "image.normalize")
        tg.start_soon(job, text, "markdown.sanitize")
        await wait_all_tasks_blocked()
        assert started == [md1, detect]  # both slots busy: the short jobs queue in FIFO order
        jails.open(detect)
        await wait_all_tasks_blocked()
        assert started == [md1, detect, image]
        jails.open(md1)
        await wait_all_tasks_blocked()
        assert started == [md1, detect, image, text]  # text queued for a slot first; render now has the long lane
        jails.open(image)
        await wait_all_tasks_blocked()
        assert started == [md1, detect, image, text, render]  # md2 (Docling) waits for the long lane
        jails.open(render)
        await wait_all_tasks_blocked()
        assert started[-1] == md2
        jails.open(text)
        jails.open(md2)
    assert jails.running == started and jails.cancelled == []


@asynccontextmanager
async def daemon(spool_root: Path) -> AsyncIterator[Path]:
    server = Sandboxd(runner(spool_root), spool_root=spool_root, allowed_uid=os.getuid())
    async with anyio.create_task_group() as tg:
        sock = await tg.start(serve, server, spool_root / "sandboxd.sock")
        try:
            yield sock
        finally:
            tg.cancel_scope.cancel()


async def remote_run(
    spool_root: Path,
    sock: Path,
    jid: str,
    kind: SandboxKind,
    limits: SandboxLimits | None = None,
    *,
    queue_wait_s: float = QUEUE_WAIT_S,
    grace_s: float = GRACE_S,
) -> ExtractResult:
    client = RemoteSandboxRunner(
        "test", socket_path=sock, spool_root=spool_root, queue_wait_s=queue_wait_s, grace_s=grace_s
    )
    spool.prepare(spool_root, jid, b"x")
    params = {"format": "PNG"} if kind == "image.normalize" else {}
    job = SandboxJob(jid, kind, spool.input_path(spool_root, jid), params)
    return await client.run(job, limits or KIND_LIMITS[kind])


async def test_the_wall_bound_starts_when_the_jail_starts(tmp_path: Path, jails: Jails) -> None:
    quick = SandboxLimits(wall_s=0.1, cpu_s=1, mem_mib=3072, fsize_mib=1, nproc=32, max_outputs=0)
    a, b, late = job_id(), job_id(), job_id()
    jails.open(late)
    results: dict[str, ExtractResult] = {}
    async with daemon(tmp_path) as sock, anyio.create_task_group() as tg:
        for name in (a, b):  # both slots busy

            async def hold(name: str = name) -> None:
                results[name] = await remote_run(tmp_path, sock, name, "image.normalize")

            tg.start_soon(hold)
        await wait_all_tasks_blocked()

        async def queued() -> None:
            results[late] = await remote_run(tmp_path, sock, late, "detect", quick, grace_s=0.1)

        tg.start_soon(queued)
        await anyio.sleep(0.5)  # queued for longer than its wall bound (0.1 + 0.1 s)
        assert late not in jails.running
        jails.open(a)
        jails.open(b)
    assert results[late].status == "ok" and results[late].facts["magika_label"] == "png"


async def test_a_full_queue_answers_busy_and_the_queued_job_never_runs(tmp_path: Path, jails: Jails) -> None:
    a, b, dropped, after = job_id(), job_id(), job_id(), job_id()
    jails.open(after)
    async with daemon(tmp_path) as sock, anyio.create_task_group() as tg:
        for name in (a, b):
            tg.start_soon(remote_run, tmp_path, sock, name, "image.normalize")
        await wait_all_tasks_blocked()
        busy = await remote_run(tmp_path, sock, dropped, "detect", queue_wait_s=0.2)
        assert (busy.status, busy.facts["error"]) == ("failed", "busy")
        jails.open(a)
        jails.open(b)
        done = await remote_run(tmp_path, sock, after, "detect")  # FIFO: a still-queued `dropped` would run first
    assert done.status == "ok" and dropped not in jails.running and after in jails.running


async def test_a_running_jail_is_killed_when_the_client_goes_away(tmp_path: Path, jails: Jails) -> None:
    quick = SandboxLimits(wall_s=0.1, cpu_s=1, mem_mib=3072, fsize_mib=1, nproc=32, max_outputs=0)
    stuck = job_id()
    async with daemon(tmp_path) as sock:
        result = await remote_run(tmp_path, sock, stuck, "detect", quick, grace_s=0.1)
        assert (result.status, result.facts["error"]) == ("timeout", "timeout")
        with anyio.fail_after(5):
            while stuck not in jails.cancelled:
                await wait_all_tasks_blocked()
                await anyio.sleep(0.01)
    assert jails.running == [stuck]
