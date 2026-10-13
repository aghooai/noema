"""The media-worker ↔ sandboxd IPC (ADR-002 §6): one newline-delimited JSON request and one response per UDS
connection, each at most 64 KiB. Stdlib only.

    request:  {"v": 1, "job_id": "sbx_<ULID>", "kind": "<SandboxKind>", "input": "in/input.bin",
               "params": {...closed per kind...}, "limits": {"wall_s", "cpu_s", "mem_mib", "fsize_mib", "nproc",
               "max_outputs"}}
    started:  {"v": 1, "job_id": ..., "event": "started"}        (when the jail starts, after the slot wait)
    response: {"v": 1, "job_id": ..., "status": "ok|failed|timeout|violation|bad_request", "exit_code": int|null,
               "signal": int|null, "wall_ms": int, "manifest": "out/manifest.json"|null, "stderr_tail": str}

The `started` line precedes the response of every job that reaches a jail (a `bad_request` answered before any jail
has none), so the worker bounds the queue wait and the jail run (`wall_s + 5`) separately. Closing the connection
cancels the job: a queued one never runs, a running jail is killed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from noema.core.protocols.sandbox import SandboxKind, SandboxLimits
from noema.core.values import JsonValue
from noema.media.sandbox.limits import KINDS, BadParams, check_params
from noema.media.sandbox.spool import INPUT_NAME, JOB_ID_RE

MAX_MESSAGE_BYTES: Final = 64 * 1024
STDERR_TAIL_BYTES: Final = 2048
INPUT_REF: Final = f"in/{INPUT_NAME}"
MANIFEST_REF: Final = "out/manifest.json"
RawStatus = Literal["ok", "failed", "timeout", "violation", "bad_request"]
RAW_STATUSES: Final = frozenset({"ok", "failed", "timeout", "violation", "bad_request"})
_REQUEST_KEYS: Final = frozenset({"v", "job_id", "kind", "input", "params", "limits"})
_LIMIT_KEYS: Final = ("wall_s", "cpu_s", "mem_mib", "fsize_mib", "nproc", "max_outputs")
STARTED: Final = "started"
_STARTED_KEYS: Final = frozenset({"v", "job_id", "event"})
_RESPONSE_KEYS: Final = frozenset(
    {"v", "job_id", "status", "exit_code", "signal", "wall_ms", "manifest", "stderr_tail"}
)


class BadRequest(ValueError):
    """A request outside the IPC schema (sandboxd answers `bad_request`)."""


@dataclass(frozen=True, slots=True)
class JobRequest:
    """A validated request."""

    job_id: str
    kind: SandboxKind
    params: dict[str, JsonValue]
    limits: SandboxLimits


@dataclass(frozen=True, slots=True)
class JobResponse:
    """sandboxd's answer for one job."""

    job_id: str
    status: RawStatus
    exit_code: int | None
    signal: int | None
    wall_ms: int
    manifest: str | None
    stderr_tail: str


def _no_constants(name: str) -> None:
    raise BadRequest(f"non-finite number {name}")


def _strict_loads(raw: bytes) -> Any:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise BadRequest("message larger than 64 KiB")
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_no_constants)
    except BadRequest:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):  # RecursionError: nesting deeper than the stack
        raise BadRequest("message is not strict UTF-8 JSON") from None


def _dumps(doc: Mapping[str, Any]) -> bytes:
    raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode() + b"\n"
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("IPC message larger than 64 KiB")
    return raw


def _int(value: object, name: str, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise BadRequest(f"limits.{name} out of range")
    return value


def _limits(raw: object) -> SandboxLimits:
    if not isinstance(raw, dict) or set(raw) != set(_LIMIT_KEYS):
        raise BadRequest("limits keys do not match the schema")
    wall = raw["wall_s"]
    if not isinstance(wall, int | float) or isinstance(wall, bool) or not 0 < wall <= 3600:
        raise BadRequest("limits.wall_s out of range")
    mem = raw["mem_mib"]
    return SandboxLimits(
        wall_s=float(wall),
        cpu_s=_int(raw["cpu_s"], "cpu_s", 1, 3600),
        mem_mib=None if mem is None else _int(mem, "mem_mib", 16, 1 << 20),
        fsize_mib=_int(raw["fsize_mib"], "fsize_mib", 1, 1 << 14),
        nproc=_int(raw["nproc"], "nproc", 1, 4096),
        max_outputs=_int(raw["max_outputs"], "max_outputs", 0, 10_000),
    )


def encode_request(job_id: str, kind: SandboxKind, params: Mapping[str, JsonValue], limits: SandboxLimits) -> bytes:
    """The request line."""
    doc = {
        "v": 1,
        "job_id": job_id,
        "kind": kind,
        "input": INPUT_REF,
        "params": dict(params),
        "limits": {k: getattr(limits, k) for k in _LIMIT_KEYS},
    }
    return _dumps(doc)


def decode_request(raw: bytes) -> JobRequest:
    """Validate one request line; raises `BadRequest`."""
    doc = _strict_loads(raw)
    if not isinstance(doc, dict) or set(doc) != _REQUEST_KEYS or doc["v"] != 1:
        raise BadRequest("request keys or version do not match the schema")
    job_id, kind, params = doc["job_id"], doc["kind"], doc["params"]
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise BadRequest("job_id must match sbx_<ULID>")
    if kind not in KINDS:
        raise BadRequest("unknown job kind")
    if doc["input"] != INPUT_REF or not isinstance(params, dict):
        raise BadRequest("input must be in/input.bin and params an object")
    try:
        checked = check_params(cast(SandboxKind, kind), params)
    except BadParams as exc:
        raise BadRequest(str(exc)) from None
    return JobRequest(job_id, cast(SandboxKind, kind), checked, _limits(doc["limits"]))


def encode_started(job_id: str) -> bytes:
    """The line sandboxd sends when the job's jail starts."""
    return _dumps({"v": 1, "job_id": job_id, "event": STARTED})


def is_started(raw: bytes, *, job_id: str) -> bool:
    """True for the started line of `job_id`; any other line is the final response (validated by
    `decode_response`). Raises `BadRequest` for a line that is not strict JSON."""
    doc = _strict_loads(raw)
    return (
        isinstance(doc, dict)
        and set(doc) == _STARTED_KEYS
        and doc["v"] == 1
        and doc["job_id"] == job_id
        and doc["event"] == STARTED
    )


def encode_response(resp: JobResponse) -> bytes:
    """The response line (stderr_tail cut to its last 2 KiB)."""
    tail = resp.stderr_tail.encode("utf-8", errors="replace")[-STDERR_TAIL_BYTES:].decode("utf-8", errors="ignore")
    doc = {
        "v": 1,
        "job_id": resp.job_id,
        "status": resp.status,
        "exit_code": resp.exit_code,
        "signal": resp.signal,
        "wall_ms": resp.wall_ms,
        "manifest": resp.manifest,
        "stderr_tail": tail,
    }
    return _dumps(doc)


def decode_response(raw: bytes, *, job_id: str) -> JobResponse:
    """Validate sandboxd's answer to `job_id`; raises `BadRequest` (the worker treats it as a failed job). A
    `bad_request` may carry the job id "" (refused before the request was read: peer uid, no line in time)."""
    doc = _strict_loads(raw)
    if not isinstance(doc, dict) or set(doc) != _RESPONSE_KEYS or doc["v"] != 1:
        raise BadRequest("response keys or version do not match")
    if doc["job_id"] != job_id and not (doc["job_id"] == "" and doc["status"] == "bad_request"):
        raise BadRequest("response job id does not match")
    status, wall_ms, tail = doc["status"], doc["wall_ms"], doc["stderr_tail"]
    if status not in RAW_STATUSES or not isinstance(tail, str):
        raise BadRequest("response status or stderr_tail invalid")
    code, sig, manifest = doc["exit_code"], doc["signal"], doc["manifest"]
    for value in (code, sig):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise BadRequest("exit_code and signal must be integers or null")
    if manifest not in (None, MANIFEST_REF):
        raise BadRequest("manifest must be out/manifest.json or null")
    return JobResponse(doc["job_id"], status, code, sig, _int(wall_ms, "wall_ms", 0, 1 << 31), manifest, tail)
