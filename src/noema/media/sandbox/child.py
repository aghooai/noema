"""Run one sandbox child process (inproc, bwrap, nsjail) with a wall-clock limit (ADR-002 §5, §7). Stdlib only.

The child gets its own session (process group), so a timeout or a cancellation kills the whole group (an extractor's
tesseract child included). Sandbox timeouts are real time: they bound real processes, not the injected `Clock`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import resource
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio

from noema.core.protocols.sandbox import SandboxLimits

STDERR_TAIL_BYTES: Final = 2048
MIB: Final = 1024 * 1024
SIGSYS: Final = int(signal.SIGSYS)
SIGXCPU: Final = int(signal.SIGXCPU)
SIGKILL: Final = int(signal.SIGKILL)


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """How a child ended: `returncode` (negative = killed by that signal), whether our wall timer killed it, its
    wall time and the last 2 KiB of its stderr."""

    returncode: int
    timed_out: bool
    wall_ms: int
    stderr_tail: str


def rlimit_preexec(limits: SandboxLimits, *, nofile: int = 64) -> Callable[[], None]:
    """A `preexec_fn` that pins the child to one CPU (nsjail's `max_cpus: 1`: thread pools of onnxruntime/BLAS
    size themselves to it) and applies RLIMIT_AS/CPU/FSIZE/NOFILE/CORE before exec (no RLIMIT_NPROC: it counts
    every process of the real uid, so only per-slot uids (nsjail) can use it)."""

    def apply() -> None:  # runs in the forked child: no locks, no allocation-heavy work
        cpus = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, {cpus[os.getpid() % len(cpus)]})
        if limits.mem_mib is not None:
            resource.setrlimit(resource.RLIMIT_AS, (limits.mem_mib * MIB, limits.mem_mib * MIB))
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_s, limits.cpu_s + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits.fsize_mib * MIB, limits.fsize_mib * MIB))
        resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def run_child(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_s: float,
    cwd: Path | None = None,
    preexec_fn: Callable[[], None] | None = None,
) -> ChildOutcome:
    """Run `argv` (stdin and stdout /dev/null, stderr captured) until it exits or `timeout_s` passes; on timeout or
    cancellation the process group is killed and reaped."""
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=dict(env),
        start_new_session=True,
        preexec_fn=preexec_fn,  # rlimits only (ADR-002 §7)
        close_fds=True,
    )
    tail = bytearray()

    async def drain() -> None:
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(65536):
            tail.extend(chunk)
            del tail[:-STDERR_TAIL_BYTES]

    timed_out = False
    try:
        with anyio.move_on_after(timeout_s) as scope:
            async with anyio.create_task_group() as tg:
                tg.start_soon(drain)
                await proc.wait()
                _kill_group(proc)  # lingering grandchildren would hold stderr open
        timed_out = scope.cancelled_caught
    finally:
        if proc.returncode is None:
            _kill_group(proc)
            with anyio.CancelScope(shield=True):
                await proc.wait()
    wall_ms = int((time.monotonic() - started) * 1000)
    assert proc.returncode is not None
    return ChildOutcome(proc.returncode, timed_out, wall_ms, tail.decode("utf-8", errors="replace"))
