"""Where the security tests write their evidence (`NOEMA_SEC_ARTIFACTS`, default a per-run directory under the
system temp dir; `make qa-security` sets it outside the repository)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import orjson

from noema.core.values import JsonValue


def artifact_dir() -> Path:
    path = Path(os.environ.get("NOEMA_SEC_ARTIFACTS") or Path(tempfile.gettempdir()) / "noema-sec-artifacts")
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_jsonl(name: str, rows: list[JsonValue]) -> Path:
    path = artifact_dir() / name
    path.write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in rows))
    return path
