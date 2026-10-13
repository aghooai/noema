"""`BwrapRunner` (ADR-002 §7): host development without Docker (test/dev only). Each job runs under
`bwrap --unshare-all --die-with-parent --new-session --clearenv --cap-drop ALL` with read-only binds of `/usr`, the
venv, the interpreter home (`sys.base_prefix`: uv-managed interpreters live outside `/usr`) and the noema sources,
a private `/dev` and `/tmp`, `/work/in` read-only and `/work/out` writable, the ADR-002 §3 environment, rlimits from
`preexec_fn` and the wall-clock limit. There is no seccomp policy: namespaces and rlimits only, not a security
boundary claim. Kinds whose tool is missing (`ocr` without tesseract) return `failed` with extractor
`unavailable`."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Final

import noema
from noema.core.protocols.sandbox import ExtractResult, SandboxJob, SandboxLimits
from noema.media.sandbox import spool
from noema.media.sandbox.backends import assert_backend_allowed
from noema.media.sandbox.base import jail_env, local_job_id, params_json, raw_status, result_in_thread
from noema.media.sandbox.child import rlimit_preexec, run_child
from noema.media.sandbox.inproc import clear_out
from noema.media.sandbox.limits import BadParams, check_params, clamp
from noema.media.sandbox.manifest import failure

BACKEND: Final = "bwrap"
_ROOT_LINKS: Final = ("/lib", "/lib64", "/bin", "/sbin")


def _ro(path: str) -> list[str]:
    return ["--ro-bind", path, path]


def base_args(bwrap: str, python: str) -> list[str]:
    """The fixed part of the bwrap command line (everything but the job binds and the command)."""
    args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--cap-drop", "ALL"]
    args += _ro("/usr")
    for link in _ROOT_LINKS:
        p = Path(link)
        if p.is_symlink():
            args += ["--symlink", str(p.readlink()), link]
        elif p.is_dir():
            args += _ro(link)
    args += ["--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache"]
    binds: list[str] = []
    for candidate in (sys.prefix, sys.base_prefix, str(Path(noema.__file__).resolve().parents[1])):
        if not candidate.startswith("/usr/") and not any(candidate.startswith(b + "/") for b in binds):
            binds.append(candidate)
    for bind in sorted(set(binds)):
        args += _ro(bind)
    if not Path(python).resolve().is_relative_to("/usr"):
        args += _ro(str(Path(python).resolve().parent))
    return [*args, "--dev", "/dev", "--tmpfs", "/tmp", "--chdir", "/work"]  # noqa: S108 (a private tmpfs)


class BwrapRunner:
    """`SandboxRunner` for host development (see the module docstring)."""

    def __init__(self, env: str, *, spool_root: Path, bwrap: str = "bwrap", python: str | None = None) -> None:
        assert_backend_allowed(BACKEND, env)
        found = shutil.which(bwrap)
        if found is None:
            raise FileNotFoundError("bwrap is not installed")
        self._spool = spool_root
        self._python = python or sys.executable
        self._env = jail_env()
        self._base = base_args(found, self._python)
        self._ocr = shutil.which("tesseract", path=self._env["PATH"]) is not None

    def command(self, job_dir: Path, kind: str, params: str) -> list[str]:
        """The full bwrap argv of one job."""
        args = [*self._base, "--ro-bind", str(job_dir / "in"), "/work/in", "--bind", str(job_dir / "out"), "/work/out"]
        for key, value in sorted(self._env.items()):
            args += ["--setenv", key, value]
        return [*args, "--", self._python, "-I", "-m", "noema.media.extractors.entry", kind, params]

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
        argv = self.command(jd, job.kind, params_json(params))
        outcome = await run_child(argv, env={}, timeout_s=lim.wall_s, preexec_fn=rlimit_preexec(lim, nofile=256))
        spool.reclaim_out(jd / "out", uid=None, gid=None)
        status = raw_status(outcome, lim, wrapped=True)
        return await result_in_thread(
            job.kind, jd / "out", status, wall_ms=outcome.wall_ms, limits=lim, backend=BACKEND
        )
