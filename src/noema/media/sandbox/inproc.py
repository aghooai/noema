"""`InprocRlimitRunner` (ADR-002 §7): unit tests only. Each job is a child `python -I -m
noema.media.extractors.entry` with RLIMIT_AS/CPU/FSIZE/NOFILE/CORE and the wall-clock limit; no namespaces, so no
network or filesystem isolation. Refuses to exist outside NOEMA_ENV=test."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Final

from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxLimits
from noema.media.sandbox import spool
from noema.media.sandbox.backends import assert_backend_allowed
from noema.media.sandbox.base import jail_env, local_job_id, params_json, raw_status, result_in_thread
from noema.media.sandbox.child import rlimit_preexec, run_child
from noema.media.sandbox.limits import BadParams, check_params, clamp
from noema.media.sandbox.manifest import failure

BACKEND: Final = "inproc_rlimit"


def clear_out(out_dir: Path) -> None:
    """Empty `out/` before a run (a retried job must not see an earlier attempt's files)."""
    if out_dir.is_dir():
        shutil.rmtree(out_dir)
    out_dir.mkdir(mode=0o700)


class InprocRlimitRunner:
    """`SandboxRunner` for unit tests (see the module docstring). Jobs live under `spool_root/<job_id>`; the caller
    deletes them (`spool.remove`) after reading the outputs."""

    def __init__(self, env: str, *, spool_root: Path, python: str | None = None) -> None:
        assert_backend_allowed(BACKEND, env)
        self._spool = spool_root
        self._python = python or sys.executable
        self._env = jail_env()
        self._ocr = shutil.which("tesseract", path=self._env["PATH"]) is not None

    async def run(self, job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        """Run one job; job failures are result statuses, never exceptions."""
        lim = clamp(job.kind, limits)
        try:
            params = check_params(job.kind, job.params)
        except BadParams:
            return failure("failed", "bad_request", wall_ms=0, extractor=f"{BACKEND}:{job.kind}")
        if job.kind == "ocr" and not self._ocr:
            return failure("failed", "unavailable", wall_ms=0, extractor="unavailable")
        job_id = local_job_id(job.job_id)
        canonical = spool.input_path(self._spool, job_id)
        jd = spool.prepare(self._spool, job_id, None if job.input_path == canonical else job.input_path)
        clear_out(jd / "out")
        argv = [self._python, "-I", "-m", "noema.media.extractors.entry", job.kind, params_json(params)]
        argv += ["--work", str(jd)]
        outcome = await run_child(argv, env=self._env, timeout_s=lim.wall_s, cwd=jd, preexec_fn=rlimit_preexec(lim))
        spool.reclaim_out(jd / "out", uid=None, gid=None)
        status = raw_status(outcome, lim, wrapped=False)
        return await result_in_thread(
            job.kind, jd / "out", status, wall_ms=outcome.wall_ms, limits=lim, backend=BACKEND
        )
