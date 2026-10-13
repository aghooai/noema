"""Shared runner logic (ADR-002 §5-§7): the exit-status mapping, the trusted result and the local job ids.

Mapping of one finished child (direct child: negative return code = signal; nsjail/bwrap: 128 + signal):
our wall timer, SIGXCPU or a SIGKILL at the time limit → `timeout`; SIGSYS → `violation`; exit 0 → validate the
manifest (`ok`/`rejected`, or `failed{manifest_invalid}`); anything else → `failed` (OOM, crash, non-zero exit).
"""

from __future__ import annotations

import hashlib
import json
from functools import partial
from pathlib import Path
from typing import Final

import anyio

from noema.core.ids import CROCKFORD32
from noema.core.protocols.sandbox import ExtractResult, SandboxKind, SandboxLimits
from noema.core.values import JsonValue
from noema.media.sandbox.child import SIGKILL, SIGSYS, SIGXCPU, ChildOutcome
from noema.media.sandbox.ipc import RawStatus
from noema.media.sandbox.limits import OUTPUT_BUDGET_BYTES
from noema.media.sandbox.manifest import ManifestInvalid, failure, validate
from noema.media.sandbox.spool import JOB_ID_RE

MIB: Final = 1024 * 1024
EXIT_USAGE: Final = 2  # extractors.entry: params rejected inside the jail


def signal_of(returncode: int, *, wrapped: bool) -> int | None:
    """The signal that ended the child, if any (`wrapped`: a 128 + signal convention, nsjail/bwrap)."""
    if returncode < 0:
        return -returncode
    if wrapped and 128 < returncode <= 128 + 64:
        return returncode - 128
    return None


def raw_status(outcome: ChildOutcome, limits: SandboxLimits, *, wrapped: bool) -> RawStatus:
    """The ADR-002 §6 status of a finished child."""
    if outcome.timed_out:
        return "timeout"
    sig = signal_of(outcome.returncode, wrapped=wrapped)
    if outcome.returncode == 0:
        return "ok"
    if sig == SIGSYS:
        return "violation"
    if sig == SIGXCPU or (sig == SIGKILL and outcome.wall_ms >= limits.wall_s * 1000 * 0.95):
        return "timeout"
    if outcome.returncode == EXIT_USAGE:
        return "bad_request"
    return "failed"


def result_for(
    kind: SandboxKind, out_dir: Path, status: RawStatus, *, wall_ms: int, limits: SandboxLimits, backend: str
) -> ExtractResult:
    """The trusted `ExtractResult` of a finished job: validates the manifest of `ok` jobs (each output capped at
    the kind's `fsize_mib`, their sum at its `OUTPUT_BUDGET_BYTES`). Synchronous file hashing: async callers run it
    in a worker thread."""
    extractor = f"{backend}:{kind}"
    if status == "ok":
        try:
            return validate(
                kind,
                out_dir,
                max_outputs=limits.max_outputs,
                output_cap_bytes=limits.fsize_mib * MIB,
                wall_ms=wall_ms,
                max_total_bytes=OUTPUT_BUDGET_BYTES[kind],
            )
        except ManifestInvalid as exc:
            return failure("failed", "manifest_invalid", wall_ms=wall_ms, extractor=extractor, detail=exc.reason)
    if status == "bad_request":
        return failure("failed", "bad_request", wall_ms=wall_ms, extractor=extractor)
    if status == "failed":
        return failure("failed", "exit", wall_ms=wall_ms, extractor=extractor)
    return failure(status, status, wall_ms=wall_ms, extractor=extractor)


async def result_in_thread(
    kind: SandboxKind, out_dir: Path, status: RawStatus, *, wall_ms: int, limits: SandboxLimits, backend: str
) -> ExtractResult:
    """`result_for` in a worker thread: validation reads and hashes every output (up to the output budget), which
    must not block the event loop of the media-worker."""
    call = partial(result_for, kind, out_dir, status, wall_ms=wall_ms, limits=limits, backend=backend)
    return await anyio.to_thread.run_sync(call)


def local_job_id(job_id: str) -> str:
    """`job_id` when it is `sbx_<ULID>`, else a stable `sbx_` id derived from it (in-process backends accept any
    caller id; the remote and nsjail backends require the ADR format)."""
    if JOB_ID_RE.fullmatch(job_id):
        return job_id
    digest = int.from_bytes(hashlib.sha256(job_id.encode()).digest()[:17], "big") >> 6
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD32[digest & 31])
        digest >>= 5
    return "sbx_" + "".join(reversed(chars))


def params_json(params: dict[str, JsonValue]) -> str:
    """The params argument of `extractors.entry`."""
    return json.dumps(params, separators=(",", ":"), sort_keys=True)


def jail_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The ADR-002 §3 environment of every jail (nothing inherited)."""
    env = {
        "LANG": "C.UTF-8",
        "HOME": "/tmp",  # noqa: S108 (the jail-private tmpfs, ADR-002 §3)
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_THREAD_LIMIT": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "DOCLING_ARTIFACTS_PATH": "/opt/docling-models",
        "TESSDATA_PREFIX": "/usr/share/tesseract-ocr/5/tessdata",
    }
    env.update(extra or {})
    return env
