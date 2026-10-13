"""`NsjailRunner` (ADR-002 §3-§5): one nsjail per job, inside `sandboxd` (container root, network_mode none).

The config is the ADR-002 §3 template with the deploy/sandbox/PRIVILEGES.md corrections (tmpfs flags as mount
fields, `rlimit_nproc_type`/`rlimit_stack_type: VALUE`, the kafel policy passed with `--seccomp_policy`,
`mount_proc: false`). Each concurrent slot has its own outside uid (100000 + slot), so RLIMIT_NPROC is per job.
`out/` is handed to the slot uid before the run and reclaimed with the ownership walk afterwards (never following a
jail-planted link).

Queueing (ADR-002 §4: slots = cpus, Docling one at a time): the long kinds (`pdf.sanitize`, `pdf.render`,
`pdf.markdown`, `ocr`) may hold at most `slots - reserved_short` slots, so `detect`, `image.normalize` (inline
images) and `markdown.sanitize` always find a slot within one short job; `pdf.markdown` waits for the Docling lock
before it takes anything else, so a queued Docling job never holds a slot. Waiters are served in FIFO order.
"""

from __future__ import annotations

import contextlib
import math
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

import anyio

from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxKind, SandboxLimits
from noema.media.sandbox import spool
from noema.media.sandbox.backends import assert_backend_allowed
from noema.media.sandbox.base import jail_env, params_json, raw_status, result_in_thread
from noema.media.sandbox.child import ChildOutcome, run_child
from noema.media.sandbox.inproc import clear_out
from noema.media.sandbox.ipc import RawStatus
from noema.media.sandbox.limits import BadParams, check_params, clamp
from noema.media.sandbox.manifest import failure

BACKEND: Final = "nsjail"
NSJAIL: Final = "/usr/local/bin/nsjail"
POLICY: Final = "/etc/noema-sandbox/seccomp.kafel"
JAIL_PYTHON: Final = "/opt/noema-sandbox/venv/bin/python"
SLOT_BASE_UID: Final = 100000
WORKER_GRACE_S: Final = 5.0  # the worker-side timeout is wall_s + 5 (ADR-002 §5)
SHORT_KINDS: Final[frozenset[SandboxKind]] = frozenset({"detect", "image.normalize", "markdown.sanitize"})


def jail_config(job_dir: Path, kind: SandboxKind, limits: SandboxLimits, slot_uid: int) -> str:
    """The nsjail text-proto config of one job (ADR-002 §3, corrected)."""
    env = "\n".join(f'envar: "{k}={v}"' for k, v in sorted(jail_env().items()))
    rlimit_as = (
        "rlimit_as_type: INF" if limits.mem_mib is None else f"rlimit_as_type: VALUE\nrlimit_as: {limits.mem_mib}"
    )
    return f"""name: "noema-{kind}"
mode: ONCE
hostname: "sbx"
cwd: "/work"
time_limit: {max(1, math.ceil(limits.wall_s))}
max_cpus: 1
keep_env: false
{env}
clone_newnet: true
clone_newuser: true
clone_newns: true
clone_newpid: true
clone_newipc: true
clone_newuts: true
clone_newcgroup: true
iface_no_lo: true
uidmap {{ inside_id: "65534" outside_id: "{slot_uid}" count: 1 }}
gidmap {{ inside_id: "65534" outside_id: "{slot_uid}" count: 1 }}
{rlimit_as}
rlimit_cpu_type: VALUE
rlimit_cpu: {limits.cpu_s}
rlimit_fsize_type: VALUE
rlimit_fsize: {limits.fsize_mib}
rlimit_nofile_type: VALUE
rlimit_nofile: 64
rlimit_nproc_type: VALUE
rlimit_nproc: {limits.nproc}
rlimit_stack_type: VALUE
rlimit_stack: 8
rlimit_core_type: VALUE
rlimit_core: 0
mount_proc: false
mount {{ src: "/usr" dst: "/usr" is_bind: true rw: false }}
mount {{ src: "/lib" dst: "/lib" is_bind: true rw: false }}
mount {{ src: "/lib64" dst: "/lib64" is_bind: true rw: false mandatory: false }}
mount {{ src: "/etc/ld.so.cache" dst: "/etc/ld.so.cache" is_bind: true rw: false }}
mount {{ src: "/opt/noema-sandbox" dst: "/opt/noema-sandbox" is_bind: true rw: false }}
mount {{ src: "/opt/docling-models" dst: "/opt/docling-models" is_bind: true rw: false mandatory: false }}
mount {{ src: "/dev/null" dst: "/dev/null" is_bind: true rw: true }}
mount {{ src: "/dev/urandom" dst: "/dev/urandom" is_bind: true rw: false }}
mount {{ dst: "/tmp" fstype: "tmpfs" rw: true options: "size=256m" nosuid: true nodev: true }}
mount {{ src: "{job_dir}/in" dst: "/work/in" is_bind: true rw: false }}
mount {{ src: "{job_dir}/out" dst: "/work/out" is_bind: true rw: true }}
"""


class NsjailRunner:
    """`SandboxRunner` inside sandboxd (see the module docstring). `execute` is the raw run used by sandboxd (the
    worker validates the manifest itself); `run` adds the validation for in-process callers (tests)."""

    def __init__(
        self,
        env: str,
        *,
        spool_root: Path,
        nsjail: str = NSJAIL,
        policy: str | None = None,
        python: str = JAIL_PYTHON,
        slots: int = 2,
        reserved_short: int = 1,
        slot_base_uid: int = SLOT_BASE_UID,
        owner_uid: int = spool.WORKER_UID,
        owner_gid: int = spool.WORKER_GID,
    ) -> None:
        assert_backend_allowed(BACKEND, env)
        self._spool = spool_root
        self._nsjail = nsjail
        self._policy = policy or os.environ.get("NOEMA_SANDBOX_POLICY") or POLICY
        self._python = python
        self._base_uid = slot_base_uid
        self._owner = (owner_uid, owner_gid)
        self._free = list(range(slots))
        self._slots = anyio.Semaphore(slots)
        self._long = anyio.Semaphore(max(1, slots - reserved_short))
        self._docling = anyio.Lock()

    def command(self, config: Path, kind: SandboxKind, params: str, argv: list[str] | None = None) -> list[str]:
        """The nsjail argv; `argv` replaces the extractor command (selftest-style probes in tests)."""
        inner = argv or [self._python, "-I", "-m", "noema.media.extractors.entry", kind, params]
        return [self._nsjail, "--config", str(config), "--quiet", "--seccomp_policy", self._policy, "--", *inner]

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
        """Run one jail over a prepared job dir once it has a slot (see the module docstring): `on_start` runs when
        the slot is taken (sandboxd sends the started line; raising there aborts the job), then `out/` goes to the
        slot uid, nsjail runs (bound wall_s + 5 s) and `out/` is reclaimed for the media-worker uid. Cancellation
        (the client went away) drops a queued job and kills a running jail. Returns the raw status and outcome."""
        lim = clamp(kind, limits)
        async with contextlib.AsyncExitStack() as held:
            if kind == "pdf.markdown":
                await held.enter_async_context(self._docling)
            if kind not in SHORT_KINDS:
                await held.enter_async_context(self._long)
            await held.enter_async_context(self._slots)
            slot = self._free.pop(0)
            try:
                if on_start is not None:
                    await on_start()
                outcome = await self._jail(job_dir, kind, params, lim, self._base_uid + slot, argv)
            finally:
                self._free.append(slot)
        return raw_status(outcome, lim, wrapped=True), outcome

    async def _jail(
        self, job_dir: Path, kind: SandboxKind, params: str, lim: SandboxLimits, uid: int, argv: list[str] | None
    ) -> ChildOutcome:
        spool.hand_out_to(job_dir / "out", uid, uid)
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", prefix="nsjail-", delete=False) as cfg:
            cfg.write(jail_config(job_dir, kind, lim, uid))
        try:
            cmd = self.command(Path(cfg.name), kind, params, argv)
            return await run_child(cmd, env={}, timeout_s=lim.wall_s + WORKER_GRACE_S)
        finally:
            os.unlink(cfg.name)
            spool.reclaim_out(job_dir / "out", uid=self._owner[0], gid=self._owner[1])

    async def run(self, job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        """Prepare the job dir (any input path is copied in), execute and validate."""
        lim = clamp(job.kind, limits)
        try:
            params = check_params(job.kind, job.params)
            canonical = spool.input_path(self._spool, job.job_id)
        except (BadParams, ValueError):
            return failure("failed", "bad_request", wall_ms=0, extractor=f"{BACKEND}:{job.kind}")
        jd = spool.prepare(self._spool, job.job_id, None if job.input_path == canonical else job.input_path)
        clear_out(jd / "out")
        status, outcome = await self.execute(jd, job.kind, params_json(params), lim)
        return await result_in_thread(
            job.kind, jd / "out", status, wall_ms=outcome.wall_ms, limits=lim, backend=BACKEND
        )
